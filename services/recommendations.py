"""Human-in-the-loop trade approvals.

Flow
----
1. The AI proposes a trade. In **autonomous** mode it executes immediately
   (unchanged); in **advisory** mode it becomes a ``PENDING`` recommendation.
2. A human approves or rejects it from the dashboard or Telegram.
3. Approval re-validates the trade against **current** prices and limits using
   the same guard, then hands that exact decision to
   :func:`services.execution.execute_trade`, which re-runs it under the portfolio
   lock before writing the ledger.

Step 3 is the important one: approving is not a bypass. A recommendation that
was valid an hour ago may be stale, over-budget or past the stop-loss by the
time someone taps the button, and it will be blocked accordingly. A material
change between the approval evaluation and the lock is a new decision and
requires a new approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from ai_agent import TradeProposal
from core.decision_models import DecisionExecutionState
from core.models import (
    AgentDecisionLog,
    DecidedVia,
    DecisionRun,
    MarketSentiment,
    Portfolio,
    RecommendationStatus,
    TradeRecommendation,
)
from services.decision_lifecycle import complete_run
from services.events import emit_decision, emit_recommendation, emit_trade
from services.execution import (
    ExecutionRejected,
    ExecutionRequest,
    GuardDecision,
    evaluate_proposal,
    execute_trade,
    portfolio_policy_version,
)
from services.ledger import PositionState, replay_positions
from services.market_data import get_latest_quote
from services.portfolio_brief import portfolio_state_version
from services.portfolio_metrics import build_exposure

logger = logging.getLogger("alphaagent.recommendations")

__all__ = [
    "ApprovalOutcome",
    "approve_recommendation",
    "create_recommendation",
    "expire_stale_recommendations",
    "recommendation_ttl",
    "reject_recommendation",
]


def recommendation_ttl() -> timedelta:
    from django.conf import settings

    minutes = int(getattr(settings, "RECOMMENDATION_TTL_MINUTES", 60) or 60)
    return timedelta(minutes=minutes)


@dataclass
class ApprovalOutcome:
    """Result of an approve/reject action."""

    recommendation: TradeRecommendation
    executed: bool
    message: str
    transaction_id: int | None = None
    guard_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.recommendation.id,
            "status": self.recommendation.status,
            "executed": self.executed,
            "transaction_id": self.transaction_id,
            "message": self.message,
            "guard_reason": self.guard_reason,
        }


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def create_recommendation(
    portfolio: Portfolio,
    proposal: TradeProposal,
    price: Decimal,
    *,
    ticker: str,
    decision_log: AgentDecisionLog | None = None,
    approved_amount: Decimal | None = None,
    decision_run: DecisionRun | None = None,
) -> TradeRecommendation | None:
    """Persist an advisory proposal for human review.

    Returns ``None`` for HOLD (nothing to approve) or when a recommendation for
    the same ticker is already pending, so a chatty sweep cannot spam the user
    with duplicates.
    """
    if proposal.action == "HOLD":
        return None

    # ``approved_amount`` is the quantity the guard actually permitted, not the
    # raw proposal. Storing the raw number would ask the operator to approve an
    # order the guard had already refused at that size.
    amount = Decimal(str(approved_amount if approved_amount is not None else proposal.amount))
    ticker = (ticker or "").upper()
    if amount <= 0 or price <= 0 or not ticker:
        return None

    if TradeRecommendation.objects.filter(
        portfolio=portfolio, ticker=ticker, status=RecommendationStatus.PENDING
    ).exists():
        logger.info("Suppressing duplicate pending recommendation for %s", ticker)
        return None

    recommendation = TradeRecommendation.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        action=proposal.action,
        amount=amount,
        price=price,
        notional_usd=(amount * price).quantize(Decimal("0.01")),
        sentiment=proposal.sentiment,
        reasoning=proposal.reasoning,
        status=RecommendationStatus.PENDING,
        expires_at=timezone.now() + recommendation_ttl(),
        decision_log=decision_log,
        decision_run=decision_run,
    )
    emit_recommendation(portfolio.user_id, recommendation, created=True)
    logger.info(
        "Created recommendation %s: %s %s %s",
        recommendation.id,
        recommendation.action,
        amount,
        recommendation.ticker,
    )
    return recommendation


# ---------------------------------------------------------------------------
# Approval / rejection
# ---------------------------------------------------------------------------
@transaction.atomic
def approve_recommendation(recommendation_id: int, via: str = DecidedVia.WEB) -> ApprovalOutcome:
    """Approve and attempt execution through the authoritative lock.

    The approval is a decision made *now*: it re-reads the market, re-evaluates
    the guard and then hands that exact decision to ``execute_trade``, which
    re-runs it under the portfolio lock. If the lock disagrees materially with
    the approval, nothing is executed and a new approval is required.
    """
    recommendation = (
        TradeRecommendation.objects.select_for_update()
        .select_related("portfolio", "portfolio__user")
        .get(pk=recommendation_id)
    )
    portfolio = recommendation.portfolio

    if not recommendation.is_actionable:
        reason = (
            "Recommendation already decided."
            if not recommendation.is_pending
            else "Recommendation has expired."
        )
        if recommendation.is_pending and recommendation.expires_at:
            recommendation.status = RecommendationStatus.EXPIRED
            recommendation.decided_at = timezone.now()
            recommendation.decided_via = via
            recommendation.save(update_fields=["status", "decided_at", "decided_via"])
            emit_recommendation(portfolio.user_id, recommendation, created=False)
            _complete_advisory_run(
                recommendation,
                DecisionExecutionState.EXPIRED,
                reason="Recommendation expired before approval.",
                via=via,
            )
        return ApprovalOutcome(recommendation, False, reason)

    # --- Re-validate against *current* market and portfolio state ---------
    positions = replay_positions(portfolio)
    position = positions.get(recommendation.ticker, PositionState(ticker=recommendation.ticker))
    realised_today = sum((s.realised_pnl_today for s in positions.values()), Decimal("0")).quantize(
        Decimal("0.01")
    )

    quote = get_latest_quote(recommendation.ticker, fallback_price=recommendation.price)
    price = quote.price if quote else None
    marks = _current_marks(portfolio, recommendation.ticker, quote)

    proposal = TradeProposal(
        action=recommendation.action,
        amount=float(recommendation.amount),
        sentiment=recommendation.sentiment,
        reasoning=recommendation.reasoning or "Approved by operator.",
    )
    policy_version = portfolio_policy_version(portfolio)
    portfolio_version = portfolio_state_version(portfolio)
    decision: GuardDecision = evaluate_proposal(
        portfolio,
        proposal,
        price,
        position,
        realised_today,
        exposure=build_exposure(portfolio, recommendation.ticker, price_map=marks),
    )
    decision.policy_version = policy_version
    decision.evaluated_portfolio_version = portfolio_version
    decision.quote_time = quote.as_of if quote is not None else None

    recommendation.decided_at = timezone.now()
    recommendation.decided_via = via

    if not decision.approved:
        recommendation.status = RecommendationStatus.BLOCKED
        recommendation.save(update_fields=["status", "decided_at", "decided_via"])
        _log_human_decision(
            portfolio,
            recommendation,
            approved=True,
            executed=False,
            detail=f"Approval accepted but execution was blocked: {decision.reason}",
        )
        emit_recommendation(portfolio.user_id, recommendation, created=False)
        _complete_advisory_run(
            recommendation,
            DecisionExecutionState.BLOCKED,
            guard_decision=decision,
            reason=decision.reason,
            via=via,
        )
        logger.warning("Recommendation %s blocked: %s", recommendation.id, decision.reason)
        return ApprovalOutcome(
            recommendation,
            False,
            f"Blocked by guardrail: {decision.reason}",
            guard_reason=decision.reason,
        )

    request = ExecutionRequest(
        portfolio_id=portfolio.id,
        ticker=recommendation.ticker,
        side=recommendation.action,
        quantity=decision.amount,
        quotes=marks,
        requested_quantity=decision.requested_amount or decision.amount,
        execution_key=f"recommendation:{recommendation.id}",
        source="advisory",
        approval_ref=f"recommendation:{recommendation.id}",
        approved_policy_version=policy_version,
        approved_portfolio_version=portfolio_version,
        approved_price=decision.price,
    )
    try:
        execution = execute_trade(request)
    except ExecutionRejected as exc:
        blocked_decision = getattr(exc, "decision", None) or decision
        recommendation.status = RecommendationStatus.BLOCKED
        recommendation.save(update_fields=["status", "decided_at", "decided_via"])
        _log_human_decision(
            portfolio,
            recommendation,
            approved=True,
            executed=False,
            detail=f"Approval accepted but execution was blocked: {exc}",
        )
        emit_recommendation(portfolio.user_id, recommendation, created=False)
        _complete_advisory_run(
            recommendation,
            DecisionExecutionState.BLOCKED,
            guard_decision=blocked_decision,
            reason=str(exc),
            via=via,
        )
        logger.warning("Recommendation %s blocked under lock: %s", recommendation.id, exc)
        return ApprovalOutcome(
            recommendation, False, f"Blocked by guardrail: {exc}", guard_reason=str(exc)
        )

    if not execution.approved or execution.transaction is None:
        blocked_decision = execution.decision or decision
        recommendation.status = RecommendationStatus.BLOCKED
        recommendation.save(update_fields=["status", "decided_at", "decided_via"])
        _log_human_decision(
            portfolio,
            recommendation,
            approved=True,
            executed=False,
            detail=f"Approval accepted but execution was blocked: {execution.reason}",
        )
        emit_recommendation(portfolio.user_id, recommendation, created=False)
        _complete_advisory_run(
            recommendation,
            DecisionExecutionState.BLOCKED,
            guard_decision=blocked_decision,
            reason=execution.reason,
            via=via,
        )
        return ApprovalOutcome(
            recommendation,
            False,
            f"Blocked by guardrail: {execution.reason}",
            guard_reason=execution.reason,
        )

    transaction_row = execution.transaction
    executed_decision = execution.decision

    recommendation.status = RecommendationStatus.EXECUTED
    recommendation.transaction = transaction_row
    # Record what was actually executed - the guard may have clamped the size.
    recommendation.amount = executed_decision.amount
    recommendation.price = executed_decision.price or recommendation.price
    recommendation.notional_usd = executed_decision.notional
    recommendation.save(
        update_fields=[
            "status",
            "transaction",
            "amount",
            "price",
            "notional_usd",
            "decided_at",
            "decided_via",
        ]
    )

    _log_human_decision(
        portfolio,
        recommendation,
        approved=True,
        executed=True,
        detail=f"Executed {executed_decision.amount.normalize()} {recommendation.ticker} "
        f"@ ${executed_decision.price} (${executed_decision.notional}).",
    )
    emit_trade(portfolio.user_id, transaction_row)
    emit_recommendation(portfolio.user_id, recommendation, created=False)
    _complete_advisory_run(
        recommendation,
        DecisionExecutionState.EXECUTED,
        guard_decision=executed_decision,
        transaction_row=transaction_row,
        reason=executed_decision.reason,
        via=via,
    )

    # A human-approved trade deserves the same Telegram notification as an
    # autonomous one. Dispatched by name to avoid importing tasks.py here
    # (tasks imports this module), and best-effort so a broker hiccup cannot
    # fail an approval that has already been committed.
    try:
        from config.celery import app as celery_app

        celery_app.send_task("tasks.notify_trade_task", args=[transaction_row.id])
    except Exception as exc:
        logger.warning("Could not enqueue trade notification for %s: %s", transaction_row.id, exc)

    message = (
        f"Executed {recommendation.action} {executed_decision.amount.normalize()} "
        f"{recommendation.ticker} @ ${executed_decision.price}"
    )
    logger.info("Recommendation %s executed: %s", recommendation.id, message)
    return ApprovalOutcome(recommendation, True, message, transaction_id=transaction_row.id)


@transaction.atomic
def reject_recommendation(recommendation_id: int, via: str = DecidedVia.WEB) -> ApprovalOutcome:
    recommendation = (
        TradeRecommendation.objects.select_for_update()
        .select_related("portfolio", "portfolio__user")
        .get(pk=recommendation_id)
    )

    if not recommendation.is_actionable:
        return ApprovalOutcome(recommendation, False, "Recommendation already decided.")

    recommendation.status = RecommendationStatus.REJECTED
    recommendation.decided_at = timezone.now()
    recommendation.decided_via = via
    recommendation.save(update_fields=["status", "decided_at", "decided_via"])

    _log_human_decision(
        recommendation.portfolio,
        recommendation,
        approved=False,
        executed=False,
        detail="Operator declined the proposal.",
    )
    emit_recommendation(recommendation.portfolio.user_id, recommendation, created=False)
    _complete_advisory_run(
        recommendation,
        DecisionExecutionState.REJECTED,
        reason="Operator declined the proposal.",
        via=via,
    )
    logger.info("Recommendation %s rejected via %s", recommendation.id, via)
    return ApprovalOutcome(recommendation, False, "Recommendation rejected.")


def _current_marks(portfolio: Portfolio, ticker: str, primary_quote) -> dict:
    """Marks for every material holding, used for authoritative exposure."""
    marks: dict = {}
    if primary_quote is not None:
        marks[ticker.upper()] = primary_quote
    for asset in portfolio.assets.all():
        symbol = asset.ticker.upper()
        if symbol in marks:
            continue
        fallback = asset.avg_purchase_price if asset.avg_purchase_price > 0 else None
        try:
            other = get_latest_quote(symbol, fallback_price=fallback)
        except Exception as exc:  # pragma: no cover - accessor tolerates
            logger.warning("Could not price %s for exposure: %s", symbol, exc)
            other = None
        if other is not None and getattr(other, "source", "") != "fallback":
            marks[symbol] = other
    return marks


def _complete_advisory_run(
    recommendation: TradeRecommendation,
    outcome: str,
    *,
    guard_decision: GuardDecision | None = None,
    transaction_row=None,
    reason: str = "",
    via: str = "",
) -> None:
    """Connect an approval decision back to the run that proposed it.

    No run is a no-op rather than an error: recommendations created before the
    run link existed still work, and no synthetic history is manufactured for
    them.
    """
    run = recommendation.decision_run
    if run is None:
        return
    try:
        complete_run(
            run,
            outcome=outcome,
            guard_decision=guard_decision,
            transaction_row=transaction_row,
            recommendation=recommendation,
            reason=reason,
            via=via,
        )
    except Exception as exc:  # pragma: no cover - the Phase 0 audit row survives
        logger.exception("Could not complete decision run %s: %s", run.id, exc)


def _log_human_decision(
    portfolio: Portfolio,
    recommendation: TradeRecommendation,
    *,
    approved: bool,
    executed: bool,
    detail: str,
) -> None:
    """Record the human decision in the AI audit trail.

    Keeping operator actions in the same timeline as agent reasoning means the
    dashboard shows one continuous story: proposal -> decision -> outcome.
    """
    verdict = "APPROVED" if approved else "REJECTED"
    try:
        log = AgentDecisionLog.objects.create(
            portfolio=portfolio,
            transaction=recommendation.transaction,
            reasoning=(
                f"Human decision ({recommendation.decided_via or 'unknown'}): {verdict}.\n\n"
                f"AI proposal: {recommendation.action} {recommendation.amount} "
                f"{recommendation.ticker} @ ${recommendation.price}.\n\n"
                f"Outcome: {detail}\n\n"
                f"Original AI reasoning:\n{recommendation.reasoning or '(none recorded)'}"
            ),
            action_taken=f"{verdict} by operator - {detail}"[:255],
            market_sentiment=recommendation.sentiment or MarketSentiment.NEUTRAL,
        )
        emit_decision(portfolio.user_id, log)
    except Exception as exc:
        logger.exception("Failed to write human-decision audit row: %s", exc)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
def expire_stale_recommendations() -> int:
    """Mark timed-out pending recommendations as EXPIRED."""
    stale = TradeRecommendation.objects.filter(
        status=RecommendationStatus.PENDING, expires_at__lt=timezone.now()
    ).select_related("portfolio")

    expired = 0
    for recommendation in stale:
        recommendation.status = RecommendationStatus.EXPIRED
        recommendation.decided_at = timezone.now()
        recommendation.decided_via = DecidedVia.AUTO
        recommendation.save(update_fields=["status", "decided_at", "decided_via"])
        emit_recommendation(recommendation.portfolio.user_id, recommendation, created=False)
        _complete_advisory_run(
            recommendation,
            DecisionExecutionState.EXPIRED,
            reason="Recommendation expired before a decision.",
            via=DecidedVia.AUTO,
        )
        expired += 1

    if expired:
        logger.info("Expired %s stale recommendation(s)", expired)
    return expired
