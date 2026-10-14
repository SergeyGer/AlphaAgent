"""Score decisions against what actually happened next.

The audit trail answers "why was this decided?". This module answers the question
it never did: "was it right?".

Two deliberate framing choices, because both are easy to get subtly wrong in a
way that flatters the system:

**A decision is scored by what the action was worth, not by what the price did.**
A SELL is right when the price falls afterwards. Scoring every decision against
"did the price go up" would mark every correct exit as a failure and produce a hit
rate that punishes the system for protecting capital.

**A HOLD is not scored as a directional call.** ``directional_hit`` is ``None``
for a HOLD rather than ``False``: holding makes no directional claim, and counting
those rows as misses would drag the measured hit rate toward zero in proportion to
how cautious the system was. HOLDs still carry a ``decision_return_pct`` of zero,
so they are visible in the average without distorting the hit rate.

Benchmarks are recorded, never assumed. A missing benchmark is null, not zero -
an unknown comparison is not a loss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.utils import timezone

from core.models import AgentDecisionLog, DecisionOutcome

logger = logging.getLogger("alphaagent.attribution")

__all__ = [
    "BENCHMARK_TICKER",
    "Scorecard",
    "action_of",
    "build_scorecard",
    "evaluate_pending",
]

ZERO = Decimal("0")
HUNDRED = Decimal("100")
CENT = Decimal("0.0001")

#: What "the market" means here. SPY rather than an index because yfinance prices
#: it like any other ticker and it is the benchmark a reader already has in mind.
BENCHMARK_TICKER = "SPY"

#: Free-text actions are normalised through these, in order. "HALT" and "ERROR"
#: map to OTHER: they are states, not calls, and scoring them would be noise.
_ACTION_PATTERNS = (
    ("BUY", ("buy", "bought", "purchas", "accumulat", "adding", "added")),
    ("SELL", ("sell", "sold", "liquidat", "trimmed", "exit", "closed")),
    ("HOLD", ("hold", "no action", "no trade", "wait", "skip", "monitor")),
)


def action_of(action_taken: str | None) -> str:
    """Normalise a free-text ``action_taken`` into BUY / SELL / HOLD / OTHER."""
    text = (action_taken or "").lower()
    if not text.strip():
        return "OTHER"
    # Halts and errors are checked first: "HALT - daily loss limit breached" would
    # otherwise never match, and "blocked" must not be read as an exit.
    if any(marker in text for marker in ("halt", "block", "rejected", "denied", "error")):
        return "OTHER"
    for name, needles in _ACTION_PATTERNS:
        if any(needle in text for needle in needles):
            return name
    return "OTHER"


def signed_return(action: str, forward_return_pct: Decimal) -> Decimal:
    """Turn a market return into the value of the action taken.

    BUY profits from a rise, SELL profits from a fall, HOLD commits to nothing and
    therefore earns nothing either way.
    """
    if action == "BUY":
        return forward_return_pct
    if action == "SELL":
        return -forward_return_pct
    return ZERO


def directional_hit(action: str, forward_return_pct: Decimal) -> bool | None:
    """Whether a directional call was right. ``None`` when no call was made."""
    if action == "BUY":
        return forward_return_pct > ZERO
    if action == "SELL":
        return forward_return_pct < ZERO
    return None


def _as_date(stamp) -> date | None:
    """Coerce a price-history stamp into a ``date``.

    ``PriceHistory.points`` is typed ``list[tuple[str, Decimal]]`` and carries ISO
    date strings, not datetime objects - the values survive a JSON round trip
    through the cache, and JSON has no date type.
    """
    if isinstance(stamp, date) and not isinstance(stamp, datetime):
        return stamp
    if isinstance(stamp, datetime):
        return stamp.date()
    if isinstance(stamp, str):
        try:
            return date.fromisoformat(stamp[:10])
        except ValueError:
            return None
    return None


def _close_on_or_after(ticker: str, when, *, horizon_days: int) -> Decimal | None:
    """Closing price nearest to ``when``, searching forward over the window.

    Markets are shut at weekends and on holidays, so a horizon date usually has no
    price of its own. ``get_price_history`` returns only actual trading days; the
    first one at or after the target is the honest approximation, and it is what a
    human comparing two dates on a chart would read off.
    """
    from services.fundamentals import get_price_history

    # A year, because the caller may be scoring a decision up to 30 days old and
    # the window has to contain both endpoints.
    history = get_price_history(ticker, period="1y")
    if history is None or not history.points:
        return None

    target = when.date() if hasattr(when, "date") else when
    for stamp, price in history.points:
        stamp_date = _as_date(stamp)
        if stamp_date is not None and stamp_date >= target:
            return price
    return None


def _price_at(ticker: str, when) -> Decimal | None:
    """Closing price on the decision date, or the first trading day after it."""
    return _close_on_or_after(ticker, when, horizon_days=0)


def _start_price(decision) -> Decimal | None:
    """The price the decision would actually have traded at.

    For an executed decision that is the transaction's own price, not the day's
    close. The distinction is not pedantic: the guard prices a trade against the
    live quote, so measuring its outcome from the close scores a trade at a price
    it never got. On a real BTC decision the two differed by $166 - small as a
    percentage, but systematic, and biased in a direction that flatters a BUY
    whenever the close sits below the entry.

    Falls back to the close when there is no transaction, which is the only
    option for a HOLD - it traded nothing, so the close is the honest proxy.
    """
    transaction = getattr(decision, "transaction", None)
    if transaction is not None and transaction.price:
        return transaction.price
    return _price_at(decision.ticker, decision.created_at)


def _return_pct(start: Decimal, end: Decimal) -> Decimal:
    if start == ZERO:
        return ZERO
    return ((end - start) / start * HUNDRED).quantize(CENT)


def evaluate_pending(*, now=None, limit: int = 500) -> dict[str, int]:
    """Score every decision whose horizons have matured and is not yet scored.

    Idempotent: a (decision, horizon) pair already scored is skipped, so running
    this more often than necessary costs nothing but time. Safe to run on every
    Beat tick.

    Returns a small dict of counts rather than a list of rows - the caller wants
    to log it, not to inspect the work.
    """
    reference = now or timezone.now()
    horizons = DecisionOutcome.HORIZONS
    oldest_needed = reference - timedelta(days=min(horizons))

    # Only decisions that could produce at least one outcome, and that actually
    # name an instrument. Rows from before the ticker column existed are skipped:
    # there is nothing to price.
    candidates = (
        AgentDecisionLog.objects.filter(created_at__lte=reference, ticker__gt="")
        .filter(created_at__gte=oldest_needed - timedelta(days=max(horizons) * 2))
        .select_related("transaction")
        .exclude(outcomes__isnull=False)
        .order_by("-created_at")[:limit]
    )

    scored = skipped = failed = 0

    for decision in candidates:
        age_days = (reference - decision.created_at).days
        action = action_of(decision.action_taken)
        start_price = _start_price(decision)
        if start_price is None:
            # No price for the decision day means no denominator, so no return.
            # Better to score nothing than to score against a guess.
            failed += 1
            continue

        made_progress = False
        for horizon in horizons:
            if age_days < horizon:
                continue

            target = decision.created_at + timedelta(days=horizon)
            if DecisionOutcome.objects.filter(decision=decision, horizon_days=horizon).exists():
                skipped += 1
                continue

            end_price = _close_on_or_after(decision.ticker, target, horizon_days=horizon)
            if end_price is None:
                failed += 1
                continue

            forward = _return_pct(start_price, end_price)
            benchmark = None
            benchmark_start = _price_at(BENCHMARK_TICKER, decision.created_at)
            benchmark_end = _close_on_or_after(BENCHMARK_TICKER, target, horizon_days=horizon)
            if benchmark_start and benchmark_end:
                benchmark = _return_pct(benchmark_start, benchmark_end)

            DecisionOutcome.objects.update_or_create(
                decision=decision,
                horizon_days=horizon,
                defaults={
                    "ticker": decision.ticker,
                    "action": action,
                    "price_at_decision": start_price,
                    "price_at_horizon": end_price,
                    "forward_return_pct": forward,
                    "decision_return_pct": signed_return(action, forward),
                    "benchmark_return_pct": benchmark,
                    "directional_hit": directional_hit(action, forward),
                },
            )
            scored += 1
            made_progress = True

        if not made_progress:
            skipped += 1

    logger.info("Decision attribution: scored=%s skipped=%s unpriced=%s", scored, skipped, failed)
    return {"scored": scored, "skipped": skipped, "unpriced": failed}


@dataclass(frozen=True)
class Scorecard:
    """Aggregate decision quality, as shown on the dashboard."""

    horizon_days: int
    evaluated: int = 0
    directional_calls: int = 0
    hits: int = 0
    avg_decision_return_pct: Decimal = ZERO
    avg_benchmark_return_pct: Decimal | None = None
    beat_benchmark: int = 0
    benchmarked: int = 0

    @property
    def hit_rate_pct(self) -> float | None:
        """Share of directional calls that were right.

        ``None`` rather than 0 when nothing has been scored: an empty scorecard is
        not a scorecard of zero, and showing "0%" would read as total failure.
        """
        if not self.directional_calls:
            return None
        return round(self.hits / self.directional_calls * 100, 1)

    @property
    def avg_alpha_pct(self) -> Decimal | None:
        """Average decision return minus average benchmark return."""
        if self.avg_benchmark_return_pct is None:
            return None
        return (self.avg_decision_return_pct - self.avg_benchmark_return_pct).quantize(CENT)

    def as_dict(self) -> dict:
        return {
            "horizon_days": self.horizon_days,
            "evaluated": self.evaluated,
            "directional_calls": self.directional_calls,
            "hits": self.hits,
            "hit_rate_pct": self.hit_rate_pct,
            "avg_decision_return_pct": str(self.avg_decision_return_pct),
            "avg_benchmark_return_pct": (
                str(self.avg_benchmark_return_pct)
                if self.avg_benchmark_return_pct is not None
                else None
            ),
            "avg_alpha_pct": str(self.avg_alpha_pct) if self.avg_alpha_pct is not None else None,
            "beat_benchmark": self.beat_benchmark,
            "benchmarked": self.benchmarked,
        }


def build_scorecard(horizon_days: int, *, portfolio=None) -> Scorecard:
    """Aggregate the stored outcomes for one horizon.

    Computed on read rather than stored: the rows are small and indexed, a
    materialised scorecard would be one more thing to keep in step, and the answer
    has to reflect a re-evaluation immediately.
    """
    from django.db.models import Avg, Count, Q

    rows = DecisionOutcome.objects.filter(horizon_days=horizon_days)
    if portfolio is not None:
        rows = rows.filter(decision__portfolio=portfolio)

    aggregate = rows.aggregate(
        evaluated=Count("id"),
        calls=Count("id", filter=Q(directional_hit__isnull=False)),
        hits=Count("id", filter=Q(directional_hit=True)),
        avg_decision=Avg("decision_return_pct"),
        avg_benchmark=Avg("benchmark_return_pct"),
        benchmarked=Count("id", filter=Q(benchmark_return_pct__isnull=False)),
    )

    return Scorecard(
        horizon_days=horizon_days,
        evaluated=aggregate["evaluated"] or 0,
        directional_calls=aggregate["calls"] or 0,
        hits=aggregate["hits"] or 0,
        avg_decision_return_pct=Decimal(str(aggregate["avg_decision"] or 0)).quantize(CENT),
        avg_benchmark_return_pct=(
            Decimal(str(aggregate["avg_benchmark"])).quantize(CENT)
            if aggregate["avg_benchmark"] is not None
            else None
        ),
        benchmarked=aggregate["benchmarked"] or 0,
    )
