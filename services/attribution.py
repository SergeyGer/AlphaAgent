"""Score decisions against what actually happened next.

The audit trail answers "why was this decided?". This module answers the question
it never did: "was it right?".

Four framing choices, because each is easy to get subtly wrong in a way that
flatters the system:

**A decision is scored by what the action was worth, not by what the price did.**
A SELL is right when the price falls afterwards. Scoring every decision against
"did the price go up" would mark every correct exit as a failure and produce a hit
rate that punishes the system for protecting capital.

**A HOLD is not scored as a directional call.** ``directional_hit`` is ``None``
for a HOLD rather than ``False``: holding makes no directional claim, and counting
those rows as misses would drag the measured hit rate toward zero in proportion to
how cautious the system was. HOLDs still carry a ``decision_return_pct`` of zero,
so they are visible in the average without distorting the hit rate.

**An executed decision is measured from the fill, not from the day's close.** The
guard prices an order against a live quote and the delay between proposal and
approval can be days. Measuring from the proposal close scores a trade at a price
it never got, and the error is systematic rather than random. The linked
``Transaction`` stores the price that was actually paid and the instant it was
paid; both are used as the reference. A decision with no fill (HOLD, rejection,
expiry) keeps a proposal-close diagnostic and records ``fill=None`` - no fabricated
execution.

**A missing value stays null.** No price means no return and an explicit,
retryable reason. It is never zero and never backfilled from a neighbouring
session.

Horizons are calendar days from the reference instant, with the documented
session policy: the first mark at or after the target session, so a Saturday
target uses Monday's close and a horizon with no later session is unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from core.decision_json import content_hash, utc_iso
from core.decision_models import (
    AgentRole,
    DecisionRun,
    OutcomeEvaluation,
    OutcomeEvaluationState,
    OutcomeMaturity,
    OutcomeRecord,
    RunEventType,
    RunStage,
)
from core.models import (
    AgentDecisionLog,
    DecisionOutcome,
    TradeRecommendation,
    Transaction,
)
from services.decision_outcomes import (
    OUTCOME_METHOD_VERSION,
    sync_outcome_versioned,
)
from services.decision_runs import append_event

logger = logging.getLogger("alphaagent.attribution")

__all__ = [
    "BENCHMARK_TICKER",
    "HORIZON_POLICY",
    "MAX_PRICE_ATTEMPTS",
    "OUTCOME_METHOD_VERSION",
    "MeasurementReference",
    "PriceObservation",
    "Scorecard",
    "action_of",
    "build_scorecard",
    "evaluate_decision_horizons",
    "evaluate_pending",
    "horizon_timeline",
    "resolve_linked_fill",
    "resolve_measurement_reference",
]

ZERO = Decimal("0")
HUNDRED = Decimal("100")
CENT = Decimal("0.0001")

#: What "the market" means here. SPY rather than an index because yfinance prices
#: it like any other ticker and it is the benchmark a reader already has in mind.
BENCHMARK_TICKER = "SPY"

#: The session rule the outcomes are measured under. Recorded on every row so a
#: later change to the policy cannot silently reinterpret an earlier score.
HORIZON_POLICY = "first_mark_at_or_after_target_session.v1"

#: How many times an unavailable price is retried before the row is declared
#: unavailable. Bounded on purpose: every Beat tick retrying an instrument that
#: will never price is load, not diligence.
MAX_PRICE_ATTEMPTS = 3

#: Delay before the next price attempt, before the attempt count is applied.
RETRY_BACKOFF = timedelta(minutes=15)

#: Basis labels. A scorecard groups by these, and a detail response shows the
#: label so a proposal-close diagnostic cannot be read as realised performance.
BASIS_EXECUTION_FILL = "execution_fill"
BASIS_PROPOSAL_CLOSE = "proposal_close"
BASIS_UNAVAILABLE = "unavailable"

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


def _as_datetime(stamp) -> datetime | None:
    """Coerce a stamp into an aware UTC datetime, or ``None`` when unusable."""
    if isinstance(stamp, datetime):
        return stamp if timezone.is_aware(stamp) else stamp.replace(tzinfo=UTC)
    if isinstance(stamp, date):
        return datetime.combine(stamp, time.min, tzinfo=UTC)
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if timezone.is_aware(parsed) else parsed.replace(tzinfo=UTC)
    return None


@dataclass(frozen=True)
class PriceObservation:
    """One price with the session it was observed on and where it came from.

    A return without this is a number two prices happened to produce. Recording
    the observation instant and source is what lets a reader tell a fill price
    observed at 14:31 from a daily close, and what lets the report check that the
    benchmark window and the instrument window cover the same sessions instead of
    assuming it. ``observed_on`` is a **date** for a daily close; ``observed_instant``
    is set only when the producer recorded an exact instant (a fill's quote).
    """

    ticker: str
    price: Decimal
    observed_on: date | None = None
    source: str = "price_history"
    observed_instant: datetime | None = None

    def observed_at(self) -> datetime | None:
        """The recorded observation instant, or midnight UTC on the session date."""
        if self.observed_instant is not None:
            return self.observed_instant
        if self.observed_on is None:
            return None
        return datetime.combine(self.observed_on, time.min, tzinfo=UTC)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price_usd": str(self.price),
            "observed_on": self.observed_on.isoformat() if self.observed_on else None,
            "observed_at": utc_iso(self.observed_at()),
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
                ticker=ticker,
                price=price,
                observed_on=stamp_date,
                source="price_history",
            )
    return None


def _price_at(ticker: str, when) -> PriceObservation | None:
    """Closing price on the decision date, or the first trading day after it."""
    return _close_on_or_after(ticker, when, horizon_days=0)


def _price_of(value) -> Decimal | None:
    observation = _observation(value, ticker="?", source="price_history")
    return None if observation is None else observation.price


def resolve_linked_fill(decision: AgentDecisionLog) -> Transaction | None:
    """The fill this decision actually produced, if one exists.

    Three links are possible, in order of directness: the log's own
    ``transaction``, the advisory recommendation made from the log, and the
    execution attempt recorded on the run linked to the log. The recommendation
    link is what makes a **delayed approval** measurable: the proposal is logged
    when the agent proposes it, the fill is written days later, and only the
    recommendation carries both.
    """
    direct = getattr(decision, "transaction", None)
    if direct is not None:
        return direct

    recommendation = (
        TradeRecommendation.objects.filter(decision_log_id=decision.id, transaction__isnull=False)
        .select_related("transaction")
        .order_by("-decided_at", "-id")
        .first()
    )
    if recommendation is not None and recommendation.transaction is not None:
        return recommendation.transaction

    run = DecisionRun.objects.filter(legacy_log_id=decision.id).first()
    if run is None:
        return None
    attempt = (
        run.execution_attempts.filter(transaction__isnull=False)
        .select_related("transaction")
        .order_by("-created_at")
        .first()
    )
    return None if attempt is None else attempt.transaction


def _start_price(decision: AgentDecisionLog) -> PriceObservation | None:
    """The price the decision would actually have traded at.

    For an executed decision that is the transaction's own price and the instant
    it was observed - not the day's close. The distinction is not pedantic: the
    guard prices a trade against the live quote, so measuring its outcome from the
    close scores a trade at a price it never got. On a real BTC decision the two
    differed by $166, and the error is systematic: it flatters a BUY whenever the
    close sits below the entry.

    Falls back to the close when there is no fill, which is the only option for a
    HOLD or a rejected proposal - it traded nothing, so the close is the honest
    proxy and the returned observation says so.
    """
    fill = resolve_linked_fill(decision)
    if fill is not None and fill.price:
        if (fill.ticker or "").strip().upper() != (decision.ticker or "").strip().upper():
            return _price_at(decision.ticker, decision.created_at)
        observed = fill.price_observed_at or fill.timestamp
        return PriceObservation(
            ticker=decision.ticker,
            price=fill.price,
            observed_on=observed.date() if observed else None,
            source=fill.price_source or BASIS_EXECUTION_FILL,
            observed_instant=observed,
        )
    return _price_at(decision.ticker, decision.created_at)


@dataclass(frozen=True)
class MeasurementReference:
    """What a horizon is measured *from*, and whether a fill is the reason.

    ``basis`` is ``execution_fill`` for an executed trade, ``proposal_close`` for
    a decision that traded nothing, and ``unavailable`` when neither a fill nor a
    starting price exists. ``fill`` is null in the last two cases by design: a
    HOLD has no fill and inventing one would present a proposal-based diagnostic
    as realised execution.
    """

    basis: str
    reference_at: datetime
    observation: PriceObservation | None
    fill: Transaction | None = None
    reason: str = ""

    @property
    def executed(self) -> bool:
        return self.fill is not None and self.basis == BASIS_EXECUTION_FILL

    def as_dict(self) -> dict:
        return {
            "basis": self.basis,
            "reference_at": utc_iso(self.reference_at),
            "fill_id": self.fill.id if self.fill is not None else None,
            "fill_price": str(self.fill.price) if self.fill is not None else None,
            "fill_timestamp": utc_iso(self.fill.timestamp) if self.fill is not None else None,
            "observation": self.observation.as_dict() if self.observation is not None else None,
            "reason": self.reason or None,
        }


def resolve_measurement_reference(decision: AgentDecisionLog) -> MeasurementReference:
    """Resolve the reference price and instant for one decision.

    The fill, when one exists, is authoritative for both the price and the
    reference instant. A proposal that was approved days later is measured from
    the fill, so the market movement between proposal and approval cannot be
    counted as the trade's return.
    """
    fill = resolve_linked_fill(decision)
    if (
        fill is not None
        and (fill.ticker or "").strip().upper() != (decision.ticker or "").strip().upper()
    ):
        # A fill for another instrument is an attribution error, not a price.
        # Ignoring it keeps the row a proposal diagnostic and leaves the bad link
        # for the backfill report rather than silently measuring the wrong asset.
        logger.warning(
            "Ignoring mismatched fill decision=%s transaction=%s",
            decision.id,
            fill.id,
        )
        fill = None
    observation = _observation(
        _start_price(decision), ticker=decision.ticker, source="price_history"
    )
    if fill is not None and observation is not None:
        return MeasurementReference(
            basis=BASIS_EXECUTION_FILL,
            reference_at=fill.timestamp,
            observation=observation,
            fill=fill,
        )
    if observation is not None:
        return MeasurementReference(
            basis=BASIS_PROPOSAL_CLOSE,
            reference_at=decision.created_at,
            observation=observation,
            fill=None,
            reason="No fill is linked to this decision; the proposal close is the honest proxy.",
        )
    return MeasurementReference(
        basis=BASIS_UNAVAILABLE,
        reference_at=decision.created_at,
        observation=None,
        fill=None,
        reason="No fill is linked and no price exists at or after the proposal; no return can be measured.",
    )


def _next_session_date(day: date) -> date:
    """First weekday at or after ``day``.

    The price provider returns trading days only. Holidays are not modelled
    here: the next *available* mark is selected by the price lookup, so a holiday
    simply moves the observation one session further and the row records that
    observation date.
    """
    shifted = day
    while shifted.weekday() >= 5:
        shifted += timedelta(days=1)
    return shifted


def horizon_timeline(reference_at: datetime, horizon_days: int) -> tuple[datetime, datetime]:
    """Return ``(target, mature_at)`` for one horizon.

    ``target`` is the calendar instant the horizon names. ``mature_at`` is when
    the documented session policy says a measurement can first exist: the first
    session at or after the target. A Saturday target therefore becomes eligible
    on Monday, and a horizon is never scored against Friday's close merely
    because Friday was the last mark before the weekend.
    """
    target = reference_at + timedelta(days=horizon_days)
    session = _next_session_date(target.date())
    if session == target.date():
        return target, target
    session_start = datetime.combine(session, time.min, tzinfo=UTC)
    return target, max(target, session_start)


def _retry_backoff(attempts: int) -> timedelta:
    return RETRY_BACKOFF * max(attempts, 1)


def _decimal_text(value) -> str | None:
    return None if value is None else str(value)


def _return_pct(start: Decimal, end: Decimal) -> Decimal:
    if start == ZERO:
        return ZERO
    return ((end - start) / start * HUNDRED).quantize(CENT)


# ---------------------------------------------------------------------------
# The evaluation loop
# ---------------------------------------------------------------------------
def _run_for_decision(decision: AgentDecisionLog) -> DecisionRun | None:
    return (
        DecisionRun.objects.filter(legacy_log_id=decision.id)
        .select_related("portfolio", "decision")
        .first()
    )


def _update_projection(
    decision: AgentDecisionLog,
    *,
    horizon_days: int,
    maturity: str,
    reason: str,
    reference: MeasurementReference | None,
    now: datetime,
) -> OutcomeRecord | None:
    """Keep the detail projection in step with the evaluation state.

    A pending or unavailable horizon is visible in the decision detail with its
    reason. It is not given a score, and an evaluated projection is never
    downgraded by a later failed retry.
    """
    run = _run_for_decision(decision)
    if run is None:
        return None
    try:
        decision_record = run.decision
    except Exception:
        return None
    record, _created = OutcomeRecord.objects.get_or_create(
        decision=decision_record,
        horizon_days=horizon_days,
        method_version=OUTCOME_METHOD_VERSION,
    )
    if record.maturity == OutcomeMaturity.EVALUATED and record.is_scored:
        return record

    diagnostics = dict(record.signal_diagnostics or {})
    diagnostics["evaluation"] = {
        "state": maturity,
        "reason": reason or None,
        "method_version": OUTCOME_METHOD_VERSION,
        "horizon_policy": HORIZON_POLICY,
        "evaluated_at": utc_iso(now),
    }
    if reference is not None:
        diagnostics["measurement_basis"] = {
            "basis": reference.basis,
            "reference_at": utc_iso(reference.reference_at),
            "fill_id": reference.fill.id if reference.fill is not None else None,
            "fill_price": (str(reference.fill.price) if reference.fill is not None else None),
            "fill_timestamp": (
                utc_iso(reference.fill.timestamp) if reference.fill is not None else None
            ),
            "portfolio_performance": False,
        }
    record.maturity = maturity
    record.unavailable_reason = reason
    record.measurement_basis = reference.basis if reference is not None else ""
    record.reference_at = reference.reference_at if reference is not None else None
    record.signal_diagnostics = diagnostics
    record.save(
        update_fields=[
            "maturity",
            "unavailable_reason",
            "measurement_basis",
            "reference_at",
            "signal_diagnostics",
        ]
    )
    return record


def _benchmark_observations(reference_at: datetime, target: datetime):
    """Instrument-matched benchmark marks, or nulls when either is missing."""
    start = _observation(_price_at(BENCHMARK_TICKER, reference_at), ticker=BENCHMARK_TICKER)
    end = _observation(
        _close_on_or_after(BENCHMARK_TICKER, target, horizon_days=0),
        ticker=BENCHMARK_TICKER,
    )
    return start, end


def _write_outcome(
    decision: AgentDecisionLog,
    *,
    horizon_days: int,
    action: str,
    reference: MeasurementReference,
    start: PriceObservation,
    end: PriceObservation,
    benchmark: Decimal | None,
    benchmark_start,
    benchmark_end,
    now: datetime,
) -> tuple[DecisionOutcome, str]:
    """Persist the scored row with its full provenance, preserving corrections."""
    target, _mature_at = horizon_timeline(reference.reference_at, horizon_days)
    forward = _return_pct(start.price, end.price)
    values = {
        "price_at_decision": str(start.price),
        "price_at_horizon": str(end.price),
        "price_observed_at_decision": utc_iso(start.observed_at()),
        "price_observed_at": utc_iso(end.observed_at()),
        "price_source": end.source,
        "forward_return_pct": str(forward),
        "decision_return_pct": str(signed_return(action, forward)),
        "benchmark_return_pct": None if benchmark is None else str(benchmark),
        "benchmark_price_at_decision": (
            None if benchmark_start is None else str(benchmark_start.price)
        ),
        "benchmark_price_at_horizon": (None if benchmark_end is None else str(benchmark_end.price)),
        "benchmark_observed_at_decision": (
            None if benchmark_start is None else utc_iso(benchmark_start.observed_at())
        ),
        "benchmark_price_observed_at": (
            None if benchmark_end is None else utc_iso(benchmark_end.observed_at())
        ),
        "benchmark_price_source": (
            benchmark_end.source
            if benchmark_end is not None
            else (benchmark_start.source if benchmark_start is not None else "")
        ),
        "measurement_basis": reference.basis,
        "reference_at": utc_iso(reference.reference_at),
        "method_version": OUTCOME_METHOD_VERSION,
    }
    digest = content_hash(values)

    existing = DecisionOutcome.objects.filter(decision=decision, horizon_days=horizon_days).first()
    revision = 1
    prior_revisions: list[dict] = []
    provenance = {
        "basis": reference.basis,
        "reference_at": utc_iso(reference.reference_at),
        "fill_id": reference.fill.id if reference.fill is not None else None,
        "fill_price": str(reference.fill.price) if reference.fill is not None else None,
        "fill_timestamp": (
            utc_iso(reference.fill.timestamp) if reference.fill is not None else None
        ),
        "start_observation": start.as_dict(),
        "end_observation": end.as_dict(),
        "benchmark_start_observation": (
            benchmark_start.as_dict() if benchmark_start is not None else None
        ),
        "benchmark_end_observation": (
            benchmark_end.as_dict() if benchmark_end is not None else None
        ),
        "horizon_policy": HORIZON_POLICY,
        "target_at": utc_iso(target),
        "method_version": OUTCOME_METHOD_VERSION,
    }
    if existing is not None:
        prior_revisions = list((existing.provenance or {}).get("prior_revisions") or [])
        if not existing.content_hash:
            # A legacy row has no hash. It is corrected when what it recorded
            # differs from what the fill basis measures, not merely because a
            # hash is now being written for the first time.
            changed = (
                existing.measurement_basis != reference.basis
                or existing.price_at_decision != start.price
                or existing.reference_at != reference.reference_at
            )
        else:
            changed = existing.content_hash != digest
        if changed:
            prior_revisions.append(
                {
                    "revision": existing.revision,
                    "content_hash": existing.content_hash,
                    "measurement_basis": existing.measurement_basis,
                    "reference_at": utc_iso(existing.reference_at),
                    "fill_id": existing.fill_id,
                    "price_at_decision": _decimal_text(existing.price_at_decision),
                    "price_at_horizon": _decimal_text(existing.price_at_horizon),
                    "forward_return_pct": _decimal_text(existing.forward_return_pct),
                    "decision_return_pct": _decimal_text(existing.decision_return_pct),
                    "price_observed_at_decision": utc_iso(existing.price_observed_at_decision),
                    "price_observed_at": utc_iso(existing.price_observed_at),
                    "recorded_at": utc_iso(existing.evaluated_at),
                }
            )
            revision = max(existing.revision + 1, 2)
        else:
            revision = existing.revision or 1
    provenance["prior_revisions"] = prior_revisions

    outcome, _created = DecisionOutcome.objects.update_or_create(
        decision=decision,
        horizon_days=horizon_days,
        defaults={
            "ticker": decision.ticker,
            "action": action,
            "price_at_decision": start.price,
            "price_at_horizon": end.price,
            "price_observed_at_decision": start.observed_at(),
            "price_observed_at": end.observed_at(),
            "price_source": end.source,
            "forward_return_pct": forward,
            "decision_return_pct": signed_return(action, forward),
            "benchmark_return_pct": benchmark,
            "benchmark_price_at_decision": (
                None if benchmark_start is None else benchmark_start.price
            ),
            "benchmark_price_at_horizon": (None if benchmark_end is None else benchmark_end.price),
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
            "method_version": OUTCOME_METHOD_VERSION,
            "measurement_basis": reference.basis,
            "reference_at": reference.reference_at,
            "fill": reference.fill,
            "revision": revision,
            "content_hash": digest,
            "provenance": provenance,
        },
    )
    return outcome, digest


def _outcome_event_payload(
    outcome: DecisionOutcome,
    record: OutcomeRecord,
    *,
    decision: AgentDecisionLog,
    now: datetime,
) -> dict:
    """The versioned values of one recorded outcome.

    The payload carries the values themselves, not just a pointer: a correction
    can update the projection row, and an event consumer replaying the log must
    still be able to read what was recorded at this revision.
    """
    return {
        "event_schema": "outcome.recorded.v1",
        "outcome_id": str(record.id),
        "outcome_record_id": str(record.id),
        "decision_outcome_id": outcome.id,
        "decision_id": decision.id,
        "run_id": str(record.decision.run_id),
        "horizon_days": record.horizon_days,
        "method_version": record.method_version,
        "maturity": record.maturity,
        "measurement_basis": record.measurement_basis or None,
        "reference_at": utc_iso(record.reference_at),
        "evaluated_at": utc_iso(record.evaluated_at or now),
        "price_observed_at_decision": utc_iso(record.price_observed_at_decision),
        "price_observed_at": utc_iso(record.price_observed_at),
        "benchmark_observed_at_decision": utc_iso(record.benchmark_observed_at_decision),
        "benchmark_price_observed_at": utc_iso(record.benchmark_price_observed_at),
        "price_source": record.price_source or None,
        "benchmark_price_source": record.benchmark_price_source or None,
        "revision": record.revision,
        "content_hash": record.content_hash,
        "values": {
            "price_at_decision": _decimal_text(record.price_at_decision),
            "price_at_horizon": _decimal_text(record.price_at_horizon),
            "forward_return_pct": _decimal_text(record.forward_return_pct),
            "decision_return_pct": _decimal_text(record.decision_return_pct),
            "benchmark_return_pct": _decimal_text(record.benchmark_return_pct),
        },
    }


def _publish_outcome_event(
    decision: AgentDecisionLog,
    outcome: DecisionOutcome,
    record: OutcomeRecord | None,
    *,
    now: datetime,
) -> None:
    """Append the versioned outcome event and its outbox row.

    ``append_event`` writes the ordered ``RunEvent`` and a queued notification in
    the caller's transaction. The dedupe key contains the revision and content
    hash, so a retry of identical values returns the first event (one timeline
    entry) while a genuine correction produces a second, differently keyed event.
    """
    if record is None:
        return
    run = _run_for_decision(decision)
    if run is None:
        return
    payload = _outcome_event_payload(outcome, record, decision=decision, now=now)
    dedupe_key = (
        f"outcome_recorded:{decision.id}:{record.horizon_days}:{record.method_version}:"
        f"{record.revision}:{record.content_hash[:32]}"
    )
    append_event(
        run,
        RunEventType.OUTCOME_RECORDED,
        stage=RunStage.COMPLETED,
        agent_role=AgentRole.SYSTEM,
        payload=payload,
        occurred_at=now,
        dedupe_key=dedupe_key,
    )


def _mark_attempt_failure(
    evaluation: OutcomeEvaluation,
    *,
    reason_code: str,
    detail: str,
    reference: MeasurementReference | None,
    now: datetime,
    decision: AgentDecisionLog,
) -> str:
    """Record a failed price attempt under the bounded retry policy."""
    evaluation.reference_basis = reference.basis if reference is not None else BASIS_UNAVAILABLE
    if reference is not None:
        evaluation.reference_at = reference.reference_at
        evaluation.fill = reference.fill
    attempts = evaluation.attempt_count
    if attempts >= evaluation.max_attempts:
        evaluation.state = OutcomeEvaluationState.UNAVAILABLE
        evaluation.next_attempt_at = None
        reason = (
            f"{reason_code}: {detail} No usable price after {attempts} attempt(s); "
            "the horizon is unavailable and will not be retried automatically."
        )
        maturity = OutcomeMaturity.UNAVAILABLE
    else:
        evaluation.state = OutcomeEvaluationState.MATURED
        evaluation.next_attempt_at = now + _retry_backoff(attempts)
        reason = (
            f"{reason_code}: {detail} Attempt {attempts}/{evaluation.max_attempts}; "
            f"next retry at {utc_iso(evaluation.next_attempt_at)}."
        )
        maturity = OutcomeMaturity.MATURED
    evaluation.reason = reason
    evaluation.save(
        update_fields=[
            "state",
            "reason",
            "next_attempt_at",
            "attempt_count",
            "last_attempt_at",
            "reference_basis",
            "reference_at",
            "fill",
            "updated_at",
        ]
    )
    _update_projection(
        decision,
        horizon_days=evaluation.horizon_days,
        maturity=maturity,
        reason=reason,
        reference=reference,
        now=now,
    )
    return "unpriced"


def _attempt_horizon(
    decision: AgentDecisionLog,
    *,
    horizon_days: int,
    reference: MeasurementReference,
    action: str,
    now: datetime,
    force: bool,
) -> str:
    """Evaluate one (decision, horizon, method) unit of work.

    Returns ``scored``, ``skipped``, ``pending`` or ``unpriced``. The whole
    attempt runs under a row lock so two overlapping schedulers cannot both
    record the result.
    """
    evaluation, created = OutcomeEvaluation.objects.get_or_create(
        decision=decision,
        horizon_days=horizon_days,
        method_version=OUTCOME_METHOD_VERSION,
        defaults={
            "max_attempts": MAX_PRICE_ATTEMPTS,
            "reference_basis": reference.basis,
            "reference_at": reference.reference_at,
            "fill": reference.fill,
        },
    )
    target, mature_at = horizon_timeline(reference.reference_at, horizon_days)
    eligible = now >= mature_at

    if not force and not created:
        if evaluation.state == OutcomeEvaluationState.EVALUATED:
            return "skipped"
        if evaluation.state == OutcomeEvaluationState.UNAVAILABLE:
            return "skipped"
        if evaluation.next_attempt_at is not None and evaluation.next_attempt_at > now:
            return "skipped"

    if not eligible and not force:
        pending_reason = (
            f"Horizon +{horizon_days}d targets {utc_iso(target)} and matures at "
            f"{utc_iso(mature_at)} under {HORIZON_POLICY}."
        )
        if (
            evaluation.state != OutcomeEvaluationState.PENDING
            or evaluation.reason != pending_reason
        ):
            evaluation.state = OutcomeEvaluationState.PENDING
            evaluation.reason = pending_reason
            evaluation.next_attempt_at = mature_at
            evaluation.reference_basis = reference.basis
            evaluation.reference_at = reference.reference_at
            evaluation.fill = reference.fill
            evaluation.save(
                update_fields=[
                    "state",
                    "reason",
                    "next_attempt_at",
                    "reference_basis",
                    "reference_at",
                    "fill",
                    "updated_at",
                ]
            )
        _update_projection(
            decision,
            horizon_days=horizon_days,
            maturity=OutcomeMaturity.PENDING,
            reason=pending_reason,
            reference=reference,
            now=now,
        )
        return "pending"

    with transaction.atomic():
        locked = OutcomeEvaluation.objects.select_for_update().get(pk=evaluation.pk)
        if not force:
            if locked.state == OutcomeEvaluationState.EVALUATED:
                return "skipped"
            if locked.state == OutcomeEvaluationState.UNAVAILABLE:
                return "skipped"
            if locked.next_attempt_at is not None and locked.next_attempt_at > now:
                return "skipped"

        locked.attempt_count = (locked.attempt_count or 0) + 1
        locked.last_attempt_at = now
        locked.reference_basis = reference.basis
        locked.reference_at = reference.reference_at
        locked.fill = reference.fill
        locked.save(
            update_fields=[
                "attempt_count",
                "last_attempt_at",
                "reference_basis",
                "reference_at",
                "fill",
                "updated_at",
            ]
        )

        start = reference.observation
        if start is None or start.price <= ZERO:
            return _mark_attempt_failure(
                locked,
                reason_code="no_entry_price_at_or_after_reference",
                detail=(
                    "There is no fill price and no close at or after the proposal; "
                    "the horizon has no denominator."
                ),
                reference=reference,
                now=now,
                decision=decision,
            )

        end = _observation(
            _close_on_or_after(decision.ticker, target, horizon_days=horizon_days),
            ticker=decision.ticker,
        )
        if end is None:
            return _mark_attempt_failure(
                locked,
                reason_code="no_session_at_or_after_target",
                detail=(
                    f"No mark exists at or after {utc_iso(target)}; the prior close is "
                    "never substituted."
                ),
                reference=reference,
                now=now,
                decision=decision,
            )

        benchmark_start, benchmark_end = _benchmark_observations(reference.reference_at, target)
        benchmark = None
        if benchmark_start is not None and benchmark_end is not None:
            benchmark = _return_pct(benchmark_start.price, benchmark_end.price)

        outcome, digest = _write_outcome(
            decision,
            horizon_days=horizon_days,
            action=action,
            reference=reference,
            start=start,
            end=end,
            benchmark=benchmark,
            benchmark_start=benchmark_start,
            benchmark_end=benchmark_end,
            now=now,
        )

        record, _record_created, changed = sync_outcome_versioned(
            decision,
            outcome,
            method_version=OUTCOME_METHOD_VERSION,
            measurement_basis=reference.basis,
            reference_at=reference.reference_at,
            content_digest=digest,
        )

        locked.state = OutcomeEvaluationState.EVALUATED
        locked.reason = ""
        locked.next_attempt_at = None
        locked.outcome = outcome
        locked.outcome_record = record
        locked.save(
            update_fields=[
                "state",
                "reason",
                "next_attempt_at",
                "outcome",
                "outcome_record",
                "updated_at",
            ]
        )

        _publish_outcome_event(decision, outcome, record, now=now)
        logger.debug(
            "Recorded outcome decision=%s horizon=%s revision=%s corrected=%s",
            decision.id,
            horizon_days,
            getattr(record, "revision", None),
            changed,
        )
        return "scored"


def _mark_legacy_evaluated(
    decision: AgentDecisionLog,
    *,
    outcome: DecisionOutcome,
    now: datetime,
) -> None:
    """Adopt a pre-existing score without rewriting it.

    Legacy rows keep their original basis and values. The work ledger records
    that the horizon exists so the scheduler does not keep treating it as
    missing; a correction is an explicit backfill decision, not a side effect of
    the hourly sweep.
    """
    OutcomeEvaluation.objects.get_or_create(
        decision=decision,
        horizon_days=outcome.horizon_days,
        method_version=OUTCOME_METHOD_VERSION,
        defaults={
            "state": OutcomeEvaluationState.EVALUATED,
            "attempt_count": 0,
            "reason": "",
            "reference_basis": outcome.measurement_basis or "legacy_unknown",
            "reference_at": outcome.reference_at or outcome.price_observed_at_decision,
            "outcome": outcome,
            "max_attempts": MAX_PRICE_ATTEMPTS,
        },
    )


def evaluate_decision_horizons(
    decision: AgentDecisionLog,
    *,
    now: datetime | None = None,
    horizons: Iterable[int] | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Evaluate the requested horizons for one decision.

    Used by the scheduler for a batch and by the backfill command for one
    decision. A malformed decision raises from here; the batch caller isolates
    it. Per-horizon price failures are recorded and do not raise.
    """
    reference_now = now or timezone.now()
    counts = {"scored": 0, "skipped": 0, "pending": 0, "unpriced": 0}
    reference = resolve_measurement_reference(decision)
    action = action_of(decision.action_taken)
    horizons = tuple(horizons) if horizons is not None else DecisionOutcome.HORIZONS

    existing = {
        row.horizon_days: row
        for row in DecisionOutcome.objects.filter(decision=decision, horizon_days__in=horizons)
    }
    for horizon in horizons:
        if horizon in existing and not force:
            _mark_legacy_evaluated(decision, outcome=existing[horizon], now=reference_now)
            counts["skipped"] += 1
            continue
        try:
            result = _attempt_horizon(
                decision,
                horizon_days=horizon,
                reference=reference,
                action=action,
                now=reference_now,
                force=force,
            )
        except Exception as exc:
            logger.warning(
                "Outcome evaluation failed decision=%s horizon=%s error=%s",
                decision.id,
                horizon,
                type(exc).__name__,
            )
            counts["unpriced"] += 1
            continue
        counts[result] = counts.get(result, 0) + 1
    return counts


def evaluate_pending(*, now=None, limit: int = 500, decision_ids=None) -> dict[str, int]:
    """Score every matured horizon that does not yet have a result.

    The unit of work is ``(decision, horizon, method)`` rather than the decision:
    a one-day score must not prevent the seven- and thirty-day horizons from
    being evaluated later. The selection is therefore per missing horizon, and
    the work ledger stores the pending/unavailable state so a price that is not
    available yet is visible and retried under a bound instead of being
    permanently skipped.

    Idempotent: an existing evaluated horizon is skipped, overlapping runs
    contend on the work row's unique key, and a retry that produces the same
    values does not append a second event. Safe to run on every Beat tick.
    """
    reference = now or timezone.now()
    horizons = DecisionOutcome.HORIZONS
    oldest_needed = reference - timedelta(days=min(horizons))
    counts = {
        "scored": 0,
        "skipped": 0,
        "pending": 0,
        "unpriced": 0,
        "failed": 0,
        "decisions": 0,
    }

    candidates = AgentDecisionLog.objects.filter(
        created_at__lte=oldest_needed, ticker__gt=""
    ).order_by("-created_at")
    if decision_ids is not None:
        candidates = candidates.filter(id__in=list(decision_ids))
    # No lower age bound: a day-one score on an old decision must not hide its
    # later matured horizons. The ``limit`` bounds one tick's work; the backfill
    # command remains the bounded repair path for a large historic backlog.
    candidates = candidates.select_related("transaction")[:limit]

    for decision in candidates:
        counts["decisions"] += 1
        try:
            decision_counts = evaluate_decision_horizons(decision, now=reference)
        except Exception as exc:
            counts["failed"] += 1
            logger.warning(
                "Skipping malformed decision id=%s in attribution batch: %s",
                decision.id,
                type(exc).__name__,
            )
            continue
        for key, value in decision_counts.items():
            counts[key] = counts.get(key, 0) + value

    logger.info(
        "Decision attribution: scored=%s skipped=%s pending=%s unpriced=%s failed=%s",
        counts["scored"],
        counts["skipped"],
        counts["pending"],
        counts["unpriced"],
        counts["failed"],
    )
    return counts


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------
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
    pending: int = 0
    matured: int = 0
    unavailable: int = 0
    basis_counts: dict = field(default_factory=dict)
    unresolved: list = field(default_factory=list)

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

    def measurement_basis(self) -> dict:
        """What this horizon's number is, and what it is not.

        This is published beside the aggregate because a per-decision forward
        return is a signal diagnostic, not portfolio performance. An executed
        decision is measured from its fill; a HOLD or rejection from the proposal
        close. The counts make the mix readable instead of averaging two
        different questions into one number.
        """
        return {
            "family": "signal_diagnostics",
            "method_version": OUTCOME_METHOD_VERSION,
            "horizon_policy": HORIZON_POLICY,
            "reference_policy": (
                "Executed decisions use the linked fill's stored price and timestamp; "
                "HOLD, rejection and other non-executed decisions use the first close "
                "at or after the proposal and record no fill."
            ),
            "portfolio_performance": False,
            "summable": False,
            "basis_counts": {
                BASIS_EXECUTION_FILL: self.basis_counts.get(BASIS_EXECUTION_FILL, 0),
                BASIS_PROPOSAL_CLOSE: self.basis_counts.get(BASIS_PROPOSAL_CLOSE, 0),
                "unrecorded_legacy": self.basis_counts.get("unrecorded_legacy", 0),
            },
            "note": (
                "A positive value here means the action's instrument move was "
                "favourable. It is not the account's realised return and must not "
                "be read as portfolio performance."
            ),
        }

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
            "pending": self.pending,
            "matured": self.matured,
            "unavailable": self.unavailable,
            "basis_counts": {
                BASIS_EXECUTION_FILL: self.basis_counts.get(BASIS_EXECUTION_FILL, 0),
                BASIS_PROPOSAL_CLOSE: self.basis_counts.get(BASIS_PROPOSAL_CLOSE, 0),
                "unrecorded_legacy": self.basis_counts.get("unrecorded_legacy", 0),
            },
            "unresolved": list(self.unresolved),
            "measurement_basis": self.measurement_basis(),
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

    basis_rows = rows.values("measurement_basis").annotate(count=Count("id")).order_by()
    basis_counts: dict[str, int] = {}
    for item in basis_rows:
        key = item["measurement_basis"] or "unrecorded_legacy"
        basis_counts[key] = basis_counts.get(key, 0) + item["count"]

    evaluation_rows = OutcomeEvaluation.objects.filter(
        horizon_days=horizon_days, method_version=OUTCOME_METHOD_VERSION
    )
    if portfolio is not None:
        evaluation_rows = evaluation_rows.filter(decision__portfolio=portfolio)
    state_counts = {
        item["state"]: item["count"]
        for item in evaluation_rows.values("state").annotate(count=Count("id"))
    }
    unresolved = list(
        evaluation_rows.exclude(state=OutcomeEvaluationState.EVALUATED)
        .order_by("decision_id")
        .values(
            "id",
            "decision_id",
            "horizon_days",
            "state",
            "attempt_count",
            "max_attempts",
            "reason",
            "next_attempt_at",
        )[:50]
    )
    for item in unresolved:
        item["id"] = str(item["id"])
        item["next_attempt_at"] = utc_iso(item["next_attempt_at"])

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
        pending=state_counts.get(OutcomeEvaluationState.PENDING, 0),
        matured=state_counts.get(OutcomeEvaluationState.MATURED, 0),
        unavailable=state_counts.get(OutcomeEvaluationState.UNAVAILABLE, 0),
        basis_counts=basis_counts,
        unresolved=unresolved,
    )
