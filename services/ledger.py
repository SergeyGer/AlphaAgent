"""Ledger replay / position accounting.

Weighted-average-cost accounting is deterministic given the immutable
``Transaction`` ledger, so replaying it yields the exact cost basis per ticker
and the realised P&L for the current UTC day - no snapshot table required.

Shared by the Celery execution guard (``tasks.py``) and the DRF API layer, and
deliberately free of Celery imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from django.utils import timezone

from core.models import Portfolio, Transaction, TxType

__all__ = [
    "ZERO",
    "PositionState",
    "realised_pnl_today",
    "replay_positions",
    "utc_day_start",
]

ZERO = Decimal("0")
CENT = Decimal("0.01")


def utc_day_start(moment: datetime | None = None) -> datetime:
    """Midnight UTC of the day containing ``moment``."""
    reference = (moment or timezone.now()).astimezone(UTC)
    return reference.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class PositionState:
    """Reconstructed position for one ticker."""

    ticker: str
    amount: Decimal = ZERO
    avg_cost: Decimal = ZERO
    realised_pnl_today: Decimal = ZERO
    realised_pnl_total: Decimal = ZERO


def replay_positions(portfolio: Portfolio) -> dict[str, PositionState]:
    """Replay the whole ledger and derive per-ticker positions + realised P&L."""
    day_start = utc_day_start()
    rows = (
        Transaction.objects.filter(portfolio=portfolio)
        .order_by("timestamp", "id")
        .values_list("ticker", "tx_type", "amount", "price", "timestamp")
    )

    states: dict[str, PositionState] = {}
    for ticker, tx_type, amount, price, timestamp in rows.iterator(chunk_size=2000):
        state = states.setdefault(ticker, PositionState(ticker=ticker))

        if tx_type == TxType.BUY:
            new_amount = state.amount + amount
            if new_amount > 0:
                state.avg_cost = ((state.amount * state.avg_cost) + (amount * price)) / new_amount
            state.amount = new_amount
        else:  # SELL - realise P&L against the running average cost
            realised = amount * (price - state.avg_cost)
            state.realised_pnl_total += realised
            state.amount = max(state.amount - amount, ZERO)
            if state.amount == 0:
                state.avg_cost = ZERO
            if timestamp >= day_start:
                state.realised_pnl_today += realised

    return states


def realised_pnl_today(portfolio: Portfolio) -> Decimal:
    """Portfolio-level realised P&L for the current UTC day."""
    states = replay_positions(portfolio)
    return sum((s.realised_pnl_today for s in states.values()), ZERO).quantize(CENT)
