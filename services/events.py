"""Real-time event bus.

Celery workers publish here; the dashboard's WebSocket consumer subscribes.
Every publish is best-effort: if Redis or the channel layer is unavailable the
event is logged and dropped. **A live-update failure must never fail a trade.**
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from django.utils import timezone

logger = logging.getLogger("alphaagent.events")

__all__ = [
    "EventType",
    "emit_agent_thinking",
    "emit_autonomy_changed",
    "emit_budget_exhausted",
    "emit_budget_updated",
    "emit_decision",
    "emit_portfolio_snapshot",
    "emit_recommendation",
    "emit_trade",
    "portfolio_group",
    "publish",
    "serialize",
]


class EventType:
    """Wire values shared with the SPA. Keep in sync with ``frontend/src/types.ts``."""

    CONNECTED = "connection.established"
    PORTFOLIO_SNAPSHOT = "portfolio.snapshot"
    TRADE_EXECUTED = "trade.executed"
    DECISION_CREATED = "decision.created"
    AGENT_THINKING = "agent.thinking"
    RECOMMENDATION_CREATED = "recommendation.created"
    RECOMMENDATION_UPDATED = "recommendation.updated"
    AUTONOMY_CHANGED = "autonomy.changed"
    #: Carries today's AI spend against the ceiling, so the dashboard meter
    #: moves as runs happen rather than only on a page reload.
    BUDGET_UPDATED = "budget.updated"
    BUDGET_EXHAUSTED = "budget.exhausted"


def portfolio_group(user_id: int) -> str:
    return f"portfolio.{user_id}"


def serialize(value: Any) -> Any:
    """Convert Decimals/datetimes into JSON-safe primitives, recursively."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    return value


def publish(user_id: int | None, event_type: str, payload: dict[str, Any]) -> bool:
    """Broadcast ``event_type`` to one user's dashboard.

    Returns ``True`` when the event reached the channel layer. Never raises.
    """
    if user_id is None:
        return False

    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            logger.debug("No channel layer configured; dropping %s", event_type)
            return False

        async_to_sync(channel_layer.group_send)(
            portfolio_group(user_id),
            {
                "type": "portfolio_event",  # -> PortfolioConsumer.portfolio_event
                "event_type": event_type,
                "payload": serialize(payload),
            },
        )
        return True
    except Exception as exc:
        logger.warning("Failed to publish %s to user %s: %s", event_type, user_id, exc)
        return False


# ---------------------------------------------------------------------------
# Typed helpers - keep payload shapes in one place
# ---------------------------------------------------------------------------
def emit_agent_thinking(
    user_id: int | None, portfolio_id: int, ticker: str, stage: str, message: str
) -> None:
    """Live progress from inside the crew (drives the 'thinking' indicator)."""
    publish(
        user_id,
        EventType.AGENT_THINKING,
        {
            "ticker": ticker,
            "stage": stage,
            "message": message,
            "portfolio_id": portfolio_id,
            "at": timezone.now(),
        },
    )


def emit_decision(user_id: int | None, log) -> None:
    """A new AgentDecisionLog row (the AI 'thoughts' feed)."""
    publish(
        user_id,
        EventType.DECISION_CREATED,
        {
            "id": log.id,
            "action_taken": log.action_taken,
            "market_sentiment": log.market_sentiment,
            "ticker": getattr(getattr(log, "transaction", None), "ticker", None),
            "transaction_id": log.transaction_id,
            "tokens_used": log.tokens_used,
            "api_cost_usd": log.api_cost_usd,
            "reasoning": log.reasoning,
            "bull_case": getattr(log, "bull_case", ""),
            "bear_case": getattr(log, "bear_case", ""),
            "created_at": log.created_at,
        },
    )


def emit_trade(user_id: int | None, transaction) -> None:
    publish(
        user_id,
        EventType.TRADE_EXECUTED,
        {
            "id": transaction.id,
            "ticker": transaction.ticker,
            "tx_type": transaction.tx_type,
            "amount": transaction.amount,
            "price": transaction.price,
            "gross_value_usd": transaction.gross_value_usd,
            "executed_by": transaction.executed_by,
            "timestamp": transaction.timestamp,
        },
    )


def emit_portfolio_snapshot(user_id: int | None, payload: dict) -> None:
    """Publish a portfolio snapshot built by ``build_portfolio_payload``.

    ``payload`` must already carry ``metrics`` and ``assets``: the client's
    ``PortfolioSnapshotEvent`` type destructures exactly those two keys. This
    function deliberately does not reshape the data - it previously published
    the flat snapshot-row fields instead, which the dashboard silently wrote
    over its own state as ``undefined``.
    """
    publish(
        user_id,
        EventType.PORTFOLIO_SNAPSHOT,
        {
            "metrics": payload.get("metrics"),
            "assets": payload.get("assets"),
            "captured_at": payload.get("captured_at"),
        },
    )


def emit_recommendation(user_id: int | None, recommendation, *, created: bool = True) -> None:
    publish(
        user_id,
        EventType.RECOMMENDATION_CREATED if created else EventType.RECOMMENDATION_UPDATED,
        {
            "id": recommendation.id,
            "ticker": recommendation.ticker,
            "action": recommendation.action,
            "amount": recommendation.amount,
            "price": recommendation.price,
            "notional_usd": recommendation.notional_usd,
            "sentiment": recommendation.sentiment,
            "reasoning": recommendation.reasoning,
            "status": recommendation.status,
            "decided_via": recommendation.decided_via,
            "decided_at": recommendation.decided_at,
            "expires_at": recommendation.expires_at,
            "created_at": recommendation.created_at,
        },
    )


def emit_autonomy_changed(user_id: int | None, portfolio) -> None:
    publish(
        user_id,
        EventType.AUTONOMY_CHANGED,
        {"portfolio_id": portfolio.id, "is_autonomous": portfolio.is_autonomous},
    )


def emit_budget_updated(user_id: int | None, payload: dict) -> None:
    """Publish the current spend against the ceiling.

    Sent after every agent run so the meter on the dashboard reflects money that
    has actually been spent, without the client polling for it.
    """
    publish(user_id, EventType.BUDGET_UPDATED, payload)


def emit_budget_exhausted(user_id: int | None, payload: dict) -> None:
    """Publish that the ceiling has been reached and runs are halted.

    Separate from ``budget.updated`` because it is the one budget event a client
    must not ignore: it explains why no further decisions are arriving.
    """
    publish(user_id, EventType.BUDGET_EXHAUSTED, payload)
