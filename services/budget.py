"""Daily AI spend accounting and the ceiling that halts dispatching.

The execution guard answers "may this trade happen?". This answers a different
question the guard deliberately does not: "may we afford to think about it?".

Nothing capped language-model spend before this module. Celery Beat sweeps every
portfolio and ticker on a schedule, so an unattended deployment could run up an
API bill indefinitely - the guard would keep refusing trades on risk grounds while
the meter kept running. The trading limits and the spend limit are separate
controls because they protect different things: one protects the portfolio, the
other protects the operator.

Everything here is derived from ``AgentDecisionLog.api_cost_usd``, which is itself
computed from the real per-bucket token split. There is no separate counter to
drift out of sync with the audit trail - recomputing today's spend is a single
aggregate over rows that already exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

__all__ = ["BudgetState", "budget_state", "spend_limit", "spend_today"]

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def spend_limit() -> Decimal:
    """The configured daily ceiling, in USD.

    Always non-zero. ``settings.AI_DAILY_SPEND_LIMIT_USD`` defaults to 5.00, and
    a value that fails to parse falls back to that default rather than to zero -
    a ceiling of zero would be indistinguishable from "disabled", which is the
    state this module exists to prevent.
    """
    limit = getattr(settings, "AI_DAILY_SPEND_LIMIT_USD", Decimal("5.00"))
    return max(Decimal(str(limit)), ZERO)


def spend_today(*, since=None) -> Decimal:
    """Total AI spend booked since midnight UTC."""
    from core.models import AgentDecisionLog
    from services.ledger import utc_day_start

    window_start = since or utc_day_start()
    total = AgentDecisionLog.objects.filter(created_at__gte=window_start).aggregate(
        total=models_sum("api_cost_usd")
    )["total"]
    return (total or ZERO).quantize(CENT)


def models_sum(field: str):
    """``Sum(field)`` with a zero default, so an empty day totals to 0 not None."""
    from django.db.models import Sum

    return Sum(field, default=ZERO)


@dataclass(frozen=True)
class BudgetState:
    """Today's spend against the ceiling, as shown on the dashboard."""

    spent_usd: Decimal = ZERO
    limit_usd: Decimal = ZERO
    enforced: bool = True

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.limit_usd - self.spent_usd, ZERO).quantize(CENT)

    @property
    def exhausted(self) -> bool:
        """True when new runs must not be dispatched.

        Only meaningful while enforcement is on: with enforcement disabled the
        figure is still tracked and displayed, it simply does not stop anything.
        """
        return self.enforced and self.spent_usd >= self.limit_usd

    @property
    def used_pct(self) -> float:
        """Percent of the ceiling consumed, clamped to 0-100 for display."""
        if self.limit_usd <= ZERO:
            return 100.0
        return float(
            min(self.spent_usd / self.limit_usd * 100, Decimal("100")).quantize(Decimal("0.1"))
        )

    def as_dict(self) -> dict[str, str | float | bool]:
        return {
            "spent_usd": str(self.spent_usd),
            "limit_usd": str(self.limit_usd),
            "remaining_usd": str(self.remaining_usd),
            "used_pct": self.used_pct,
            "enforced": self.enforced,
            "exhausted": self.exhausted,
        }


def budget_state() -> BudgetState:
    """Current spend, ceiling and enforcement flag in one call."""
    return BudgetState(
        spent_usd=spend_today(),
        limit_usd=spend_limit(),
        enforced=bool(getattr(settings, "AI_SPEND_LIMIT_ENFORCED", True)),
    )
