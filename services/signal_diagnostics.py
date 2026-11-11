"""Signal diagnostics - a metric family that is deliberately not P&L.

A forward return is an analytical observation about an instrument. It is not the
return of the account, and it cannot be added to one: decision windows overlap,
position sizes differ, and a HOLD commits no capital at all. This module builds
the *diagnostic* family and makes the distinction structural:

* :class:`PerDecisionScoreSeries` has no way to produce a total. Its ``total()``
  and arithmetic operators raise :class:`MetricFamilyError` rather than returning
  a number someone could mistake for portfolio performance.
* A HOLD's zero action score is labelled as a **convention**, not a P&L result.
  ``directional_hit`` is ``None`` for a HOLD, never ``False``.
* The proposal action and the executed action are separate fields. A guard that
  rejected or clamped a proposal did not execute it.
* Relative returns are computed from **matched strategy/benchmark pairs only**.
  Two averages over different populations are never subtracted from one another.
* Every row and every summary displays the ``method_version`` that produced it,
  the horizon convention and the session policy.
* A stored price observation that predates its decision is rejected as leakage;
  a stored outcome evaluated after the requested ``as_of`` is treated as
  unavailable rather than allowed to travel back in time.

The existing Phase 0 scorer in :mod:`services.attribution` is not replaced. Its
BUY/SELL/HOLD conventions are reused so the two views cannot disagree about what
a SELL earns, and legacy ``DecisionOutcome`` rows are tagged with the declared
legacy method version instead of a version invented for them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.utils import timezone

from core.decision_json import utc_iso
from core.decision_models import (
    DecisionAction,
    DecisionRun,
    GuardStatus,
    OutcomeMaturity,
    OutcomeRecord,
    RunMode,
    RunSource,
)
from services.attribution import (
    BENCHMARK_TICKER,
    action_of,
    directional_hit,
    signed_return,
)
from services.portfolio_accounting import (
    PriceBook,
    PriceMark,
    select_fill_price,
)

__all__ = [
    "BENCHMARK_RULES",
    "HOLD_ZERO_CONVENTION",
    "HORIZONS",
    "HORIZON_POLICY_VERSION",
    "LEGACY_METHOD_VERSION",
    "METHOD_VERSION",
    "SESSION_POLICY",
    "MetricFamilyError",
    "PerDecisionScoreSeries",
    "SignalDiagnosticsReport",
    "SignalObservation",
    "aggregate_observations",
    "build_signal_diagnostics",
    "evaluate_horizon",
    "execution_state_for_run",
    "summarize_observations",
]

#: Version of the diagnostics definitions. A number without this is not
#: comparable to a number from another version.
METHOD_VERSION = "signal_diagnostics.v1"

#: The 1/7/30 convention is frozen here and quoted in every report. Changing it
#: is a new method version, not an edit.
HORIZONS = (1, 7, 30)
HORIZON_POLICY_VERSION = "horizons.1_7_30.v1"

#: Outcomes from the Phase 0 table predate method versioning. The convention is
#: known (BUY=forward, SELL=-forward, HOLD=0; SPY benchmark), so it is declared
#: rather than left blank.
LEGACY_METHOD_VERSION = "attribution.legacy.v1"

TIMEZONE_POLICY = "UTC"
SESSION_POLICY = "first_mark_at_or_after_target_session.v1"

#: A HOLD commits no direction. Its zero is a reporting convention.
HOLD_ZERO_CONVENTION = "hold_zero_convention.v1"

#: Named benchmark rules. SPY is the equity benchmark; a mixed portfolio must
#: declare an asset-appropriate benchmark instead of silently borrowing SPY.
BENCHMARK_RULES = {
    "equity": {"ticker": BENCHMARK_TICKER, "selection": "declared_equity_benchmark"},
    "mixed": {"ticker": None, "selection": "declared_asset_appropriate_benchmark"},
}

BENCHMARK_MATCH_RULE = "pairwise_same_horizon_forward_returns.v1"

#: The declared rule: the stored benchmark return refers to the same decision and
#: horizon as the strategy. Whether that can be *checked* is a separate question,
#: answered per row by ``_verify_benchmark_interval``.
BENCHMARK_INTERVAL_RULE = (
    "stored_benchmark_return_uses_the_same_decision_and_horizon_as_the_strategy.v1"
)

#: The verification rule (FIX-07C). Applied only when both sides persisted their
#: observation sessions; see ``_verify_benchmark_interval``.
BENCHMARK_INTERVAL_VERIFICATION_RULE = (
    "benchmark_and_instrument_observations_share_the_decision_and_horizon_sessions.v1"
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")
PCT4 = Decimal("0.0001")
PCT1 = Decimal("0.1")

ACTION_SCORE_DEFINITION = (
    "BUY score = forward return; SELL score = -forward return; HOLD score = 0 by "
    "convention and makes no directional claim."
)


class MetricFamilyError(Exception):
    """Raised when one metric family is asked to behave like another."""


# ---------------------------------------------------------------------------
# Horizon evaluation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HorizonEvaluation:
    """One instrument's forward return at one horizon under a declared policy."""

    ticker: str
    decision_at: datetime
    horizon_days: int
    target_at: datetime
    maturity: str
    method_version: str
    price_at_decision: Decimal | None = None
    price_at_horizon: Decimal | None = None
    price_observed_at: datetime | None = None
    session_shift_days: int | None = None
    forward_return_pct: Decimal | None = None
    reason: str | None = None
    rejected_earlier_marks: int = 0

    @property
    def evaluated(self) -> bool:
        return self.maturity == OutcomeMaturity.EVALUATED

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "decision_at": utc_iso(self.decision_at),
            "horizon_days": self.horizon_days,
            "target_at": utc_iso(self.target_at),
            "maturity": self.maturity,
            "method_version": self.method_version,
            "price_at_decision": (
                None if self.price_at_decision is None else str(self.price_at_decision)
            ),
            "price_at_horizon": (
                None if self.price_at_horizon is None else str(self.price_at_horizon)
            ),
            "price_observed_at": utc_iso(self.price_observed_at),
            "session_shift_days": self.session_shift_days,
            "forward_return_pct": (
                None
                if self.forward_return_pct is None
                else str(self.forward_return_pct.quantize(PCT4))
            ),
            "reason": self.reason,
            "rejected_earlier_marks": self.rejected_earlier_marks,
        }


def evaluate_horizon(
    *,
    ticker: str,
    decision_at: datetime,
    horizon_days: int,
    marks: PriceBook | Iterable[PriceMark],
    start_price: Decimal | None = None,
    as_of: datetime | None = None,
    method_version: str = METHOD_VERSION,
    session_policy: str = SESSION_POLICY,
) -> HorizonEvaluation:
    """Measure one forward return without ever looking backwards for a price.

    A weekend or holiday target has no price of its own. The first subsequent
    mark is the honest session, and ``session_shift_days`` records how far it
    moved. If there is no subsequent mark, the maturity is ``unavailable`` and
    the price is null - never zero and never the prior close.
    """
    symbol = (ticker or "").strip().upper()
    if timezone.is_naive(decision_at):
        raise MetricFamilyError("decision_at must be timezone-aware.")
    if as_of is not None:
        if timezone.is_naive(as_of):
            raise MetricFamilyError("as_of must be timezone-aware.")
        if as_of < decision_at:
            raise MetricFamilyError("as_of cannot precede the decision being scored.")
    if horizon_days <= 0:
        raise MetricFamilyError("horizon_days must be positive.")

    target = decision_at + timedelta(days=horizon_days)
    book = marks if isinstance(marks, PriceBook) else PriceBook(marks)

    selection = select_fill_price(
        ticker=symbol,
        decision_at=decision_at,
        marks=book,
        fill_rule=session_policy,
        as_of=as_of,
    )
    entry = start_price if start_price is not None else selection.price
    if entry is None or entry <= ZERO:
        return HorizonEvaluation(
            ticker=symbol,
            decision_at=decision_at,
            horizon_days=horizon_days,
            target_at=target,
            maturity=OutcomeMaturity.UNAVAILABLE,
            method_version=method_version,
            reason="no_entry_price_at_or_after_decision",
            rejected_earlier_marks=selection.rejected_earlier_marks,
        )

    exit_mark = book.mark_at_or_after(symbol, target, as_of=as_of)
    if exit_mark is None:
        return HorizonEvaluation(
            ticker=symbol,
            decision_at=decision_at,
            horizon_days=horizon_days,
            target_at=target,
            maturity=OutcomeMaturity.UNAVAILABLE,
            method_version=method_version,
            price_at_decision=entry,
            reason="no_session_at_or_after_target",
            rejected_earlier_marks=selection.rejected_earlier_marks,
        )

    forward = ((exit_mark.price - entry) / entry * HUNDRED).quantize(PCT4)
    shift = (exit_mark.at.date() - target.date()).days
    return HorizonEvaluation(
        ticker=symbol,
        decision_at=decision_at,
        horizon_days=horizon_days,
        target_at=target,
        maturity=OutcomeMaturity.EVALUATED,
        method_version=method_version,
        price_at_decision=entry,
        price_at_horizon=exit_mark.price,
        price_observed_at=exit_mark.at,
        session_shift_days=max(shift, 0),
        forward_return_pct=forward,
        rejected_earlier_marks=selection.rejected_earlier_marks,
    )


# ---------------------------------------------------------------------------
# Proposal vs executed action
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExecutionState:
    """What was proposed, what actually reached the ledger, and why not."""

    proposal_action: str | None
    executed_action: str | None
    status: str
    guard_status: str | None = None
    transaction_source: str | None = None

    @property
    def counterfactual(self) -> bool:
        return self.status == "rejected_by_guard"

    def as_dict(self) -> dict:
        return {
            "proposal_action": self.proposal_action,
            "executed_action": self.executed_action,
            "status": self.status,
            "guard_status": self.guard_status,
            "transaction_source": self.transaction_source,
        }


def _proposal_action(run: DecisionRun) -> str | None:
    try:
        decision = run.decision
    except Exception:
        decision = None
    if decision is not None:
        return decision.action
    legacy = getattr(run, "legacy_log", None)
    if legacy is not None:
        action = action_of(legacy.action_taken)
        return action if action in {"BUY", "SELL", "HOLD"} else None
    return None


def execution_state_for_run(run: DecisionRun) -> ExecutionState:
    """Resolve the proposal and the executed action independently.

    The proposal lives on ``DecisionRecord``. The execution lives in the ledger
    and in the guard's verdict: ``rejected`` means no fill, ``clamped`` means a
    smaller fill if the transaction exists, and a HOLD means no trade at all.
    Conflating the two is how a rejected trade gets counted as a loss.
    """
    proposal = _proposal_action(run)
    try:
        decision = run.decision
    except Exception:
        decision = None
    guards = list(decision.guards.all()) if decision is not None else []
    guard = max(guards, key=lambda item: item.created_at) if guards else None
    guard_status = guard.status if guard is not None else None
    transaction = getattr(getattr(run, "legacy_log", None), "transaction", None)

    if proposal == DecisionAction.HOLD:
        return ExecutionState(
            proposal_action=proposal,
            executed_action=None,
            status="hold_no_trade",
            guard_status=guard_status,
        )
    if guard_status == GuardStatus.REJECTED:
        return ExecutionState(
            proposal_action=proposal,
            executed_action=None,
            status="rejected_by_guard",
            guard_status=guard_status,
        )
    if transaction is not None and transaction.tx_type in {"BUY", "SELL"}:
        return ExecutionState(
            proposal_action=proposal,
            executed_action=transaction.tx_type,
            status="executed",
            guard_status=guard_status,
            transaction_source=f"transaction:{transaction.id}",
        )
    if guard_status in {GuardStatus.APPROVED, GuardStatus.CLAMPED}:
        # The guard permitted some quantity, but no linked fill exists. That is
        # "execution not recorded", not proof that nothing executed.
        return ExecutionState(
            proposal_action=proposal,
            executed_action=None,
            status="execution_not_recorded",
            guard_status=guard_status,
        )
    return ExecutionState(
        proposal_action=proposal,
        executed_action=None,
        status="not_executed",
        guard_status=guard_status,
    )


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SignalObservation:
    """One (run, horizon) analytical observation."""

    run_id: str
    run_key: str
    decision_at: datetime
    ticker: str
    horizon_days: int | None
    method_version: str
    maturity: str
    proposal_action: str | None
    executed_action: str | None
    execution_status: str
    counterfactual: bool
    forward_return_pct: Decimal | None = None
    action_score_pct: Decimal | None = None
    action_score_convention: str | None = None
    directional_hit: bool | None = None
    benchmark_ticker: str = ""
    benchmark_return_pct: Decimal | None = None
    benchmark_matched: bool = False
    benchmark_interval_verified: bool = False
    #: Persisted observation provenance (FIX-07C). ``None`` means the row does not
    #: carry it, which is why verification is false rather than assumed.
    benchmark_observed_at_decision: datetime | None = None
    benchmark_observed_at_horizon: datetime | None = None
    benchmark_observation_source: str = ""
    benchmark_verification_reason: str = ""
    relative_forward_return_pct: Decimal | None = None
    action_relative_return_pct: Decimal | None = None
    cohort: str = "llm"
    mode: str = ""
    source: str = ""
    evaluated_at: datetime | None = None
    price_timestamp_verified: bool = False
    unavailable_reason: str | None = None
    flags: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "run_key": self.run_key,
            "decision_at": utc_iso(self.decision_at),
            "ticker": self.ticker,
            "horizon_days": self.horizon_days,
            "method_version": self.method_version,
            "maturity": self.maturity,
            "proposal_action": self.proposal_action,
            "executed_action": self.executed_action,
            "execution_status": self.execution_status,
            "measurement": "counterfactual" if self.counterfactual else "signal",
            "forward_return_pct": (
                None
                if self.forward_return_pct is None
                else str(self.forward_return_pct.quantize(PCT4))
            ),
            "action_score_pct": (
                None if self.action_score_pct is None else str(self.action_score_pct.quantize(PCT4))
            ),
            "action_score_convention": self.action_score_convention,
            "directional_hit": self.directional_hit,
            "benchmark": {
                "ticker": self.benchmark_ticker or None,
                "return_pct": (
                    None
                    if self.benchmark_return_pct is None
                    else str(self.benchmark_return_pct.quantize(PCT4))
                ),
                "matched": self.benchmark_matched,
                "interval_verified": self.benchmark_interval_verified,
                "observation": {
                    "at_decision": (
                        None
                        if self.benchmark_observed_at_decision is None
                        else utc_iso(self.benchmark_observed_at_decision)
                    ),
                    "at_horizon": (
                        None
                        if self.benchmark_observed_at_horizon is None
                        else utc_iso(self.benchmark_observed_at_horizon)
                    ),
                    "source": self.benchmark_observation_source or None,
                    "precision": "session_date",
                    "verification_reason": self.benchmark_verification_reason or None,
                },
            },
            "relative_forward_return_pct": (
                None
                if self.relative_forward_return_pct is None
                else str(self.relative_forward_return_pct.quantize(PCT4))
            ),
            "action_relative_return_pct": (
                None
                if self.action_relative_return_pct is None
                else str(self.action_relative_return_pct.quantize(PCT4))
            ),
            "cohort": self.cohort,
            "mode": self.mode or None,
            "source": self.source or None,
            "evaluated_at": utc_iso(self.evaluated_at),
            "price_timestamp_verified": self.price_timestamp_verified,
            "unavailable_reason": self.unavailable_reason,
            "flags": list(self.flags),
            "note": (
                "This is an analytical signal observation. It is not a portfolio "
                "return and overlapping windows must not be summed."
            ),
        }


def _cohort_for(run: DecisionRun) -> str:
    if run.mode == RunMode.HEURISTIC or run.source == RunSource.HEURISTIC_FALLBACK:
        return "heuristic"
    if run.mode == RunMode.SYNTHETIC:
        return "synthetic"
    if run.mode == RunMode.SIMULATED or run.source == RunSource.STUB:
        # A scripted provider is not an LLM sample. Pooling it with live model
        # runs would let authored output borrow the credibility of a real one.
        return "simulated"
    return "llm"


def _as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _decision_timestamp(run: DecisionRun) -> datetime:
    """The proposal's timestamp, which is when a price could first be acted on."""
    try:
        return run.decision.created_at
    except Exception:
        return run.started_at


def _outcome_rows(run: DecisionRun) -> tuple[list[Any], str]:
    """Return Phase 3 outcomes when present, otherwise legacy outcomes."""
    try:
        decision = run.decision
    except Exception:
        decision = None
    if decision is not None:
        rows = list(decision.outcomes.all())
        if rows:
            return rows, "outcome_record"
    legacy = getattr(run, "legacy_log", None)
    if legacy is not None:
        rows = list(legacy.outcomes.all())
        if rows:
            return rows, "decision_outcome"
    return [], "none"


def _verify_benchmark_interval(
    *,
    decision_at: datetime,
    horizon_days: int | None,
    instrument_at_decision: datetime | None,
    instrument_at_horizon: datetime | None,
    benchmark_at_decision: datetime | None,
    benchmark_at_horizon: datetime | None,
) -> tuple[bool, str]:
    """Check that the benchmark window is the instrument window (FIX-07C).

    Verified means: both sides persisted an observation for both endpoints, the
    benchmark's decision observation falls on the same session as the
    instrument's, and the benchmark's horizon observation falls on the same
    session as the instrument's and not before the target session.

    What this does **not** verify: the intraday instant of either observation.
    Prices are daily closes, so the comparison is at session-date precision. A
    row can carry a benchmark return with no provenance at all (legacy rows); it
    is reported unverified with the missing field named, not assumed matched.
    """
    missing = []
    if benchmark_at_decision is None:
        missing.append("benchmark_decision_observation_missing")
    if benchmark_at_horizon is None:
        missing.append("benchmark_horizon_observation_missing")
    if instrument_at_decision is None:
        missing.append("instrument_decision_observation_missing")
    if instrument_at_horizon is None:
        missing.append("instrument_horizon_observation_missing")
    if missing:
        return False, missing[0]
    if benchmark_at_decision.date() != instrument_at_decision.date():
        return False, "benchmark_decision_session_mismatch"
    if benchmark_at_horizon.date() != instrument_at_horizon.date():
        return False, "benchmark_horizon_session_mismatch"
    if horizon_days is not None:
        target = (decision_at + timedelta(days=horizon_days)).date()
        if benchmark_at_horizon.date() < target:
            return False, "benchmark_horizon_before_target_session"
    return True, ""


def _observation_from_outcome(
    *,
    run: DecisionRun,
    outcome: Any,
    kind: str,
    execution: ExecutionState,
    as_of: datetime | None,
) -> SignalObservation:
    decision_at = _decision_timestamp(run)
    ticker = (run.ticker or "").strip().upper()
    flags: list[str] = []
    cohort = _cohort_for(run)

    if kind == "outcome_record":
        record: OutcomeRecord = outcome
        horizon = record.horizon_days
        method_version = record.method_version or METHOD_VERSION
        maturity = record.maturity
        forward = _as_decimal(record.forward_return_pct)
        action_score = _as_decimal(record.decision_return_pct)
        benchmark_return = _as_decimal(record.benchmark_return_pct)
        benchmark_ticker = (record.benchmark_ticker or "").strip().upper()
        evaluated_at = record.evaluated_at
        price_observed_at = record.price_observed_at
        price_observed_at_decision = record.price_observed_at_decision
        benchmark_observed_at_decision = record.benchmark_observed_at_decision
        benchmark_observed_at_horizon = record.benchmark_price_observed_at
        benchmark_observation_source = record.benchmark_price_source
        benchmark_interval_verified = False
        benchmark_verification_reason = ""
        reason = record.unavailable_reason or None
        if maturity == OutcomeMaturity.EVALUATED and (forward is None or action_score is None):
            maturity = OutcomeMaturity.UNAVAILABLE
            reason = "stored_evaluated_outcome_has_no_score"
            flags.append("incomplete_stored_outcome")
    else:
        record = outcome
        horizon = record.horizon_days
        method_version = LEGACY_METHOD_VERSION
        forward = _as_decimal(record.forward_return_pct)
        action_score = _as_decimal(record.decision_return_pct)
        benchmark_return = _as_decimal(record.benchmark_return_pct)
        benchmark_ticker = BENCHMARK_TICKER
        evaluated_at = record.evaluated_at
        price_observed_at = None
        price_observed_at_decision = None
        benchmark_observed_at_decision = None
        benchmark_observed_at_horizon = None
        benchmark_observation_source = ""
        benchmark_interval_verified = False
        benchmark_verification_reason = ""
        reason = None
        maturity = (
            OutcomeMaturity.EVALUATED
            if forward is not None and action_score is not None
            else OutcomeMaturity.UNAVAILABLE
        )
        if maturity == OutcomeMaturity.UNAVAILABLE:
            reason = "legacy_outcome_has_no_return"
        flags.append("legacy_outcome_method_declared")

    if run.is_synthetic:
        flags.append("synthetic_fixture")
    if kind == "decision_outcome":
        flags.append("price_timestamp_not_recorded")

    # A stored horizon price observed before the decision is not a forward
    # return; it is a price the decision could not have seen. Refuse the score.
    if price_observed_at is not None and price_observed_at < decision_at:
        maturity = OutcomeMaturity.UNAVAILABLE
        reason = "price_observed_before_decision"
        flags.append("price_before_decision_rejected")
        forward = None
        action_score = None
    elif price_observed_at is None:
        flags.append("price_timestamp_unverified")

    # as-of enforcement: an outcome evaluated after the requested cut-off cannot
    # enter a historical evaluation, even if it is stored today.
    if as_of is not None and evaluated_at is not None and evaluated_at > as_of:
        maturity = OutcomeMaturity.UNAVAILABLE
        reason = "evaluated_after_as_of"
        flags.append("future_evaluation_excluded")
        forward = None
        action_score = None
    if as_of is not None and price_observed_at is not None and price_observed_at > as_of:
        maturity = OutcomeMaturity.UNAVAILABLE
        reason = "price_observed_after_as_of"
        flags.append("future_price_excluded")
        forward = None
        action_score = None

    proposal = execution.proposal_action
    action = proposal if proposal in {"BUY", "SELL", "HOLD"} else "OTHER"
    if action == "OTHER":
        # A halt/error is a state, not a directional call. It must not be scored
        # as a zero, which would otherwise enter the average.
        action_score = None
        flags.append("action_not_scored")
    elif maturity == OutcomeMaturity.EVALUATED and forward is not None:
        expected_score = signed_return(action, forward)
        if action_score is None:
            action_score = expected_score
        elif action_score != expected_score:
            flags.append("stored_score_disagrees_with_method")
            action_score = expected_score
    elif forward is None:
        action_score = None

    hit = None
    if maturity == OutcomeMaturity.EVALUATED and forward is not None:
        hit = directional_hit(action, forward)

    convention = HOLD_ZERO_CONVENTION if action == "HOLD" else None
    benchmark_matched = (
        maturity == OutcomeMaturity.EVALUATED
        and forward is not None
        and benchmark_return is not None
        and bool(benchmark_ticker)
    )
    relative_forward = None
    action_relative = None
    if benchmark_matched:
        relative_forward = (forward - benchmark_return).quantize(PCT4)
        if action in {"BUY", "SELL"}:
            benchmark_action = signed_return(action, benchmark_return)
            action_relative = (action_score - benchmark_action).quantize(PCT4)
    elif maturity == OutcomeMaturity.EVALUATED and forward is not None and benchmark_return is None:
        flags.append("benchmark_missing")

    if benchmark_matched:
        (
            benchmark_interval_verified,
            benchmark_verification_reason,
        ) = _verify_benchmark_interval(
            decision_at=decision_at,
            horizon_days=horizon,
            instrument_at_decision=price_observed_at_decision,
            instrument_at_horizon=price_observed_at,
            benchmark_at_decision=benchmark_observed_at_decision,
            benchmark_at_horizon=benchmark_observed_at_horizon,
        )
        if not benchmark_interval_verified:
            flags.append("benchmark_interval_unverified")
    elif benchmark_ticker and benchmark_return is not None:
        benchmark_verification_reason = "benchmark_not_matched_to_a_scored_observation"

    if (
        kind == "outcome_record"
        and benchmark_ticker
        and benchmark_ticker != BENCHMARK_TICKER
        and not benchmark_ticker.startswith("SPY")
    ):
        flags.append("declared_non_equity_benchmark")

    return SignalObservation(
        run_id=str(run.id),
        run_key=getattr(run, "fixture_id", "") or str(run.id),
        decision_at=decision_at,
        ticker=ticker,
        horizon_days=horizon,
        method_version=method_version,
        maturity=maturity,
        proposal_action=proposal,
        executed_action=execution.executed_action,
        execution_status=execution.status,
        counterfactual=execution.counterfactual,
        forward_return_pct=forward,
        action_score_pct=action_score,
        action_score_convention=convention,
        directional_hit=hit,
        benchmark_ticker=benchmark_ticker,
        benchmark_return_pct=benchmark_return if benchmark_matched else None,
        benchmark_matched=benchmark_matched,
        benchmark_interval_verified=benchmark_interval_verified,
        benchmark_observed_at_decision=benchmark_observed_at_decision,
        benchmark_observed_at_horizon=benchmark_observed_at_horizon,
        benchmark_observation_source=benchmark_observation_source,
        benchmark_verification_reason=benchmark_verification_reason,
        relative_forward_return_pct=relative_forward,
        action_relative_return_pct=action_relative,
        cohort=cohort,
        mode=run.mode,
        source=run.source,
        evaluated_at=evaluated_at,
        price_timestamp_verified=price_observed_at is not None and price_observed_at >= decision_at,
        unavailable_reason=reason,
        flags=tuple(dict.fromkeys(flags)),
    )


def _observations_for_runs(
    runs: Iterable[DecisionRun], *, as_of: datetime | None
) -> list[SignalObservation]:
    observations: list[SignalObservation] = []
    for run in runs:
        execution = execution_state_for_run(run)
        outcomes, kind = _outcome_rows(run)
        if not outcomes:
            observations.append(
                SignalObservation(
                    run_id=str(run.id),
                    run_key=getattr(run, "fixture_id", "") or str(run.id),
                    decision_at=_decision_timestamp(run),
                    ticker=run.ticker,
                    horizon_days=None,
                    method_version=METHOD_VERSION,
                    maturity="not_recorded",
                    proposal_action=execution.proposal_action,
                    executed_action=execution.executed_action,
                    execution_status=execution.status,
                    counterfactual=execution.counterfactual,
                    cohort=_cohort_for(run),
                    mode=run.mode,
                    source=run.source,
                    price_timestamp_verified=False,
                    unavailable_reason="no_outcome_record",
                    flags=(("synthetic_fixture",) if run.is_synthetic else ())
                    + (("rejected_counterfactual",) if execution.counterfactual else ()),
                )
            )
            continue
        for outcome in outcomes:
            observations.append(
                _observation_from_outcome(
                    run=run,
                    outcome=outcome,
                    kind=kind,
                    execution=execution,
                    as_of=as_of,
                )
            )
    observations.sort(
        key=lambda row: (
            row.decision_at,
            row.run_key,
            row.horizon_days or 0,
            row.method_version,
        )
    )
    return observations


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, ZERO) / len(values)).quantize(PCT4)


def _hit_rate(hits: int, calls: int) -> Decimal | None:
    if not calls:
        return None
    return (Decimal(hits) / Decimal(calls) * HUNDRED).quantize(PCT1)


def _benchmark_interval_block(observations: Sequence[SignalObservation]) -> dict:
    """Summarise what the stored observations actually allow a reader to verify.

    ``benchmark_interval_verified`` is true only when there is at least one
    matched pair and **every** matched pair carries the four observation
    timestamps and passes the same-session check. The report names the reason it
    is false instead of declaring the rule and hoping. ``precision`` is
    ``session_date``: the prices are daily closes, so the intraday observation
    instant is not recorded and is not claimed.
    """
    matched = [row for row in observations if row.benchmark_matched]
    verified = [row for row in matched if row.benchmark_interval_verified]
    reasons: dict[str, int] = {}
    for row in matched:
        if row.benchmark_interval_verified:
            continue
        key = row.benchmark_verification_reason or "unverified"
        reasons[key] = reasons.get(key, 0) + 1
    verified_flag = bool(matched) and len(verified) == len(matched)
    if not matched:
        reason = "no_matched_benchmark_pairs"
    elif verified_flag:
        reason = None
    else:
        reason = "some_matched_pairs_lack_observation_provenance"
    return {
        "benchmark_interval_verified": verified_flag,
        "benchmark_interval": {
            "verification_rule": BENCHMARK_INTERVAL_VERIFICATION_RULE,
            "matched_pairs": len(matched),
            "verified_pairs": len(verified),
            "unverified_pairs": len(matched) - len(verified),
            "precision": "session_date",
            "intraday_observation_instant_verified": False,
            "unverified_reasons": dict(sorted(reasons.items())),
            "reason": reason,
            "what_is_verified": (
                "For every matched pair, the stored benchmark decision and horizon "
                "observations fall on the same sessions as the instrument's, and the "
                "benchmark horizon observation is not before the target session."
            ),
            "what_is_not_verified": (
                "The intraday instant of any observation (prices are daily closes), and "
                "the exchange calendar itself: sessions are compared as stored dates. "
                "Rows written before observation timestamps existed cannot be verified at "
                "all and are counted, not assumed."
            ),
        },
        "benchmark_interval_note": (
            "A benchmark return is reported as interval-verified only when its stored "
            "observation timestamps exist and match the instrument's sessions. A row "
            "without provenance keeps its benchmark return but is counted as unverified."
        ),
    }


def aggregate_observations(
    observations: Sequence[SignalObservation],
    *,
    horizon_days: int | None,
    cohort: str | None = None,
    include_counterfactual: bool = False,
) -> dict:
    """Aggregate one slice without ever producing a summable total.

    ``action_scores_sum`` is intentionally absent, and the block says why.
    """
    rows = [
        row
        for row in observations
        if row.horizon_days == horizon_days
        and (cohort is None or row.cohort == cohort)
        and (include_counterfactual or not row.counterfactual)
    ]
    evaluated = [
        row
        for row in rows
        if row.maturity == OutcomeMaturity.EVALUATED and row.action_score_pct is not None
    ]
    directional = [row for row in evaluated if row.proposal_action in {"BUY", "SELL"}]
    hits = sum(1 for row in directional if row.directional_hit is True)
    matched = [row for row in evaluated if row.benchmark_matched]
    unmatched = [
        row for row in evaluated if row.forward_return_pct is not None and not row.benchmark_matched
    ]
    hold_rows = [row for row in evaluated if row.proposal_action == "HOLD"]

    by_action: dict[str, dict] = {}
    for action in ("BUY", "SELL", "HOLD", "OTHER"):
        action_rows = [row for row in rows if (row.proposal_action or "OTHER") == action]
        action_eval = [
            row
            for row in action_rows
            if row.maturity == OutcomeMaturity.EVALUATED and row.action_score_pct is not None
        ]
        action_calls = [row for row in action_eval if row.proposal_action in {"BUY", "SELL"}]
        by_action[action] = {
            "observations": len(action_rows),
            "evaluated": len(action_eval),
            "directional_calls": len(action_calls),
            "hits": sum(1 for row in action_calls if row.directional_hit is True),
            "hit_rate_pct": str(
                _hit_rate(
                    sum(1 for row in action_calls if row.directional_hit is True),
                    len(action_calls),
                )
            )
            if action_calls
            else None,
            "mean_action_score_pct": str(_mean([row.action_score_pct for row in action_eval]))
            if action_eval
            else None,
            "zero_score_is_convention": action == "HOLD",
        }

    maturities = Counter(row.maturity for row in rows)
    methods = sorted({row.method_version for row in rows})
    execution_statuses = Counter(row.execution_status for row in rows)
    return {
        "metric_family": "signal_diagnostics",
        "horizon_days": horizon_days,
        "cohort": cohort,
        "include_counterfactual": include_counterfactual,
        "observations": len(rows),
        "evaluated": len(evaluated),
        "pending": maturities.get(OutcomeMaturity.PENDING, 0)
        + maturities.get(OutcomeMaturity.MATURED, 0),
        "unavailable": maturities.get(OutcomeMaturity.UNAVAILABLE, 0),
        "not_recorded": maturities.get("not_recorded", 0),
        "directional_calls": len(directional),
        "hits": hits,
        "hit_rate_pct": None if not directional else str(_hit_rate(hits, len(directional))),
        "hit_rate_denominator": len(directional),
        "mean_action_score_pct": (
            str(_mean([row.action_score_pct for row in evaluated])) if evaluated else None
        ),
        "mean_action_score_includes_hold_zero_rows": True,
        "hold_rows": len(hold_rows),
        "hold_zero_is_convention": True,
        "mean_directional_action_score_pct": (
            str(_mean([row.action_score_pct for row in directional])) if directional else None
        ),
        "mean_forward_return_pct": (
            str(
                _mean(
                    [
                        row.forward_return_pct
                        for row in evaluated
                        if row.forward_return_pct is not None
                    ]
                )
            )
            if any(row.forward_return_pct is not None for row in evaluated)
            else None
        ),
        "matched_benchmark_pairs": len(matched),
        "unmatched_benchmark_rows_excluded": len(unmatched),
        "mean_benchmark_return_pct": (
            str(_mean([row.benchmark_return_pct for row in matched])) if matched else None
        ),
        "mean_relative_forward_return_pct": (
            str(_mean([row.relative_forward_return_pct for row in matched])) if matched else None
        ),
        "mean_action_relative_return_pct": (
            str(
                _mean(
                    [
                        row.action_relative_return_pct
                        for row in matched
                        if row.action_relative_return_pct is not None
                    ]
                )
            )
            if any(row.action_relative_return_pct is not None for row in matched)
            else None
        ),
        "by_action": by_action,
        "execution_statuses": dict(sorted(execution_statuses.items())),
        "method_versions": methods,
        "summable": False,
        "sum_rejected_reason": (
            "Per-decision scores cover overlapping windows and unequal position sizes; "
            "their sum is not a portfolio return and no total is provided."
        ),
        "action_score_definition": ACTION_SCORE_DEFINITION,
    }


@dataclass(frozen=True)
class PerDecisionScoreSeries:
    """A deliberately non-additive series of per-decision scores.

    ``total()`` raises. ``0 + series`` raises. There is no ``sum`` property. That
    is the point: the only way to obtain portfolio performance is the accounting
    module, which works from cash and holdings rather than from these rows.
    """

    observations: tuple[SignalObservation, ...]
    method_version: str = METHOD_VERSION
    metric_family: str = "signal_diagnostics"

    def total(self) -> Decimal:
        raise MetricFamilyError(
            "Per-decision scores cannot be summed into a portfolio return. Decision "
            "windows overlap, HOLDs commit no capital, and position sizes differ. Use "
            "services.portfolio_accounting for portfolio performance."
        )

    def sum(self) -> Decimal:
        raise MetricFamilyError("Per-decision scores cannot be summed into a portfolio return.")

    def __add__(self, other):
        raise MetricFamilyError(
            "A per-decision score series is not additive. It belongs to the "
            "signal_diagnostics family and can never be combined into the "
            "portfolio_performance family."
        )

    def __radd__(self, other):
        raise MetricFamilyError(
            "A per-decision score series is not additive. It belongs to the "
            "signal_diagnostics family and can never be combined into the "
            "portfolio_performance family."
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SignalDiagnosticsReport:
    """The assembled signal-diagnostics family."""

    observations: tuple[SignalObservation, ...]
    primary_horizon: int
    method_version: str = METHOD_VERSION
    benchmark_policy: dict | None = None
    generated_at: datetime | None = None

    @property
    def series(self) -> PerDecisionScoreSeries:
        return PerDecisionScoreSeries(
            observations=self.observations, method_version=self.method_version
        )

    def as_dict(self) -> dict:
        discovered = {row.horizon_days for row in self.observations if row.horizon_days}
        horizons = sorted(set(HORIZONS) | discovered)
        summaries = {
            str(horizon): aggregate_observations(self.observations, horizon_days=horizon)
            for horizon in horizons
        }
        cohorts = {}
        for cohort in ("heuristic", "llm", "synthetic", "simulated"):
            cohort_rows = [row for row in self.observations if row.cohort == cohort]
            cohorts[cohort] = {
                "observations": len(cohort_rows),
                "runs": len({row.run_id for row in cohort_rows}),
                "summaries": {
                    str(horizon): aggregate_observations(
                        cohort_rows, horizon_days=horizon, cohort=cohort
                    )
                    for horizon in horizons
                },
                "note": (
                    "Heuristic fallback attempts are reported separately from LLM runs; "
                    "they are not the same producer and must not be pooled."
                ),
            }

        executed = [row for row in self.observations if not row.counterfactual]
        counterfactual = [row for row in self.observations if row.counterfactual]
        unresolved = [
            row
            for row in self.observations
            if row.maturity
            in {
                OutcomeMaturity.UNAVAILABLE,
                "not_recorded",
                OutcomeMaturity.PENDING,
                OutcomeMaturity.MATURED,
            }
            or (row.maturity == OutcomeMaturity.EVALUATED and row.action_score_pct is None)
        ]

        return {
            "metric_family": "signal_diagnostics",
            "method_version": self.method_version,
            "generated_at": utc_iso(self.generated_at),
            "definition": {
                "action_score": ACTION_SCORE_DEFINITION,
                "hold_zero_is_convention": True,
                "hold_zero_note": (
                    "A HOLD's zero is a reporting convention. It is not a claim about P&L "
                    "and a HOLD has no directional hit."
                ),
                "summable": False,
                "sum_rejected_reason": (
                    "Overlapping windows and unequal position sizes make per-decision scores "
                    "non-additive; this family can never produce portfolio performance."
                ),
                "proposal_vs_executed": (
                    "proposal_action is what the CIO proposed; executed_action is what "
                    "reached the ledger. Rejected proposals are counterfactuals."
                ),
            },
            "horizon_policy": {
                "version": HORIZON_POLICY_VERSION,
                "horizons_days": list(HORIZONS),
                "timezone": TIMEZONE_POLICY,
                "session_policy": SESSION_POLICY,
                "note": (
                    "Weekend and holiday targets use the first subsequent session; the "
                    "shift is recorded per row. No earlier price is substituted."
                ),
            },
            "benchmark_policy": self.benchmark_policy or BENCHMARK_RULES,
            "benchmark_match_rule": BENCHMARK_MATCH_RULE,
            "benchmark_interval_rule": BENCHMARK_INTERVAL_RULE,
            **_benchmark_interval_block(self.observations),
            "primary_horizon_days": self.primary_horizon,
            "rows": [row.as_dict() for row in self.observations],
            "summary_by_horizon": summaries,
            "cohorts": cohorts,
            "proposal_vs_executed": {
                "counts": [
                    {
                        "proposal_action": proposal,
                        "executed_action": executed_action,
                        "count": count,
                    }
                    for (proposal, executed_action), count in sorted(
                        Counter(
                            (row.proposal_action or "NONE", row.executed_action or "NONE")
                            for row in self.observations
                        ).items()
                    )
                ],
                "executed_observations": sum(
                    1 for row in executed if row.executed_action is not None
                ),
                "directional_not_executed_observations": sum(
                    1
                    for row in executed
                    if row.executed_action is None and row.proposal_action in {"BUY", "SELL"}
                ),
                "hold_observations": sum(
                    1 for row in executed if row.proposal_action == DecisionAction.HOLD
                ),
                "rejected_counterfactual_rows": len(counterfactual),
            },
            "counterfactual": {
                "rows": [row.as_dict() for row in counterfactual],
                "summaries": {
                    str(horizon): aggregate_observations(
                        counterfactual,
                        horizon_days=horizon,
                        include_counterfactual=True,
                    )
                    for horizon in horizons
                },
                "note": (
                    "Rejected proposals are analytical counterfactuals, never executed "
                    "performance. They are excluded from the headline aggregates."
                ),
            },
            "unresolved": {
                "count": len(unresolved),
                "by_maturity": dict(sorted(Counter(row.maturity for row in unresolved).items())),
                "runs_without_outcomes": len(
                    {row.run_id for row in unresolved if row.maturity == "not_recorded"}
                ),
                "runs": sorted({row.run_key for row in unresolved}),
            },
            "method_versions": sorted({row.method_version for row in self.observations}),
            "flags": sorted({flag for row in self.observations for flag in row.flags}),
            "note": (
                "Forward returns and hit rates are analytical measures of decision quality. "
                "They are not portfolio performance and are not additive."
            ),
        }


def build_signal_diagnostics(
    *,
    runs: Iterable[DecisionRun] | None = None,
    portfolio=None,
    primary_horizon: int = 7,
    as_of: datetime | None = None,
    benchmark_policy: dict | None = None,
    generated_at: datetime | None = None,
) -> SignalDiagnosticsReport:
    """Build the family from stored runs and their recorded outcomes.

    ``as_of`` is the historical cut-off. An outcome evaluated after it is shown
    as unavailable instead of being allowed to inform the past.
    """
    if runs is None:
        if portfolio is None:
            raise MetricFamilyError("build_signal_diagnostics needs either runs or a portfolio.")
        queryset = (
            DecisionRun.objects.filter(portfolio=portfolio)
            .select_related("decision", "legacy_log", "legacy_log__transaction", "snapshot")
            .prefetch_related("decision__outcomes", "decision__guards")
            .order_by("started_at", "id")
        )
        runs = list(queryset)
    else:
        runs = list(runs)
    observations = _observations_for_runs(runs, as_of=as_of)
    return SignalDiagnosticsReport(
        observations=tuple(observations),
        primary_horizon=primary_horizon,
        benchmark_policy=benchmark_policy or BENCHMARK_RULES,
        generated_at=generated_at,
    )


def summarize_observations(
    observations: Sequence[SignalObservation],
    *,
    primary_horizon: int = 7,
    benchmark_policy: dict | None = None,
    generated_at: datetime | None = None,
) -> SignalDiagnosticsReport:
    """Rebuild the report around a subset (a variant cohort)."""
    return SignalDiagnosticsReport(
        observations=tuple(observations),
        primary_horizon=primary_horizon,
        benchmark_policy=benchmark_policy or BENCHMARK_RULES,
        generated_at=generated_at,
    )
