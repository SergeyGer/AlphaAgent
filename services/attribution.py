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
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from core.models import AgentDecisionLog, DecisionOutcome
from services.decision_outcomes import OUTCOME_METHOD_VERSION, sync_outcome

logger = logging.getLogger("alphaagent.attribution")

__all__ = [
    "BENCHMARK_TICKER",
    "OUTCOME_METHOD_VERSION",
    "PriceObservation",
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


@dataclass(frozen=True)
class PriceObservation:
    """One price with the session it was observed on and where it came from.

    A return without this is a number two prices happened to produce. Recording
    the observation date is what lets the report check that the benchmark window
    and the instrument window cover the same sessions instead of assuming it.
    ``observed_on`` is a **date**, not an instant: the price provider publishes
    daily closes, so the intraday observation instant is not recorded. The report
    says so rather than presenting session-date precision as an exact interval.
    """

    ticker: str
    price: Decimal
    observed_on: date | None = None
    source: str = "price_history"

    def observed_at(self) -> datetime | None:
        """Midnight UTC on the observation date, or ``None`` when unknown."""
        if self.observed_on is None:
            return None
        return datetime.combine(self.observed_on, time.min, tzinfo=UTC)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price_usd": str(self.price),
            "observed_on": self.observed_on.isoformat() if self.observed_on else None,
            "source": self.source,
        }


def _observation(value, *, ticker: str, source: str = "price_history"):
    """Coerce a price or observation into ``PriceObservation``.

    Callers (and tests) that supply a bare ``Decimal`` still work; the provenance
    is then unknown, which is exactly what such a row should record.
    """
    if value is None:
        return None
    if isinstance(value, PriceObservation):
        return value
    try:
        return PriceObservation(ticker=ticker, price=Decimal(str(value)), source=source)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _close_on_or_after(ticker: str, when, *, horizon_days: int) -> PriceObservation | None:
    """Closing price nearest to ``when``, searching forward over the window.

    Markets are shut at weekends and on holidays, so a horizon date usually has no
    price of its own. ``get_price_history`` returns only actual trading days; the
    first one at or after the target is the honest approximation, and it is what a
    human comparing two dates on a chart would read off. The observation date and
    source travel with the price so the interval can be checked rather than
    assumed.
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
            return PriceObservation(
                ticker=ticker, price=price, observed_on=stamp_date, source="price_history"
            )
    return None


def _price_at(ticker: str, when) -> PriceObservation | None:
    """Closing price on the decision date, or the first trading day after it."""
    return _close_on_or_after(ticker, when, horizon_days=0)


def _price_of(value) -> Decimal | None:
    observation = _observation(value, ticker="?", source="price_history")
    return None if observation is None else observation.price


def _start_price(decision) -> PriceObservation | None:
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
        return PriceObservation(
            ticker=decision.ticker,
            price=transaction.price,
            observed_on=transaction.created_at.date() if transaction.created_at else None,
            source="transaction",
        )
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
        start_observation = _observation(
            _start_price(decision), ticker=decision.ticker, source="price_history"
        )
        if start_observation is None:
            # No price for the decision day means no denominator, so no return.
            # Better to score nothing than to score against a guess.
            failed += 1
            continue
        start_price = start_observation.price

        made_progress = False
        for horizon in horizons:
            if age_days < horizon:
                continue

            target = decision.created_at + timedelta(days=horizon)
            if DecisionOutcome.objects.filter(decision=decision, horizon_days=horizon).exists():
                skipped += 1
                continue

            end_observation = _observation(
                _close_on_or_after(decision.ticker, target, horizon_days=horizon),
                ticker=decision.ticker,
            )
            if end_observation is None:
                failed += 1
                continue
            end_price = end_observation.price

            forward = _return_pct(start_price, end_price)
            benchmark = None
            benchmark_start = _observation(
                _price_at(BENCHMARK_TICKER, decision.created_at),
                ticker=BENCHMARK_TICKER,
            )
            benchmark_end = _observation(
                _close_on_or_after(BENCHMARK_TICKER, target, horizon_days=horizon),
                ticker=BENCHMARK_TICKER,
            )
            if benchmark_start is not None and benchmark_end is not None:
                benchmark = _return_pct(benchmark_start.price, benchmark_end.price)

            outcome, _created = DecisionOutcome.objects.update_or_create(
                decision=decision,
                horizon_days=horizon,
                defaults={
                    "ticker": decision.ticker,
                    "action": action,
                    "price_at_decision": start_price,
                    "price_at_horizon": end_price,
                    "price_observed_at_decision": start_observation.observed_at(),
                    "price_observed_at": end_observation.observed_at(),
                    "price_source": end_observation.source,
                    "forward_return_pct": forward,
                    "decision_return_pct": signed_return(action, forward),
                    "benchmark_return_pct": benchmark,
                    # Benchmark provenance: the prices and the sessions they were
                    # observed on. Without these a stored return cannot be
                    # interval-checked, which is why the report used to say
                    # ``benchmark_interval_verified: false`` for every row.
                    "benchmark_price_at_decision": (
                        None if benchmark_start is None else benchmark_start.price
                    ),
                    "benchmark_price_at_horizon": (
                        None if benchmark_end is None else benchmark_end.price
                    ),
                    "benchmark_observed_at_decision": (
                        None if benchmark_start is None else benchmark_start.observed_at()
                    ),
                    "benchmark_price_observed_at": (
                        None if benchmark_end is None else benchmark_end.observed_at()
                    ),
                    "benchmark_price_source": (
                        benchmark_end.source
                        if benchmark_end is not None
                        else (benchmark_start.source if benchmark_start is not None else "")
                    ),
                    "directional_hit": directional_hit(action, forward),
                },
            )
            # The Phase 1 detail contract reads OutcomeRecord. Mirror the scored
            # row through the versioned bridge so the detail screen and the
            # scorecard cannot disagree about what this decision returned.
            sync_outcome(decision, outcome, method_version=OUTCOME_METHOD_VERSION)
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
