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
* a missing value stays ``None``. Nothing is backfilled from a guess;
* a retry that produces identical values keeps the revision and the content
  hash. A **genuine correction** - the fill-attribution repair - increments the
  revision and archives the superseded values in
  ``signal_diagnostics['prior_revisions']``. The prior provenance is preserved,
  not overwritten into silence.

The bridge only writes for runs that were linked to a legacy log. A synthetic
fixture's hand-authored ``OutcomeRecord`` is never touched, because it has no
matching legacy log / run pair to bridge.
"""

from __future__ import annotations

from core.decision_json import content_hash
from core.decision_models import DecisionRun, OutcomeMaturity, OutcomeRecord
from core.models import DecisionOutcome

__all__ = [
    "OUTCOME_METHOD_VERSION",
    "sync_outcome",
    "sync_outcome_versioned",
    "sync_outcomes_for_run",
    "sync_projection_status",
]

#: The definition of return used by ``services.attribution``. Frozen before any
#: comparative claim is made, per the Phase 3 precondition. ``attribution.v2``
#: measures an executed decision from its fill price and timestamp; v1 rows are
#: history and are not relabelled.
OUTCOME_METHOD_VERSION = "attribution.v2"


def _maturity(outcome: DecisionOutcome) -> str:
    if outcome.decision_return_pct is None or outcome.forward_return_pct is None:
        return OutcomeMaturity.UNAVAILABLE
    return OutcomeMaturity.EVALUATED


def _values_digest(
    outcome: DecisionOutcome,
    *,
    measurement_basis: str,
    reference_at,
) -> str:
    """Canonical identity of the values a retry must reproduce.

    Timestamps are included as ISO text so a retry of the same market data is
    the same revision, and a correction that changes the reference instant is a
    different one.
    """
    from core.decision_json import utc_iso

    return content_hash(
        {
            "maturity": _maturity(outcome),
            "price_at_decision": None
            if outcome.price_at_decision is None
            else str(outcome.price_at_decision),
            "price_at_horizon": None
            if outcome.price_at_horizon is None
            else str(outcome.price_at_horizon),
            "price_observed_at_decision": utc_iso(outcome.price_observed_at_decision),
            "price_observed_at": utc_iso(outcome.price_observed_at),
            "price_source": outcome.price_source or "",
            "forward_return_pct": None
            if outcome.forward_return_pct is None
            else str(outcome.forward_return_pct),
            "decision_return_pct": None
            if outcome.decision_return_pct is None
            else str(outcome.decision_return_pct),
            "benchmark_return_pct": None
            if outcome.benchmark_return_pct is None
            else str(outcome.benchmark_return_pct),
            "benchmark_price_at_decision": None
            if outcome.benchmark_price_at_decision is None
            else str(outcome.benchmark_price_at_decision),
            "benchmark_price_at_horizon": None
            if outcome.benchmark_price_at_horizon is None
            else str(outcome.benchmark_price_at_horizon),
            "benchmark_observed_at_decision": utc_iso(outcome.benchmark_observed_at_decision),
            "benchmark_price_observed_at": utc_iso(outcome.benchmark_price_observed_at),
            "benchmark_price_source": outcome.benchmark_price_source or "",
            "measurement_basis": measurement_basis,
            "reference_at": utc_iso(reference_at),
        }
    )


def _basis_diagnostics(
    outcome: DecisionOutcome,
    *,
    measurement_basis: str,
    reference_at,
    now,
) -> dict:
    from core.decision_json import utc_iso

    provenance = dict(getattr(outcome, "provenance", None) or {})
    return {
        "source": "services.attribution.evaluate_pending",
        "directional_hit": outcome.directional_hit,
        "action": outcome.action,
        "bridge_schema_version": "decision_outcome_bridge.v2",
        "method_version": outcome.method_version or OUTCOME_METHOD_VERSION,
        "horizon_policy": provenance.get("horizon_policy"),
        "measurement_basis": {
            "basis": measurement_basis,
            "reference_at": utc_iso(reference_at),
            "fill_id": outcome.fill_id,
            "fill_price": str(outcome.fill.price) if outcome.fill_id else None,
            "fill_timestamp": utc_iso(outcome.fill.timestamp) if outcome.fill_id else None,
            "start_observation": provenance.get("start_observation"),
            "end_observation": provenance.get("end_observation"),
            "benchmark_start_observation": provenance.get("benchmark_start_observation"),
            "benchmark_end_observation": provenance.get("benchmark_end_observation"),
            "portfolio_performance": False,
            "note": (
                "A per-decision forward return is a signal diagnostic. It is not "
                "the portfolio's realised performance."
            ),
        },
        "provenance": provenance,
        "bridged_at": utc_iso(now),
    }


def _apply(
    record: OutcomeRecord,
    outcome: DecisionOutcome,
    *,
    measurement_basis: str,
    reference_at,
) -> OutcomeRecord:
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
    record.measurement_basis = measurement_basis
    record.reference_at = reference_at
    record.unavailable_reason = (
        ""
        if record.maturity == OutcomeMaturity.EVALUATED
        else "Attribution recorded no usable price pair for this horizon."
    )
    record.evaluated_at = outcome.evaluated_at
    return record


def sync_outcome_versioned(
    decision,
    outcome: DecisionOutcome,
    *,
    method_version: str = OUTCOME_METHOD_VERSION,
    measurement_basis: str = "",
    reference_at=None,
    content_digest: str = "",
) -> tuple[OutcomeRecord | None, bool, bool]:
    """Mirror one ``DecisionOutcome`` onto its run's ``DecisionRecord``.

    Returns ``(record, created, corrected)``. ``corrected`` is true when the
    values differ from the stored revision: the revision is incremented and the
    prior values are archived in ``signal_diagnostics['prior_revisions']``.
    """
    run = DecisionRun.objects.filter(legacy_log_id=decision.id).select_related("decision").first()
    if run is None:
        return None, False, False
    try:
        decision_record = run.decision
    except Exception:
        return None, False, False

    digest = content_digest or _values_digest(
        outcome, measurement_basis=measurement_basis, reference_at=reference_at
    )
    record, created = OutcomeRecord.objects.get_or_create(
        decision=decision_record,
        horizon_days=outcome.horizon_days,
        method_version=method_version,
        defaults={},
    )
    prior_diagnostics = dict(record.signal_diagnostics or {})
    prior_revisions = list(prior_diagnostics.get("prior_revisions") or [])
    corrected = bool(record.content_hash) and record.content_hash != digest
    if created:
        revision = 1
    elif corrected:
        prior_revisions.append(
            {
                "revision": record.revision,
                "content_hash": record.content_hash,
                "measurement_basis": record.measurement_basis,
                "reference_at": (record.reference_at.isoformat() if record.reference_at else None),
                "price_at_decision": str(record.price_at_decision)
                if record.price_at_decision is not None
                else None,
                "price_at_horizon": str(record.price_at_horizon)
                if record.price_at_horizon is not None
                else None,
                "forward_return_pct": str(record.forward_return_pct)
                if record.forward_return_pct is not None
                else None,
                "decision_return_pct": str(record.decision_return_pct)
                if record.decision_return_pct is not None
                else None,
                "price_observed_at_decision": (
                    record.price_observed_at_decision.isoformat()
                    if record.price_observed_at_decision
                    else None
                ),
                "price_observed_at": (
                    record.price_observed_at.isoformat() if record.price_observed_at else None
                ),
                "evaluated_at": (record.evaluated_at.isoformat() if record.evaluated_at else None),
            }
        )
        revision = (record.revision or 1) + 1
    else:
        revision = record.revision or 1

    from django.utils import timezone

    diagnostics = {
        **prior_diagnostics,
        **_basis_diagnostics(
            outcome,
            measurement_basis=measurement_basis,
            reference_at=reference_at,
            now=timezone.now(),
        ),
        "prior_revisions": prior_revisions,
    }
    record.signal_diagnostics = diagnostics
    _apply(
        record,
        outcome,
        measurement_basis=measurement_basis,
        reference_at=reference_at,
    )
    record.revision = revision
    record.content_hash = digest
    record.save()
    return record, created, corrected


def sync_outcome(
    decision,
    outcome: DecisionOutcome,
    *,
    method_version: str = OUTCOME_METHOD_VERSION,
) -> OutcomeRecord | None:
    """Backwards-compatible wrapper returning only the projection row."""
    record, _created, _corrected = sync_outcome_versioned(
        decision,
        outcome,
        method_version=method_version,
        measurement_basis=getattr(outcome, "measurement_basis", "") or "",
        reference_at=getattr(outcome, "reference_at", None) or outcome.price_observed_at_decision,
        content_digest=getattr(outcome, "content_hash", "") or "",
    )
    return record


def sync_projection_status(
    decision,
    *,
    horizon_days: int,
    maturity: str,
    reason: str,
    measurement_basis: str = "",
    reference_at=None,
    method_version: str = OUTCOME_METHOD_VERSION,
) -> OutcomeRecord | None:
    """Write a pending/unavailable detail projection without inventing a score."""
    run = DecisionRun.objects.filter(legacy_log_id=decision.id).select_related("decision").first()
    if run is None:
        return None
    try:
        decision_record = run.decision
    except Exception:
        return None
    record, _created = OutcomeRecord.objects.get_or_create(
        decision=decision_record,
        horizon_days=horizon_days,
        method_version=method_version,
    )
    if record.maturity == OutcomeMaturity.EVALUATED and record.is_scored:
        return record
    record.maturity = maturity
    record.unavailable_reason = reason
    record.measurement_basis = measurement_basis
    record.reference_at = reference_at
    record.signal_diagnostics = {
        **dict(record.signal_diagnostics or {}),
        "evaluation": {
            "state": maturity,
            "reason": reason or None,
            "method_version": method_version,
        },
    }
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
