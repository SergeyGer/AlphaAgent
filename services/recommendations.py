"""Human-in-the-loop trade approvals.

Flow
----
1. The AI proposes a trade. In **autonomous** mode it executes immediately
   (unchanged); in **advisory** mode it becomes a ``PENDING`` recommendation.
2. A human approves or rejects it from the dashboard or Telegram.
3. Approval re-validates the trade against **current** prices and limits using
   the *same* :func:`services.execution.evaluate_proposal` guard.

Step 3 is the important one: approving is not a bypass. A recommendation that
was valid an hour ago may be stale, over-budget or past the stop-loss by the
time someone taps the button, and it will be blocked accordingly.
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
from core.models import (
    AgentDecisionLog,
    DecidedVia,
    MarketSentiment,
    Portfolio,
    RecommendationStatus,
    TradeRecommendation,
)
from services.events import emit_decision, emit_recommendation, emit_trade
from services.execution import GuardDecision, apply_trade, evaluate_proposal
from services.ledger import PositionState, replay_positions
from services.market_data import get_latest_quote

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
) -> TradeRecommendation | None:
    """Persist an advisory proposal for human review.

    Returns ``None`` for HOLD (nothing to approve) or when a recommendation for
    the same ticker is already pending, so a chatty sweep cannot spam the user
    with duplicates.
    """
    if proposal.action == "HOLD":
        return None

    amount = Decimal(str(proposal.amount))
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
    """Approve and attempt execution, re-validating against live state."""
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
        return ApprovalOutcome(recommendation, False, reason)

    # --- Re-validate against *current* market and portfolio state ---------
    positions = replay_positions(portfolio)
    position = positions.get(recommendation.ticker, PositionState(ticker=recommendation.ticker))
    realised_today = sum((s.realised_pnl_today for s in positions.values()), Decimal("0")).quantize(
        Decimal("0.01")
    )

    quote = get_latest_quote(recommendation.ticker, fallback_price=recommendation.price)
    price = quote.price if quote else None

    proposal = TradeProposal(
        action=recommendation.action,
        amount=float(recommendation.amount),
        sentiment=recommendation.sentiment,
        reasoning=recommendation.reasoning or "Approved by operator.",
    )
    decision: GuardDecision = evaluate_proposal(
        portfolio, proposal, price, position, realised_today
    )

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
        logger.warning("Recommendation %s blocked: %s", recommendation.id, decision.reason)
        return ApprovalOutcome(
            recommendation,
            False,
            f"Blocked by guardrail: {decision.reason}",
            guard_reason=decision.reason,
        )

    transaction_row = apply_trade(portfolio.id, recommendation.ticker, decision)

    recommendation.status = RecommendationStatus.EXECUTED
    recommendation.transaction = transaction_row
    # Record what was actually executed - the guard may have clamped the size.
    recommendation.amount = decision.amount
    recommendation.price = decision.price or recommendation.price
    recommendation.notional_usd = decision.notional
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
        detail=f"Executed {decision.amount.normalize()} {recommendation.ticker} "
        f"@ ${decision.price} (${decision.notional}).",
    )
    emit_trade(portfolio.user_id, transaction_row)
    emit_recommendation(portfolio.user_id, recommendation, created=False)

    message = (
        f"Executed {recommendation.action} {decision.amount.normalize()} "
        f"{recommendation.ticker} @ ${decision.price}"
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
    logger.info("Recommendation %s rejected via %s", recommendation.id, via)
    return ApprovalOutcome(recommendation, False, "Recommendation rejected.")


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
        expired += 1

    if expired:
        logger.info("Expired %s stale recommendation(s)", expired)
    return expired
