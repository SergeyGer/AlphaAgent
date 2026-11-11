"""Bridge matured legacy outcomes into the new detail contract (FIX-01).

Phase 0 attribution writes ``DecisionOutcome``. Phase 1 detail reads
``OutcomeRecord``. Running both definitions independently is not acceptable:
they would drift and the detail screen would score a decision differently from
the scorecard. This bridge is the one place the translation happens, and it is
versioned and idempotent:

* ``OUTCOME_METHOD_VERSION`` is stored on every bridged row and is the same
  version the attribution service claims;
* the unique ``(decision, horizon, method_version)`` constraint plus
  ``get_or_create`` means re-running the evaluator or a backfill never appends a
  duplicate;
* a missing value stays ``None``. Nothing is backfilled from a guess.

The bridge only writes for runs that were linked to a legacy log. A synthetic
fixture's hand-authored ``OutcomeRecord`` is never touched, because it has no
matching legacy log / run pair to bridge.
"""

from __future__ import annotations

from core.decision_models import DecisionRun, OutcomeMaturity, OutcomeRecord
from core.models import DecisionOutcome

__all__ = ["OUTCOME_METHOD_VERSION", "sync_outcome", "sync_outcomes_for_run"]

#: The definition of return used by ``services.attribution``. Frozen before any
#: comparative claim is made, per the Phase 3 precondition.
OUTCOME_METHOD_VERSION = "attribution.v1"


def _maturity(outcome: DecisionOutcome) -> str:
    if outcome.decision_return_pct is None or outcome.forward_return_pct is None:
        return OutcomeMaturity.UNAVAILABLE
    return OutcomeMaturity.EVALUATED


def _apply(record: OutcomeRecord, outcome: DecisionOutcome) -> OutcomeRecord:
    record.maturity = _maturity(outcome)
    record.price_at_decision = outcome.price_at_decision
    record.price_at_horizon = outcome.price_at_horizon
    # Real observation dates when the producer recorded them. Falling back to
    # ``evaluated_at`` would present the evaluation instant as a market
    # observation; the fallback is kept only for rows whose producer predates
    # provenance, and ``price_source`` labels it.
    record.price_observed_at_decision = outcome.price_observed_at_decision
    record.price_observed_at = outcome.price_observed_at or outcome.evaluated_at
    record.price_source = (
        outcome.price_source
        if outcome.price_observed_at is not None
        else "decision_outcome_bridge_evaluated_at"
    )
    record.forward_return_pct = outcome.forward_return_pct
    record.decision_return_pct = outcome.decision_return_pct
    record.benchmark_return_pct = outcome.benchmark_return_pct
    record.benchmark_price_at_decision = outcome.benchmark_price_at_decision
    record.benchmark_price_at_horizon = outcome.benchmark_price_at_horizon
    record.benchmark_observed_at_decision = outcome.benchmark_observed_at_decision
    record.benchmark_price_observed_at = outcome.benchmark_price_observed_at
    record.benchmark_price_source = outcome.benchmark_price_source
    record.signal_diagnostics = {
        "source": "services.attribution.evaluate_pending",
        "directional_hit": outcome.directional_hit,
        "action": outcome.action,
        "bridge_schema_version": "decision_outcome_bridge.v1",
    }
    record.unavailable_reason = (
        ""
        if record.maturity == OutcomeMaturity.EVALUATED
        else "Attribution recorded no usable price pair for this horizon."
    )
    record.evaluated_at = outcome.evaluated_at
    record.save()
    return record


def sync_outcome(
    decision,
    outcome: DecisionOutcome,
    *,
    method_version: str = OUTCOME_METHOD_VERSION,
) -> OutcomeRecord | None:
    """Mirror one ``DecisionOutcome`` onto its run's ``DecisionRecord``."""
    run = DecisionRun.objects.filter(legacy_log_id=decision.id).select_related("decision").first()
    if run is None:
        return None
    try:
        decision_record = run.decision
    except Exception:
        return None
    record, _created = OutcomeRecord.objects.get_or_create(
        decision=decision_record,
        horizon_days=outcome.horizon_days,
        method_version=method_version,
        defaults={},
    )
    return _apply(record, outcome)


def sync_outcomes_for_run(
    run: DecisionRun,
    *,
    method_version: str = OUTCOME_METHOD_VERSION,
) -> int:
    """Backfill every matured outcome for one run. Idempotent."""
    if run.legacy_log_id is None:
        return 0
    rows = DecisionOutcome.objects.filter(decision_id=run.legacy_log_id).order_by("horizon_days")
    synced = 0
    for outcome in rows:
        if sync_outcome(run.legacy_log, outcome, method_version=method_version) is not None:
            synced += 1
    return synced
