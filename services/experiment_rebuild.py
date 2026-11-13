"""Rebuild an archived experiment-comparison report from its recorded inputs.

Why this exists
---------------
The comparison report is evidence. A rebuild that re-read a current fixture file
or a module constant would prove only that today's code agrees with itself. This
module reads the archive's own recorded inputs - the opportunity-set document,
the snapshot payloads, the decisions and the marks - and runs the same pure
simulator and metric calculator as the original build. It then compares the
result with the archived document and names every difference rather than
silently presenting a new number.

What is deliberately not done
-----------------------------
* No database read is required: an archived report carries every input.
* No model is called, and no protocol is re-run. The decisions are the recorded
  decisions; rebuilding measures the report, not the model.
* A synthetic archive stays synthetic. The rebuild output is labelled
  ``synthetic_simulation`` and never becomes a real market outcome.
* An unsupported report or archive format is refused **by name**, not parsed as
  if it were this version.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from core.decision_json import content_hash
from core.models import Portfolio
from services.experiment_execution import (
    ARCHIVE_FORMAT,
    ARM_ORDER,
    REPORT_SCHEMA_VERSION,
    ArmRun,
    ExecutionError,
    build_comparison_report,
    methods_block,
    parse_at,
    simulate_arm_accounting,
    validate_opportunity_set,
)
from services.experiment_identity import AttemptIdentity
from services.experiment_simulation import (
    DecisionInput,
    SnapshotInput,
    compute_registered_metric,
)

__all__ = [
    "REBUILD_METHOD_VERSION",
    "RebuildError",
    "rebuild_comparison_report",
    "recompute_primary_metric",
]

REBUILD_METHOD_VERSION = "archive_replay.v2"
_MAX_DIFFERENCES = 100


class RebuildError(Exception):
    """An archived report cannot be rebuilt from its recorded inputs.

    ``code`` is a stable string a caller can report without echoing an exception.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _detail(exc: BaseException) -> str:
    if exc.args:
        first = exc.args[0]
        return first if isinstance(first, str) else type(exc).__name__
    return type(exc).__name__


def _decimal(value: Any, *, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RebuildError("invalid_recorded_inputs", f"{field_name} is not a decimal.") from exc


def _short(value: Any) -> Any:
    if isinstance(value, dict):
        return {"object_keys": sorted(value)[:20], "key_count": len(value)}
    if isinstance(value, list):
        return {"item_count": len(value), "items": value[:3]}
    if isinstance(value, str) and len(value) > 240:
        return value[:240] + "..."
    return value


def _diff(
    archived: Any, rebuilt: Any, *, path: str = "", found: list[dict] | None = None
) -> list[dict]:
    found = [] if found is None else found
    if len(found) >= _MAX_DIFFERENCES:
        return found
    if isinstance(archived, dict) and isinstance(rebuilt, dict):
        for key in sorted(set(archived) | set(rebuilt)):
            child = f"{path}.{key}" if path else key
            if key not in archived:
                found.append({"path": child, "archived": None, "rebuilt": _short(rebuilt[key])})
            elif key not in rebuilt:
                found.append({"path": child, "archived": _short(archived[key]), "rebuilt": None})
            else:
                _diff(archived[key], rebuilt[key], path=child, found=found)
            if len(found) >= _MAX_DIFFERENCES:
                break
        return found
    if isinstance(archived, list) and isinstance(rebuilt, list):
        if len(archived) != len(rebuilt):
            found.append(
                {
                    "path": path,
                    "archived": f"list_length={len(archived)}",
                    "rebuilt": f"list_length={len(rebuilt)}",
                }
            )
            return found
        for index, (left, right) in enumerate(zip(archived, rebuilt, strict=True)):
            _diff(left, right, path=f"{path}[{index}]", found=found)
            if len(found) >= _MAX_DIFFERENCES:
                break
        return found
    if archived != rebuilt:
        found.append({"path": path, "archived": _short(archived), "rebuilt": _short(rebuilt)})
    return found


def _require_mapping(archive: dict, key: str, *, code: str = "archive_missing_recorded_inputs"):
    value = archive.get(key)
    if not isinstance(value, dict):
        raise RebuildError(code, f"{key} is required in the archived report.")
    return value


def _build_portfolio(recorded: dict) -> Portfolio:
    if not recorded.get("available"):
        raise RebuildError(
            "archive_missing_recorded_inputs",
            "frozen.portfolio is required to rebuild the guard and fairness checks.",
        )
    limits = recorded.get("limits") or {}
    values = {
        key: _decimal(limit, field_name=f"frozen.portfolio.limits.{key}")
        for key, limit in limits.items()
    }
    portfolio = Portfolio(
        id=recorded.get("portfolio_id"),
        risk_profile=str(recorded.get("risk_profile") or "medium"),
        balance_usd=Decimal("0"),
        **{
            key: values[key]
            for key in (
                "max_portfolio_exposure_pct",
                "max_ticker_concentration_pct",
                "max_sector_concentration_pct",
                "max_turnover_pct",
                "min_cash_buffer_pct",
                "max_trade_allocation_pct",
                "daily_loss_limit_usd",
            )
            if key in values
        },
    )
    return portfolio


def _snapshot_payload(recorded: dict, *, portfolio_id) -> dict:
    return {
        "portfolio_id": portfolio_id,
        "portfolio_version": recorded.get("portfolio_version"),
        "analysis_cutoff": parse_at(
            recorded.get("analysis_cutoff"), field_name="snapshot.analysis_cutoff"
        ),
        "captured_at": parse_at(recorded.get("captured_at"), field_name="snapshot.captured_at"),
        "holdings": recorded.get("holdings") or [],
        "cash": recorded.get("cash") or {},
        "quotes": recorded.get("quotes") or {},
    }


def _recorded_snapshots(archive: dict, execution: dict, portfolio_id) -> dict[str, SnapshotInput]:
    raw = execution.get("snapshots_used")
    if not isinstance(raw, dict) or not raw:
        raise RebuildError(
            "archive_missing_recorded_inputs",
            "execution.snapshots_used is required: a rebuild may not invent the inputs.",
        )
    snapshots: dict[str, SnapshotInput] = {}
    for key, payload in raw.items():
        if not isinstance(payload, dict):
            raise RebuildError(
                "invalid_recorded_inputs", f"execution.snapshots_used[{key!r}] must be an object."
            )
        snapshot = SnapshotInput.from_payload(payload)
        expected = content_hash(_snapshot_payload(payload, portfolio_id=portfolio_id))
        if snapshot.content_hash != expected:
            raise RebuildError(
                "snapshot_hash_mismatch",
                f"execution.snapshots_used[{key!r}].content_hash is "
                f"{snapshot.content_hash!r} but the recorded payload hashes to {expected!r}.",
            )
        snapshots[str(key)] = snapshot
    return snapshots


def _decision_from_run(
    run: dict,
    *,
    opportunity,
    snapshot: SnapshotInput,
) -> DecisionInput | None:
    recorded = run.get("decision")
    if recorded is None:
        return None
    if not isinstance(recorded, dict):
        raise RebuildError(
            "invalid_recorded_inputs",
            f"arms[{run.get('fixture_id')!r}].decision must be an object.",
        )
    action = str(recorded.get("action") or "HOLD").upper()
    quantity = recorded.get("requested_quantity")
    try:
        requested = Decimal(str(quantity)) if quantity not in (None, "") else Decimal("0")
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RebuildError(
            "invalid_recorded_inputs", "decision.requested_quantity is not a decimal."
        ) from exc
    return DecisionInput(
        run_id=str(recorded.get("run_id") or run.get("decision_run_id") or ""),
        opportunity_key=opportunity.key,
        ticker=opportunity.ticker,
        decision_at=opportunity.scheduled_at,
        action=action,
        requested_quantity=requested,
        rationale=str(recorded.get("rationale") or ""),
        snapshot=snapshot,
        recorded_at=(
            parse_at(recorded.get("recorded_at"), field_name="decision.recorded_at")
            if recorded.get("recorded_at")
            else None
        ),
    )


def _prepare(archive: dict) -> dict:
    if not isinstance(archive, dict):
        raise RebuildError("invalid_recorded_inputs", "The archive must be a JSON object.")
    version = archive.get("report_schema_version")
    if version != REPORT_SCHEMA_VERSION:
        raise RebuildError(
            "unsupported_report_schema_version",
            f"report_schema_version {version!r} is not supported; this build supports "
            f"{REPORT_SCHEMA_VERSION!r}. Refusing to read an older format as this one.",
        )
    archive_format = archive.get("archive_format")
    if archive_format != ARCHIVE_FORMAT:
        raise RebuildError(
            "unsupported_archive_format",
            f"archive_format {archive_format!r} is not supported; this build supports "
            f"{ARCHIVE_FORMAT!r}.",
        )
    frozen = _require_mapping(archive, "frozen")
    recorded_document = frozen.get("opportunity_set")
    if not isinstance(recorded_document, dict):
        raise RebuildError(
            "archive_missing_recorded_inputs",
            "frozen.opportunity_set is required: a rebuild reads the recorded inputs, not a "
            "current fixture file or a code constant.",
        )
    try:
        opportunity_set = validate_opportunity_set(recorded_document)
    except ExecutionError as exc:
        raise RebuildError(
            "invalid_recorded_inputs",
            f"frozen.opportunity_set is not valid: {_detail(exc)}",
        ) from exc
    archived_hash = str(frozen.get("opportunity_set_hash") or "")
    if archived_hash and archived_hash != opportunity_set.content_hash:
        raise RebuildError(
            "opportunity_set_hash_mismatch",
            f"frozen.opportunity_set hashes to {opportunity_set.content_hash!r}, but the "
            f"archived frozen.opportunity_set_hash is {archived_hash!r}. The recorded inputs "
            "were altered.",
        )
    opportunity_set.source_path = str(frozen.get("source_path") or "")

    execution = _require_mapping(archive, "execution")
    portfolio_recorded = frozen.get("portfolio") or {}
    portfolio = _build_portfolio(portfolio_recorded)
    snapshots = _recorded_snapshots(archive, execution, portfolio_recorded.get("portfolio_id"))

    arms_raw = archive.get("arms")
    if not isinstance(arms_raw, list) or not arms_raw:
        raise RebuildError("archive_missing_recorded_inputs", "arms is required in the archive.")
    arm_runs: dict[str, list[ArmRun]] = {arm: [] for arm in ARM_ORDER}
    by_arm_raw: dict[str, dict] = {}
    for arm_block in arms_raw:
        if not isinstance(arm_block, dict):
            raise RebuildError("invalid_recorded_inputs", "Each arms[] entry must be an object.")
        arm = str(arm_block.get("key") or "")
        if arm not in ARM_ORDER:
            raise RebuildError(
                "invalid_recorded_inputs", f"Unknown arm {arm!r} in the archived report."
            )
        by_arm_raw[arm] = arm_block
        for run in arm_block.get("runs") or []:
            if not isinstance(run, dict):
                raise RebuildError("invalid_recorded_inputs", "Each run must be an object.")
            opportunity_key = str(run.get("opportunity") or "")
            opportunity = next(
                (item for item in opportunity_set.opportunities if item.key == opportunity_key),
                None,
            )
            if opportunity is None:
                raise RebuildError(
                    "invalid_recorded_inputs",
                    f"Run names opportunity {opportunity_key!r}, which is not in the archived "
                    "opportunity set.",
                )
            snapshot = snapshots.get(opportunity_key)
            has_recorded_snapshot = bool(run.get("snapshot_id")) or (
                run.get("decision") is not None or run.get("status") == "completed"
            )
            if has_recorded_snapshot and snapshot is None:
                raise RebuildError(
                    "archive_missing_recorded_inputs",
                    f"execution.snapshots_used has no snapshot for {opportunity_key!r}.",
                )
            if not has_recorded_snapshot:
                snapshot = None
            decision = (
                _decision_from_run(run, opportunity=opportunity, snapshot=snapshot)
                if snapshot is not None
                else None
            )
            if run.get("status") == "completed" and decision is None:
                raise RebuildError(
                    "archive_missing_recorded_inputs",
                    f"Run {run.get('fixture_id')!r} is completed but has no recorded decision.",
                )
            arm_runs[arm].append(
                ArmRun(
                    arm=arm,
                    opportunity=opportunity_key,
                    fixture_id=str(run.get("fixture_id") or ""),
                    status=str(run.get("status") or "not_executed"),
                    failure_code=str(run.get("failure_code") or ""),
                    failure_detail=str(run.get("failure_detail") or ""),
                    protocol_version=str(run.get("protocol_version") or ""),
                    cost=dict(run.get("cost") or {}),
                    decision=decision,
                    snapshot=snapshot,
                    decision_run_id=str(run.get("decision_run_id") or ""),
                    identity=(
                        AttemptIdentity.from_dict(run["attempt_identity"])
                        if isinstance(run.get("attempt_identity"), dict)
                        else None
                    ),
                    attempt=int(run.get("attempt") or 0),
                    reused=bool(run.get("reused")),
                )
            )

    trajectories: dict[str, dict] = {}
    for arm in ("V0", "V1", "V2", "V3"):
        runs = arm_runs[arm]
        if any(run.decision is not None for run in runs):
            trajectories[arm] = simulate_arm_accounting(
                arm=arm,
                opportunity_set=opportunity_set,
                runs=runs,
                portfolio=portfolio,
                persist=False,
            )

    current_methods = methods_block(opportunity_set.primary_metric)
    archived_methods = frozen.get("methods") or archive.get("methods") or {}
    method_differences = [
        {
            "path": f"frozen.methods.{key}",
            "archived": _short(value),
            "rebuilt": _short(current_methods.get(key)),
            "reason": "method_version_changed",
        }
        for key, value in archived_methods.items()
        if current_methods.get(key) != value
    ]

    started_at = execution.get("started_at") or archive.get("generated_at")
    generated_at = parse_at(archive.get("generated_at"), field_name="generated_at")
    execution_block = {
        "execution_id": execution.get("execution_id"),
        "execution_id_kind": execution.get("execution_id_kind") or "deterministic_namespace.v1",
        "configuration_id": execution.get("configuration_id"),
        "configuration_id_kind": execution.get("configuration_id_kind"),
        "namespace": execution.get("namespace"),
        "namespace_version": execution.get("namespace_version") or "experiment_execution.v1",
        "portfolio_id": execution.get("portfolio_id"),
        "provider_mode_requested": execution.get("provider_mode_requested"),
        "provider_mode_used": execution.get("provider_mode_used") or archive.get("provider_mode"),
        "started_at": started_at,
        "snapshots_used": {key: snapshot.as_dict() for key, snapshot in snapshots.items()},
        "evidence": execution.get("evidence") or {},
        "client_modes": execution.get("client_modes") or {},
        "attempt_identity_version": execution.get("attempt_identity_version"),
        "attempt_identities": execution.get("attempt_identities") or {},
        "run_ids": {
            arm: [run.decision_run_id for run in arm_runs[arm]] for arm in ("V0", "V1", "V2", "V3")
        },
        "run_count": sum(
            1 for arm in ("V0", "V1", "V2", "V3") for run in arm_runs[arm] if run.decision_run_id
        ),
        "idempotent": True,
        "note": execution.get("note") or "",
    }
    rebuilt = build_comparison_report(
        opportunity_set=opportunity_set,
        arm_runs=arm_runs,
        trajectories=trajectories,
        provider_mode=str(archive.get("provider_mode") or "auto"),
        generated_at=generated_at,
        portfolio=portfolio,
        execution=execution_block,
    )
    differences = _diff(
        {key: value for key, value in archive.items() if key != "contents_hash"},
        {key: value for key, value in rebuilt.items() if key != "contents_hash"},
    )
    differences = method_differences + differences
    archived_contents_hash = archive.get("contents_hash")
    rebuilt_contents_hash = rebuilt.get("contents_hash")
    hash_matches = archived_contents_hash == rebuilt_contents_hash
    if not hash_matches:
        differences.append(
            {
                "path": "contents_hash",
                "archived": archived_contents_hash,
                "rebuilt": rebuilt_contents_hash,
            }
        )
    archived_metric = (archive.get("pre_registration") or {}).get("primary_metric_result") or {}
    rebuilt_metric = rebuilt.get("pre_registration", {}).get("primary_metric_result") or {}
    return {
        "opportunity_set": opportunity_set,
        "portfolio": portfolio,
        "arm_runs": arm_runs,
        "trajectories": trajectories,
        "rebuilt": rebuilt,
        "differences": differences[:_MAX_DIFFERENCES],
        "difference_count": len(differences),
        "hash_matches": hash_matches,
        "archived_contents_hash": archived_contents_hash,
        "rebuilt_contents_hash": rebuilt_contents_hash,
        "archived_metric": archived_metric,
        "rebuilt_metric": rebuilt_metric,
    }


def rebuild_comparison_report(archive: dict) -> dict:
    """Rebuild an archived report and compare it with the recorded one.

    Returns a summary (never the database) with ``status`` ``reproduced`` or
    ``differs``, the differences named by path, the metric comparison and the
    rebuilt document. A malformed or unsupported archive raises
    :class:`RebuildError` with a stable ``code``.
    """
    try:
        prepared = _prepare(archive)
    except RebuildError:
        raise
    except Exception as exc:
        raise RebuildError(
            "invalid_recorded_inputs", f"{type(exc).__name__}: {_detail(exc)}"
        ) from exc
    rebuilt = prepared["rebuilt"]
    metric_matches = (
        prepared["archived_metric"] == prepared["rebuilt_metric"]
        if prepared["archived_metric"]
        else True
    )
    return {
        "status": "reproduced" if not prepared["differences"] else "differs",
        "rebuild_method": REBUILD_METHOD_VERSION,
        "archive_format": archive.get("archive_format"),
        "report_schema_version": archive.get("report_schema_version"),
        "synthetic": True,
        "synthetic_label": rebuilt.get("honesty", {}).get("synthetic_label"),
        "honesty": rebuilt.get("honesty"),
        "metric": {
            "id": prepared["rebuilt_metric"].get("id"),
            "archived_value": prepared["archived_metric"].get("value"),
            "rebuilt_value": prepared["rebuilt_metric"].get("value"),
            "archived_status": prepared["archived_metric"].get("status"),
            "rebuilt_status": prepared["rebuilt_metric"].get("status"),
            "matches": metric_matches,
        },
        "contents_hash": {
            "archived": prepared["archived_contents_hash"],
            "rebuilt": prepared["rebuilt_contents_hash"],
            "matches": prepared["hash_matches"],
        },
        "differences": prepared["differences"],
        "difference_count": prepared["difference_count"],
        "rebuilt_report": rebuilt,
    }


def recompute_primary_metric(archive: dict) -> dict:
    """Recompute the registered metric from the archive's recorded inputs.

    This is the acceptance-test seam for "recompute the metric from the
    committed fixtures": it uses the same pure metric calculator as the report,
    reads the registration from the archive, and does not use a metric constant.
    """
    try:
        prepared = _prepare(archive)
    except RebuildError:
        raise
    except Exception as exc:
        raise RebuildError(
            "invalid_recorded_inputs", f"{type(exc).__name__}: {_detail(exc)}"
        ) from exc
    metric = compute_registered_metric(
        metric=prepared["opportunity_set"].primary_metric,
        opportunities=prepared["opportunity_set"].opportunities,
        marks=prepared["opportunity_set"].marks,
        decisions_by_arm={
            arm: [run.decision for run in prepared["arm_runs"][arm] if run.decision is not None]
            for arm in ("V0", "V1", "V2", "V3")
        },
        trajectories=prepared["trajectories"],
        as_of=prepared["opportunity_set"].as_of,
        uncertainty=dict(prepared["opportunity_set"].pre_registration.get("uncertainty") or {}),
        benchmark=prepared["opportunity_set"].benchmark,
        benchmark_marks=prepared["opportunity_set"].benchmark_marks,
        run_states_by_arm={
            arm: {
                run.opportunity: {"status": run.status, "failure_code": run.failure_code}
                for run in prepared["arm_runs"][arm]
            }
            for arm in ("V0", "V1", "V2", "V3")
        },
    )
    return {
        "metric": metric,
        "contents_hash": prepared["rebuilt_contents_hash"],
        "matches_archived_value": metric.get("value") == prepared["archived_metric"].get("value"),
    }
