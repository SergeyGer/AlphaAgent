"""Daily AI spend accounting and the ceiling that halts dispatching.

The execution guard answers "may this trade happen?". This answers a different
question the guard deliberately does not: "may we afford to think about it?".

Nothing capped language-model spend before this module. Celery Beat sweeps every
portfolio and ticker on a schedule, so an unattended deployment could run up an
API bill indefinitely - the guard would keep refusing trades on risk grounds while
the meter kept running. The trading limits and the spend limit are separate
controls because they protect different things: one protects the portfolio, the
other protects the operator.

Everything here is derived from ``AgentDecisionLog``. ``api_cost_usd`` is the
**billed** charge and may be NULL: a run whose provider reported no charge has an
unknown cost, not a zero one. Such rows are counted in ``unknown_cost_runs`` and
their price-table estimate (``estimated_cost_usd``) is published separately as
``estimated_spend_usd``. The ceiling is therefore a ceiling on *known* billed
spend; it is not a guarantee that the provider account never went higher, and
``basis`` says so on every payload.

The consequence is deliberate: a deployment whose provider reports tokens but no
dollars will show ``spent_usd = 0`` with ``unknown_cost_runs > 0``, which is
visibly different from "spent nothing".
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
    """Total **billed** AI spend booked since midnight UTC.

    NULL ``api_cost_usd`` rows contribute nothing to this figure; they are not
    zeros and are reported by :func:`cost_state` as unknown.
    """
    from core.models import AgentDecisionLog
    from services.ledger import utc_day_start

    window_start = since or utc_day_start()
    total = AgentDecisionLog.objects.filter(created_at__gte=window_start).aggregate(
        total=models_sum("api_cost_usd")
    )["total"]
    return (total or ZERO).quantize(CENT)


def cost_state(*, since=None) -> dict:
    """Known billed spend, estimated spend and the unknown count for today."""
    from django.db.models import Count, Q

    from core.models import AgentDecisionLog
    from services.ledger import utc_day_start

    window_start = since or utc_day_start()
    aggregate = AgentDecisionLog.objects.filter(created_at__gte=window_start).aggregate(
        billed=models_sum("api_cost_usd"),
        estimated=models_sum("estimated_cost_usd"),
        unknown=Count("id", filter=Q(api_cost_usd__isnull=True)),
        total=Count("id"),
    )
    billed = (aggregate["billed"] or ZERO).quantize(CENT)
    estimated = (aggregate["estimated"] or ZERO).quantize(CENT)
    unknown = int(aggregate["unknown"] or 0)
    return {
        "billed_spend_usd": str(billed),
        "estimated_spend_usd": str(estimated),
        "unknown_cost_runs": unknown,
        "runs_booked": int(aggregate["total"] or 0),
        "basis": (
            "billed_only"
            if not unknown
            else "billed_only; some runs have unknown cost and are excluded"
        ),
    }


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
    #: Runs booked today whose provider reported no charge. They are *not* zeros
    #: in ``spent_usd`` and they are why the ceiling is not a strict billed cap.
    unknown_cost_runs: int = 0
    estimated_spend_usd: Decimal = ZERO

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
            "unknown_cost_runs": self.unknown_cost_runs,
            "estimated_spend_usd": str(self.estimated_spend_usd),
            "basis": (
                "billed_only"
                if not self.unknown_cost_runs
                else "billed_only; unknown-cost runs are excluded, not counted as zero"
            ),
        }


def budget_state() -> BudgetState:
    """Current spend, ceiling and enforcement flag in one call.

    ``spent_usd`` is billed spend only. ``unknown_cost_runs`` and
    ``estimated_spend_usd`` publish what could not be billed-measured, so a
    reader cannot mistake "no charge reported" for "no charge incurred".
    """
    costs = cost_state()
    return BudgetState(
        spent_usd=Decimal(costs["billed_spend_usd"]),
        limit_usd=spend_limit(),
        enforced=bool(getattr(settings, "AI_SPEND_LIMIT_ENFORCED", True)),
        unknown_cost_runs=costs["unknown_cost_runs"],
        estimated_spend_usd=Decimal(costs["estimated_spend_usd"]),
    )
