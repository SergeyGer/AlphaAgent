"""Celery task layer - autonomous market monitoring with a hard execution guard.

Flow
----
``autonomous_market_monitoring_task`` (Celery Beat, every 15 min)
    -> one flat query for every ``is_autonomous=True`` portfolio
    -> fan-out: ``run_alpha_agent_task.delay(portfolio_id, ticker)`` per ticker
       (a single celery ``group`` publish - no nested loops, no serial waiting)

``run_alpha_agent_task`` (worker)
    1. Re-check autonomy and the portfolio-level daily stop-loss.
    2. Gather read-only context (live price, news, ledger replay).
    3. Run the CrewAI bull -> bear -> CIO debate (``ai_agent.run_alpha_agent``).
    4. **Execution Guard**: run the read-only simulation for sizing and for the
       audit trail. It is not an authorisation.
    5. Only if the simulation approves, call the authoritative execution service,
       which locks ``Portfolio`` / ``Asset``, re-runs the guard against current
       policy and marks, and mutates the ledger in the same transaction.
    6. Always write an ``AgentDecisionLog`` row - including HOLDs and rejections -
       so the audit trail is complete and explainable, then complete the decision
       run with the actual execution state.

In **advisory mode** (``require_approval=True`` on a non-autonomous portfolio)
step 5 is replaced by a ``PENDING TradeRecommendation``; a human then approves
or rejects it and the same guard runs again against live prices.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from celery import group, shared_task
from django.conf import settings
from django.db import DatabaseError, OperationalError, transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from ai_agent import (
    AgentRunResult,
    DecisionContext,
    build_news_report,
    run_alpha_agent,
)
from core.decision_models import (
    DecisionExecutionState,
    DecisionRun,
)
from core.log_safety import log_safe
from core.models import (
    AgentDecisionLog,
    Asset,
    MarketSentiment,
    Portfolio,
    TradeRecommendation,
    Transaction,
)
from services.budget import (
    budget_state,
    check_and_reserve,
    logical_call_key,
    mark_unknown,
    reconcile_decision_run,
    reserve_bound_for_run,
)
from services.decision_lifecycle import complete_run
from services.decision_outcomes import sync_outcomes_for_run
from services.decision_runs import link_legacy_log
from services.events import (
    emit_agent_thinking,
    emit_budget_exhausted,
    emit_budget_updated,
    emit_decision,
    emit_trade,
)
from services.execution import (
    CENT,
    ZERO,
    ExecutionRejected,
    ExecutionRequest,
    apply_trade,
    evaluate_proposal,
    portfolio_policy_version,
)
from services.execution_audit import ExecutionAudit, record_fill_outcome, record_terminal_outcome
from services.fundamentals import get_financial_health, get_price_history
from services.ledger import PositionState, realised_pnl_today, replay_positions
from services.market_data import get_latest_quote
from services.portfolio_brief import portfolio_state_version
from services.portfolio_metrics import build_exposure
from services.recommendations import (
    create_recommendation,
    expire_stale_recommendations,
    find_pending_recommendation,
)
from services.snapshots import capture_all_snapshots, capture_snapshot
from services.thesis import current_thesis, sync_after_trade
from services.throttle import claim_sweep_slot, claim_ticker_slot

logger = logging.getLogger("alphaagent.tasks")

# Fallback universe when a portfolio holds nothing yet.
DEFAULT_WATCHLIST = ["AAPL", "TSLA", "BTC"]


def _watchlist() -> list[str]:
    configured = getattr(settings, "ALPHA_WATCHLIST", None) or DEFAULT_WATCHLIST
    return [str(t).upper() for t in configured]


# ---------------------------------------------------------------------------
# Subtask: one portfolio x one ticker
# ---------------------------------------------------------------------------
@shared_task(
    bind=True,
    name="tasks.run_alpha_agent_task",
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    acks_late=True,
)
def run_alpha_agent_task(
    self, portfolio_id: int, ticker: str, require_approval: bool = False
) -> dict[str, Any]:
    """Run the AI pipeline for a single (portfolio, ticker) pair.

    One broker message per pair keeps the workload embarrassingly parallel and
    isolates failures - a bad ticker can never stall the whole sweep.

    ``require_approval`` selects advisory mode: the AI still analyses and
    proposes, but the trade is stored as a PENDING ``TradeRecommendation`` for
    a human instead of being executed. Autonomous portfolios use the default
    ``False`` and keep executing automatically.
    """
    ticker = str(ticker).upper()
    logger.info("run_alpha_agent_task start portfolio=%s ticker=%s", portfolio_id, ticker)

    try:
        portfolio = Portfolio.objects.select_related("user").get(pk=portfolio_id)
    except Portfolio.DoesNotExist:
        logger.warning("Portfolio %s no longer exists; skipping %s", portfolio_id, ticker)
        return {"status": "skipped", "reason": "portfolio_missing", "ticker": ticker}

    if not portfolio.is_autonomous and not require_approval:
        logger.info("Portfolio %s autonomy disabled; skipping %s", portfolio_id, ticker)
        return {"status": "skipped", "reason": "autonomy_disabled", "ticker": ticker}

    # Autonomy always wins: an autonomous portfolio never queues for approval.
    require_approval = require_approval and not portfolio.is_autonomous
    user_id = portfolio.user_id

    # ---- 0a. Duplicate delivery ------------------------------------------
    # The Celery task id is the stable identity of this decision *delivery*.
    # ``acks_late`` means a worker can receive the same message twice; the
    # second delivery must return the first result, not re-run the model and
    # then discover that the portfolio has moved.
    execution_key = f"celery:{self.request.id}" if self.request.id else ""
    if execution_key:
        existing = Transaction.objects.filter(execution_key=execution_key).first()
        if existing is not None:
            logger.info(
                "Duplicate delivery for task %s: returning transaction %s",
                self.request.id,
                existing.id,
            )
            return {
                "status": "executed",
                "duplicate_delivery": True,
                "portfolio_id": portfolio_id,
                "ticker": ticker,
                "action": existing.tx_type,
                "approved": True,
                "amount": str(existing.amount),
                "price": str(existing.price),
                "notional": str(existing.gross_value_usd),
                "transaction_id": existing.id,
                "decision_run_id": None,
            }

    # ---- 0. Throttle ------------------------------------------------------
    # Claimed before any expensive work. The allocation ceiling is per-trade, so
    # without this two overlapping sweeps could each trade inside their own
    # limit and compound the portfolio's real exposure.
    if not claim_ticker_slot(portfolio_id, ticker):
        return {
            "status": "cooldown",
            "reason": "ticker_cooldown",
            "portfolio_id": portfolio_id,
            "ticker": ticker,
        }

    # ---- 1. AI spend ceiling ---------------------------------------------
    # Checked after the throttle but before any work that costs money, and before
    # the context is built. The guard limits what the system may trade; this
    # limits what it may spend. Without it an unattended Beat schedule keeps
    # calling the model forever, refusing every trade on risk grounds while the
    # API bill grows.
    #
    # The check reserves the *declared upper bound* for the run before the
    # provider is called. A provider that reports tokens but no dollars cannot
    # make a day look free: the bound is held while the call is open and is
    # reconciled to a price-table estimate (or kept as unknown) afterwards.
    #
    # The call key is the durable task delivery id. A retry of the same delivery
    # returns the existing reservation instead of adding a second one.
    ledger_key = logical_call_key("decision", execution_key or f"task-{uuid.uuid4().hex}")
    reserve = check_and_reserve(
        call_key=ledger_key,
        upper_bound_usd=reserve_bound_for_run("decision"),
        provider=str(getattr(settings, "AI_LLM_PROVIDER", "") or ""),
        model=str(
            (getattr(settings, "AI_CONFIG", {}) or {}).get("MODEL")
            or getattr(settings, "AI_LLM_MODEL", "")
            or ""
        ),
        stage="decision_run",
        metadata={
            "portfolio_id": portfolio_id,
            "ticker": ticker,
            "execution_key": execution_key or None,
        },
    )
    if not reserve.allowed:
        # Wrapped for the same reason as every other sink: a value containing a
        # newline would otherwise forge an extra log entry.
        logger.warning(
            "AI spend reservation refused (%s): bound $%s, committed $%s of $%s; "
            "skipping %s on portfolio %s",
            log_safe(reserve.reason),
            reserve.upper_bound_usd,
            reserve.committed_usd,
            reserve.limit_usd,
            log_safe(ticker),
            portfolio_id,
        )
        # Recorded as a decision row so the refusal is auditable in the same
        # place as every other decision, rather than only in the worker log.
        AgentDecisionLog.objects.create(
            portfolio=portfolio,
            ticker=ticker,
            action_taken=(
                "HALT - AI spend ceiling reached"
                if reserve.reason == "ai_spend_ceiling"
                else "HALT - AI spend reserve exceeded"
            ),
            market_sentiment=MarketSentiment.NEUTRAL,
            reasoning=(
                (
                    "Daily AI spend ceiling reached: committed "
                    if reserve.reason == "ai_spend_ceiling"
                    else "Daily AI spend ceiling would be exceeded: committed "
                )
                + f"${reserve.committed_usd} plus a reserved upper bound of "
                f"${reserve.upper_bound_usd} against a ${reserve.limit_usd} ceiling "
                f"(billed ${reserve.spent_usd}, estimated "
                f"${reserve.estimated_spend_usd}, reserved "
                f"${reserve.reserved_usd}, unknown-cost runs "
                f"{reserve.unknown_cost_runs}). Skipped {ticker}; the provider was "
                f"not called. No further agent runs are dispatched until 00:00 UTC. "
                f"Raise AI_DAILY_SPEND_LIMIT_USD or set "
                f"AI_SPEND_LIMIT_ENFORCED=false to keep running."
            ),
        )
        emit_budget_exhausted(user_id, budget_state().as_dict())
        return {
            "status": "halted",
            "reason": reserve.reason,
            "portfolio_id": portfolio_id,
            "ticker": ticker,
            "spent_usd": str(reserve.spent_usd),
            "estimated_spend_usd": str(reserve.estimated_spend_usd),
            "reserved_usd": str(reserve.reserved_usd),
            "committed_usd": str(reserve.committed_usd),
            "reserved_upper_bound_usd": str(reserve.upper_bound_usd),
            "limit_usd": str(reserve.limit_usd),
            "remaining_usd": str(reserve.remaining_usd),
            "unknown_cost_runs": reserve.unknown_cost_runs,
            "call_key": ledger_key,
        }

    # ---- 2. Read-only context -------------------------------------------
    positions = replay_positions(portfolio)
    position = positions.get(ticker, PositionState(ticker=ticker))
    realised_today = sum((s.realised_pnl_today for s in positions.values()), ZERO).quantize(CENT)

    stored_asset = next((a for a in portfolio.assets.all() if a.ticker == ticker), None)
    fallback_price = stored_asset.avg_purchase_price if stored_asset else None
    quote = get_latest_quote(ticker, fallback_price=fallback_price)
    price = quote.price if quote else None

    try:
        news_report = build_news_report(ticker)
    except Exception as exc:
        logger.warning("News gathering failed for %s: %s", ticker, exc)
        news_report = None

    # Evidence for the Risk Assessor. Both degrade to None rather than raising.
    try:
        financial_health = get_financial_health(ticker, price=price)
    except Exception as exc:
        logger.warning("Fundamentals gathering failed for %s: %s", ticker, exc)
        financial_health = None

    try:
        price_history = get_price_history(ticker)
    except Exception as exc:
        logger.warning("Price history gathering failed for %s: %s", ticker, exc)
        price_history = None

    context = DecisionContext(
        ticker=ticker,
        risk_profile=portfolio.risk_profile,
        cash_balance=portfolio.balance_usd,
        position_amount=position.amount,
        avg_purchase_price=position.avg_cost,
        current_price=price,
        price_change_pct=quote.change_pct if quote else None,
        max_trade_budget_usd=portfolio.max_trade_budget_usd,
        daily_loss_limit_usd=portfolio.daily_loss_limit_usd,
        realised_pnl_today_usd=realised_today,
        news_report=news_report,
        financial_health=financial_health,
        price_history=price_history,
        portfolio_id=portfolio.id,
        # The agent's own prior reasoning, so it argues with what it believed
        # rather than from a blank slate. None when no position is open.
        current_thesis=current_thesis(portfolio, ticker),
    )

    # ---- 2. AI decision --------------------------------------------------
    emit_agent_thinking(
        user_id,
        portfolio.id,
        ticker,
        "analyst_started",
        f"Analysing {ticker}: scraping headlines, scoring sentiment and sizing "
        f"against a ${portfolio.max_trade_budget_usd} budget.",
    )
    try:
        run_result: AgentRunResult = run_alpha_agent(context)
    except Exception as exc:
        logger.exception("Agent pipeline crashed for %s/%s: %s", portfolio_id, ticker, exc)
        failure_log = AgentDecisionLog.objects.create(
            portfolio=portfolio,
            ticker=ticker,
            reasoning=f"Agent pipeline failure: {type(exc).__name__}: {exc}",
            action_taken="ERROR - agent pipeline failed",
            market_sentiment=MarketSentiment.NEUTRAL,
        )
        emit_agent_thinking(user_id, portfolio.id, ticker, "failed", str(exc)[:200])
        # The provider may have been called before the crash. The charge is
        # therefore unknown, not zero; the reservation stays held so the day's
        # ceiling remains conservative.
        try:
            mark_unknown(
                call_key=ledger_key,
                reason="agent_pipeline_failed",
                metadata={"error_type": type(exc).__name__},
            )
        except Exception as ledger_exc:  # pragma: no cover - accounting must not mask
            logger.exception(
                "Could not mark AI usage %s unknown: %s",
                log_safe(ledger_key),
                log_safe(ledger_exc),
            )
        emit_decision(user_id, failure_log)
        emit_budget_updated(user_id, budget_state().as_dict())
        return {"status": "error", "ticker": ticker, "error": str(exc)}

    emit_agent_thinking(
        user_id,
        portfolio.id,
        ticker,
        "cio_finished",
        f"Decision: {run_result.proposal.action} "
        f"({run_result.proposal.sentiment}) via {run_result.source}.",
    )

    proposal = run_result.proposal

    # ---- 3. Execution guard ---------------------------------------------
    # This is a read-only *simulation*. It sizes the proposal for the audit trail
    # and for advisory review, but it is not an authorisation: the authoritative
    # revalidation happens under the portfolio lock in services.execution.
    marks = _current_marks(portfolio, ticker, quote)
    exposure = build_exposure(portfolio, ticker, price_map=marks)
    policy_version = portfolio_policy_version(portfolio)
    portfolio_version = portfolio_state_version(portfolio)
    decision = evaluate_proposal(
        portfolio, proposal, price, position, realised_today, exposure=exposure
    )
    decision.policy_version = policy_version
    decision.evaluated_portfolio_version = portfolio_version
    decision.quote_time = quote.as_of if quote is not None else None

    run = DecisionRun.objects.filter(pk=run_result.decision_run_id).first()
    if not execution_key:
        execution_key = f"run:{run.id if run is not None else 'none'}"

    transaction_row: Transaction | None = None
    recommendation = None
    duplicate_recommendation = None
    final_decision = decision
    execution_state = None
    execution_error = ""
    audit_recorded = False

    if require_approval and decision.approved:
        # Advisory mode: the trade is valid, but a human must approve it. The
        # recommendation and the run's awaiting-approval state are one
        # transaction; entering that state without an actionable recommendation
        # would wait forever for a decision nobody can make.
        with transaction.atomic():
            recommendation = create_recommendation(
                portfolio,
                proposal,
                decision.price,
                ticker=ticker,
                approved_amount=decision.amount,
                decision_run=run,
                quote_observed_at=quote.as_of if quote is not None else None,
                quote_retrieved_at=getattr(quote, "retrieved_at", None),
                price_source=(getattr(quote, "source", "") if quote is not None else ""),
                price_currency=(getattr(quote, "currency", "") if quote is not None else "USD"),
            )
            if recommendation is None:
                duplicate_recommendation = find_pending_recommendation(portfolio, ticker)
                if duplicate_recommendation is not None and run is not None:
                    execution_state = DecisionExecutionState.SUPPRESSED
                    record_terminal_outcome(
                        run=run,
                        outcome=execution_state,
                        reason=(
                            "A duplicate advisory proposal was suppressed; the existing "
                            "pending recommendation keeps its original lifecycle."
                        ),
                        duplicate_recommendation=duplicate_recommendation,
                        user_id=user_id,
                    )
                    audit_recorded = True
                else:
                    execution_state = DecisionExecutionState.BLOCKED
                    decision.approved = False
                    decision.reason = (
                        "The proposal could not become an actionable recommendation; "
                        "no approval state was entered."
                    )
                    if run is not None:
                        record_terminal_outcome(
                            run=run,
                            outcome=execution_state,
                            reason=decision.reason,
                            user_id=user_id,
                        )
                        audit_recorded = True
            else:
                execution_state = DecisionExecutionState.AWAITING_APPROVAL
                if run is not None:
                    complete_run(
                        run,
                        outcome=execution_state,
                        recommendation=recommendation,
                        reason="A human decision is required.",
                        requires_approval=True,
                    )
                    audit_recorded = True
    elif decision.approved:
        audit = ExecutionAudit(
            run=run,
            outcome=DecisionExecutionState.EXECUTED,
            reason=decision.reason,
            via="autonomous",
            user_id=user_id,
            execution_key=execution_key,
        )
        request = ExecutionRequest(
            portfolio_id=portfolio.id,
            ticker=ticker,
            side=decision.action,
            quantity=decision.amount,
            quotes=marks,
            requested_quantity=decision.requested_amount or decision.amount,
            execution_key=execution_key,
            source="autonomous",
            approval_ref=f"run:{run.id}" if run is not None else "",
            approved_policy_version=policy_version,
            approved_portfolio_version=portfolio_version,
            approved_price=decision.price,
            approved_quantity=decision.amount,
            quote_time=quote.as_of if quote is not None else None,
            quote_source=(getattr(quote, "source", "") if quote is not None else ""),
            quote_retrieved_at=getattr(quote, "retrieved_at", None),
        )
        try:
            with transaction.atomic():
                execution = apply_trade(
                    portfolio.id, ticker, decision, request=request, audit=audit
                )
                if (
                    execution.transaction is not None
                    and run is not None
                    and run.execution_state != DecisionExecutionState.EXECUTED
                ):
                    record_fill_outcome(
                        run=run,
                        transaction_row=execution.transaction,
                        decision=execution.decision,
                        request=request,
                        reason=execution.reason,
                        via="autonomous",
                        user_id=user_id,
                    )
                audit_recorded = execution.transaction is not None
        except (ExecutionRejected, DatabaseError, OperationalError) as exc:
            decision.approved = False
            decision.reason = f"Execution failed and was rolled back: {exc}"
            execution_error = str(exc)
            final_decision = getattr(exc, "decision", None) or decision
            execution_state = (
                DecisionExecutionState.BLOCKED
                if isinstance(exc, ExecutionRejected)
                else DecisionExecutionState.FAILED
            )
            logger.exception("Trade execution failed for %s/%s: %s", portfolio_id, ticker, exc)
            if run is not None:
                with transaction.atomic():
                    record_terminal_outcome(
                        run=run,
                        outcome=execution_state,
                        guard_decision=final_decision,
                        reason=decision.reason,
                        user_id=user_id,
                    )
                audit_recorded = True
        else:
            final_decision = execution.decision
            if not execution.approved or execution.transaction is None:
                # The under-lock revalidation refused the proposal. That is a
                # blocked decision, never an execution with a null fill.
                decision.approved = False
                decision.reason = execution.reason
                execution_state = DecisionExecutionState.BLOCKED
                if run is not None:
                    with transaction.atomic():
                        record_terminal_outcome(
                            run=run,
                            outcome=execution_state,
                            guard_decision=final_decision,
                            reason=execution.reason,
                            user_id=user_id,
                        )
                    audit_recorded = True
            else:
                transaction_row = execution.transaction
                execution_state = DecisionExecutionState.EXECUTED
                emit_trade(user_id, transaction_row)
                _capture_after_trade(portfolio)
    elif decision.action == "HOLD":
        execution_state = DecisionExecutionState.HELD
        if run is not None:
            with transaction.atomic():
                record_terminal_outcome(
                    run=run,
                    outcome=execution_state,
                    reason=decision.reason,
                    user_id=user_id,
                )
            audit_recorded = True
    else:
        execution_state = DecisionExecutionState.BLOCKED
        if run is not None:
            with transaction.atomic():
                record_terminal_outcome(
                    run=run,
                    outcome=execution_state,
                    guard_decision=decision,
                    reason=decision.reason,
                    user_id=user_id,
                )
            audit_recorded = True

    # Notifications are dispatched only after the ledger transaction committed.
    if transaction_row is not None:
        try:
            from config.celery import app as celery_app

            celery_app.send_task("tasks.notify_trade_task", args=[transaction_row.id])
        except Exception as exc:  # pragma: no cover - notification is best effort
            logger.warning(
                "Could not enqueue trade notification for %s: %s", transaction_row.id, exc
            )

    # ---- 4. Explainability log (always written) -------------------------
    audit_reasoning = run_result.reasoning
    if decision.notes:
        audit_reasoning = f"{audit_reasoning}\n\nGUARDRAIL: " + " ".join(decision.notes)
    if run_result.error:
        audit_reasoning = f"{audit_reasoning}\n\nPIPELINE NOTE: {run_result.error}"

    if transaction_row is not None:
        action_taken = (
            f"{'Purchased' if decision.action == 'BUY' else 'Sold'} "
            f"{decision.amount.normalize()} {ticker} @ ${decision.price} "
            f"(${decision.notional})"
        )
    elif recommendation is not None:
        action_taken = (
            f"AWAITING APPROVAL - {decision.action} {decision.amount.normalize()} "
            f"{ticker} @ ${decision.price} (${decision.notional})"
        )
    elif decision.action == "HOLD":
        action_taken = f"HOLD - {proposal.sentiment.title()} market, no position change"
    else:
        action_taken = f"BLOCKED {decision.action} on {ticker} - {decision.reason}"

    decision_log = None
    try:
        decision_log = AgentDecisionLog.objects.create(
            portfolio=portfolio,
            ticker=ticker,
            transaction=transaction_row,
            reasoning=audit_reasoning[:20000],
            bull_case=run_result.bull_case[:20000],
            bear_case=run_result.bear_case[:20000],
            action_taken=action_taken[:255],
            market_sentiment=proposal.sentiment,
            tokens_used=run_result.tokens_used,
            input_tokens=run_result.input_tokens,
            output_tokens=run_result.output_tokens,
            cached_input_tokens=run_result.cached_input_tokens,
            cache_write_tokens=run_result.cache_write_tokens,
            api_cost_usd=run_result.api_cost_usd,
            estimated_cost_usd=run_result.estimated_cost_usd,
            cost_basis=run_result.cost_basis,
            cost_ledger_key=ledger_key,
        )
        # Reconcile the reservation before publishing the budget: the event must
        # show the estimate (or the held unknown bound), not the pre-call reserve.
        try:
            reconcile_decision_run(ledger_key, run_result)
        except Exception as ledger_exc:  # pragma: no cover - accounting is best effort
            logger.exception(
                "Could not reconcile AI usage %s: %s",
                log_safe(ledger_key),
                log_safe(ledger_exc),
            )
        emit_decision(user_id, decision_log)
        # Re-read rather than adding this run's cost locally: the figure must
        # match what the audit trail says, including any run from another worker.
        emit_budget_updated(user_id, budget_state().as_dict())
    except Exception as exc:
        logger.exception("Failed to persist AgentDecisionLog for %s: %s", portfolio_id, exc)

    # Link the Phase 1/2 decision run to the audit row it explains (DATA-04).
    # The run is written by the AI layer before the execution guard runs, so the
    # link can only be made here. A failure is logged and never fails the task:
    # the audit row is the Phase 0 contract and must survive.
    if decision_log is not None and run is not None:
        try:
            link_legacy_log(run, decision_log)
        except Exception as exc:
            logger.warning(
                "Could not link decision run %s to AgentDecisionLog %s: %s",
                run.id,
                decision_log.id,
                exc,
            )

    # ---- 4b. Complete the decision lifecycle ----------------------------
    # The run stayed in ``validating`` until this point. Only now, with an actual
    # guard result and (where applicable) a ledger row, does it become a
    # completed decision - and the execution state says exactly what happened.
    if run is not None and execution_state is not None and not audit_recorded:
        try:
            complete_run(
                run,
                outcome=execution_state,
                guard_decision=final_decision if final_decision.action != "HOLD" else None,
                transaction_row=transaction_row,
                recommendation=recommendation,
                reason=final_decision.reason or execution_error,
                error_code="execution_failed" if execution_state == "failed" else "",
                error_detail=execution_error,
                requires_approval=execution_state == DecisionExecutionState.AWAITING_APPROVAL,
            )
            if decision_log is not None:
                sync_outcomes_for_run(run)
        except Exception as exc:  # pragma: no cover - audit row is the Phase 0 contract
            logger.exception("Could not complete decision run %s: %s", run.id, exc)

    if recommendation is not None and decision_log is not None:
        # Link the audit row so the dashboard can show proposal -> approval.
        TradeRecommendation.objects.filter(pk=recommendation.pk).update(decision_log=decision_log)
        notify_recommendation.delay(recommendation.id)

    # ---- 5. Position thesis ---------------------------------------------
    # Only after an executed trade, and only once the audit row exists so the
    # thesis can point at the decision that wrote it. Advisory trades do not move
    # the position, so they must not move the thesis either - the recommendation
    # may yet be rejected, and a thesis recorded for a trade that never happened
    # would be fed back to the next run as though it had.
    if transaction_row is not None and decision_log is not None:
        try:
            # Re-read rather than deriving from the proposal: apply_trade may have
            # clamped, and the stored position is the authority on whether this
            # instrument is still held.
            held = Asset.objects.filter(portfolio=portfolio, ticker=ticker).first()
            sync_after_trade(
                portfolio=portfolio,
                ticker=ticker,
                position_amount=held.amount if held is not None else Decimal("0"),
                decision=decision_log,
                thesis=proposal.thesis,
                invalidation=proposal.invalidation,
            )
        except Exception as exc:  # pragma: no cover - must never break a run
            logger.exception("Thesis sync failed for %s/%s: %s", portfolio_id, ticker, exc)

    if transaction_row is not None:
        status = "executed"
    elif recommendation is not None:
        status = "awaiting_approval"
    elif execution_state == DecisionExecutionState.SUPPRESSED:
        status = "suppressed"
    elif decision.action == "HOLD":
        status = "hold"
    elif execution_state == DecisionExecutionState.FAILED:
        status = "failed"
    else:
        status = "blocked"

    result = {
        "status": status,
        "portfolio_id": portfolio_id,
        "ticker": ticker,
        "action": proposal.action,
        "approved": decision.approved,
        "amount": str(decision.amount),
        "price": str(price) if price is not None else None,
        "notional": str(decision.notional),
        "sentiment": proposal.sentiment,
        "guard_reason": decision.reason,
        "agent_source": run_result.source,
        "tokens_used": run_result.tokens_used,
        "latency_ms": run_result.latency_ms,
        "transaction_id": transaction_row.id if transaction_row else None,
        "recommendation_id": recommendation.id if recommendation else None,
        "duplicate_recommendation_id": (
            duplicate_recommendation.id if duplicate_recommendation is not None else None
        ),
        "decision_run_id": str(run.id) if run is not None else None,
        "execution_state": execution_state,
        "bull_case_chars": len(run_result.bull_case),
        "bear_case_chars": len(run_result.bear_case),
    }
    logger.info("run_alpha_agent_task done: %s", result)
    return result


# ---------------------------------------------------------------------------
# Fan-out entrypoint
#
# Plain function, deliberately not a task: the API calls it inline so it can
# report what happened (including a debounced no-op) instead of handing back a
# task id and asserting success. `autonomous_market_monitoring_task` below is
# the Celery wrapper Beat uses.
# ---------------------------------------------------------------------------
def dispatch_market_sweep(portfolio_id: int | None = None) -> dict[str, Any]:
    """Fan out one subtask per ``(portfolio, ticker)`` pair.

    A single ``group`` publish replaces a nested loop, so N portfolios x M tickers
    are dispatched to the worker pool concurrently instead of being processed
    sequentially inside one task.

    ``portfolio_id`` scopes the sweep to a single portfolio. The API uses this:
    the on-demand endpoint authenticates one user, so dispatching the *global*
    sweep from it let one user's click spend every other user's LLM budget.

    Returns the summary rather than a task handle so the caller can report what
    actually happened - the endpoint previously returned 202 even when the
    debounce had silently discarded the request.
    """
    if not claim_sweep_slot(portfolio_id=portfolio_id):
        logger.info("market sweep skipped by the debounce (portfolio_id=%s)", portfolio_id)
        return {"status": "debounced", "dispatched": 0, "portfolio_id": portfolio_id}

    logger.info("market sweep starting (portfolio_id=%s)", portfolio_id)

    # One query, joined/prefetched - no per-portfolio round trips.
    portfolios = Portfolio.objects.filter(is_autonomous=True)
    if portfolio_id is not None:
        portfolios = portfolios.filter(pk=portfolio_id)
    portfolios = portfolios.select_related("user").prefetch_related(
        Prefetch("assets", queryset=Asset.objects.only("id", "portfolio_id", "ticker"))
    )

    watchlist = _watchlist()
    pairs: list[tuple[int, str]] = []
    scanned = 0

    for portfolio in portfolios:
        scanned += 1
        held = {a.ticker.upper() for a in portfolio.assets.all()}
        # Held tickers first (so the AI can exit positions), then the watchlist.
        tickers = list(dict.fromkeys([*sorted(held), *watchlist]))
        pairs.extend((portfolio.id, ticker) for ticker in tickers)

    if not pairs:
        logger.info("autonomous_market_monitoring_task: no autonomous portfolios to process")
        return {"status": "noop", "portfolios_scanned": scanned, "dispatched": 0}

    signatures = [run_alpha_agent_task.s(portfolio_id, ticker) for portfolio_id, ticker in pairs]
    async_result = group(signatures).apply_async()

    summary = {
        "status": "dispatched",
        "portfolios_scanned": scanned,
        "dispatched": len(signatures),
        "group_id": async_result.id,
        "watchlist": watchlist,
        "portfolio_id": portfolio_id,
    }
    logger.info("market sweep: %s", summary)
    return summary


@shared_task(bind=True, name="tasks.autonomous_market_monitoring_task")
def autonomous_market_monitoring_task(self, portfolio_id: int | None = None) -> dict[str, Any]:
    """Beat entry point: fans out across every autonomous portfolio."""
    return dispatch_market_sweep(portfolio_id)


# ---------------------------------------------------------------------------
# Periodic risk sweep
# ---------------------------------------------------------------------------
@shared_task(bind=True, name="tasks.daily_loss_limit_sweep_task")
def daily_loss_limit_sweep_task(self) -> dict[str, Any]:
    """End-of-session stop-loss sweep.

    Any autonomous portfolio whose realised loss for the UTC day has breached
    ``daily_loss_limit_usd`` is switched back to manual mode and the event is
    recorded in the explainability log.
    """
    logger.info("daily_loss_limit_sweep_task: starting")
    halted: list[dict[str, Any]] = []

    for portfolio in Portfolio.objects.filter(is_autonomous=True).select_related("user"):
        try:
            realised = realised_pnl_today(portfolio)
        except Exception as exc:
            logger.exception("P&L replay failed for portfolio %s: %s", portfolio.id, exc)
            continue

        limit = portfolio.daily_loss_limit_usd
        if limit > 0 and realised <= -limit:
            portfolio.is_autonomous = False
            portfolio.save(update_fields=["is_autonomous"])
            halted.append(
                {
                    "portfolio_id": portfolio.id,
                    "user": portfolio.user.username,
                    "realised_pnl_today": str(realised),
                    "limit": str(limit),
                }
            )
            try:
                AgentDecisionLog.objects.create(
                    portfolio=portfolio,
                    # No ticker: this is a portfolio-wide halt, not a decision
                    # about one instrument. The column stays blank rather than
                    # naming an arbitrary position, and the reasoning below says
                    # why - a fabricated ticker would be worse than an empty one.
                    reasoning=(
                        f"Portfolio stop-loss engaged. Realised P&L for the UTC day is "
                        f"${realised}, which breaches the configured daily loss limit of "
                        f"-${limit}. Autonomous trading has been suspended pending review."
                    ),
                    action_taken=f"HALT - daily loss limit breached (${realised})",
                    market_sentiment=MarketSentiment.BEARISH,
                )
            except Exception as exc:
                logger.exception("Failed to log halt for portfolio %s: %s", portfolio.id, exc)
            logger.warning("Portfolio %s halted: realised %s <= -%s", portfolio.id, realised, limit)

    return {"halted_count": len(halted), "halted": halted}


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
@shared_task(bind=True, name="tasks.purge_expired_auth_tokens_task")
def purge_expired_auth_tokens_task(self, max_age_days: int = 30) -> dict[str, Any]:
    """Delete auth tokens whose owner has not been active for ``max_age_days``.

    "Last used" is the owning user's ``last_login``, which
    :class:`core.auth.ActivityTokenAuthentication` keeps current on every
    authenticated request.

    This previously filtered on ``Token.created`` while documenting itself as
    last-use based: an actively-used token was deleted 30 days after issue, and
    an abandoned-but-recent one was kept. A token is now retained when *either*
    signal is recent, so the failure mode is keeping a token rather than
    revoking a live client.
    """
    from rest_framework.authtoken.models import Token

    cutoff = timezone.now() - timedelta(days=max_age_days)
    deleted, _detail = (
        Token.objects.filter(created__lt=cutoff)
        .filter(Q(user__last_login__isnull=True) | Q(user__last_login__lt=cutoff))
        .delete()
    )
    logger.info(
        "purge_expired_auth_tokens_task: removed %s token row(s) inactive for %s days",
        deleted,
        max_age_days,
    )
    return {"deleted": deleted, "max_age_days": max_age_days}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _current_marks(portfolio: Portfolio, ticker: str, primary_quote) -> dict[str, Any]:
    """Current marks for every material holding, for authoritative exposure.

    The traded ticker's quote is already in hand. Other holdings are priced with
    the same tolerant accessor; a name the provider cannot price is omitted, and
    the execution service then refuses a BUY rather than substituting cost.
    """
    marks: dict[str, Any] = {}
    if primary_quote is not None:
        marks[ticker.upper()] = primary_quote
    for asset in portfolio.assets.all():
        symbol = asset.ticker.upper()
        if symbol in marks:
            continue
        fallback = asset.avg_purchase_price if asset.avg_purchase_price > 0 else None
        try:
            other = get_latest_quote(symbol, fallback_price=fallback)
        except Exception as exc:  # pragma: no cover - accessor already tolerates
            logger.warning("Could not price %s for exposure: %s", symbol, exc)
            other = None
        if other is not None and getattr(other, "source", "") != "fallback":
            marks[symbol] = other
    return marks


def _capture_after_trade(portfolio: Portfolio) -> None:
    """Record a snapshot immediately after execution so the chart has a marker."""
    try:
        snapshot = capture_snapshot(portfolio)

        from core.serializers import build_portfolio_payload
        from services.events import emit_portfolio_snapshot

        payload = build_portfolio_payload(portfolio)
        payload["captured_at"] = snapshot.captured_at.isoformat()
        emit_portfolio_snapshot(portfolio.user_id, payload)
    except Exception as exc:
        logger.warning("Post-trade snapshot failed for %s: %s", portfolio.id, exc)


# ---------------------------------------------------------------------------
# Advisory sweep - analyse without executing
# ---------------------------------------------------------------------------
@shared_task(bind=True, name="tasks.advisory_sweep_task")
def advisory_sweep_task(self, portfolio_id: int) -> dict[str, Any]:
    """Analyse one portfolio's watchlist and queue recommendations for review.

    Used by the dashboard's "Analyse now" action and by the Telegram bot. Never
    executes: ``run_alpha_agent_task`` is dispatched in advisory mode.
    """
    if not claim_sweep_slot(portfolio_id=portfolio_id):
        logger.info("advisory sweep skipped by the debounce (portfolio_id=%s)", portfolio_id)
        return {"status": "debounced", "dispatched": 0}

    try:
        portfolio = Portfolio.objects.select_related("user").get(pk=portfolio_id)
    except Portfolio.DoesNotExist:
        return {"status": "skipped", "reason": "portfolio_missing"}

    held = {a.ticker.upper() for a in portfolio.assets.all()}
    tickers = list(dict.fromkeys([*sorted(held), *_watchlist()]))

    signatures = [run_alpha_agent_task.s(portfolio.id, ticker, True) for ticker in tickers]
    if not signatures:
        return {"status": "noop", "dispatched": 0}

    async_result = group(signatures).apply_async()
    summary = {
        "status": "dispatched",
        "portfolio_id": portfolio.id,
        "dispatched": len(signatures),
        "group_id": async_result.id,
    }
    logger.info("advisory_sweep_task: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Portfolio snapshots (equity time series)
# ---------------------------------------------------------------------------
@shared_task(bind=True, name="tasks.capture_portfolio_snapshots_task")
def capture_portfolio_snapshots_task(self) -> dict[str, Any]:
    """Write one valuation row per portfolio and publish it to live dashboards.

    The write alone was not enough: this task recorded the equity series every
    15 minutes but never emitted an event, so the metric cards and allocation
    panel of an open dashboard stayed stale until a trade or a reconnect. It now
    reuses the same payload builder as the REST endpoint and the post-trade
    path, so all three describe the portfolio identically.
    """
    captured = capture_all_snapshots()

    from core.serializers import build_portfolio_payload
    from services.events import emit_portfolio_snapshot

    emitted = 0
    for portfolio, snapshot in captured:
        try:
            payload = build_portfolio_payload(portfolio)
            payload["captured_at"] = snapshot.captured_at.isoformat()
            emit_portfolio_snapshot(portfolio.user_id, payload)
            emitted += 1
        except Exception as exc:
            logger.warning("Snapshot event failed for portfolio %s: %s", portfolio.id, exc)

    return {"status": "ok", "captured": len(captured), "emitted": emitted}


# ---------------------------------------------------------------------------
# Durable event delivery
# ---------------------------------------------------------------------------
@shared_task(bind=True, name="tasks.flush_event_outbox_task")
def flush_event_outbox_task(self, limit: int = 500) -> dict[str, Any]:
    """Retry queued notifications. Periodic catch-up, independent of sockets.

    The run event log and the ledger are the authority; this only reconciles the
    delivery queue with them. It also reports backlog and lag so a delivery
    failure is visible as a metric rather than as a silently stale dashboard.
    """
    from services.event_outbox import flush_outbox, outbox_metrics

    result = flush_outbox(limit=limit)
    metrics = outbox_metrics()
    logger.info("Event outbox flush: %s metrics=%s", result, metrics)
    return {
        "status": "ok",
        **result,
        "pending": metrics["pending"],
        "oldest_pending_age_seconds": metrics["oldest_pending_age_seconds"],
    }


# ---------------------------------------------------------------------------
# Recommendation lifecycle
# ---------------------------------------------------------------------------
@shared_task(bind=True, name="tasks.expire_stale_recommendations_task")
def expire_stale_recommendations_task(self) -> dict[str, Any]:
    """Auto-expire recommendations nobody acted on before their TTL."""
    expired = expire_stale_recommendations()
    return {"status": "ok", "expired": expired}


@shared_task(bind=True, name="tasks.notify_trade_task")
def notify_trade(self, transaction_id: int) -> dict[str, Any]:
    """Push an executed trade to the owner's Telegram chat, if linked.

    ``telegram_bot.notify_trade`` (and the ``notify_trades`` preference it
    honours) existed but had no caller anywhere, so the setting silently did
    nothing. Kept off the agent's critical path: a Telegram outage must not
    affect execution.
    """
    try:
        from telegram_bot import notify_trade as send

        return send(transaction_id)
    except Exception as exc:
        logger.warning("Trade notification failed for %s: %s", transaction_id, exc)
        return {"status": "error", "error": str(exc)}


@shared_task(bind=True, name="tasks.notify_recommendation_task")
def notify_recommendation(self, recommendation_id: int) -> dict[str, Any]:
    """Push a new recommendation to the owner's Telegram chat, if linked."""
    try:
        from telegram_bot import notify_recommendation as send

        return send(recommendation_id)
    except Exception as exc:
        logger.warning("Telegram notification failed for %s: %s", recommendation_id, exc)
        return {"status": "error", "error": str(exc)}


@shared_task(bind=True, name="tasks.evaluate_decision_outcomes_task")
def evaluate_decision_outcomes_task(self) -> dict[str, Any]:
    """Score matured decisions against what the market actually did next.

    The audit trail records why every decision was made. This is the half that
    records whether any of them were right, so the system can be measured rather
    than believed.

    Runs hourly rather than on the 15-minute sweep: a new outcome only becomes
    available when a decision crosses a horizon boundary, which happens on a
    day-scale, and the scoring itself fetches price history for every pending
    decision. Hourly keeps the yfinance load trivial while ensuring a decision
    scored within an hour of maturing.

    Idempotent, so a retry or an overlapping run costs only time.
    """
    from services.attribution import evaluate_pending

    try:
        counts = evaluate_pending()
    except Exception as exc:  # pragma: no cover - defensive, like the other sweeps
        logger.exception("Decision attribution failed: %s", exc)
        return {"status": "error", "error": str(exc)}

    logger.info("evaluate_decision_outcomes_task done: %s", counts)
    return {"status": "ok", **counts}


# ---------------------------------------------------------------------------
# Portfolio analysis worker (P1 step 6)
# ---------------------------------------------------------------------------
@shared_task(
    bind=True,
    name="tasks.run_allocation_submission_task",
    max_retries=0,
    acks_late=True,
)
def run_allocation_submission_task(self, submission_id: str) -> dict[str, Any]:
    """Run one durable portfolio-analysis submission.

    The HTTP endpoint only writes ``AllocationSubmission`` and publishes this
    task; it never calls a model. The task is deliberately not auto-retried:
    `process_submission` owns the attempt counter, owner lease and provider
    checkpoint, so a redelivery that does not own a live lease is a no-op and a
    retry after a lost worker is a deliberate repair decision rather than a
    broker retry that could spend twice.
    """
    # Local import keeps the task module importable without importing the whole
    # allocation service (which imports the worker module for dispatch).
    from services.allocation_submission import process_submission

    request = getattr(self, "request", None)
    task_id = str(getattr(request, "id", "") or "eager")
    hostname = str(getattr(request, "hostname", "") or "worker")
    owner = f"celery:{hostname}:{task_id}"[:128]
    submission = process_submission(submission_id, owner=owner)
    return {
        "submission_id": str(submission.id),
        "status": submission.status,
        "attempt_count": submission.attempt_count,
        "plan_id": str(submission.plan_id) if submission.plan_id else None,
    }
