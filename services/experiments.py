"""Experiment manifests, variant comparison and the versioned report (EXP-01..05).

An experiment is a **frozen question**: which variants, on which instruments,
over which window, from which starting state, under which risk and execution
rules, against which benchmark, with which pre-registered primary metric and
sample gate. The manifest records that question before the answer exists. The
report answers it from stored outputs only - it never calls a model.

Three rules shape the report:

**Every scheduled run stays in the denominator.** A failed run is a result; an
unexecuted run is a missing result. Dropping either from the table would make a
broken variant look complete by omission. Completion, failure and cost rates are
all computed over ``scheduled`` runs.

**The two metric families never merge.** ``signal_diagnostics`` comes from
:mod:`services.signal_diagnostics`; ``portfolio_performance`` comes from
:mod:`services.portfolio_accounting`. The report keeps them in separate blocks
with their own method versions and says explicitly that per-decision scores are
not summable. No number in the report is a sum of decision scores masquerading
as account performance.

**Uncertainty is published, not implied.** The primary metric is pre-registered;
its interval comes from a seeded time-block bootstrap, which respects the
dependence between overlapping observations. Missing intervals are null with a
reason. A small sample can validate the engineering, and the report says so; it
never asserts a profitability threshold.

The report is stored on the experiment with a monotonically increasing
``report_version``. Replaying the command recomputes it from stored rows and
increments the version; the manifest checksum refuses a silent edit.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.decision_json import content_hash, json_safe, utc_iso
from core.decision_models import (
    DecisionAction,
    DecisionRun,
    DecisionStatus,
    OutcomeMaturity,
    RunMode,
)
from core.measurement_models import (
    Experiment,
    ExperimentEvaluationMode,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
    ExperimentVariant,
    PrimaryMetricFamily,
)
from core.models import Portfolio, Transaction
from services.portfolio_accounting import (
    METHOD_VERSION as ACCOUNTING_METHOD_VERSION,
)
from services.portfolio_accounting import (
    TWR_METHOD,
    AccountingError,
    CounterfactualRule,
    ExternalFlow,
    Fill,
    LlmCost,
    OpeningState,
    PortfolioPerformance,
    PriceBook,
    PriceMark,
    SnapshotAnchor,
    build_performance,
    derive_opening_state,
    evaluate_counterfactual,
    load_fills,
    load_flows,
    load_snapshot_anchors,
    positions_at,
)
from services.signal_diagnostics import (
    BENCHMARK_RULES,
    HORIZONS,
    SignalDiagnosticsReport,
    SignalObservation,
    build_signal_diagnostics,
    execution_state_for_run,
    summarize_observations,
)
from services.signal_diagnostics import (
    METHOD_VERSION as DIAGNOSTICS_METHOD_VERSION,
)

__all__ = [
    "DEFAULT_VARIANTS",
    "MANIFEST_VERSION",
    "REPORT_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "ExperimentError",
    "ManifestError",
    "build_experiment",
    "build_report",
    "checksum_of",
    "load_manifest",
    "replay_experiment",
    "time_block_bootstrap",
    "validate_manifest",
]

MANIFEST_VERSION = 1
SCHEMA_VERSION = "1.0"
REPORT_SCHEMA_VERSION = "1.0"

ZERO = Decimal("0")
HUNDRED = Decimal("100")
PCT4 = Decimal("0.0001")
PCT1 = Decimal("0.1")
CENT = Decimal("0.01")

#: The five arms EXP-01 requires. V4 is defined and deliberately unimplemented;
#: a report says so instead of presenting an empty arm as a measured zero.
DEFAULT_VARIANTS: tuple[dict, ...] = (
    {
        "key": "V0",
        "label": "Deterministic baseline",
        "engine": "deterministic_baseline",
        "protocol_version": "heuristic_fallback.v1",
        "requires_rebuttals": False,
        "implemented": True,
        "description": (
            "No LLM. The deterministic engine's proposal is the baseline every model "
            "variant has to beat on the same snapshots and rules."
        ),
    },
    {
        "key": "V1",
        "label": "One analyst",
        "engine": "single_agent",
        "protocol_version": "single_agent_baseline.v1",
        "requires_rebuttals": False,
        "implemented": True,
        "description": "One analyst, validated proposal, no adversarial round.",
    },
    {
        "key": "V2",
        "label": "Current pipeline",
        "engine": "debate",
        "protocol_version": "debate.v1",
        "requires_rebuttals": False,
        "implemented": True,
        "description": "The production debate protocol as currently shipped.",
    },
    {
        "key": "V3",
        "label": "Explicit rebuttal",
        "engine": "debate_with_rebuttal",
        "protocol_version": "debate.v1",
        "requires_rebuttals": True,
        "implemented": True,
        "description": (
            "Runs whose recorded rebuttal round is present. Phase 2 delivers this inside "
            "debate.v1; the variant is identified by the recorded round, not a second "
            "producer."
        ),
    },
    {
        "key": "V4",
        "label": "Portfolio allocation",
        "engine": "portfolio_allocation",
        "protocol_version": "",
        "requires_rebuttals": False,
        "implemented": False,
        "unimplemented_reason": (
            "Portfolio allocation is Phase 4 scope. The variant is declared so a manifest "
            "can name it, but Phase 3 publishes no result for it."
        ),
        "description": "Portfolio-level allocation across instruments. Not implemented here.",
    },
)

SUPPORTED_PRIMARY_METRICS = frozenset(
    {
        "mean_action_score_pct",
        "hit_rate_pct",
        "mean_forward_return_pct",
        "mean_relative_forward_return_pct",
        "twr_pct",
        "net_twr_pct",
    }
)

DEFAULT_UNCERTAINTY = {
    "method": "time_block_bootstrap.v1",
    "block_size": 5,
    "resamples": 1000,
    "confidence": 0.95,
    "seed": 0,
}


class ExperimentError(Exception):
    """Base error for an experiment that cannot be built or replayed."""


class ManifestError(ExperimentError):
    """The manifest is malformed, unsupported, or its checksum drifted."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _parse_at(value, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = parse_datetime(str(value)) if value not in (None, "") else None
    if parsed is None:
        raise ManifestError(f"{field_name} must be an ISO-8601 timestamp.")
    if timezone.is_naive(parsed):
        raise ManifestError(f"{field_name} must be timezone-aware (the convention is UTC).")
    return parsed.astimezone(UTC)


def _decimal(value, *, field_name: str, default: Decimal | None = ZERO) -> Decimal | None:
    if value is None:
        return default
    if isinstance(value, float):
        raise ManifestError(f"{field_name} must not be a float; use a decimal string.")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ManifestError(f"{field_name} is not a decimal: {value!r}") from exc


def _int(value, *, field_name: str, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{field_name} is not an integer: {value!r}") from exc


def _quantize(value: Decimal | None, quantum: Decimal = PCT4) -> str | None:
    return None if value is None else str(value.quantize(quantum))


def _rate(part: int, whole: int) -> str | None:
    if not whole:
        return None
    return str((Decimal(part) / Decimal(whole) * HUNDRED).quantize(PCT1))


def _ratio(part: int, whole: int) -> str | None:
    if not whole:
        return None
    return str((Decimal(part) / Decimal(whole)).quantize(PCT4))


# ---------------------------------------------------------------------------
# Manifest checksum + validation
# ---------------------------------------------------------------------------
def checksum_of(manifest: dict) -> str:
    """SHA-256 over the manifest with its ``checksum`` member removed."""
    payload = {key: value for key, value in manifest.items() if key != "checksum"}
    return content_hash(payload)


def _declared_checksum(manifest: dict) -> str | None:
    block = manifest.get("checksum")
    if isinstance(block, str):
        return block.strip() or None
    if isinstance(block, dict):
        value = block.get("value") or block.get("sha256")
        return str(value).strip() if value else None
    return None


def _normalise_variants(entries) -> list[dict]:
    if not isinstance(entries, list) or not entries:
        raise ManifestError("The manifest must declare a non-empty 'variants' list.")
    defaults = {entry["key"]: dict(entry) for entry in DEFAULT_VARIANTS}
    merged: dict[str, dict] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise ManifestError("Each variant must be a JSON object.")
        key = str(raw.get("key") or "").strip().upper()
        if key not in defaults:
            raise ManifestError(
                f"Unknown variant {key!r}; EXP-01 defines {', '.join(sorted(defaults))}."
            )
        if key in merged:
            raise ManifestError(f"Duplicate variant {key!r}.")
        base = defaults[key]
        entry = {**base, **{k: v for k, v in raw.items() if v is not None}}
        entry["key"] = key
        entry["label"] = str(entry.get("label") or base["label"])
        entry["engine"] = str(entry.get("engine") or base["engine"])
        entry["protocol_version"] = str(entry.get("protocol_version") or "")
        entry["requires_rebuttals"] = bool(entry.get("requires_rebuttals", False))
        entry["implemented"] = bool(entry.get("implemented", base["implemented"]))
        entry["display_order"] = _int(entry.get("display_order"), field_name="display_order")
        entry["model_configuration"] = dict(entry.get("model_configuration") or {})
        entry["budget"] = dict(entry.get("budget") or {})
        entry["unimplemented_reason"] = str(entry.get("unimplemented_reason") or "")
        merged[key] = entry
    missing = [key for key in defaults if key not in merged]
    if missing:
        raise ManifestError(
            "The manifest must define every EXP-01 variant; missing "
            + ", ".join(sorted(missing))
            + "."
        )
    if merged["V4"]["implemented"]:
        raise ManifestError(
            "V4 (portfolio allocation) is defined but unimplemented in Phase 3; a manifest "
            "must not mark it implemented."
        )
    return [merged[key] for key in sorted(merged)]


def _normalise_accounting(raw: dict | None) -> dict:
    block = dict(raw or {})
    source = str(block.get("source") or "stored_ledger").strip().lower()
    if source not in {"stored_ledger", "manifest_fixture"}:
        raise ManifestError(
            f"Unknown accounting source {source!r}; expected stored_ledger or manifest_fixture."
        )
    normalised = {
        "source": source,
        "fills": list(block.get("fills") or []),
        "flows": list(block.get("flows") or []),
        "marks": list(block.get("marks") or []),
        "opening_state": block.get("opening_state") or {},
        "llm_costs": list(block.get("llm_costs") or []),
        "llm_costs_complete": bool(block.get("llm_costs_complete", False)),
        "llm_cost_missing_runs": _int(
            block.get("llm_cost_missing_runs"), field_name="accounting.llm_cost_missing_runs"
        ),
        "counterfactual_rule": block.get("counterfactual_rule"),
    }
    if source == "manifest_fixture":
        opening = normalised["opening_state"]
        if not isinstance(opening, dict) or "cash" not in opening:
            raise ManifestError(
                "accounting.source='manifest_fixture' needs opening_state with at least cash."
            )
        for field in ("fills", "flows", "marks", "llm_costs"):
            if not isinstance(normalised[field], list):
                raise ManifestError(f"accounting.{field} must be a list.")
    return normalised


def validate_manifest(manifest: dict, *, manifest_path: str | Path | None = None) -> dict:
    """Validate and normalise a manifest. Returns a new dict; never mutates input."""
    if not isinstance(manifest, dict):
        raise ManifestError("An experiment manifest must be a JSON object.")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ManifestError(
            f"Unsupported manifest_version {manifest.get('manifest_version')!r}; "
            f"this build supports {MANIFEST_VERSION}."
        )
    if str(manifest.get("schema_version")) != SCHEMA_VERSION:
        raise ManifestError(
            f"Unsupported schema_version {manifest.get('schema_version')!r}; "
            f"this build supports {SCHEMA_VERSION}."
        )

    experiment_id = str(manifest.get("experiment_id") or "").strip()
    if not experiment_id:
        raise ManifestError("The manifest needs a string 'experiment_id'.")
    if len(experiment_id) > 96:
        raise ManifestError("experiment_id must be at most 96 characters.")

    label = str(manifest.get("label") or experiment_id).strip()
    window = manifest.get("observation_window")
    if not isinstance(window, dict):
        raise ManifestError("The manifest needs an 'observation_window' object.")
    start = _parse_at(window.get("start"), field_name="observation_window.start")
    end = _parse_at(window.get("end"), field_name="observation_window.end")
    if end < start:
        raise ManifestError("observation_window.end precedes observation_window.start.")

    evaluation_mode = str(manifest.get("evaluation_mode") or "historical")
    if evaluation_mode not in ExperimentEvaluationMode.values:
        raise ManifestError(
            f"Unknown evaluation_mode {evaluation_mode!r}; expected historical or forward."
        )

    primary = manifest.get("primary_metric")
    if not isinstance(primary, dict) or not primary.get("id"):
        raise ManifestError("The manifest needs a 'primary_metric' object with an 'id'.")
    metric_id = str(primary["id"]).strip()
    if metric_id not in SUPPORTED_PRIMARY_METRICS:
        raise ManifestError(
            f"Unknown primary metric {metric_id!r}; supported: "
            + ", ".join(sorted(SUPPORTED_PRIMARY_METRICS))
        )
    metric_family = str(primary.get("family") or "").strip()
    if metric_family not in PrimaryMetricFamily.values:
        raise ManifestError(
            f"primary_metric.family must be one of {', '.join(PrimaryMetricFamily.values)}."
        )
    if metric_id.startswith("mean_") or metric_id == "hit_rate_pct":
        expected_family = PrimaryMetricFamily.SIGNAL_DIAGNOSTICS
    else:
        expected_family = PrimaryMetricFamily.PORTFOLIO_PERFORMANCE
    if metric_family != expected_family:
        raise ManifestError(
            f"primary metric {metric_id!r} belongs to {expected_family}, not {metric_family}."
        )
    horizon = _int(primary.get("horizon_days"), field_name="primary_metric.horizon_days", default=7)
    if horizon not in HORIZONS:
        raise ManifestError(f"primary_metric.horizon_days must be one of {HORIZONS}.")

    sample_raw = manifest.get("sample_requirements") or {}
    if not isinstance(sample_raw, dict):
        raise ManifestError("sample_requirements must be an object.")
    sample_requirements = {
        "minimum_scheduled_runs": _int(
            sample_raw.get("minimum_scheduled_runs") or sample_raw.get("minimum_runs"),
            field_name="sample_requirements.minimum_scheduled_runs",
        ),
        "minimum_completed_runs": _int(
            sample_raw.get("minimum_completed_runs"),
            field_name="sample_requirements.minimum_completed_runs",
        ),
        "minimum_observations": _int(
            sample_raw.get("minimum_observations"),
            field_name="sample_requirements.minimum_observations",
        ),
        "note": str(sample_raw.get("note") or ""),
    }

    uncertainty_raw = manifest.get("uncertainty") or {}
    if not isinstance(uncertainty_raw, dict):
        raise ManifestError("uncertainty must be an object.")
    uncertainty = {**DEFAULT_UNCERTAINTY, **uncertainty_raw}
    uncertainty["block_size"] = max(
        1, _int(uncertainty.get("block_size"), field_name="uncertainty.block_size", default=5)
    )
    uncertainty["resamples"] = max(
        10, _int(uncertainty.get("resamples"), field_name="uncertainty.resamples", default=1000)
    )
    try:
        confidence = Decimal(str(uncertainty.get("confidence", "0.95")))
    except (InvalidOperation, ValueError) as exc:
        raise ManifestError("uncertainty.confidence must be a decimal.") from exc
    if not (Decimal("0.5") <= confidence < Decimal("1")):
        raise ManifestError("uncertainty.confidence must be in [0.5, 1).")
    uncertainty["confidence"] = str(confidence)
    uncertainty["seed"] = _int(uncertainty.get("seed"), field_name="uncertainty.seed", default=0)
    uncertainty["method"] = str(uncertainty.get("method") or "time_block_bootstrap.v1")

    variants = _normalise_variants(manifest.get("variants"))
    variant_keys = {variant["key"] for variant in variants}

    runs_raw = manifest.get("runs")
    if not isinstance(runs_raw, list) or not runs_raw:
        raise ManifestError("The manifest must declare a non-empty 'runs' list.")
    seen_keys: set[str] = set()
    runs: list[dict] = []
    for raw in runs_raw:
        if not isinstance(raw, dict):
            raise ManifestError("Each run must be a JSON object.")
        run_key = str(raw.get("run_key") or "").strip()
        if not run_key:
            raise ManifestError("Each run needs a string 'run_key'.")
        if run_key in seen_keys:
            raise ManifestError(f"Duplicate run_key {run_key!r}.")
        seen_keys.add(run_key)
        variant_key = str(raw.get("variant") or "").strip().upper()
        if variant_key not in variant_keys:
            raise ManifestError(f"Run {run_key!r} names unknown variant {variant_key!r}.")
        scheduled_at = _parse_at(raw.get("scheduled_at"), field_name=f"run {run_key}.scheduled_at")
        reference = raw.get("decision_run_id") or raw.get("fixture_id")
        if not reference:
            raise ManifestError(
                f"Run {run_key!r} needs a decision_run_id or fixture_id; a schedule with no "
                "stored output is still allowed, but it must name the intended run."
            )
        runs.append(
            {
                "run_key": run_key,
                "variant": variant_key,
                "scheduled_at": scheduled_at,
                "decision_run_id": str(raw.get("decision_run_id") or "").strip(),
                "fixture_id": str(raw.get("fixture_id") or "").strip(),
                "expected_failure": bool(raw.get("expected_failure", False)),
                "failure_code": str(raw.get("failure_code") or "").strip(),
                "failure_detail": str(raw.get("failure_detail") or "").strip(),
                "note": str(raw.get("note") or "").strip(),
            }
        )

    accounting = _normalise_accounting(manifest.get("accounting"))
    honesty = manifest.get("honesty") or {}
    if not isinstance(honesty, dict):
        raise ManifestError("honesty must be an object.")
    if accounting["source"] == "manifest_fixture" and not honesty.get(
        "accounting_fixture_synthetic", True
    ):
        raise ManifestError(
            "accounting.source='manifest_fixture' is fixture data and must be declared "
            "synthetic in the honesty block."
        )

    frozen_raw = manifest.get("frozen") or {}
    if not isinstance(frozen_raw, dict):
        raise ManifestError("frozen must be an object.")
    benchmark = manifest.get("benchmark") or {
        "ticker": BENCHMARK_RULES["equity"]["ticker"],
        "asset_class": "equity",
        "selection_rule": BENCHMARK_RULES["equity"]["selection"],
    }
    if not isinstance(benchmark, dict) or not benchmark.get("asset_class"):
        raise ManifestError("benchmark must be an object with an asset_class.")
    if benchmark.get("asset_class") == "mixed" and not benchmark.get("selection_rule"):
        raise ManifestError(
            "A mixed portfolio needs a declared asset-appropriate benchmark selection rule."
        )

    declared = _declared_checksum(manifest)
    computed = checksum_of(manifest)
    source_path = None
    if manifest_path:
        try:
            from django.conf import settings

            source_path = str(
                Path(manifest_path).resolve().relative_to(Path(settings.BASE_DIR).resolve())
            )
        except (ValueError, ImportError):
            source_path = str(manifest_path)
    if declared is not None and declared != computed:
        raise ManifestError(
            f"The manifest checksum does not match: declared {declared}, computed {computed}. "
            "The manifest was edited without updating its checksum."
        )

    return {
        "manifest_version": MANIFEST_VERSION,
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "label": label,
        "description": str(manifest.get("description") or ""),
        "generated_for": str(manifest.get("generated_for") or ""),
        "declared_at": _parse_at(manifest.get("declared_at"), field_name="declared_at"),
        "observation_start": start,
        "observation_end": end,
        "evaluation_mode": evaluation_mode,
        "primary_metric": {
            "id": metric_id,
            "family": metric_family,
            "horizon_days": horizon,
            "direction": str(primary.get("direction") or "higher_is_better"),
            "description": str(primary.get("description") or ""),
        },
        "sample_requirements": sample_requirements,
        "cost_ceiling_usd": _decimal(
            manifest.get("cost_ceiling_usd"), field_name="cost_ceiling_usd"
        ),
        "acceptable_drawdown_pct": _decimal(
            manifest.get("acceptable_drawdown_pct"), field_name="acceptable_drawdown_pct"
        ),
        "uncertainty": uncertainty,
        "variants": variants,
        "runs": runs,
        "accounting": accounting,
        "honesty": honesty,
        "frozen": frozen_raw,
        "benchmark": benchmark,
        "as_of": (
            _parse_at(manifest.get("as_of"), field_name="as_of") if manifest.get("as_of") else None
        ),
        "checksum": computed,
        "normalised": True,
        "source_path": source_path,
    }


def load_manifest(path: str | Path) -> dict:
    """Read and validate a manifest from disk."""
    file_path = Path(path)
    if not file_path.exists():
        raise ManifestError(f"Experiment manifest not found: {file_path}")
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Experiment manifest is not valid JSON: {exc}") from exc
    return validate_manifest(raw, manifest_path=file_path)


# ---------------------------------------------------------------------------
# Uncertainty: seeded time-block bootstrap
# ---------------------------------------------------------------------------
def time_block_bootstrap(
    values: Sequence,
    *,
    block_size: int = 5,
    resamples: int = 1000,
    confidence: Decimal | str = "0.95",
    seed: int = 0,
    compounded: bool = False,
) -> dict:
    """Percentile bootstrap over contiguous time blocks.

    Adjacent decisions share market conditions, so resampling single
    observations would understate dependence. Whole blocks are resampled with
    replacement; the reported interval is a percentile interval. It is a
    statement about sampling variability on this path, not a guarantee.
    """
    data = [value if isinstance(value, Decimal) else Decimal(str(value)) for value in values]
    n = len(data)
    method = "time_block_bootstrap"
    if n < 2:
        return {
            "method": method,
            "point": _quantize(sum(data, ZERO) / n) if n == 1 else None,
            "lower": None,
            "upper": None,
            "n": n,
            "block_size": block_size,
            "resamples": 0,
            "confidence": str(confidence),
            "seed": seed,
            "reason": "fewer_than_two_observations",
            "statistic": "compounded_return" if compounded else "mean",
        }

    block = max(1, min(int(block_size), n))
    blocks = [data[index : index + block] for index in range(0, n, block)]
    rng = random.Random(seed)  # noqa: S311 - a seeded statistical bootstrap, not crypto
    samples: list[Decimal] = []
    for _ in range(max(10, int(resamples))):
        draw: list[Decimal] = []
        while len(draw) < n:
            draw.extend(rng.choice(blocks))
        draw = draw[:n]
        if compounded:
            factor = Decimal("1")
            for value in draw:
                factor *= Decimal("1") + (value / HUNDRED)
            samples.append((factor - 1) * HUNDRED)
        else:
            samples.append(sum(draw, ZERO) / len(draw))
    samples.sort()
    confidence_decimal = Decimal(str(confidence))
    tail = (Decimal("1") - confidence_decimal) / 2
    lower_index = max(0, min(len(samples) - 1, int(tail * (len(samples) - 1))))
    upper_index = max(0, min(len(samples) - 1, int((Decimal("1") - tail) * (len(samples) - 1))))
    if compounded:
        point = Decimal("1")
        for value in data:
            point *= Decimal("1") + (value / HUNDRED)
        point = (point - 1) * HUNDRED
    else:
        point = sum(data, ZERO) / n
    return {
        "method": method,
        "statistic": "compounded_return" if compounded else "mean",
        "point": _quantize(point),
        "lower": _quantize(samples[lower_index]),
        "upper": _quantize(samples[upper_index]),
        "n": n,
        "block_size": block,
        "resamples": len(samples),
        "confidence": str(confidence_decimal),
        "seed": seed,
        "reason": None,
    }


def _bootstrap_block(
    values: Sequence,
    uncertainty: dict,
    *,
    compounded: bool = False,
) -> dict:
    return time_block_bootstrap(
        values,
        block_size=int(uncertainty.get("block_size", 5)),
        resamples=int(uncertainty.get("resamples", 1000)),
        confidence=uncertainty.get("confidence", "0.95"),
        seed=int(uncertainty.get("seed", 0)),
        compounded=compounded,
    )


# ---------------------------------------------------------------------------
# Manifest fixture accounting inputs
# ---------------------------------------------------------------------------
def _fixture_opening(block: dict, *, start: datetime) -> OpeningState:
    opening = block.get("opening_state") or {}
    at = _parse_at(opening.get("at") or start, field_name="accounting.opening_state.at")
    cash = _decimal(opening.get("cash"), field_name="accounting.opening_state.cash")
    positions = {
        str(ticker).upper(): _decimal(quantity, field_name="opening position")
        for ticker, quantity in (opening.get("positions") or {}).items()
    }
    return OpeningState(
        at=at,
        cash=cash if cash is not None else ZERO,
        positions=positions,
        basis="manifest_accounting_fixture",
        flags=("synthetic_accounting_fixture",),
    )


def _fixture_fills(block: dict) -> list[Fill]:
    fills = []
    for index, raw in enumerate(block.get("fills") or []):
        if not isinstance(raw, dict):
            raise ManifestError("Each accounting fill must be an object.")
        fills.append(
            Fill(
                at=_parse_at(raw.get("at"), field_name=f"accounting.fills[{index}].at"),
                ticker=str(raw.get("ticker") or ""),
                side=str(raw.get("side") or ""),
                quantity=_decimal(
                    raw.get("quantity"), field_name=f"accounting.fills[{index}].quantity"
                ),
                price=_decimal(raw.get("price"), field_name=f"accounting.fills[{index}].price"),
                fee=_decimal(
                    raw.get("fee"),
                    field_name=f"accounting.fills[{index}].fee",
                    default=None,
                ),
                source=f"manifest_fixture:fill:{index}",
            )
        )
    return fills


def _fixture_flows(block: dict) -> list[ExternalFlow]:
    flows = []
    for index, raw in enumerate(block.get("flows") or []):
        if not isinstance(raw, dict):
            raise ManifestError("Each accounting flow must be an object.")
        flows.append(
            ExternalFlow(
                at=_parse_at(raw.get("at"), field_name=f"accounting.flows[{index}].at"),
                flow_type=str(raw.get("flow_type") or ""),
                amount=_decimal(raw.get("amount"), field_name=f"accounting.flows[{index}].amount"),
                source=f"manifest_fixture:flow:{index}",
            )
        )
    return flows


def _fixture_marks(block: dict) -> list[PriceMark]:
    marks = []
    for index, raw in enumerate(block.get("marks") or []):
        if not isinstance(raw, dict):
            raise ManifestError("Each accounting mark must be an object.")
        observed = raw.get("observed_at")
        marks.append(
            PriceMark(
                ticker=str(raw.get("ticker") or ""),
                at=_parse_at(raw.get("at"), field_name=f"accounting.marks[{index}].at"),
                price=_decimal(raw.get("price"), field_name=f"accounting.marks[{index}].price"),
                source=str(raw.get("source") or "manifest_fixture"),
                observed_at=(
                    _parse_at(observed, field_name=f"accounting.marks[{index}].observed_at")
                    if observed
                    else None
                ),
                provenance=f"manifest_fixture:mark:{index}",
            )
        )
    return marks


def _fixture_costs(block: dict) -> list[LlmCost]:
    costs = []
    for index, raw in enumerate(block.get("llm_costs") or []):
        if not isinstance(raw, dict):
            raise ManifestError("Each accounting LLM cost must be an object.")
        costs.append(
            LlmCost(
                at=_parse_at(raw.get("at"), field_name=f"accounting.llm_costs[{index}].at"),
                amount=_decimal(
                    raw.get("amount"), field_name=f"accounting.llm_costs[{index}].amount"
                ),
                source=str(raw.get("source") or "manifest_fixture"),
                run_key=str(raw.get("run_key") or ""),
            )
        )
    return costs


def _stored_marks(
    *, runs: Sequence[DecisionRun], start: datetime, end: datetime
) -> list[PriceMark]:
    """Marks that can be recovered from stored observations, and only those."""
    marks: list[PriceMark] = []
    for run in runs:
        snapshot = getattr(run, "snapshot", None)
        if snapshot is not None:
            captured = snapshot.captured_at
            for ticker, quote in (snapshot.quotes or {}).items():
                if not isinstance(quote, dict):
                    continue
                raw_price = quote.get("price_usd") or quote.get("price")
                if raw_price in (None, ""):
                    continue
                try:
                    price = Decimal(str(raw_price))
                except (InvalidOperation, ValueError):
                    continue
                if price <= ZERO:
                    continue
                raw_at = quote.get("as_of") or snapshot.captured_at
                try:
                    at = _parse_at(raw_at, field_name="snapshot quote as_of")
                except ManifestError:
                    at = snapshot.captured_at
                if not (start <= at <= end):
                    continue
                marks.append(
                    PriceMark(
                        ticker=str(ticker),
                        at=at,
                        price=price,
                        source="input_snapshot_quote",
                        observed_at=captured if captured >= at else at,
                        provenance=f"run:{run.id}",
                    )
                )
        try:
            decision = run.decision
        except Exception:
            decision = None
        if decision is None:
            continue
        for outcome in decision.outcomes.all():
            # The entry price belongs to the proposal, not to the earlier run start.
            decision_at = getattr(decision, "created_at", run.started_at)
            if outcome.price_at_decision is not None and start <= decision_at <= end:
                marks.append(
                    PriceMark(
                        ticker=run.ticker,
                        at=decision_at,
                        price=outcome.price_at_decision,
                        source="recorded_outcome_entry",
                        observed_at=decision_at,
                        provenance=f"outcome:{outcome.id}",
                    )
                )
            if outcome.price_at_horizon is None:
                continue
            target = decision_at + timedelta(days=outcome.horizon_days)
            observed = outcome.price_observed_at or target
            if observed < target or not (start <= target <= end):
                # A horizon price observed before its own horizon is not usable.
                continue
            marks.append(
                PriceMark(
                    ticker=run.ticker,
                    at=target,
                    price=outcome.price_at_horizon,
                    source="recorded_outcome_horizon",
                    observed_at=observed,
                    provenance=f"outcome:{outcome.id}",
                )
            )
    return marks


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------
def _model_configuration(variant: ExperimentVariant) -> dict:
    declared = dict(variant.model_configuration or {})
    return {
        "declared": declared,
        "source": "manifest" if declared else "not_declared",
        "note": (
            "Every LLM variant names its provider, model and parameters in the manifest so "
            "a comparison cannot silently change model mid-experiment."
        ),
    }


def _recorded_model_configuration(runs: Sequence[DecisionRun]) -> dict:
    for run in runs:
        protocol = (run.protocol_metadata or {}).get("protocol") or {}
        if protocol:
            return {
                "provider": protocol.get("provider"),
                "model": protocol.get("model"),
                "parameters": protocol.get("parameters"),
                "protocol_version": protocol.get("version"),
                "prompt_template_version": protocol.get("prompt_template_version"),
            }
    return {}


def _claims_summary(runs: Sequence[DecisionRun]) -> dict:
    total_claims = 0
    with_evidence = 0
    unsupported: list[dict] = []
    unavailable_at_cutoff: list[str] = []
    for run in runs:
        try:
            decision = run.decision
        except Exception:
            decision = None
        if decision is None:
            continue
        cutoff = getattr(run.snapshot, "analysis_cutoff", None) if run.snapshot else None
        for claim in run.claims.all():
            total_claims += 1
            evidence = list(claim.evidence.all())
            if evidence:
                with_evidence += 1
                if cutoff is not None and any(item.available_at > cutoff for item in evidence):
                    unavailable_at_cutoff.append(str(claim.claim_id))
            else:
                unsupported.append(
                    {
                        "run_key": getattr(run, "fixture_id", "") or str(run.id),
                        "claim_id": str(claim.claim_id),
                        "statement": claim.statement,
                    }
                )
    coverage = None
    if total_claims:
        coverage = str((Decimal(with_evidence) / Decimal(total_claims)).quantize(PCT4))
    return {
        "total_claims": total_claims,
        "claims_with_evidence": with_evidence,
        "evidence_coverage_ratio": coverage,
        "claims_without_evidence": len(unsupported),
        "unsupported_claim_flags": unsupported,
        "claims_citing_evidence_unavailable_at_cutoff": unavailable_at_cutoff,
        "note": (
            "Coverage is pooled over claims, not averaged over runs: an average of run-level "
            "ratios gives a one-claim run the same weight as a ten-claim run."
        ),
    }


def _action_change_summary(runs: Sequence[DecisionRun]) -> dict:
    runs_with_rebuttals = 0
    position_changes = 0
    changed = 0
    affected: list[str] = []
    for run in runs:
        rebuttals = list(run.rebuttals.all())
        if not rebuttals:
            continue
        runs_with_rebuttals += 1
        position_changes += sum(
            1
            for rebuttal in rebuttals
            if (rebuttal.position_change or "").strip().lower()
            and not (rebuttal.position_change or "").strip().lower().startswith("unchanged")
        )
        bull = sum(1 for claim in run.claims.all() if claim.stance == "bull")
        bear = sum(1 for claim in run.claims.all() if claim.stance == "bear")
        lean = "bull" if bull > bear else "bear" if bear > bull else "balanced"
        try:
            action = run.decision.action
        except Exception:
            action = None
        moved = (
            (lean == "bull" and action != DecisionAction.BUY)
            or (lean == "bear" and action != DecisionAction.SELL)
            or (lean == "balanced" and action != DecisionAction.HOLD)
        )
        if moved and position_changes:
            changed += 1
            affected.append(getattr(run, "fixture_id", "") or str(run.id))
    return {
        "runs_with_rebuttals": runs_with_rebuttals,
        "declared_position_changes": position_changes,
        "action_changes_after_rebuttal": changed,
        "affected_runs": sorted(affected),
        "basis": "final_action_vs_initial_case_lean.v1",
        "note": (
            "The protocol does not store a pre-rebuttal action, so this is a declared proxy: "
            "the final action differs from the initial cases' directional lean and at least "
            "one rebuttal declared a position change. It is not a recorded action diff."
        ),
    }


def _variant_trade_summary(
    runs: Sequence[DecisionRun], performance: PortfolioPerformance | None
) -> dict:
    transactions: dict[int, Transaction] = {}
    for run in runs:
        transaction = getattr(getattr(run, "legacy_log", None), "transaction", None)
        if transaction is not None:
            transactions[transaction.id] = transaction
    notional = sum((tx.gross_value_usd for tx in transactions.values()), ZERO)
    average_equity = None
    if performance is not None:
        average_equity = performance.turnover.get("average_equity_usd")
    ratio = None
    basis = "unavailable"
    if average_equity is not None and Decimal(str(average_equity)) > ZERO:
        ratio = notional / Decimal(str(average_equity))
        basis = "shared_experiment_average_equity"
    return {
        "trade_count": len(transactions),
        "traded_notional_usd": str(notional.quantize(CENT)),
        "turnover_ratio": _quantize(ratio),
        "turnover_basis": basis,
        "note": (
            "Trades are attributed to a variant only through the linked decision log's "
            "transaction. A variant did not have an isolated account, so turnover uses the "
            "shared experiment average equity as its denominator."
        ),
    }


def _operational_metrics(runs: Sequence[DecisionRun], scheduled: Sequence[ExperimentRun]) -> dict:
    costs = [row.cost_usd for row in scheduled if row.cost_usd is not None]
    missing_cost = sum(1 for row in scheduled if row.cost_usd is None)
    latencies = [row.latency_ms for row in scheduled if row.latency_ms is not None]
    input_tokens = [row.input_tokens for row in scheduled if row.input_tokens is not None]
    output_tokens = [row.output_tokens for row in scheduled if row.output_tokens is not None]
    total_cost = sum(costs, ZERO) if costs else None
    failure_codes = Counter(row.failure_code for row in scheduled if row.status == "failed")
    return {
        "api_cost_usd": None
        if total_cost is None
        else str(total_cost.quantize(Decimal("0.000001"))),
        "cost_reported_runs": len(costs),
        "cost_missing_runs": missing_cost,
        "cost_complete": missing_cost == 0,
        "tokens": {
            "input": sum(input_tokens) if input_tokens else None,
            "output": sum(output_tokens) if output_tokens else None,
            "reported_runs": len(input_tokens),
            "missing_runs": sum(1 for row in scheduled if row.input_tokens is None),
        },
        "latency_ms": {
            "mean": (
                str((Decimal(sum(latencies)) / Decimal(len(latencies))).quantize(CENT))
                if latencies
                else None
            ),
            "max": max(latencies) if latencies else None,
            "reported_runs": len(latencies),
            "missing_runs": sum(1 for row in scheduled if row.latency_ms is None),
        },
        "failure_codes": dict(sorted(failure_codes.items())),
        "attempted_runs": sum(1 for row in scheduled if row.was_attempted),
        "completed_runs_used": sum(1 for run in runs if run.status == DecisionStatus.COMPLETED),
    }


def _attempted_scheduled(scheduled: Sequence[ExperimentRun]) -> list[ExperimentRun]:
    return [row for row in scheduled if row.was_attempted]


def _linked_runs(scheduled: Sequence[ExperimentRun]) -> list[DecisionRun]:
    return [row.decision_run for row in scheduled if row.decision_run is not None]


def _primary_values(
    *,
    metric: dict,
    observations: Sequence[SignalObservation],
    performance: PortfolioPerformance | None,
) -> list[Decimal]:
    metric_id = metric["id"]
    if metric["family"] == PrimaryMetricFamily.SIGNAL_DIAGNOSTICS:
        horizon = metric["horizon_days"]
        rows = [
            row
            for row in observations
            if row.horizon_days == horizon
            and row.maturity == OutcomeMaturity.EVALUATED
            and not row.counterfactual
        ]
        if metric_id == "hit_rate_pct":
            return [
                HUNDRED if row.directional_hit else ZERO
                for row in rows
                if row.proposal_action in {"BUY", "SELL"} and row.directional_hit is not None
            ]
        if metric_id == "mean_action_score_pct":
            return [row.action_score_pct for row in rows if row.action_score_pct is not None]
        if metric_id == "mean_forward_return_pct":
            return [row.forward_return_pct for row in rows if row.forward_return_pct is not None]
        if metric_id == "mean_relative_forward_return_pct":
            return [
                row.relative_forward_return_pct
                for row in rows
                if row.relative_forward_return_pct is not None
            ]
        return []

    if performance is None:
        return []
    if metric_id == "net_twr_pct" and not performance.llm_costs.get("costs_complete"):
        return []
    twr = performance.twr if metric_id == "twr_pct" else performance.net_twr
    if twr.get("method") != TWR_METHOD:
        return []
    return [Decimal(value) for value in twr.get("subperiod_returns_pct") or []]


def _primary_metric(
    *,
    metric: dict,
    observations: Sequence[SignalObservation],
    performance: PortfolioPerformance | None,
    uncertainty: dict,
) -> dict:
    values = _primary_values(metric=metric, observations=observations, performance=performance)
    compounded = metric["family"] == PrimaryMetricFamily.PORTFOLIO_PERFORMANCE
    bootstrap = _bootstrap_block(values, uncertainty, compounded=compounded)
    if metric["family"] == PrimaryMetricFamily.SIGNAL_DIAGNOSTICS:
        point = _quantize(sum(values, ZERO) / len(values)) if values else None
    else:
        point = bootstrap.get("point")
    sample_requirement = int(metric.get("minimum_observations", 0) or 0)
    insufficient_reason = None
    if metric["family"] == PrimaryMetricFamily.PORTFOLIO_PERFORMANCE:
        if performance is None:
            insufficient_reason = "portfolio_performance_unavailable"
        elif metric["id"] == "net_twr_pct" and not performance.llm_costs.get("costs_complete"):
            insufficient_reason = (
                "the net-of-LLM primary metric cannot be established because at least one "
                "run reported no cost"
            )
        elif performance.twr.get("method") != TWR_METHOD:
            insufficient_reason = (
                "primary metric is a TWR whose boundary valuation was unavailable; "
                "the approximate fallback cannot be decomposed into resamplable periods"
            )
    return {
        "id": metric["id"],
        "family": metric["family"],
        "horizon_days": metric.get("horizon_days"),
        "direction": metric.get("direction"),
        "description": metric.get("description"),
        "value": point,
        "unit": "pct",
        "observations": len(values),
        "minimum_observations": sample_requirement,
        "sample_sufficient": len(values) >= sample_requirement if sample_requirement else None,
        "interval": {
            key: bootstrap.get(key)
            for key in (
                "method",
                "statistic",
                "lower",
                "upper",
                "confidence",
                "block_size",
                "resamples",
                "seed",
                "reason",
            )
        },
        "interval_established": bootstrap.get("lower") is not None,
        "interval_reason": insufficient_reason or bootstrap.get("reason"),
        "pre_registered": True,
        "profitability_threshold": None,
        "note": (
            "The interval is a seeded time-block bootstrap over dependent observations. "
            "It is a sampling interval, not a promise of future profitability."
        ),
    }


# ---------------------------------------------------------------------------
# Accounting for an experiment
# ---------------------------------------------------------------------------
def _accounting_provenance(manifest: dict) -> dict:
    return {
        "source": manifest["accounting"]["source"],
        "synthetic": manifest["accounting"]["source"] == "manifest_fixture",
        "note": (
            "portfolio performance is computed from the ledger/fixture, never from "
            "per-decision signal scores."
        ),
    }


def _build_accounting(
    experiment: Experiment,
    *,
    manifest: dict,
    runs: Sequence[DecisionRun],
    now: datetime,
) -> tuple[PortfolioPerformance | None, str | None]:
    block = manifest["accounting"]
    start = experiment.observation_start
    end = experiment.observation_end
    try:
        if block["source"] == "manifest_fixture":
            opening = _fixture_opening(block, start=start)
            fills = _fixture_fills(block)
            flows = _fixture_flows(block)
            marks = _fixture_marks(block)
            costs = _fixture_costs(block)
            missing = (
                0
                if block.get("llm_costs_complete")
                else int(block.get("llm_cost_missing_runs") or 0)
            )
            performance = build_performance(
                opening=opening,
                end=end,
                fills=fills,
                flows=flows,
                marks=marks,
                llm_costs=costs,
                llm_cost_missing_count=missing,
                provenance=_accounting_provenance(manifest),
            )
            return performance, None

        all_fills = load_fills(experiment.portfolio)
        all_flows = load_flows(experiment.portfolio)
        opening = derive_opening_state(
            experiment.portfolio, start=start, fills=all_fills, flows=all_flows
        )
        window_fills = [fill for fill in all_fills if start < fill.at <= end]
        window_flows = [flow for flow in all_flows if start < flow.at <= end]
        marks = _stored_marks(runs=runs, start=start, end=end)
        anchors = [
            SnapshotAnchor(
                at=row.at,
                equity_usd=row.equity_usd,
                cash_usd=row.cash_usd,
                positions_value_usd=row.positions_value_usd,
                source=row.source,
            )
            for row in load_snapshot_anchors(experiment.portfolio, after=start, on_or_before=end)
        ]

        costs: list[LlmCost] = []
        missing = 0
        for row in experiment.scheduled_runs.select_related("decision_run").all():
            run = row.decision_run
            if run is None:
                continue
            if run.total_cost_usd is None:
                if run.status in {DecisionStatus.COMPLETED, DecisionStatus.FAILED}:
                    missing += 1
                continue
            at = run.completed_at or run.started_at
            costs.append(
                LlmCost(
                    at=at,
                    amount=run.total_cost_usd,
                    source=f"decision_run:{run.id}",
                    run_key=row.run_key,
                )
            )
        performance = build_performance(
            opening=opening,
            end=end,
            fills=window_fills,
            flows=window_flows,
            marks=marks,
            llm_costs=costs,
            llm_cost_missing_count=missing,
            snapshot_anchors=anchors,
            provenance=_accounting_provenance(manifest),
        )
        return performance, None
    except (AccountingError, ManifestError) as exc:
        return None, str(exc)


def _snapshot_holdings_check(
    *, runs: Sequence[DecisionRun], portfolio: Portfolio, source: str
) -> dict:
    if source != "stored_ledger":
        return {
            "status": "not_applicable",
            "runs": [],
            "note": "The experiment uses a manifest-declared accounting fixture, not the ledger.",
        }
    all_fills = load_fills(portfolio)
    rows = []
    for run in runs:
        snapshot = getattr(run, "snapshot", None)
        if snapshot is None:
            continue
        declared: dict[str, Decimal] = {}
        for holding in snapshot.holdings or []:
            if not isinstance(holding, dict):
                continue
            ticker = str(holding.get("ticker") or "").upper()
            quantity = holding.get("quantity")
            if not ticker or quantity in (None, ""):
                continue
            try:
                declared[ticker] = Decimal(str(quantity))
            except (InvalidOperation, ValueError):
                continue
        try:
            ledger = positions_at(all_fills, snapshot.captured_at)
            ledger_view = {ticker: value for ticker, value in ledger.items() if value > ZERO}
        except AccountingError as exc:
            rows.append(
                {
                    "run_key": getattr(run, "fixture_id", "") or str(run.id),
                    "snapshot_at": utc_iso(snapshot.captured_at),
                    "declared_holdings": {k: str(v) for k, v in declared.items()},
                    "ledger_holdings": None,
                    "differences": None,
                    "status": "ledger_unavailable",
                    "reason": str(exc),
                }
            )
            continue
        declared_view = {ticker: value for ticker, value in declared.items() if value > ZERO}
        tickers = sorted(set(declared_view) | set(ledger_view))
        differences = {
            ticker: {
                "snapshot": str(declared_view.get(ticker, ZERO)),
                "ledger": str(ledger_view.get(ticker, ZERO)),
            }
            for ticker in tickers
            if declared_view.get(ticker, ZERO) != ledger_view.get(ticker, ZERO)
        }
        rows.append(
            {
                "run_key": getattr(run, "fixture_id", "") or str(run.id),
                "snapshot_at": utc_iso(snapshot.captured_at),
                "declared_holdings": {k: str(v) for k, v in declared_view.items()},
                "ledger_holdings": {k: str(v) for k, v in ledger_view.items()},
                "differences": differences,
                "status": "matched" if not differences else "mismatched",
            }
        )
    status = "no_snapshot"
    if rows:
        statuses = {row["status"] for row in rows}
        status = "mismatched" if "mismatched" in statuses else "matched"
    return {
        "status": status,
        "runs": rows,
        "note": (
            "InputSnapshot holdings are the only stored per-instrument quantities. They are "
            "compared with the ledger replay at the snapshot instant; a mismatch means the "
            "accounting and the decision inputs describe different portfolios."
        ),
    }


# ---------------------------------------------------------------------------
# Sample integrity, fairness, uncertainty
# ---------------------------------------------------------------------------
def _asset_universe(
    runs: Sequence[DecisionRun], performance: PortfolioPerformance | None
) -> list[str]:
    tickers = {run.ticker for run in runs if run.ticker}
    if performance is not None:
        tickers.update(fill.ticker for fill in performance.fills)
    return sorted(tickers)


def _eligibility(
    *,
    experiment: Experiment,
    manifest: dict,
    runs: Sequence[DecisionRun],
) -> dict:
    reasons: list[str] = []
    snapshot_checks = []
    for run in runs:
        snapshot = getattr(run, "snapshot", None)
        key = getattr(run, "fixture_id", "") or str(run.id)
        if snapshot is None:
            reasons.append(f"run {key} has no input snapshot")
            snapshot_checks.append({"run_key": key, "status": "missing_snapshot"})
            continue
        late_evidence = snapshot.evidence_items.filter(
            available_at__gt=snapshot.analysis_cutoff
        ).count()
        decision = _decision(run)
        decision_at = (
            decision.created_at if decision is not None else (run.completed_at or run.started_at)
        )
        cutoff_ok = snapshot.analysis_cutoff <= decision_at
        if not cutoff_ok:
            reasons.append(f"run {key} has an analysis cutoff after the decision it informed")
        if late_evidence:
            reasons.append(
                f"run {key} has {late_evidence} evidence item(s) available after its cutoff"
            )
        snapshot_checks.append(
            {
                "run_key": key,
                "status": "ok" if cutoff_ok and not late_evidence else "as_of_violation",
                "analysis_cutoff": utc_iso(snapshot.analysis_cutoff),
                "decision_at": utc_iso(decision_at),
                "late_evidence_count": late_evidence,
            }
        )
    if experiment.evaluation_mode != ExperimentEvaluationMode.HISTORICAL:
        reasons.append("the experiment is a forward run, not a historical reconstruction")
    if any(run.is_synthetic for run in runs):
        reasons.append("at least one stored run is a synthetic fixture")
    if any(run.mode == RunMode.HEURISTIC for run in runs):
        reasons.append("at least one stored run is a heuristic fallback, not an LLM run")
    if not runs:
        reasons.append("no stored runs are linked, so no historical claim is possible")

    honesty = manifest.get("honesty") or {}
    strict = not reasons
    return {
        "strict_historical_claims": strict,
        "reasons": reasons,
        "snapshot_checks": snapshot_checks,
        "survivorship_bias": honesty.get("survivorship_bias")
        or "not declared; the asset universe and any delisting/survivor selection are unknown",
        "pretraining_contamination": honesty.get("pretraining_contamination")
        or "not measurable from stored outputs; model pretraining data is not recorded",
        "missing_as_of_evidence": sum(
            1 for check in snapshot_checks if check.get("late_evidence_count")
        ),
        "note": (
            "A strict historical claim needs point-in-time snapshots and no evidence that "
            "became available after the run's cutoff. Missing as-of evidence makes the run "
            "ineligible rather than merely imprecise."
        ),
    }


def _fairness(
    *,
    experiment: Experiment,
    manifest: dict,
    variants: Sequence[ExperimentVariant],
    scheduled: Sequence[ExperimentRun],
    runs: Sequence[DecisionRun],
    performance: PortfolioPerformance | None,
    observations: Sequence[SignalObservation],
    synthetic: bool = False,
) -> dict:
    checks: list[dict] = []
    frozen = manifest.get("frozen") or {}

    # Instruments.
    declared_instruments = sorted({str(t).upper() for t in frozen.get("instruments") or []})
    observed_instruments = _asset_universe(runs, performance)
    if declared_instruments:
        checks.append(
            {
                "check": "instruments",
                "status": "match" if declared_instruments == observed_instruments else "mismatch",
                "declared": declared_instruments,
                "observed": observed_instruments,
            }
        )
    else:
        checks.append(
            {
                "check": "instruments",
                "status": "not_declared",
                "declared": [],
                "observed": observed_instruments,
            }
        )

    # Dates.
    declared_window = {
        "start": utc_iso(experiment.observation_start),
        "end": utc_iso(experiment.observation_end),
    }
    outside = [
        run
        for run in runs
        if not (experiment.observation_start <= run.started_at <= experiment.observation_end)
    ]
    checks.append(
        {
            "check": "dates",
            "status": "match"
            if runs and not outside
            else "mismatch"
            if outside
            else "not_verifiable",
            "declared": declared_window,
            "observed_run_count": len(runs),
            "runs_outside_window": [str(run.id) for run in outside],
        }
    )

    # Starting state.
    declared_state = frozen.get("starting_portfolio_state") or {}
    opening_cash = None
    opening_positions = None
    if performance is not None:
        opening_cash = performance.methodology.get("opening_cash_usd")
        opening_positions = performance.methodology.get("opening_positions")
    if declared_state and opening_cash is not None:
        declared_cash = declared_state.get("cash")
        cash_ok = declared_cash is None or Decimal(str(declared_cash)) == Decimal(str(opening_cash))
        declared_positions = {
            str(ticker).upper(): str(quantity)
            for ticker, quantity in (declared_state.get("positions") or {}).items()
        }
        observed_positions = {
            str(ticker).upper(): str(quantity)
            for ticker, quantity in (opening_positions or {}).items()
        }
        checks.append(
            {
                "check": "portfolio_starting_state",
                "status": "match"
                if cash_ok and declared_positions == observed_positions
                else "mismatch",
                "declared": declared_state,
                "observed": {
                    "cash": opening_cash,
                    "positions": observed_positions,
                },
                "basis": performance.methodology.get("cash_basis"),
            }
        )
    else:
        checks.append(
            {
                "check": "portfolio_starting_state",
                "status": "not_verifiable",
                "declared": declared_state,
                "observed": {"cash": opening_cash, "positions": opening_positions},
            }
        )

    # Risk rules.
    declared_rules = frozen.get("risk_rules") or {}
    observed_rules = {
        "max_trade_allocation_pct": str(experiment.portfolio.max_trade_allocation_pct),
        "max_portfolio_exposure_pct": str(experiment.portfolio.max_portfolio_exposure_pct),
        "max_ticker_concentration_pct": str(experiment.portfolio.max_ticker_concentration_pct),
        "daily_loss_limit_usd": str(experiment.portfolio.daily_loss_limit_usd),
    }
    if declared_rules:
        mismatches = [
            key
            for key, value in declared_rules.items()
            if key in observed_rules and str(value) != str(observed_rules[key])
        ]
        checks.append(
            {
                "check": "risk_rules",
                "status": "mismatch" if mismatches else "match",
                "declared": declared_rules,
                "observed": observed_rules,
                "mismatched_fields": mismatches,
                "note": (
                    "Compared with the portfolio's current configuration. Historical risk "
                    "configuration is not versioned, so a match here is necessary but not "
                    "sufficient for historical equality."
                ),
            }
        )
    else:
        checks.append(
            {
                "check": "risk_rules",
                "status": "not_declared",
                "declared": {},
                "observed": observed_rules,
            }
        )

    # Execution assumptions.
    declared_execution = frozen.get("execution_assumptions") or {}
    guard_versions = sorted(
        {
            guard.policy_version
            for run in runs
            if _decision(run) is not None
            for guard in _decision(run).guards.all()
            if guard.policy_version
        }
    )
    declared_guard = declared_execution.get("guard_policy_version")
    if declared_guard:
        checks.append(
            {
                "check": "execution_assumptions",
                "status": "match"
                if guard_versions and guard_versions == [declared_guard]
                else "mismatch"
                if guard_versions
                else "not_verifiable",
                "declared": declared_execution,
                "observed_guard_policy_versions": guard_versions,
            }
        )
    else:
        checks.append(
            {
                "check": "execution_assumptions",
                "status": "not_declared",
                "declared": declared_execution,
                "observed_guard_policy_versions": guard_versions,
            }
        )

    # Benchmark.
    declared_benchmark = manifest["benchmark"]
    observed_benchmarks = sorted(
        {row.benchmark_ticker for row in observations if row.benchmark_ticker}
    )
    declared_tickers = {str(declared_benchmark.get("ticker") or "").upper()}
    if declared_benchmark.get("asset_class") == "mixed":
        status = "declared_not_verifiable"
        note = (
            "A mixed portfolio declares an asset-appropriate benchmark rule; the report "
            "cannot verify per-asset benchmark coverage from stored rows alone."
        )
    elif observed_benchmarks:
        status = "match" if set(observed_benchmarks) <= declared_tickers else "mismatch"
        note = None
    else:
        status = "not_verifiable"
        note = "No stored outcome carries a benchmark."
    checks.append(
        {
            "check": "benchmark",
            "status": status,
            "declared": declared_benchmark,
            "observed_tickers": observed_benchmarks,
            "note": note,
        }
    )

    # Snapshot hashes declared per run.
    declared_hashes = frozen.get("snapshot_hashes") or {}
    if declared_hashes:
        mismatches = []
        for run in runs:
            key = getattr(run, "fixture_id", "") or str(run.id)
            expected = declared_hashes.get(key)
            actual = getattr(getattr(run, "snapshot", None), "content_hash", None)
            if expected and expected != actual:
                mismatches.append({"run_key": key, "declared": expected, "observed": actual})
        checks.append(
            {
                "check": "snapshots",
                "status": "mismatch" if mismatches else "match",
                "declared": declared_hashes,
                "mismatches": mismatches,
            }
        )
    else:
        checks.append(
            {
                "check": "snapshots",
                "status": "not_declared",
                "declared": {},
            }
        )

    # Model configurations.
    model_checks = []
    for variant in variants:
        declared = dict(variant.model_configuration or {})
        if not declared:
            continue
        recorded = _recorded_model_configuration(
            [run for run in runs if _run_variant(run, scheduled) == variant.key]
        )
        mismatches = [
            field
            for field, value in declared.items()
            if field in recorded
            and recorded.get(field) is not None
            and recorded.get(field) != value
        ]
        model_checks.append(
            {
                "variant": variant.key,
                "declared": declared,
                "recorded": recorded,
                "status": "mismatch" if mismatches else "match" if recorded else "not_verifiable",
                "mismatched_fields": mismatches,
            }
        )

    # Equal opportunity and equal budget. Unimplemented variants are declared
    # but not counted as zero-opportunity arms.
    scheduled_by_variant = {
        variant.key: sum(1 for row in scheduled if row.variant_id == variant.id)
        for variant in variants
        if variant.implemented
    }
    equal_opportunity = {
        "basis": "scheduled_runs_per_implemented_variant",
        "scheduled": dict(sorted(scheduled_by_variant.items())),
        "equal": len(set(scheduled_by_variant.values())) == 1 if scheduled_by_variant else False,
        "note": (
            "Equal opportunity means every implemented variant was scheduled on the same "
            "number of independent trade opportunities. Unimplemented variants (V4) are "
            "declared and excluded rather than counted as zero opportunities."
        ),
    }
    budgets = {variant.key: dict(variant.budget or {}) for variant in variants}
    declared_budget_sets = {json.dumps(value, sort_keys=True) for value in budgets.values()}
    observed_costs: dict[str, str | None] = {}
    for variant in variants:
        rows = [row for row in scheduled if row.variant_id == variant.id]
        known = [row.cost_usd for row in rows if row.cost_usd is not None]
        observed_costs[variant.key] = (
            str(sum(known, ZERO).quantize(Decimal("0.000001"))) if known else None
        )
    equal_budget = {
        "declared": budgets,
        "declared_equal": len(declared_budget_sets) == 1,
        "observed_cost_usd": observed_costs,
        "note": (
            "Equal budget compares declared caps, not observed spend; a variant that spent "
            "less did not necessarily have the same opportunity."
        ),
    }

    mismatches = [check["check"] for check in checks if check["status"] == "mismatch"]
    comparable = not mismatches and equal_opportunity["equal"]
    return {
        "checks": checks,
        "model_configuration_checks": model_checks,
        "comparable": comparable,
        "comparable_for_protocol_claims": comparable and not synthetic,
        "synthetic_content": synthetic,
        "mismatches": mismatches,
        "reasons": (
            []
            if comparable
            else [
                *[f"frozen check failed: {name}" for name in mismatches],
                *(
                    []
                    if equal_opportunity["equal"]
                    else ["variants do not have equal scheduled opportunities"]
                ),
            ]
        ),
        "equal_opportunity": equal_opportunity,
        "equal_budget": equal_budget,
        "frozen": frozen,
    }


def _decision(run: DecisionRun):
    try:
        return run.decision
    except Exception:
        return None


def _run_variant(run: DecisionRun, scheduled: Sequence[ExperimentRun]) -> str | None:
    for row in scheduled:
        if row.decision_run_id == run.id:
            return row.variant.key
    return None


def _sample_integrity(
    *,
    experiment: Experiment,
    manifest: dict,
    variants: Sequence[ExperimentVariant],
    scheduled: Sequence[ExperimentRun],
    runs: Sequence[DecisionRun],
    diagnostics: SignalDiagnosticsReport,
    performance: PortfolioPerformance | None,
) -> dict:
    by_status = Counter(row.status for row in scheduled)
    total = len(scheduled)
    completed = by_status.get(ExperimentRunStatus.COMPLETED, 0)
    failed = by_status.get(ExperimentRunStatus.FAILED, 0)
    cancelled = by_status.get(ExperimentRunStatus.CANCELLED, 0)
    not_executed = by_status.get(ExperimentRunStatus.NOT_EXECUTED, 0)
    requirements = manifest["sample_requirements"]
    schedule_met = total >= requirements["minimum_scheduled_runs"]
    completion_met = completed >= requirements["minimum_completed_runs"]
    diagnostics_dict = diagnostics.as_dict()
    unresolved = diagnostics_dict["unresolved"]["count"]
    modes = Counter(run.mode for run in runs)
    source_modes = Counter(run.source for run in runs)
    dates = [run.started_at for run in runs]
    scheduled_dates = [row.scheduled_at for row in scheduled]
    counts_by_variant = {}
    for variant in variants:
        rows = [row for row in scheduled if row.variant_id == variant.id]
        statuses = Counter(row.status for row in rows)
        counts_by_variant[variant.key] = {
            "scheduled": len(rows),
            "completed": statuses.get(ExperimentRunStatus.COMPLETED, 0),
            "failed": statuses.get(ExperimentRunStatus.FAILED, 0),
            "cancelled": statuses.get(ExperimentRunStatus.CANCELLED, 0),
            "not_executed": statuses.get(ExperimentRunStatus.NOT_EXECUTED, 0),
            "completion_rate_pct": _rate(statuses.get(ExperimentRunStatus.COMPLETED, 0), len(rows)),
            "failure_rate_pct": _rate(statuses.get(ExperimentRunStatus.FAILED, 0), len(rows)),
        }
    return {
        "counts": {
            "scheduled": total,
            "attempted": sum(1 for row in scheduled if row.was_attempted),
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "not_executed": not_executed,
        },
        "counts_by_variant": counts_by_variant,
        "completion_rate_pct": _rate(completed, total),
        "failure_rate_pct": _rate(failed, total),
        "not_executed_rate_pct": _rate(not_executed, total),
        "denominator": {
            "rule": "all_scheduled_runs",
            "scheduled": total,
            "note": (
                "Every scheduled run remains in the completion, failure and cost denominator, "
                "including deliberate failures and runs that produced no stored output."
            ),
        },
        "date_range": {
            "window_start": utc_iso(experiment.observation_start),
            "window_end": utc_iso(experiment.observation_end),
            "scheduled_first": utc_iso(min(scheduled_dates)) if scheduled_dates else None,
            "scheduled_last": utc_iso(max(scheduled_dates)) if scheduled_dates else None,
            "decisions_first": utc_iso(min(dates)) if dates else None,
            "decisions_last": utc_iso(max(dates)) if dates else None,
        },
        "asset_universe": _asset_universe(runs, performance),
        "source_mode": (
            "synthetic"
            if modes.get(RunMode.SYNTHETIC)
            else modes.most_common(1)[0][0]
            if modes
            else "none"
        ),
        "mode_counts": dict(sorted(modes.items())),
        "source_counts": dict(sorted(source_modes.items())),
        "cohorts": {
            key: diagnostics_dict["cohorts"][key]["runs"]
            for key in ("heuristic", "llm", "synthetic")
        },
        "unresolved_observations": unresolved,
        "unresolved_by_maturity": diagnostics_dict["unresolved"]["by_maturity"],
        "runs_without_outcomes": diagnostics_dict["unresolved"]["runs_without_outcomes"],
        "minimum_usable_sample": {
            **requirements,
            "met": schedule_met and completion_met,
            "scheduled_met": schedule_met,
            "completed_met": completion_met,
        },
        "eligibility": _eligibility(experiment=experiment, manifest=manifest, runs=runs),
        "note": (
            "Counts, date range, asset universe, source mode and unresolved observations "
            "travel with every result so a reader can judge the sample before the number."
        ),
    }


def _uncertainty_block(
    *,
    manifest: dict,
    primary: dict,
    performance: PortfolioPerformance | None,
    sample_integrity: dict,
) -> dict:
    requirements = manifest["sample_requirements"]
    ceiling = manifest["cost_ceiling_usd"]
    acceptable = manifest["acceptable_drawdown_pct"]
    sample_met = sample_integrity["minimum_usable_sample"]["met"]
    cost_observed = None
    cost_complete = False
    if performance is not None:
        cost_observed = performance.llm_costs.get("recorded_cost_usd")
        cost_complete = bool(performance.llm_costs.get("costs_complete"))
    if ceiling is None:
        cost_gate = {"status": "not_declared", "ceiling_usd": None}
    elif cost_observed is None or not cost_complete:
        cost_gate = {
            "status": "not_computable",
            "ceiling_usd": str(ceiling),
            "observed_usd": cost_observed,
            "reason": "cost was not reported for every scheduled run",
        }
    else:
        observed = Decimal(str(cost_observed))
        cost_gate = {
            "status": "within" if observed <= ceiling else "exceeded",
            "ceiling_usd": str(ceiling),
            "observed_usd": str(observed),
        }
    drawdown_observed = None
    if performance is not None:
        drawdown_observed = performance.drawdown.get("max_drawdown_pct")
    if acceptable is None:
        drawdown_gate = {"status": "not_declared", "acceptable_pct": None}
    elif drawdown_observed is None:
        drawdown_gate = {
            "status": "not_computable",
            "acceptable_pct": str(acceptable),
            "observed_pct": None,
            "reason": "no priced equity observations",
        }
    else:
        observed = abs(Decimal(str(drawdown_observed)))
        drawdown_gate = {
            "status": "within" if observed <= acceptable else "exceeded",
            "acceptable_pct": str(acceptable),
            "observed_pct": str(observed),
        }
    return {
        "pre_registration": {
            "primary_metric": manifest["primary_metric"],
            "observation_window": {
                "start": utc_iso(manifest["observation_start"]),
                "end": utc_iso(manifest["observation_end"]),
            },
            "minimum_usable_sample": requirements,
            "cost_ceiling_usd": None if ceiling is None else str(ceiling),
            "acceptable_drawdown_pct": None if acceptable is None else str(acceptable),
            "declared_before_results": True,
        },
        "method": {
            **manifest["uncertainty"],
            "why_time_blocks": (
                "Adjacent decisions share market conditions and some windows overlap, so "
                "individual observations are dependent. Resampling contiguous blocks keeps "
                "that dependence instead of pretending the sample is independent."
            ),
        },
        "primary_metric": primary,
        "gates": {
            "sample": {
                "status": "met" if sample_met else "not_met",
                **requirements,
            },
            "cost": cost_gate,
            "drawdown": drawdown_gate,
        },
        "profitability_threshold": None,
        "no_fixed_profitability_threshold": True,
        "note": (
            "Intervals are published for every estimated metric. A small sample can validate "
            "the engineering, not a profit claim; no fixed profitability threshold is asserted."
        ),
    }


def _performance_limitations(performance: PortfolioPerformance | None) -> list[dict]:
    if performance is None:
        return [
            {
                "code": "portfolio_performance_unavailable",
                "detail": "Accounting could not be built from the stored ledger/fixture",
                "impact": "portfolio performance is null, not zero",
            }
        ]
    limitations: list[dict] = []
    if performance.approximate:
        fallback = performance.twr.get("method") != TWR_METHOD
        limitations.append(
            {
                "code": "approximate_twr",
                "detail": "TWR is approximate: "
                + "; ".join(performance.methodology["approximate_reasons"]),
                "impact": (
                    "the exact flow-boundary chain was unavailable; Modified Dietz was used"
                    if fallback
                    else "recorded fees/marks are incomplete, so even the exact chain is approximate"
                ),
            }
        )
    if not performance.fees["fees_complete"]:
        limitations.append(
            {
                "code": "fees_unrecorded",
                "detail": f"{performance.fees['unknown_fee_fill_count']} fill(s) have no recorded fee",
                "impact": "net equity excludes unknown fees and is flagged approximate",
            }
        )
    if not performance.llm_costs["costs_complete"]:
        limitations.append(
            {
                "code": "llm_cost_incomplete",
                "detail": f"{performance.llm_costs['missing_cost_run_count']} run(s) reported no cost",
                "impact": "net-of-LLM return is null; no zero-cost assumption is made",
            }
        )
    for flag in performance.flags:
        if flag in {"unpriced_equity_points", "snapshot_reconciliation_mismatch", "stale_marks"}:
            limitations.append(
                {
                    "code": flag,
                    "detail": "See portfolio_performance.methodology and reconciliation",
                    "impact": "a measurement is missing or approximate",
                }
            )
    if performance.reconciliation["status"] == "mismatched":
        limitations.append(
            {
                "code": "snapshot_reconciliation_mismatch",
                "detail": "The ledger replay does not agree with a stored portfolio snapshot",
                "impact": (
                    "the cash basis or the stored snapshot is wrong; portfolio performance is "
                    "not reliable until resolved"
                ),
            }
        )
    return limitations


def _limitations(
    *,
    manifest: dict,
    performance: PortfolioPerformance | None,
    diagnostics: SignalDiagnosticsReport,
    integrity: dict,
    variants: Sequence[ExperimentVariant],
) -> list[dict]:
    limitations: list[dict] = [
        {
            "code": "per_decision_not_summable",
            "detail": (
                "Per-decision signal scores cover overlapping windows and unequal sizes; they "
                "are never summed into portfolio performance."
            ),
            "impact": "metric-family separation",
        }
    ]
    diagnostics_dict = diagnostics.as_dict()
    unmatched = sum(
        summary.get("unmatched_benchmark_rows_excluded", 0)
        for summary in diagnostics_dict["summary_by_horizon"].values()
    )
    if unmatched:
        limitations.append(
            {
                "code": "benchmark_unmatched_rows",
                "detail": (
                    f"{unmatched} evaluated observation(s) have no matched benchmark and are "
                    "excluded from relative returns"
                ),
                "impact": "relative-return sample is smaller than the observed sample",
            }
        )
    limitations.extend(_performance_limitations(performance))

    eligibility = integrity["eligibility"]
    if not eligibility["strict_historical_claims"]:
        limitations.append(
            {
                "code": "not_strict_historical",
                "detail": "Strict historical claims are ineligible: "
                + "; ".join(eligibility["reasons"]),
                "impact": "results are engineering evidence, not proof of historical performance",
            }
        )
    synthetic = (
        bool(manifest["honesty"].get("all_runs_synthetic"))
        or integrity["source_mode"] == "synthetic"
    )
    if synthetic:
        limitations.append(
            {
                "code": "synthetic_content",
                "detail": "Stored runs or accounting inputs are synthetic fixtures",
                "impact": "no comparative or profitability claim may be drawn from this report",
            }
        )
    if not integrity["minimum_usable_sample"]["met"]:
        limitations.append(
            {
                "code": "sample_below_minimum",
                "detail": "The scheduled/completed sample is below the pre-registered minimum",
                "impact": "engineering validation only; variant estimates are unstable",
            }
        )
    for variant in variants:
        if not variant.implemented:
            limitations.append(
                {
                    "code": f"variant_{variant.key}_unimplemented",
                    "detail": variant.unimplemented_reason or "declared but unimplemented",
                    "impact": f"{variant.key} result is null, not a measured zero",
                }
            )
    return limitations


# ---------------------------------------------------------------------------
# Variant results
# ---------------------------------------------------------------------------
def _variant_result(
    *,
    variant: ExperimentVariant,
    scheduled: Sequence[ExperimentRun],
    performance: PortfolioPerformance | None,
    observations: Sequence[SignalObservation],
    manifest: dict,
    experiment: Experiment,
) -> dict:
    rows = [row for row in scheduled if row.variant_id == variant.id]
    statuses = Counter(row.status for row in rows)
    completed = statuses.get(ExperimentRunStatus.COMPLETED, 0)
    failed = statuses.get(ExperimentRunStatus.FAILED, 0)
    cancelled = statuses.get(ExperimentRunStatus.CANCELLED, 0)
    not_executed = statuses.get(ExperimentRunStatus.NOT_EXECUTED, 0)
    attempted = completed + failed + cancelled
    linked = _linked_runs(rows)
    variant_observations = [row for row in observations if _run_variant_id(row.run_id, rows)]
    diagnostics = summarize_observations(
        variant_observations,
        primary_horizon=manifest["primary_metric"]["horizon_days"],
        benchmark_policy={"declared": manifest["benchmark"]},
        generated_at=experiment.report_generated_at,
    )
    diagnostics_dict = diagnostics.as_dict()
    operational = _operational_metrics(linked, rows)
    claims = _claims_summary(linked)
    reb = _action_change_summary(linked)
    trades = _variant_trade_summary(linked, performance)
    primary = _primary_metric(
        metric={
            **manifest["primary_metric"],
            **manifest["sample_requirements"],
            "minimum_observations": manifest["sample_requirements"]["minimum_observations"],
        },
        observations=variant_observations,
        performance=None,
        uncertainty=manifest["uncertainty"],
    )
    if not variant.implemented:
        status = "unimplemented"
        reason = variant.unimplemented_reason
    elif not rows:
        status = "no_scheduled_runs"
        reason = "No run was scheduled for this variant."
    elif completed == 0:
        status = "no_completed_runs"
        reason = "Scheduled runs exist but none completed."
    else:
        status = "measured"
        reason = None
    return {
        "key": variant.key,
        "label": variant.label,
        "description": variant.description,
        "engine": variant.engine,
        "protocol_version": variant.protocol_version,
        "requires_rebuttals": variant.requires_rebuttals,
        "implemented": variant.implemented,
        "unimplemented_reason": variant.unimplemented_reason or None,
        "status": status,
        "status_reason": reason,
        "model_configuration": _model_configuration(variant),
        "recorded_model_configuration": _recorded_model_configuration(linked),
        "budget": dict(variant.budget or {}),
        "counts": {
            "scheduled": len(rows),
            "attempted": attempted,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "not_executed": not_executed,
        },
        "completion_rate_pct": _rate(completed, len(rows)),
        "failure_rate_pct": _rate(failed, len(rows)),
        "denominator": {"rule": "all_scheduled_runs", "scheduled": len(rows)},
        "operational": operational,
        "signal_diagnostics": {
            "method_version": DIAGNOSTICS_METHOD_VERSION,
            "summary_by_horizon": diagnostics_dict["summary_by_horizon"],
            "unresolved": diagnostics_dict["unresolved"],
            "proposal_vs_executed": diagnostics_dict["proposal_vs_executed"],
            "counterfactual_rows": len(diagnostics_dict["counterfactual"]["rows"]),
            "rows_count": len(diagnostics_dict["rows"]),
            "note": diagnostics_dict["note"],
        },
        "portfolio_performance": {
            "available": False,
            "value": None,
            "reason": (
                "Variants were not executed in isolated accounts, and per-decision scores "
                "cannot be summed into a portfolio return. Portfolio performance is published "
                "once for the experiment account and is not attributed to a variant."
            ),
            "experiment_level_reference": "portfolio_performance",
        },
        "turnover": trades,
        "evidence": claims,
        "action_changes_after_rebuttal": reb,
        "primary_metric": primary,
        "notes": [
            "Signal results are variant-level; portfolio performance is not.",
            "A variant with zero completed runs has distinct null results, not zero results.",
        ],
    }


def _run_variant_id(run_id: str, rows: Sequence[ExperimentRun]) -> bool:
    return any(str(row.decision_run_id) == str(run_id) for row in rows if row.decision_run_id)


# ---------------------------------------------------------------------------
# Rejected proposals: counterfactuals, never executed performance
# ---------------------------------------------------------------------------
def _counterfactual_rule(manifest: dict) -> CounterfactualRule | None:
    block = (manifest["accounting"] or {}).get("counterfactual_rule")
    if not block:
        return None
    if not isinstance(block, dict):
        raise ManifestError("accounting.counterfactual_rule must be an object when declared.")
    return CounterfactualRule(
        holding_period_days=_int(
            block.get("holding_period_days"),
            field_name="counterfactual_rule.holding_period_days",
            default=7,
        ),
        horizons_days=tuple(
            _int(value, field_name="counterfactual_rule.horizons_days")
            for value in (block.get("horizons_days") or [1, 7, 30])
        ),
        exit_rule=str(block.get("exit_rule") or "first_known_mark_at_or_after_horizon.v1"),
        quantity_basis=str(block.get("quantity_basis") or "allowed_quantity"),
        include_fees=bool(block.get("include_fees", False)),
    )


def _counterfactuals(
    *,
    manifest: dict,
    scheduled: Sequence[ExperimentRun],
    runs: Sequence[DecisionRun],
    performance: PortfolioPerformance | None,
) -> dict:
    """Measure guard-rejected proposals under a declared rule, or not at all."""
    try:
        rule = _counterfactual_rule(manifest)
    except AccountingError as exc:
        return {
            "rule": None,
            "rows": [],
            "established_count": 0,
            "reason": str(exc),
            "note": "No counterfactual was computed.",
        }

    if manifest["accounting"]["source"] == "manifest_fixture":
        marks = _fixture_marks(manifest["accounting"])
    else:
        marks = _stored_marks(
            runs=runs,
            start=manifest["observation_start"],
            end=manifest["observation_end"],
        )
    book = PriceBook(marks)
    rows = []
    established = 0
    for row in scheduled:
        run = row.decision_run
        if run is None:
            continue
        state = execution_state_for_run(run)
        if not state.counterfactual:
            continue
        decision = _decision(run)
        guard = None
        if decision is not None:
            guards = list(decision.guards.all())
            guard = max(guards, key=lambda item: item.created_at) if guards else None
        quantity = ZERO
        if rule is not None and rule.quantity_basis == "allowed_quantity" and guard is not None:
            quantity = guard.allowed_quantity
        elif decision is not None and decision.requested_quantity is not None:
            quantity = decision.requested_quantity
        elif guard is not None:
            quantity = guard.allowed_quantity
        decision_at = getattr(decision, "created_at", None) or run.started_at
        result = evaluate_counterfactual(
            ticker=run.ticker,
            decision_at=decision_at,
            action=state.proposal_action or "",
            quantity=quantity,
            marks=book,
            rule=rule,
            as_of=manifest.get("as_of"),
        ).as_dict()
        if result["established"]:
            established += 1
        rows.append(
            {
                "run_key": row.run_key,
                "variant": row.variant.key,
                "decision_run_id": str(run.id),
                "proposal": state.as_dict(),
                "counterfactual": result,
                "portfolio_effect": "none_no_fill",
                "note": (
                    "The guard rejected this proposal, so it has no transaction and no "
                    "portfolio return. This block is a hypothetical path, not performance."
                ),
            }
        )
    return {
        "rule": rule.as_dict() if rule else None,
        "rows": rows,
        "rejected_proposal_count": len(rows),
        "established_count": established,
        "not_established_count": len(rows) - established,
        "note": (
            "A rejected proposal is measured only as a counterfactual under a declared "
            "holding/exit rule. Without one, loss prevention is not established."
        ),
    }


# ---------------------------------------------------------------------------
# Public report builders
# ---------------------------------------------------------------------------
def _rehydrate_manifest(experiment: Experiment) -> dict:
    """Restore datetime fields that JSON storage necessarily made strings."""
    manifest = dict(experiment.manifest or {})
    for key in ("declared_at", "observation_start", "observation_end", "as_of"):
        value = manifest.get(key)
        if isinstance(value, str) and value:
            manifest[key] = _parse_at(value, field_name=f"manifest.{key}")
    for key in ("cost_ceiling_usd", "acceptable_drawdown_pct"):
        value = manifest.get(key)
        if isinstance(value, str) and value:
            manifest[key] = Decimal(value)
    runs = []
    for entry in manifest.get("runs") or []:
        item = dict(entry)
        if isinstance(item.get("scheduled_at"), str):
            item["scheduled_at"] = _parse_at(
                item["scheduled_at"], field_name="manifest.runs[].scheduled_at"
            )
        runs.append(item)
    manifest["runs"] = runs
    return manifest


def build_report(experiment: Experiment, *, now: datetime | None = None) -> dict:
    """Assemble the versioned report from stored rows only (no model calls)."""
    now = now or timezone.now()
    manifest = _rehydrate_manifest(experiment)
    variants = list(experiment.variants.order_by("display_order", "key"))
    scheduled = list(
        experiment.scheduled_runs.select_related("variant", "decision_run").order_by(
            "scheduled_at", "run_key"
        )
    )
    run_queryset = (
        DecisionRun.objects.filter(
            pk__in=[row.decision_run_id for row in scheduled if row.decision_run_id]
        )
        .select_related(
            "decision", "legacy_log", "legacy_log__transaction", "snapshot", "portfolio"
        )
        .prefetch_related(
            "decision__outcomes",
            "decision__guards",
            "claims__evidence",
            "rebuttals",
            "legacy_log__outcomes",
        )
        .order_by("started_at", "id")
    )
    runs = list(run_queryset)

    as_of = manifest.get("as_of") or now
    diagnostics = build_signal_diagnostics(
        runs=runs,
        portfolio=experiment.portfolio,
        primary_horizon=manifest["primary_metric"]["horizon_days"],
        as_of=as_of,
        benchmark_policy={"declared": manifest["benchmark"], "rules": BENCHMARK_RULES},
        generated_at=now,
    )
    diagnostics_dict = diagnostics.as_dict()
    # Signal rows carry the stored run's natural key (fixture id or UUID). The
    # experiment's scheduled run_key is the identifier a reader sees in the
    # manifest, so expose both rather than silently renaming one to the other.
    scheduled_keys = {
        str(row.decision_run_id): row.run_key for row in scheduled if row.decision_run_id
    }
    for row in diagnostics_dict["rows"]:
        row["stored_run_key"] = row["run_key"]
        row["run_key"] = scheduled_keys.get(row["run_id"], row["run_key"])
    key_aliases = {row["stored_run_key"]: row["run_key"] for row in diagnostics_dict["rows"]}
    diagnostics_dict["unresolved"]["runs"] = [
        key_aliases.get(key, key) for key in diagnostics_dict["unresolved"]["runs"]
    ]
    performance, accounting_error = _build_accounting(
        experiment, manifest=manifest, runs=runs, now=now
    )
    performance_dict = performance.as_dict() if performance is not None else None
    if performance_dict is not None:
        holdings_check = _snapshot_holdings_check(
            runs=runs, portfolio=experiment.portfolio, source=manifest["accounting"]["source"]
        )
        performance_dict["reconciliation"]["input_snapshots"] = holdings_check

    integrity = _sample_integrity(
        experiment=experiment,
        manifest=manifest,
        variants=variants,
        scheduled=scheduled,
        runs=runs,
        diagnostics=diagnostics,
        performance=performance,
    )
    fairness = _fairness(
        experiment=experiment,
        manifest=manifest,
        variants=variants,
        scheduled=scheduled,
        runs=runs,
        performance=performance,
        observations=diagnostics.observations,
        synthetic=integrity["source_mode"] == "synthetic",
    )
    primary = _primary_metric(
        metric={
            **manifest["primary_metric"],
            **manifest["sample_requirements"],
            "minimum_observations": manifest["sample_requirements"]["minimum_observations"],
        },
        observations=diagnostics.observations,
        performance=performance,
        uncertainty=manifest["uncertainty"],
    )
    requirement = manifest["sample_requirements"]["minimum_observations"]
    observations_met = primary["observations"] >= requirement if requirement else True
    integrity["minimum_usable_sample"].update(
        {
            "observations_available": primary["observations"],
            "observations_met": observations_met,
            "met": integrity["minimum_usable_sample"]["met"] and observations_met,
        }
    )
    uncertainty = _uncertainty_block(
        manifest=manifest,
        primary=primary,
        performance=performance,
        sample_integrity=integrity,
    )
    counterfactuals = _counterfactuals(
        manifest=manifest,
        scheduled=scheduled,
        runs=runs,
        performance=performance,
    )
    variant_results = [
        _variant_result(
            variant=variant,
            scheduled=scheduled,
            performance=performance,
            observations=diagnostics.observations,
            manifest=manifest,
            experiment=experiment,
        )
        for variant in variants
    ]
    limitations = _limitations(
        manifest=manifest,
        performance=performance,
        diagnostics=diagnostics,
        integrity=integrity,
        variants=variants,
    )

    metric_families = {
        "signal_diagnostics": {
            "description": "Forward returns, signed action scores and directional hit rates.",
            "analytical": True,
            "summable": False,
            "sum_rejected_reason": diagnostics_dict["definition"]["sum_rejected_reason"],
            "method_version": DIAGNOSTICS_METHOD_VERSION,
            "source": "stored DecisionRun outcomes",
        },
        "portfolio_performance": {
            "description": "Marked-to-market equity from cash, holdings, fills, fees and flows.",
            "analytical": False,
            "summable_within_family": True,
            "method_version": ACCOUNTING_METHOD_VERSION,
            "source": "ledger/fixture replay; never per-decision scores",
        },
        "separation": (
            "The two families are published in separate blocks with separate method versions. "
            "No field in this report is a sum of per-decision scores presented as account return."
        ),
    }

    synthetic = (
        integrity["source_mode"] == "synthetic"
        or bool(manifest["honesty"].get("all_runs_synthetic"))
        or bool(performance.provenance.get("synthetic") if performance else False)
    )
    engineering_only = synthetic or not integrity["minimum_usable_sample"]["met"]
    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": utc_iso(now),
        "experiment": {
            "experiment_id": experiment.experiment_key,
            "experiment_uuid": str(experiment.id),
            "label": experiment.label,
            "description": experiment.description,
            "declared_at": utc_iso(experiment.declared_at),
            "evaluation_mode": experiment.evaluation_mode,
            "frozen": experiment.frozen,
            "observation_window": {
                "start": utc_iso(experiment.observation_start),
                "end": utc_iso(experiment.observation_end),
            },
        },
        "manifest": {
            "manifest_version": experiment.manifest_version,
            "schema_version": experiment.schema_version,
            "checksum": experiment.manifest_checksum,
            "source_path": manifest.get("source_path"),
            "honesty": manifest.get("honesty") or {},
            "benchmark": manifest["benchmark"],
            "accounting_source": manifest["accounting"]["source"],
        },
        "metric_families": metric_families,
        "signal_diagnostics": diagnostics_dict,
        "counterfactuals": counterfactuals,
        "portfolio_performance": performance_dict
        if performance_dict is not None
        else {
            "metric_family": "portfolio_performance",
            "available": False,
            "reason": accounting_error or "accounting unavailable",
            "method_version": ACCOUNTING_METHOD_VERSION,
            "note": "A missing accounting result is null, never zero.",
        },
        "variants": variant_results,
        "sample_integrity": integrity,
        "fairness": fairness,
        "uncertainty": uncertainty,
        "limitations": limitations,
        "no_profitability_claim": True,
        "profitability_threshold": None,
        "engineering_validation_only": engineering_only,
        "report_version": 0,
    }
    return report


def replay_experiment(experiment: Experiment, *, now: datetime | None = None) -> dict:
    """Rebuild the report from stored output and persist it under a new version."""
    now = now or timezone.now()
    report = build_report(experiment, now=now)
    with transaction.atomic():
        locked = Experiment.objects.select_for_update().get(pk=experiment.pk)
        version = locked.report_version + 1
        report["report_version"] = version
        report["generated_at"] = utc_iso(now)
        locked.report = json_safe(report)
        locked.report_version = version
        locked.report_generated_at = now
        locked.status = ExperimentStatus.READY
        locked.save(
            update_fields=[
                "report",
                "report_version",
                "report_generated_at",
                "status",
                "updated_at",
            ]
        )
    experiment.refresh_from_db()
    return experiment.report


# ---------------------------------------------------------------------------
# Resolve scheduled runs against stored outputs
# ---------------------------------------------------------------------------
def _resolve_run(reference: dict, portfolio: Portfolio) -> tuple[DecisionRun | None, str | None]:
    if reference["decision_run_id"]:
        try:
            run = DecisionRun.objects.filter(
                pk=reference["decision_run_id"], portfolio=portfolio
            ).first()
        except Exception:
            run = None
        if run is None:
            return None, "decision_run_not_found"
        return run, None
    run = DecisionRun.objects.filter(
        fixture_id=reference["fixture_id"], portfolio=portfolio
    ).first()
    if run is None:
        return None, "fixture_run_not_found"
    return run, None


def _status_for(run: DecisionRun | None, reference: dict) -> tuple[str, str, str]:
    if run is None:
        return (
            ExperimentRunStatus.NOT_EXECUTED,
            reference["failure_code"] or "scheduled_run_not_found",
            reference["failure_detail"]
            or "The manifest named a stored run that is not present for this portfolio.",
        )
    if run.status == DecisionStatus.COMPLETED:
        return ExperimentRunStatus.COMPLETED, "", ""
    if run.status == DecisionStatus.FAILED:
        return (
            ExperimentRunStatus.FAILED,
            run.error_code or reference["failure_code"] or "run_failed",
            run.error_detail or reference["failure_detail"],
        )
    if run.status == DecisionStatus.CANCELLED:
        return (
            ExperimentRunStatus.CANCELLED,
            run.error_code or reference["failure_code"] or "run_cancelled",
            run.error_detail or reference["failure_detail"],
        )
    return (
        ExperimentRunStatus.NOT_EXECUTED,
        "run_not_terminal",
        f"The stored run is still {run.status!r}; a non-terminal run cannot be measured.",
    )


def _run_metadata(run: DecisionRun | None, reference: dict, variant: dict) -> dict:
    metadata = {
        "reference": reference["decision_run_id"] or reference["fixture_id"],
        "note": reference["note"] or None,
        "expected_failure": reference["expected_failure"],
    }
    if run is None:
        return metadata
    metadata["stored_status"] = run.status
    metadata["mode"] = run.mode
    metadata["source"] = run.source
    violations = []
    if reference["expected_failure"] and run.status == DecisionStatus.COMPLETED:
        violations.append("expected_failure_not_observed")
    if variant["requires_rebuttals"] and not run.rebuttals.exists():
        violations.append("variant_requires_rebuttals_but_none_recorded")
    metadata["expectation_violations"] = violations
    return metadata


def build_experiment(
    *,
    manifest: dict,
    portfolio: Portfolio,
    force: bool = False,
    now: datetime | None = None,
) -> tuple[Experiment, dict]:
    """Create or replay an experiment from a validated (or raw) manifest."""
    normalised = (
        manifest
        if isinstance(manifest, dict) and manifest.get("normalised") is True
        else validate_manifest(manifest)
    )
    if portfolio is None:
        raise ManifestError("An experiment needs a portfolio.")
    checksum = normalised["checksum"]
    now = now or timezone.now()

    with transaction.atomic():
        existing = Experiment.objects.filter(
            portfolio=portfolio, experiment_key=normalised["experiment_id"]
        ).first()
        if existing is not None and existing.manifest_checksum != checksum and not force:
            raise ManifestError(
                f"Experiment {normalised['experiment_id']!r} already exists with a different "
                f"manifest checksum ({existing.manifest_checksum} != {checksum}). Re-run with "
                "--force to replace it."
            )
        experiment = existing or Experiment(
            portfolio=portfolio,
            experiment_key=normalised["experiment_id"],
        )
        experiment.label = normalised["label"]
        experiment.description = normalised["description"]
        experiment.manifest_version = normalised["manifest_version"]
        experiment.schema_version = normalised["schema_version"]
        experiment.manifest_checksum = checksum
        experiment.declared_at = normalised["declared_at"]
        experiment.frozen = True
        experiment.observation_start = normalised["observation_start"]
        experiment.observation_end = normalised["observation_end"]
        experiment.evaluation_mode = normalised["evaluation_mode"]
        experiment.primary_metric = normalised["primary_metric"]
        experiment.sample_requirements = normalised["sample_requirements"]
        experiment.cost_ceiling_usd = normalised["cost_ceiling_usd"]
        experiment.acceptable_drawdown_pct = normalised["acceptable_drawdown_pct"]
        experiment.manifest = json_safe(normalised)
        experiment.status = ExperimentStatus.DECLARED
        experiment.save()

        variants: dict[str, ExperimentVariant] = {}
        for entry in normalised["variants"]:
            variant, _ = ExperimentVariant.objects.update_or_create(
                experiment=experiment,
                key=entry["key"],
                defaults={
                    "label": entry["label"],
                    "description": entry["description"],
                    "engine": entry["engine"],
                    "protocol_version": entry["protocol_version"],
                    "requires_rebuttals": entry["requires_rebuttals"],
                    "implemented": entry["implemented"],
                    "unimplemented_reason": entry["unimplemented_reason"],
                    "model_configuration": entry["model_configuration"],
                    "budget": entry["budget"],
                    "display_order": entry["display_order"],
                },
            )
            variants[entry["key"]] = variant
        ExperimentVariant.objects.filter(experiment=experiment).exclude(
            key__in=list(variants)
        ).delete()

        seen_keys = []
        for reference in normalised["runs"]:
            variant_entry = next(
                entry for entry in normalised["variants"] if entry["key"] == reference["variant"]
            )
            run, resolution_error = _resolve_run(reference, portfolio)
            status, failure_code, failure_detail = _status_for(run, reference)
            if resolution_error and not failure_detail:
                failure_detail = resolution_error
            seen_keys.append(reference["run_key"])
            ExperimentRun.objects.update_or_create(
                experiment=experiment,
                run_key=reference["run_key"],
                defaults={
                    "variant": variants[reference["variant"]],
                    "scheduled_at": reference["scheduled_at"],
                    "status": status,
                    "decision_run": run,
                    "failure_code": failure_code[:64],
                    "failure_detail": failure_detail,
                    "cost_usd": run.total_cost_usd if run is not None else None,
                    "latency_ms": run.latency_ms if run is not None else None,
                    "input_tokens": run.input_tokens if run is not None else None,
                    "output_tokens": run.output_tokens if run is not None else None,
                    "metadata": _run_metadata(run, reference, variant_entry),
                },
            )
        ExperimentRun.objects.filter(experiment=experiment).exclude(run_key__in=seen_keys).delete()

    report = replay_experiment(experiment, now=now)
    return experiment, report
