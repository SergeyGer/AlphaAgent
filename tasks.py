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
    3. Run the CrewAI analyst -> CIO pipeline (``ai_agent.run_alpha_agent``).
    4. **Execution Guard**: validate the JSON payload against
       ``max_trade_allocation_pct`` and ``daily_loss_limit_usd``.
    5. Only if approved, open ``transaction.atomic()`` + ``select_for_update()``
       and mutate ``Portfolio`` / ``Asset`` / ``Transaction``.
    6. Always write an ``AgentDecisionLog`` row - including HOLDs and rejections -
       so the audit trail is complete and explainable.

In **advisory mode** (``require_approval=True`` on a non-autonomous portfolio)
step 5 is replaced by a ``PENDING TradeRecommendation``; a human then approves
or rejects it and the same guard runs again against live prices.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from celery import group, shared_task
from django.conf import settings
from django.db import DatabaseError, OperationalError
from django.db.models import Prefetch
from django.utils import timezone

from ai_agent import (
    AgentRunResult,
    DecisionContext,
    build_news_report,
    run_alpha_agent,
)
from core.models import (
    AgentDecisionLog,
    Asset,
    MarketSentiment,
    Portfolio,
    TradeRecommendation,
    Transaction,
)
from services.events import (
    emit_agent_thinking,
    emit_decision,
    emit_trade,
)
from services.execution import (
    CENT,
    ZERO,
    apply_trade,
    evaluate_proposal,
)
from services.fundamentals import get_financial_health, get_price_history
from services.ledger import PositionState, realised_pnl_today, replay_positions
from services.market_data import get_latest_quote
from services.recommendations import (
    create_recommendation,
    expire_stale_recommendations,
)
from services.snapshots import capture_all_snapshots, capture_snapshot
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

    # ---- 1. Read-only context -------------------------------------------
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
            reasoning=f"Agent pipeline failure: {type(exc).__name__}: {exc}",
            action_taken="ERROR - agent pipeline failed",
            market_sentiment=MarketSentiment.NEUTRAL,
        )
        emit_agent_thinking(user_id, portfolio.id, ticker, "failed", str(exc)[:200])
        emit_decision(user_id, failure_log)
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
    decision = evaluate_proposal(portfolio, proposal, price, position, realised_today)

    transaction_row: Transaction | None = None
    recommendation = None

    if require_approval and decision.approved:
        # Advisory mode: the trade is valid, but a human must approve it.
        recommendation = create_recommendation(portfolio, proposal, decision.price, ticker=ticker)
    elif decision.approved:
        try:
            transaction_row = apply_trade(portfolio.id, ticker, decision)
        except (DatabaseError, OperationalError) as exc:
            decision.approved = False
            decision.reason = f"Execution failed and was rolled back: {exc}"
            logger.exception("Trade execution failed for %s/%s: %s", portfolio_id, ticker, exc)
        else:
            emit_trade(user_id, transaction_row)
            _capture_after_trade(portfolio)

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
            transaction=transaction_row,
            reasoning=audit_reasoning[:20000],
            bull_case=run_result.bull_case[:20000],
            bear_case=run_result.bear_case[:20000],
            action_taken=action_taken[:255],
            market_sentiment=proposal.sentiment,
            tokens_used=run_result.tokens_used,
            api_cost_usd=run_result.api_cost_usd,
        )
        emit_decision(user_id, decision_log)
    except Exception as exc:
        logger.exception("Failed to persist AgentDecisionLog for %s: %s", portfolio_id, exc)

    if recommendation is not None and decision_log is not None:
        # Link the audit row so the dashboard can show proposal -> approval.
        TradeRecommendation.objects.filter(pk=recommendation.pk).update(decision_log=decision_log)
        notify_recommendation.delay(recommendation.id)

    if transaction_row is not None:
        status = "executed"
    elif recommendation is not None:
        status = "awaiting_approval"
    elif decision.action == "HOLD":
        status = "hold"
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
        "bull_case_chars": len(run_result.bull_case),
        "bear_case_chars": len(run_result.bear_case),
    }
    logger.info("run_alpha_agent_task done: %s", result)
    return result


# ---------------------------------------------------------------------------
# Fan-out entrypoint
# ---------------------------------------------------------------------------
@shared_task(bind=True, name="tasks.autonomous_market_monitoring_task")
def autonomous_market_monitoring_task(self) -> dict[str, Any]:
    """Query all autonomous portfolios and fan out one subtask per ticker.

    A single ``group`` publish replaces a nested loop, so N portfolios x M tickers
    are dispatched to the worker pool concurrently instead of being processed
    sequentially inside one task.
    """
    if not claim_sweep_slot():
        logger.info("autonomous_market_monitoring_task: skipped by the sweep debounce")
        return {"status": "debounced", "dispatched": 0}

    logger.info("autonomous_market_monitoring_task: sweep starting")

    # One query, joined/prefetched - no per-portfolio round trips.
    portfolios = (
        Portfolio.objects.filter(is_autonomous=True)
        .select_related("user")
        .prefetch_related(
            Prefetch("assets", queryset=Asset.objects.only("id", "portfolio_id", "ticker"))
        )
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
    }
    logger.info("autonomous_market_monitoring_task: %s", summary)
    return summary


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
    """Delete DRF auth tokens that have not been used for ``max_age_days``."""
    from rest_framework.authtoken.models import Token

    cutoff = timezone.now() - timedelta(days=max_age_days)
    deleted, _detail = Token.objects.filter(created__lt=cutoff).delete()
    logger.info("purge_expired_auth_tokens_task: removed %s token rows", deleted)
    return {"deleted": deleted, "max_age_days": max_age_days}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _capture_after_trade(portfolio: Portfolio) -> None:
    """Record a snapshot immediately after execution so the chart has a marker."""
    try:
        snapshot = capture_snapshot(portfolio)
        from services.events import emit_portfolio_snapshot

        emit_portfolio_snapshot(portfolio.user_id, snapshot)
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
    if not claim_sweep_slot():
        logger.info("advisory_sweep_task: skipped by the sweep debounce")
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
    """Write one valuation row per portfolio - the dashboard's equity series."""
    captured = capture_all_snapshots()
    return {"status": "ok", "captured": captured}


# ---------------------------------------------------------------------------
# Recommendation lifecycle
# ---------------------------------------------------------------------------
@shared_task(bind=True, name="tasks.expire_stale_recommendations_task")
def expire_stale_recommendations_task(self) -> dict[str, Any]:
    """Auto-expire recommendations nobody acted on before their TTL."""
    expired = expire_stale_recommendations()
    return {"status": "ok", "expired": expired}


@shared_task(bind=True, name="tasks.notify_recommendation_task")
def notify_recommendation(self, recommendation_id: int) -> dict[str, Any]:
    """Push a new recommendation to the owner's Telegram chat, if linked."""
    try:
        from telegram_bot import notify_recommendation as send

        return send(recommendation_id)
    except Exception as exc:
        logger.warning("Telegram notification failed for %s: %s", recommendation_id, exc)
        return {"status": "error", "error": str(exc)}
