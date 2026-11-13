"""The V0-V3 experiment execution path (FIX-07B).

Why this module exists
----------------------
``services.experiments`` could *measure* an experiment but could not *run* one:
V0-V4 were declared, reports resolved references to stored runs, and nothing
scheduled a real arm. A comparison therefore had no producer, and a manifest that
named runs no one had executed published them as ``not_executed`` - honest, but
not an experiment.

This module schedules the four implemented arms against one frozen opportunity
set:

======================  ==========================  ===================================
Arm                     Protocol                    Configuration
======================  ==========================  ===================================
``V0``                  ``heuristic_fallback.v1``   deterministic engine, no model call
``V1``                  ``single_agent_baseline.v1`` one analyst, no cases, no rebuttal
``V2``                  ``debate.no_rebuttal.v1``   Bull / Bear / CIO, no response round
``V3``                  ``debate.v1``               the Phase 2 protocol, with rebuttal
======================  ==========================  ===================================

``V4`` (portfolio allocation) is declared and **not executed**: no V4 arm has
been scheduled or measured. It is excluded from the equal-opportunity
denominator rather than counted as a zero-opportunity arm.

What is frozen and shared
-------------------------
Every arm runs against the **same** ``InputSnapshot`` per opportunity. The
snapshot is created once from the opportunity set and passed to the protocol, so
the content hash on every run is identical by construction and verified in the
report rather than asserted. The opportunity set, its marks, the starting state,
the risk rules and the execution assumptions are content-hashed; the arms differ
in their protocol, in their decisions and therefore in their fills and equity.

What the report will not claim
------------------------------
* When no model client is configured, V1-V3 stay ``not_executed`` with a reason
  and remain in the denominator. A synthetic case is never relabelled as an
  executed arm.
* With the scripted offline client, the runs are recorded ``mode="simulated"``
  and the report says ``engineering_validation_only``. A scripted provider is not
  a model sample; the code path is real, the model output is not.
* Per-arm accounting is a **simulation** of that arm's decisions under the same
  guard, fill rule and accounting method. It is not the account's realised
  history and it is labelled as simulated.
* The pre-registered primary metric is read from the registration and computed
  from the frozen marks when they reach its horizon. A horizon no mark reaches
  is ``null`` with the missing input named, never a number built from something
  else.
* Every execution has a deterministic namespace (portfolio, opportunity-set
  content hash, provider mode and client identity), so two experiments cannot
  read or overwrite each other's decisions, snapshots or spend.
* The report embeds its recorded inputs, so
  ``python manage.py rebuild_experiment_report`` can reproduce it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.decision_json import content_hash, json_safe, utc_iso
from core.decision_models import (
    DecisionRecord,
    DecisionRun,
    DecisionStatus,
    EvidenceItem,
    InputSnapshot,
    RunMode,
    RunSource,
)
from core.models import Portfolio
from services.cost_accounting import UNKNOWN_PROVIDER_COST
from services.debate_protocol import (
    BASELINE_PROTOCOL_VERSION,
    HEURISTIC_PROTOCOL_VERSION,
    NO_REBUTTAL_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    ProtocolBudget,
    StageFailure,
    record_heuristic_attempt,
)
from services.evidence import create_evidence_item, create_snapshot
from services.experiment_identity import (
    ATTEMPT_IDENTITY_VERSION,
    AttemptIdentity,
    attempt_prefix,
    fixture_id_for_attempt,
    parse_attempt,
    resolve_attempt_identity,
)
from services.experiment_simulation import (
    BENCHMARK_MATCH_POLICY,
    COST_BASIS_POLICY,
    FEE_POLICY,
    FILL_TIMING_RULE,
    FILL_VALIDATION_POLICY,
    SIMULATION_METHOD_VERSION,
    SUPPORTED_REGISTERED_METRICS,
    TRADING_DAY_POLICY,
    DecisionInput,
    SnapshotInput,
    compute_registered_metric,
    simulate_arm_timeline,
)
from services.portfolio_accounting import (
    FILL_RULE,
    LlmCost,
    OpeningState,
    PriceBook,
    PriceMark,
    build_performance,
)

__all__ = [
    "ARCHIVE_FORMAT",
    "ARM_ORDER",
    "EXECUTION_NAMESPACE_VERSION",
    "REPORT_SCHEMA_VERSION",
    "ArmRun",
    "ExecutionError",
    "OpportunitySet",
    "build_comparison_report",
    "execute_opportunity_set",
    "execution_namespace",
    "load_opportunity_set",
    "methods_block",
    "parse_at",
    "simulate_arm_accounting",
    "validate_opportunity_set",
]

SCHEMA_VERSION = "1.0"
#: The comparison report gained a recorded opportunity set, decisions, a
#: monotonic timeline and a computed registered metric. A rebuild refuses an
#: older document by name instead of interpreting it as this one.
REPORT_SCHEMA_VERSION = "3.0"
#: Version 3 records the resolved attempt identity, the benchmark observations
#: and the fill-time validation inputs a rebuild needs. Version 2 archives were
#: produced under the decision-time quantity semantics and are refused by name
#: rather than rebuilt as if the same arithmetic produced them.
ARCHIVE_FORMAT = "experiment_comparison.v3"
EXECUTION_NAMESPACE_VERSION = "experiment_execution.v1"
ZERO = Decimal("0")
CENT = Decimal("0.01")

#: Declared arms in display order. V4 is declared and excluded from execution.
ARM_ORDER = ("V0", "V1", "V2", "V3", "V4")

#: The protocol each arm must actually record. A run whose recorded protocol does
#: not match its arm is a mislabelled arm, not a comparison.
ARM_PROTOCOLS = {
    "V0": HEURISTIC_PROTOCOL_VERSION,
    "V1": BASELINE_PROTOCOL_VERSION,
    "V2": NO_REBUTTAL_PROTOCOL_VERSION,
    "V3": PROTOCOL_VERSION,
}

ARM_LABELS = {
    "V0": "Deterministic baseline",
    "V1": "One analyst",
    "V2": "Current pipeline (no rebuttal round)",
    "V3": "Explicit rebuttal",
    "V4": "Portfolio allocation",
}

ARM_ENGINES = {
    "V0": "deterministic_baseline",
    "V1": "single_agent",
    "V2": "debate",
    "V3": "debate_with_rebuttal",
    "V4": "portfolio_allocation",
}

UNEXECUTED_REASONS = {
    "V4": (
        "V4 (portfolio allocation) is declared and not executed: no V4 arm has been "
        "scheduled or measured. It is excluded from the equal-opportunity denominator "
        "rather than measured as a zero."
    ),
    "provider_not_configured": (
        "No model provider is configured, so this arm could not be executed. It stays "
        "not_executed and remains in the denominator; a scripted or synthetic case is not "
        "substituted for it."
    ),
}


class ExecutionError(Exception):
    """The opportunity set or the execution request is invalid."""


# ---------------------------------------------------------------------------
# The frozen opportunity set
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Opportunity:
    key: str
    ticker: str
    scheduled_at: datetime
    snapshot: dict
    evidence: tuple[dict, ...] = ()


@dataclass
class OpportunitySet:
    """A validated, content-hashed opportunity set with its pre-registration."""

    document: dict
    opportunity_set_id: str
    label: str
    declared_at: datetime
    window_start: datetime
    window_end: datetime
    pre_registration: dict
    starting_state: dict
    execution_assumptions: dict
    risk_rules: dict
    benchmark: dict
    as_of: datetime | None
    opportunities: tuple[Opportunity, ...]
    marks: tuple[PriceMark, ...]
    #: The declared benchmark's own observations. Kept out of the instrument
    #: ``marks`` book so a benchmark price can never value a position; the
    #: metric matches it separately under the declared session policy.
    benchmark_marks: tuple[PriceMark, ...] = ()
    content_hash: str = ""
    source_path: str = ""

    def benchmark_book(self) -> PriceBook:
        return PriceBook(self.benchmark_marks)

    @property
    def primary_metric(self) -> dict:
        return dict(self.pre_registration.get("primary_metric") or {})

    @property
    def sample_plan(self) -> dict:
        return dict(self.pre_registration.get("sample_plan") or {})

    def price_book(self) -> PriceBook:
        return PriceBook(self.marks)


def parse_at(value, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = parse_datetime(value.strip())
    else:
        parsed = None
    if parsed is None:
        raise ExecutionError(f"{field_name} must be an ISO-8601 timestamp.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decimal(value, *, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExecutionError(f"{field_name} must be a decimal.") from exc


def _require(value, *, field_name: str):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ExecutionError(f"{field_name} is required.")
    return value


def validate_opportunity_set(document: dict, *, source_path: str = "") -> OpportunitySet:
    """Validate and normalise an opportunity-set document.

    The pre-registration is *required*. An experiment that starts before its
    sample plan, primary metric, cost ceiling and drawdown criterion are written
    down is an exploration, not a registered comparison.
    """
    if not isinstance(document, dict):
        raise ExecutionError("An opportunity set must be a JSON object.")
    if str(document.get("schema_version")) != SCHEMA_VERSION:
        raise ExecutionError(
            f"Unsupported opportunity-set schema_version {document.get('schema_version')!r}; "
            f"this build supports {SCHEMA_VERSION}."
        )
    opportunity_set_id = str(
        _require(document.get("opportunity_set_id"), field_name="opportunity_set_id")
    ).strip()
    if len(opportunity_set_id) > 96:
        raise ExecutionError("opportunity_set_id must be at most 96 characters.")
    declared_at = parse_at(document.get("declared_at"), field_name="declared_at")

    pre = document.get("pre_registration")
    if not isinstance(pre, dict):
        raise ExecutionError(
            "pre_registration is required: the sample plan, primary metric, cost ceiling "
            "and drawdown criterion must be registered before any arm runs."
        )
    registered_at = parse_at(pre.get("registered_at"), field_name="pre_registration.registered_at")
    if registered_at > declared_at:
        raise ExecutionError(
            "pre_registration.registered_at is after declared_at; a registration cannot be "
            "written after the experiment it governs."
        )
    primary = pre.get("primary_metric")
    if not isinstance(primary, dict) or not primary.get("id"):
        raise ExecutionError("pre_registration.primary_metric needs an id.")
    metric_id = str(primary.get("id"))
    metric_family = str(primary.get("family") or "")
    if metric_id not in SUPPORTED_REGISTERED_METRICS:
        raise ExecutionError(
            f"pre_registration.primary_metric.id {metric_id!r} is not a registered metric "
            f"this build can calculate; supported ids are "
            f"{', '.join(sorted(SUPPORTED_REGISTERED_METRICS))}."
        )
    if SUPPORTED_REGISTERED_METRICS[metric_id] != metric_family:
        raise ExecutionError(
            f"pre_registration.primary_metric.family {metric_family!r} does not match the "
            f"registered family {SUPPORTED_REGISTERED_METRICS[metric_id]!r} for {metric_id!r}."
        )
    if metric_family == "signal_diagnostics":
        horizon = primary.get("horizon_days")
        if not isinstance(horizon, int) or horizon <= 0:
            raise ExecutionError(
                "pre_registration.primary_metric.horizon_days must be a positive integer: the "
                "registered definition, not a constant in this module, sets the horizon."
            )
    sample_plan = pre.get("sample_plan")
    if not isinstance(sample_plan, dict):
        raise ExecutionError("pre_registration.sample_plan is required.")
    for key in ("minimum_opportunities", "minimum_completed_arms"):
        if key not in sample_plan:
            raise ExecutionError(f"pre_registration.sample_plan.{key} is required.")
    if pre.get("cost_ceiling_usd") in (None, ""):
        raise ExecutionError("pre_registration.cost_ceiling_usd is required.")
    if pre.get("acceptable_drawdown_pct") in (None, ""):
        raise ExecutionError("pre_registration.acceptable_drawdown_pct is required.")

    window = document.get("observation_window")
    if not isinstance(window, dict):
        raise ExecutionError("observation_window is required.")
    window_start = parse_at(window.get("start"), field_name="observation_window.start")
    window_end = parse_at(window.get("end"), field_name="observation_window.end")
    if window_end < window_start:
        raise ExecutionError("observation_window.end precedes its start.")

    starting = document.get("starting_state")
    if not isinstance(starting, dict) or "cash" not in starting:
        raise ExecutionError("starting_state needs at least cash.")
    average_cost = {
        str(ticker).upper(): _decimal(cost, field_name="starting_state.average_cost")
        for ticker, cost in (starting.get("average_cost") or {}).items()
    }
    starting_state = {
        "cash": _decimal(starting.get("cash"), field_name="starting_state.cash"),
        "positions": {
            str(ticker).upper(): _decimal(quantity, field_name="starting_state.positions")
            for ticker, quantity in (starting.get("positions") or {}).items()
        },
        "average_cost": average_cost,
    }

    assumptions = document.get("execution_assumptions") or {}
    if not isinstance(assumptions, dict):
        raise ExecutionError("execution_assumptions must be an object.")
    risk_rules = document.get("risk_rules") or {}
    if not isinstance(risk_rules, dict):
        raise ExecutionError("risk_rules must be an object.")
    benchmark = document.get("benchmark") or {}
    if not isinstance(benchmark, dict) or not benchmark.get("ticker"):
        raise ExecutionError("benchmark needs a ticker.")
    benchmark_ticker = str(benchmark.get("ticker")).strip().upper()
    raw_benchmark_marks = benchmark.get("marks") or []
    if not isinstance(raw_benchmark_marks, list):
        raise ExecutionError("benchmark.marks must be a list when it is present.")
    benchmark_marks: list[PriceMark] = []
    for index, raw in enumerate(raw_benchmark_marks):
        if not isinstance(raw, dict):
            raise ExecutionError(f"benchmark mark {index} must be an object.")
        mark_ticker = (
            str(_require(raw.get("ticker"), field_name=f"benchmark mark {index}.ticker"))
            .strip()
            .upper()
        )
        if mark_ticker != benchmark_ticker:
            raise ExecutionError(
                f"benchmark mark {index} names {mark_ticker!r}, but the declared benchmark is "
                f"{benchmark_ticker!r}; a mark for another instrument cannot match the window."
            )
        benchmark_marks.append(
            PriceMark(
                ticker=mark_ticker,
                at=parse_at(raw.get("at"), field_name=f"benchmark mark {index}.at"),
                price=_decimal(raw.get("price"), field_name=f"benchmark mark {index}.price"),
                source=str(raw.get("source") or "declared_benchmark_mark"),
                observed_at=(
                    parse_at(
                        raw.get("observed_at"),
                        field_name=f"benchmark mark {index}.observed_at",
                    )
                    if raw.get("observed_at")
                    else None
                ),
                provenance=str(raw.get("provenance") or ""),
            )
        )

    raw_opportunities = document.get("opportunities")
    if not isinstance(raw_opportunities, list) or not raw_opportunities:
        raise ExecutionError("opportunities must be a non-empty list.")
    opportunities: list[Opportunity] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_opportunities):
        if not isinstance(raw, dict):
            raise ExecutionError(f"opportunity {index} must be an object.")
        key = str(_require(raw.get("key"), field_name=f"opportunity {index}.key")).strip()
        if key in seen:
            raise ExecutionError(f"Duplicate opportunity key {key!r}.")
        seen.add(key)
        ticker = str(_require(raw.get("ticker"), field_name=f"opportunity {key}.ticker")).upper()
        scheduled_at = parse_at(
            raw.get("scheduled_at"), field_name=f"opportunity {key}.scheduled_at"
        )
        if not (window_start <= scheduled_at <= window_end):
            raise ExecutionError(
                f"opportunity {key!r} is scheduled outside the declared observation window."
            )
        snapshot_spec = raw.get("snapshot")
        if not isinstance(snapshot_spec, dict):
            raise ExecutionError(f"opportunity {key} needs a snapshot object.")
        cutoff = parse_at(
            snapshot_spec.get("analysis_cutoff") or scheduled_at,
            field_name=f"opportunity {key}.snapshot.analysis_cutoff",
        )
        if cutoff > scheduled_at:
            raise ExecutionError(
                f"opportunity {key!r} has an analysis_cutoff after its scheduled decision time; "
                "a decision may not see data from its own future."
            )
        quote_as_of = (
            parse_at(
                quote.get("as_of"),
                field_name=f"opportunity {key}.snapshot.quotes.{ticker}.as_of",
            )
            if (quote := (snapshot_spec.get("quotes") or {}).get(ticker)) and quote.get("as_of")
            else None
        )
        if quote_as_of is not None and quote_as_of > scheduled_at:
            raise ExecutionError(
                f"opportunity {key!r} has a quote observed after its scheduled decision time; "
                "a decision may not see data from its own future."
            )
        evidence = raw.get("evidence") or []
        if not isinstance(evidence, list):
            raise ExecutionError(f"opportunity {key}.evidence must be a list.")
        for item_index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise ExecutionError(f"opportunity {key}.evidence[{item_index}] must be an object.")
            published_at = (
                parse_at(
                    item.get("published_at"),
                    field_name=f"opportunity {key}.evidence.published_at",
                )
                if item.get("published_at")
                else None
            )
            retrieved_at = (
                parse_at(
                    item.get("retrieved_at"),
                    field_name=f"opportunity {key}.evidence.retrieved_at",
                )
                if item.get("retrieved_at")
                else cutoff
            )
            if item.get("available_at"):
                available_at = parse_at(
                    item.get("available_at"),
                    field_name=f"opportunity {key}.evidence.available_at",
                )
            elif published_at is not None:
                available_at = max(published_at, retrieved_at)
            else:
                available_at = retrieved_at
            if available_at > cutoff:
                raise ExecutionError(
                    f"opportunity {key!r} has evidence available after its snapshot cutoff; "
                    "the decision could not have seen it."
                )
        opportunities.append(
            Opportunity(
                key=key,
                ticker=ticker,
                scheduled_at=scheduled_at,
                snapshot=snapshot_spec,
                evidence=tuple(evidence),
            )
        )
    opportunities.sort(key=lambda item: (item.scheduled_at, item.key))

    if registered_at > opportunities[0].scheduled_at:
        raise ExecutionError(
            "pre_registration.registered_at is after the first scheduled opportunity: the "
            "registration did not precede the run, so it is not a pre-registration."
        )

    raw_marks = document.get("marks")
    if not isinstance(raw_marks, list) or not raw_marks:
        raise ExecutionError("marks must be a non-empty list of price observations.")
    marks: list[PriceMark] = []
    for index, raw in enumerate(raw_marks):
        if not isinstance(raw, dict):
            raise ExecutionError(f"mark {index} must be an object.")
        at = parse_at(raw.get("at"), field_name=f"mark {index}.at")
        observed_at = (
            parse_at(raw.get("observed_at"), field_name=f"mark {index}.observed_at")
            if raw.get("observed_at")
            else None
        )
        marks.append(
            PriceMark(
                ticker=str(_require(raw.get("ticker"), field_name=f"mark {index}.ticker")),
                at=at,
                price=_decimal(raw.get("price"), field_name=f"mark {index}.price"),
                source=str(raw.get("source") or "declared_opportunity_set"),
                observed_at=observed_at,
                provenance=str(raw.get("provenance") or ""),
            )
        )
    book = PriceBook(marks)
    for opportunity in opportunities:
        if book.mark_at_or_after(opportunity.ticker, opportunity.scheduled_at) is None:
            raise ExecutionError(
                f"opportunity {opportunity.key!r} has no mark at or after its scheduled time "
                f"for {opportunity.ticker}; a fill could not be priced under {FILL_RULE}."
            )

    arms = document.get("arms") or {}
    if not isinstance(arms, dict):
        raise ExecutionError("arms must be an object keyed V0..V4.")
    missing = [key for key in ARM_ORDER if key not in arms]
    if missing:
        raise ExecutionError("arms must declare every arm; missing " + ", ".join(missing) + ".")
    if bool((arms.get("V4") or {}).get("implemented")):
        raise ExecutionError(
            "V4 is declared and unimplemented: no V4 arm has been scheduled or measured. It "
            "must not be marked implemented."
        )

    normalised_document = json_safe(document)
    return OpportunitySet(
        document=normalised_document,
        opportunity_set_id=opportunity_set_id,
        label=str(document.get("label") or opportunity_set_id),
        declared_at=declared_at,
        window_start=window_start,
        window_end=window_end,
        pre_registration={**pre, "registered_at": utc_iso(registered_at)},
        starting_state=starting_state,
        execution_assumptions=dict(assumptions),
        risk_rules=dict(risk_rules),
        benchmark=dict(benchmark),
        as_of=(
            parse_at(document.get("as_of"), field_name="as_of") if document.get("as_of") else None
        ),
        opportunities=tuple(opportunities),
        marks=tuple(marks),
        benchmark_marks=tuple(benchmark_marks),
        content_hash=content_hash(normalised_document),
        source_path=source_path,
    )


def load_opportunity_set(path: str | Path) -> OpportunitySet:
    file_path = Path(path)
    if not file_path.exists():
        raise ExecutionError(f"Opportunity set not found: {file_path}")
    document = json.loads(file_path.read_text(encoding="utf-8"))
    return validate_opportunity_set(document, source_path=str(file_path))


# ---------------------------------------------------------------------------
# Execution namespace
# ---------------------------------------------------------------------------
def execution_namespace(
    *,
    portfolio_id,
    opportunity_set_id: str,
    opportunity_set_hash: str,
    provider_mode: str,
    client_identity: str = "",
) -> str:
    """The deterministic namespace of one experiment execution.

    The content hash is part of the identity, so two opportunity sets that share
    a natural key but differ in tickers, marks or starting state get separate
    namespaces. The portfolio id is part of it too, so two portfolios cannot
    read or overwrite each other's decisions. The provider mode is included so a
    scripted arm is never silently reused as a live one.

    A re-run with the same inputs resolves to the same namespace and therefore
    reuses recorded runs instead of double-spending.
    """
    payload = {
        "namespace_version": EXECUTION_NAMESPACE_VERSION,
        "portfolio_id": str(portfolio_id),
        "opportunity_set_id": opportunity_set_id,
        "opportunity_set_hash": opportunity_set_hash,
        "provider_mode": provider_mode,
        "client_identity": client_identity,
    }
    return content_hash(payload)[:16]


def _snapshot_version(opportunity_set: OpportunitySet, opportunity: Opportunity) -> str:
    version = f"{opportunity_set.opportunity_set_id}:{opportunity_set.content_hash[:16]}:{opportunity.key}"
    if len(version) <= 64:
        return version
    # ``InputSnapshot.portfolio_version`` is bounded; keep it deterministic and
    # collision-resistant rather than truncating the key and losing identity.
    return f"{opportunity_set.opportunity_set_id[:16]}:{content_hash(version)[:16]}"


def ensure_snapshot(
    opportunity_set: OpportunitySet, opportunity: Opportunity, portfolio: Portfolio
) -> InputSnapshot:
    """Return the one snapshot for this opportunity, creating it on first use.

    Every arm receives this exact row. A second snapshot for the same opportunity
    would make the comparison one of inputs rather than of protocols.
    """
    spec = opportunity.snapshot
    version = _snapshot_version(opportunity_set, opportunity)
    cutoff = parse_at(
        spec.get("analysis_cutoff") or opportunity.scheduled_at,
        field_name=f"snapshot {opportunity.key}.analysis_cutoff",
    )
    captured_at = (
        parse_at(spec.get("captured_at"), field_name=f"snapshot {opportunity.key}.captured_at")
        if spec.get("captured_at")
        else cutoff
    )
    holdings = spec.get("holdings") or []
    cash = spec.get("cash") or {"balance_usd": "0.00", "currency": "USD"}
    quotes = spec.get("quotes") or {}
    existing = InputSnapshot.objects.filter(portfolio=portfolio, portfolio_version=version).first()
    if existing is not None:
        return existing
    return create_snapshot(
        portfolio=portfolio,
        portfolio_version=version,
        analysis_cutoff=cutoff,
        captured_at=captured_at,
        holdings=holdings,
        cash=cash,
        quotes=quotes,
    )


def ensure_evidence(opportunity: Opportunity, snapshot: InputSnapshot) -> dict[str, int]:
    """Create the declared evidence items for a snapshot, idempotently."""
    created = 0
    for index, raw in enumerate(opportunity.evidence):
        source_type = str(raw.get("source_type") or "news")
        excerpt = str(raw.get("excerpt") or "")[:4000]
        if EvidenceItem.objects.filter(
            snapshot=snapshot, source_type=source_type, excerpt=excerpt
        ).exists():
            continue
        retrieved_at = (
            parse_at(raw.get("retrieved_at"), field_name="evidence.retrieved_at")
            if raw.get("retrieved_at")
            else snapshot.analysis_cutoff
        )
        create_evidence_item(
            snapshot=snapshot,
            source_type=source_type,
            retrieved_at=retrieved_at,
            publisher=str(raw.get("publisher") or ""),
            published_at=(
                parse_at(raw.get("published_at"), field_name="evidence.published_at")
                if raw.get("published_at")
                else None
            ),
            available_at=(
                parse_at(raw.get("available_at"), field_name="evidence.available_at")
                if raw.get("available_at")
                else None
            ),
            excerpt=excerpt,
            observation=dict(raw.get("observation") or {"index": index}),
            units=str(raw.get("units") or ""),
            currency=str(raw.get("currency") or ""),
        )
        created += 1
    return {"created": created, "declared": len(opportunity.evidence)}


def _quote_for(snapshot: InputSnapshot, ticker: str) -> dict:
    quote = (snapshot.quotes or {}).get(ticker.upper()) or {}
    return dict(quote) if isinstance(quote, dict) else {}


def _holding_for(snapshot: InputSnapshot, ticker: str) -> tuple[Decimal, Decimal]:
    for holding in snapshot.holdings or []:
        if str(holding.get("ticker") or "").upper() != ticker.upper():
            continue
        quantity = _decimal(holding.get("quantity") or 0, field_name="holding.quantity")
        avg = _decimal(
            holding.get("avg_purchase_price") or 0, field_name="holding.avg_purchase_price"
        )
        return quantity, avg
    return ZERO, ZERO


def build_decision_context(opportunity: Opportunity, snapshot: InputSnapshot, portfolio: Portfolio):
    """The input brief for one arm. Identical for every arm by construction."""
    from ai_agent import DecisionContext

    quote = _quote_for(snapshot, opportunity.ticker)
    price_value = quote.get("price_usd") or quote.get("price")
    price = _decimal(price_value, field_name="quote.price") if price_value else None
    change_value = quote.get("change_pct")
    change = (
        _decimal(change_value, field_name="quote.change_pct")
        if change_value not in (None, "")
        else None
    )
    quantity, avg = _holding_for(snapshot, opportunity.ticker)
    cash = _decimal(
        (snapshot.cash or {}).get("balance_usd") or 0, field_name="snapshot.cash.balance_usd"
    )
    budget = (cash * portfolio.max_trade_allocation_pct / Decimal("100")).quantize(CENT)
    return DecisionContext(
        ticker=opportunity.ticker,
        risk_profile=portfolio.risk_profile,
        cash_balance=cash,
        position_amount=quantity,
        avg_purchase_price=avg,
        current_price=price,
        price_change_pct=change,
        max_trade_budget_usd=budget,
        daily_loss_limit_usd=portfolio.daily_loss_limit_usd,
        portfolio_id=portfolio.id,
    )


# ---------------------------------------------------------------------------
# Arm execution
# ---------------------------------------------------------------------------
@dataclass
class ArmRun:
    arm: str
    opportunity: str
    fixture_id: str
    run: DecisionRun | None = None
    status: str = "not_executed"
    failure_code: str = ""
    failure_detail: str = ""
    protocol_version: str = ""
    cost: dict = field(default_factory=dict)
    #: The recorded inputs a rebuild reads. Populated for a stored run and
    #: reconstructed from an archive; both paths run the same pure simulator.
    decision: DecisionInput | None = None
    snapshot: SnapshotInput | None = None
    decision_run_id: str = ""
    #: The resolved configuration this attempt ran under, and its ordinal among
    #: the attempts of this (namespace, arm, opportunity) run key.
    identity: AttemptIdentity | None = None
    attempt: int = 0
    reused: bool = False

    def as_dict(self) -> dict:
        run_id = self.decision_run_id or (str(self.run.id) if self.run is not None else "")
        snapshot_id = (
            self.snapshot.id
            if self.snapshot is not None
            else (
                str(self.run.snapshot_id) if self.run is not None and self.run.snapshot_id else ""
            )
        )
        snapshot_hash = (
            self.snapshot.content_hash
            if self.snapshot is not None
            else (
                self.run.snapshot.content_hash
                if self.run is not None and self.run.snapshot is not None
                else ""
            )
        )
        return {
            "opportunity": self.opportunity,
            "fixture_id": self.fixture_id,
            "status": self.status,
            "failure_code": self.failure_code or None,
            "failure_detail": self.failure_detail or None,
            "decision_run_id": run_id or None,
            "protocol_version": self.protocol_version or None,
            "attempt": self.attempt,
            "attempt_identity": (None if self.identity is None else self.identity.as_dict()),
            "reused": self.reused,
            "snapshot_id": snapshot_id or None,
            "snapshot_hash": snapshot_hash or None,
            "synthetic": True,
            "decision": (
                None
                if self.decision is None
                else {
                    "run_id": self.decision.run_id,
                    "opportunity": self.decision.opportunity_key,
                    "ticker": self.decision.ticker,
                    "action": self.decision.normalised_action,
                    "requested_quantity": str(self.decision.requested_quantity),
                    "rationale": self.decision.rationale,
                    "recorded_at": utc_iso(self.decision.recorded_at),
                }
            ),
            "cost": self.cost,
        }


def _attempt_records(prefix: str, portfolio: Portfolio) -> list[tuple[str, int]]:
    """Every recorded attempt for one (namespace, arm, opportunity), parsed."""
    rows = DecisionRun.objects.filter(
        portfolio=portfolio, fixture_id__startswith=f"{prefix}."
    ).values_list("fixture_id", flat=True)
    found: list[tuple[str, int]] = []
    for fixture_id in rows:
        parsed = parse_attempt(fixture_id, prefix=prefix)
        if parsed is not None:
            found.append(parsed)
    return found


def _attempt_for(
    *, prefix: str, identity_hash: str, portfolio: Portfolio, force: bool
) -> tuple[int, bool]:
    """Return ``(attempt ordinal, existed)`` for this configuration.

    An exact repeat reuses the latest attempt recorded under the same identity.
    A forced rerun appends the next attempt after **every** attempt for this run
    key, so the earlier decisions are never deleted or overwritten. A changed
    configuration has its own identity and therefore its own attempt sequence.
    """
    records = _attempt_records(prefix, portfolio)
    same_identity = [
        attempt for recorded_hash, attempt in records if recorded_hash == identity_hash
    ]
    if force:
        return (max((attempt for _, attempt in records), default=0) + 1, False)
    if same_identity:
        return (max(same_identity), True)
    return (1, False)


def _existing_run(fixture_id: str, portfolio: Portfolio | None = None) -> DecisionRun | None:
    queryset = DecisionRun.objects.select_related("snapshot", "decision").filter(
        fixture_id=fixture_id
    )
    if portfolio is not None:
        queryset = queryset.filter(portfolio=portfolio)
    return queryset.first()


def _snapshot_input(snapshot: InputSnapshot) -> SnapshotInput:
    return SnapshotInput(
        id=str(snapshot.id),
        content_hash=snapshot.content_hash,
        portfolio_version=snapshot.portfolio_version,
        analysis_cutoff=snapshot.analysis_cutoff,
        captured_at=snapshot.captured_at,
        holdings=tuple(dict(item) for item in snapshot.holdings or ()),
        cash=dict(snapshot.cash or {}),
        quotes=dict(snapshot.quotes or {}),
    )


def _decision_input(
    run: DecisionRun, opportunity: Opportunity, snapshot: SnapshotInput
) -> DecisionInput | None:
    decision = DecisionRecord.objects.filter(run=run).first()
    if decision is None:
        return None
    return DecisionInput(
        run_id=str(run.id),
        opportunity_key=opportunity.key,
        ticker=run.ticker,
        decision_at=opportunity.scheduled_at,
        action=str(decision.action or "HOLD"),
        requested_quantity=decision.requested_quantity or ZERO,
        rationale=decision.concise_rationale or "",
        snapshot=snapshot,
        recorded_at=decision.created_at,
    )


def _populate_arm_run(result: ArmRun, run: DecisionRun, opportunity: Opportunity) -> None:
    result.run = run
    result.decision_run_id = str(run.id)
    if run.snapshot is not None:
        result.snapshot = _snapshot_input(run.snapshot)
        result.decision = _decision_input(run, opportunity, result.snapshot)


def _cost_of_run(run: DecisionRun | None) -> dict:
    """Read the cost record a run carries, keeping the five quantities separate."""
    if run is None:
        return {
            "measured_tokens": {"input": 0, "output": 0},
            "estimated_tokens": {"input": 0, "output": 0},
            "price_table_cost_usd": None,
            "billed_cost_usd": None,
            "billed_cost_unknown_calls": 1,
            "basis": "",
            "unknown_reason": "run_not_recorded",
        }
    metadata = run.protocol_metadata or {}
    cost = metadata.get("cost_accounting") or {}
    usage = metadata.get("usage") or {}
    measured = cost.get("measured_tokens") or usage.get("measured_tokens") or {}
    # The protocol writes the provider-reported counts to the run row as well as into
    # metadata. When the usage-summary call fails, the metadata block records zeros
    # while `input_tokens`/`output_tokens` still hold what the provider actually
    # charged for - so prefer the row, and say which source was used.
    row_in, row_out = getattr(run, "input_tokens", None), getattr(run, "output_tokens", None)
    if not (measured.get("input") or measured.get("output")) and (row_in or row_out):
        measured = {"input": row_in or 0, "output": row_out or 0, "source": "run_row"}
    estimated = cost.get("estimated_tokens") or usage.get("estimated_tokens") or {}
    billed = cost.get("billed_cost_usd", usage.get("billed_cost_usd"))
    price_table = cost.get("price_table_cost_usd", usage.get("price_table_cost_usd"))
    basis = run.cost_basis or cost.get("monetary_ceiling_basis") or ""
    unknown_reason = (
        cost.get("billed_cost_unknown_reason")
        or usage.get("billed_cost_unknown_reason")
        or ("" if billed is not None else UNKNOWN_PROVIDER_COST)
    )
    return {
        "measured_tokens": {
            "input": int(measured.get("input") or 0),
            "output": int(measured.get("output") or 0),
        },
        "estimated_tokens": {
            "input": int(estimated.get("input") or 0),
            "output": int(estimated.get("output") or 0),
        },
        "price_table_cost_usd": price_table,
        "price_table_cost_partial_usd": cost.get("price_table_cost_partial_usd"),
        "price_table_cost_unknown_reason": cost.get("price_table_cost_unknown_reason"),
        "billed_cost_usd": billed,
        "billed_cost_unknown_calls": int(cost.get("billed_cost_unknown_calls") or 0)
        or (0 if billed is not None else 1),
        "timed_out_calls": int(cost.get("timed_out_calls") or 0),
        "unreconciled_timeout_calls": int(cost.get("unreconciled_timeout_calls") or 0),
        "basis": basis,
        "unknown_reason": unknown_reason,
        "strict_billed_ceiling": bool(cost.get("strict_billed_ceiling", False)),
    }


def _is_real_provider_client(client) -> bool:
    """True only for the adapter that calls a configured provider.

    Anything injected (a test double, a script or the offline client) is treated
    as simulated. The conservative direction matters: mislabelling a scripted
    reply as ``live`` would be exactly the fabrication this change removes.
    """
    return client is not None and client.__class__.__name__ == "CrewAIStageClient"


def _client_for_arm(arm: str, *, client, provider_mode: str, portfolio: Portfolio):
    """Return ``(client, provider_mode_used, reason)`` for one arm."""
    if client is not None:
        return client, ("live" if _is_real_provider_client(client) else "stub"), ""
    if provider_mode == "stub":
        from services.stub_stage_client import OfflineStageClient

        return OfflineStageClient(), "stub", ""
    # auto: only a configured real provider may run the LLM arms.
    try:
        from ai_agent import AlphaAgentOrchestrator, CrewAIStageClient

        orchestrator = AlphaAgentOrchestrator()
        if not orchestrator.is_configured:
            return None, "auto", UNEXECUTED_REASONS["provider_not_configured"]
        return CrewAIStageClient(orchestrator), "live", ""
    except Exception as exc:  # pragma: no cover - configuration-specific
        return None, "auto", f"provider_not_configured: {exc}"


def _identity_metadata(
    run: DecisionRun | None, *, identity: AttemptIdentity | None, attempt: int, fixture_id: str
) -> None:
    """Persist the resolved attempt identity beside the recorded run."""
    if run is None or identity is None:
        return
    metadata = dict(run.protocol_metadata or {})
    metadata["experiment_identity"] = {
        **identity.as_dict(),
        "attempt": attempt,
        "fixture_id": fixture_id,
    }
    DecisionRun.objects.filter(pk=run.pk).update(protocol_metadata=json_safe(metadata))
    run.protocol_metadata = metadata


def _run_arm_once(
    *,
    arm: str,
    opportunity: Opportunity,
    portfolio: Portfolio,
    snapshot: InputSnapshot,
    client,
    provider_mode: str,
    identity: AttemptIdentity | None = None,
    budget: ProtocolBudget | None = None,
    force: bool = False,
    namespace: str = "",
) -> ArmRun:
    from services.debate_protocol import DebateProtocol

    prefix = attempt_prefix(namespace=namespace, arm=arm, opportunity_key=opportunity.key)
    identity_hash = identity.identity_hash if identity is not None else ""
    attempt = _attempt_for(
        prefix=prefix,
        identity_hash=identity_hash,
        portfolio=portfolio,
        force=force,
    )[0]
    fixture_id = fixture_id_for_attempt(prefix=prefix, identity_hash=identity_hash, attempt=attempt)
    result = ArmRun(
        arm=arm,
        opportunity=opportunity.key,
        fixture_id=fixture_id,
        protocol_version=ARM_PROTOCOLS[arm],
        identity=identity,
        attempt=attempt,
    )
    existing = _existing_run(fixture_id, portfolio) if identity is not None else None
    if existing is not None:
        _populate_arm_run(result, existing, opportunity)
        result.protocol_version = existing.protocol_version
        result.status = (
            "completed"
            if existing.status == DecisionStatus.COMPLETED
            else "failed"
            if existing.status == DecisionStatus.FAILED
            else "not_executed"
        )
        result.failure_code = existing.error_code
        result.failure_detail = existing.error_detail or "run already recorded"
        result.cost = _cost_of_run(existing)
        result.reused = True
        return result

    context = build_decision_context(opportunity, snapshot, portfolio)
    try:
        if arm == "V0":
            from ai_agent import HeuristicDecisionEngine

            engine = HeuristicDecisionEngine()
            proposal = engine.decide(context)
            run = record_heuristic_attempt(
                context=context,
                portfolio=portfolio,
                proposal=proposal,
                reasoning=proposal.reasoning,
                error="experiment V0 deterministic baseline",
                snapshot=snapshot,
                fixture_id=fixture_id,
                baseline=True,
                zero_cost=True,
            )
        else:
            simulated = provider_mode != "live"
            protocol = DebateProtocol(
                client=client,
                budget=budget,
                mode=RunMode.SIMULATED if simulated else RunMode.LIVE,
                source=RunSource.STUB if simulated else RunSource.CREWAI,
                provider=getattr(client, "provider", "") or "unknown",
                model=getattr(client, "model", "") or "unknown",
                parameters=getattr(client, "parameters", {}) or {},
            )
            if arm == "V1":
                outcome = protocol.run_single_agent_baseline(
                    context,
                    portfolio=portfolio,
                    snapshot=snapshot,
                    record=True,
                    fixture_id=fixture_id,
                )
            elif arm == "V2":
                outcome = protocol.run_without_rebuttal(
                    context,
                    portfolio=portfolio,
                    snapshot=snapshot,
                    record=True,
                    fixture_id=fixture_id,
                )
            else:
                outcome = protocol.run(
                    context,
                    portfolio=portfolio,
                    snapshot=snapshot,
                    record=True,
                    fixture_id=fixture_id,
                )
            run = outcome.run
        if run is not None:
            _populate_arm_run(result, run, opportunity)
            _identity_metadata(run, identity=identity, attempt=attempt, fixture_id=fixture_id)
        result.protocol_version = run.protocol_version if run is not None else ""
        result.status = "completed" if run is not None else "not_executed"
        if run is None:
            result.failure_code = "run_not_recorded"
            result.failure_detail = "The protocol completed without persisting a run."
        result.cost = _cost_of_run(run)
    except StageFailure as exc:
        if exc.run is not None:
            _populate_arm_run(result, exc.run, opportunity)
            _identity_metadata(exc.run, identity=identity, attempt=attempt, fixture_id=fixture_id)
        result.status = "failed"
        result.failure_code = exc.code
        result.failure_detail = exc.detail[:2000]
        result.cost = _cost_of_run(exc.run)
    except Exception as exc:  # pragma: no cover - defensive: a crashed arm is a result
        result.status = "failed"
        result.failure_code = "execution_error"
        result.failure_detail = f"{type(exc).__name__}: {exc}"[:2000]
        result.cost = _cost_of_run(result.run)
    return result


# ---------------------------------------------------------------------------
# Per-arm simulated accounting
# ---------------------------------------------------------------------------
def _guard_view(
    portfolio: Portfolio, risk_rules: dict, cash: Decimal, positions_value: Decimal
) -> Portfolio:
    """An unsaved Portfolio carrying the arm's running cash and the frozen rules.

    The declared ``risk_rules`` are applied, not the portfolio's current
    configuration, because the comparison is over a frozen question. The report
    separately checks whether the declared rules match the portfolio's current
    configuration and says when they do not.
    """

    def rule(key: str, fallback):
        value = risk_rules.get(key)
        if value in (None, ""):
            return fallback
        return _decimal(value, field_name=f"risk_rules.{key}")

    return Portfolio(
        balance_usd=cash,
        risk_profile=portfolio.risk_profile,
        max_portfolio_exposure_pct=rule(
            "max_portfolio_exposure_pct", portfolio.max_portfolio_exposure_pct
        ),
        max_ticker_concentration_pct=rule(
            "max_ticker_concentration_pct", portfolio.max_ticker_concentration_pct
        ),
        max_sector_concentration_pct=rule(
            "max_sector_concentration_pct", portfolio.max_sector_concentration_pct
        ),
        max_turnover_pct=rule("max_turnover_pct", portfolio.max_turnover_pct),
        min_cash_buffer_pct=rule("min_cash_buffer_pct", portfolio.min_cash_buffer_pct),
        max_trade_allocation_pct=rule(
            "max_trade_allocation_pct", portfolio.max_trade_allocation_pct
        ),
        daily_loss_limit_usd=rule("daily_loss_limit_usd", portfolio.daily_loss_limit_usd),
    )


def _fee_for(notional: Decimal, assumptions: dict) -> Decimal:
    bps = _decimal(assumptions.get("fee_bps") or 0, field_name="execution_assumptions.fee_bps")
    return (notional * bps / Decimal("10000")).quantize(CENT)


def _complete_simulated_run(
    run: DecisionRun | None,
    *,
    action: str,
    guard,
    order: dict,
    decision_at: datetime,
) -> None:
    """Complete a validated run with the guard result this arm actually got.

    The protocol stops at ``validating`` by design: the execution lifecycle owns
    the terminal state, because only it sees the guard. The simulated path has a
    real guard result, so it records it through the same service rather than
    inventing a status.
    """
    if run is None or run.status != DecisionStatus.VALIDATING:
        return
    from services.decision_lifecycle import complete_run

    validation = order.get("fill_validation") or {}
    if action == "HOLD":
        outcome = "held"
        reason = "HOLD: no trade was proposed."
    elif not guard.approved:
        outcome = "rejected"
        reason = guard.reason
    elif validation.get("status") == "rejected":
        outcome = "rejected"
        reason = order.get("reason") or validation.get("reason") or "rejected_at_fill"
    elif order.get("fill") is None:
        outcome = "failed"
        reason = order.get("reason") or "no executable price"
    else:
        outcome = "executed"
        reason = guard.reason
    complete_run(
        run,
        outcome=outcome,
        guard_decision=guard if action != "HOLD" else None,
        reason=reason,
        error_code="simulated_fill_unpriced" if outcome == "failed" else "",
        error_detail=reason if outcome == "failed" else "",
    )
    run.refresh_from_db()
    metadata = dict(run.protocol_metadata or {})
    metadata["experiment"] = {
        **dict(metadata.get("experiment") or {}),
        "economic_decision_at": utc_iso(decision_at),
        "simulated_execution": True,
        "note": (
            "The decision instant used for pricing is the scheduled opportunity time, "
            "not the wall-clock time this replay ran. The run's started_at/completed_at "
            "record when the replay actually happened."
        ),
    }
    DecisionRun.objects.filter(pk=run.pk).update(protocol_metadata=json_safe(metadata))
    run.protocol_metadata = metadata


def simulate_arm_accounting(
    *,
    arm: str,
    opportunity_set: OpportunitySet,
    runs: Sequence[ArmRun],
    portfolio: Portfolio,
    snapshots_by_opportunity: dict[str, InputSnapshot] | None = None,
    persist: bool = True,
) -> dict:
    """Replay one arm's decisions on a monotonic simulated clock.

    The guard, fill rule, fee assumption and accounting method are the *same*
    objects the live path uses (``services.execution.evaluate_proposal``,
    ``first_executable_mark_at_or_after_decision.v1``,
    ``services.portfolio_accounting.build_performance``). The fill is applied at
    the mark's own instant, never at the decision instant when the mark is later.
    What differs between arms is the decisions and therefore the fills.

    ``persist=False`` is the rebuild path: it performs no database write and
    reads only the recorded inputs on ``ArmRun``.
    """
    book = opportunity_set.price_book()
    snapshots_by_opportunity = snapshots_by_opportunity or {}
    by_opportunity = {item.opportunity: item for item in runs}

    for arm_run in runs:
        if arm_run.snapshot is None:
            snapshot_row = snapshots_by_opportunity.get(arm_run.opportunity)
            if snapshot_row is None and arm_run.run is not None:
                snapshot_row = arm_run.run.snapshot
            if snapshot_row is not None:
                arm_run.snapshot = _snapshot_input(snapshot_row)
        if arm_run.decision is None and arm_run.run is not None and arm_run.snapshot is not None:
            opportunity = next(
                (item for item in opportunity_set.opportunities if item.key == arm_run.opportunity),
                None,
            )
            if opportunity is not None:
                arm_run.decision = _decision_input(arm_run.run, opportunity, arm_run.snapshot)

    decisions = {
        arm_run.opportunity: arm_run.decision for arm_run in runs if arm_run.decision is not None
    }

    timeline = simulate_arm_timeline(
        arm=arm,
        opportunities=opportunity_set.opportunities,
        decisions=decisions,
        marks=opportunity_set.marks,
        window_start=opportunity_set.window_start,
        window_end=opportunity_set.window_end,
        starting_state=opportunity_set.starting_state,
        execution_assumptions=opportunity_set.execution_assumptions,
        risk_rules=opportunity_set.risk_rules,
        guard_portfolio_factory=lambda cash, positions_value: _guard_view(
            portfolio, opportunity_set.risk_rules, cash, positions_value
        ),
        as_of=opportunity_set.as_of,
    )
    orders = timeline["orders"]
    fills = timeline["fills"]
    if persist:
        for order in orders:
            arm_run = by_opportunity.get(order["opportunity"])
            if arm_run is None or arm_run.run is None or arm_run.decision is None:
                continue
            guard = timeline["guards"].get(order["opportunity"])
            if guard is None:
                if order["fill_rule_status"] == "rejected":
                    arm_run.status = "failed"
                    arm_run.failure_code = order["fill_reason"] or "simulated_input_rejected"
                    arm_run.failure_detail = order["reason"] or arm_run.failure_code
                    _reject_simulated_run(
                        arm_run.run,
                        reason=arm_run.failure_detail,
                        code=arm_run.failure_code,
                    )
                continue
            _complete_simulated_run(
                arm_run.run,
                action=arm_run.decision.normalised_action,
                guard=guard,
                order=order,
                decision_at=arm_run.decision.decision_at,
            )
    fee_bps = _decimal(
        opportunity_set.execution_assumptions.get("fee_bps") or ZERO,
        field_name="execution_assumptions.fee_bps",
    )
    for order in orders:
        if order["proposal_action"] is not None:
            order["fee_bps"] = str(fee_bps)

    # LLM costs: a deterministic arm's zero is measured (no call was made); a
    # scripted or live arm whose provider reported no charge is *unknown*, and
    # build_performance turns that into a null net return rather than a zero.
    llm_costs = []
    missing = 0
    for arm_run in runs:
        cost = arm_run.cost or {}
        billed = cost.get("billed_cost_usd")
        if billed is None:
            missing += 1
            continue
        # The decision's recording time is a recorded input, so a rebuild uses
        # the same instant as the original build. It is external spend, clamped
        # to the observation window.
        at = (
            arm_run.decision.recorded_at
            if arm_run.decision is not None and arm_run.decision.recorded_at is not None
            else opportunity_set.window_start
        )
        llm_costs.append(
            LlmCost(
                at=min(at, opportunity_set.window_end),
                amount=_decimal(billed, field_name="billed_cost_usd"),
                source=("deterministic_no_model_call" if arm == "V0" else "billed_provider"),
                run_key=arm_run.fixture_id,
            )
        )

    opening = OpeningState(
        at=opportunity_set.window_start - timedelta(seconds=1),
        cash=opportunity_set.starting_state["cash"],
        positions=opportunity_set.starting_state["positions"],
        basis="declared_opportunity_set_starting_state",
    )
    performance = build_performance(
        opening=opening,
        end=opportunity_set.window_end,
        fills=fills,
        flows=(),
        marks=book,
        llm_costs=llm_costs,
        llm_cost_missing_count=missing,
        provenance={
            "synthetic": True,
            "simulated_arm": arm,
            "price_source": sorted(book.source_counts().items()),
            "fill_rule": FILL_RULE,
            "fill_timing": FILL_TIMING_RULE,
            "simulation_method": SIMULATION_METHOD_VERSION,
            "fee_model": (
                "declared_bps"
                if opportunity_set.execution_assumptions.get("fee_bps") not in (None, "")
                else "not_declared"
            ),
            "guard_policy": str(
                opportunity_set.execution_assumptions.get("guard_policy_version")
                or "evaluated_by_services.execution.evaluate_proposal"
            ),
            "fill_validation": FILL_VALIDATION_POLICY,
            "cost_basis": COST_BASIS_POLICY,
            "fee_policy": FEE_POLICY,
            "trading_day": TRADING_DAY_POLICY,
            "llm_cost_timing": (
                "A known LLM cost is attributed at the decision's recorded time (clamped "
                "to the window end) because it is external spend, not a portfolio flow."
            ),
            "note": (
                "Simulated replay of this arm's recorded decisions under the frozen marks, "
                "the shared guard and the same accounting method. Not realised history."
            ),
        },
    )
    result = performance.as_dict()
    result["orders"] = orders
    result["orders_count"] = len(orders)
    result["fill_count"] = len(fills)
    result["simulation"] = {
        "method_version": SIMULATION_METHOD_VERSION,
        "fill_timing_rule": FILL_TIMING_RULE,
        "fill_rule": FILL_RULE,
        "fill_validation_policy": FILL_VALIDATION_POLICY,
        "cost_basis": COST_BASIS_POLICY,
        "fee_policy": FEE_POLICY,
        "trading_day": TRADING_DAY_POLICY,
        "realised_pnl": timeline["realised_pnl"],
        "clock_started_at": timeline["clock_started_at"],
        "clock_ended_at": timeline["clock_ended_at"],
        "steps": timeline["steps"],
        "steps_hash": timeline["steps_hash"],
        "decision_records": _decision_records(timeline["steps"]),
        "decision_records_hash": content_hash(_decision_records(timeline["steps"])),
        "step_count": len(timeline["steps"]),
        "monotonic": _steps_are_monotonic(timeline["steps"]),
        "expired_fills": timeline["expired_fills"],
        "synthetic": True,
        "note": (
            "Each decision and fill carries the simulated clock and the data known at that "
            "instant. A fill priced from a later mark is applied when the clock reaches it."
        ),
    }
    result["per_opportunity"] = [
        {
            "opportunity": item.key,
            "ticker": item.ticker,
            "scheduled_at": utc_iso(item.scheduled_at),
        }
        for item in opportunity_set.opportunities
    ]
    return result


def _reject_simulated_run(run: DecisionRun | None, *, reason: str, code: str) -> None:
    """Complete a run the simulator refused before a guard could be evaluated."""
    if run is None or run.status != DecisionStatus.VALIDATING:
        return
    from services.decision_lifecycle import complete_run

    complete_run(
        run,
        outcome="failed",
        reason=reason[:2000],
        error_code=code[:64],
        error_detail=reason[:2000],
    )
    run.refresh_from_db()


def _decision_records(steps: Sequence[dict]) -> list[dict]:
    """The decision/fill records without generated ids.

    Two independent replays of the same committed inputs have different
    ``DecisionRun``/snapshot UUIDs, so their full timelines are not byte-equal.
    These records are: the clock, the decision, the fill and the stable reason.
    That is what "replaying the committed fixtures twice" can mean without
    pretending the database generated the same UUID twice.
    """
    records: list[dict] = []
    for step in steps:
        if step["event"] == "decision":
            records.append(
                {
                    "event": "decision",
                    "clock": step["clock"],
                    "opportunity": step["opportunity"],
                    "ticker": step["ticker"],
                    "action": (step.get("decision") or {}).get("action"),
                    "requested_quantity": (step.get("decision") or {}).get("requested_quantity"),
                    "fill_rule_status": step["order"]["fill_rule_status"],
                    "fill_reason": step["order"]["fill_reason"],
                }
            )
        elif step["event"] == "fill":
            fill = step["fill"]
            records.append(
                {
                    "event": "fill",
                    "clock": step["clock"],
                    "opportunity": step["opportunity"],
                    "ticker": fill["ticker"],
                    "side": fill["side"],
                    "quantity": fill["quantity"],
                    "price_usd": fill["price_usd"],
                    "fee_usd": fill["fee_usd"],
                }
            )
    return records


def _steps_are_monotonic(steps: Sequence[dict]) -> bool:
    previous: str | None = None
    for step in steps:
        clock = step.get("clock")
        if not isinstance(clock, str):
            return False
        if previous is not None and clock < previous:
            return False
        previous = clock
    return True


# ---------------------------------------------------------------------------
# Cost aggregation and the comparison report
# ---------------------------------------------------------------------------
def _aggregate_costs(arm_runs: Sequence[ArmRun]) -> dict:
    """Sum the arm's costs without ever pooling measured, estimated and unknown."""

    def tokens(key: str, field: str) -> int:
        return sum(int(run.cost.get(key, {}).get(field, 0)) for run in arm_runs)

    billed_runs = [
        _decimal(run.cost.get("billed_cost_usd"), field_name="billed_cost_usd")
        for run in arm_runs
        if run.cost.get("billed_cost_usd") is not None
    ]
    priced_runs = [
        _decimal(run.cost.get("price_table_cost_usd"), field_name="price_table_cost_usd")
        for run in arm_runs
        if run.cost.get("price_table_cost_usd") is not None
    ]
    unknown_runs = [run for run in arm_runs if run.cost.get("billed_cost_usd") is None]
    reasons: dict[str, int] = {}
    for run in unknown_runs:
        reason = run.cost.get("unknown_reason") or UNKNOWN_PROVIDER_COST
        reasons[reason] = reasons.get(reason, 0) + 1
    partial = sum(
        (
            _decimal(
                run.cost.get("price_table_cost_partial_usd"),
                field_name="price_table_cost_partial_usd",
            )
            for run in arm_runs
            if run.cost.get("price_table_cost_partial_usd") is not None
        ),
        ZERO,
    )
    every_run_priced = bool(arm_runs) and len(priced_runs) == len(arm_runs)
    return {
        "runs": len(arm_runs),
        "measured_tokens": {
            "input": tokens("measured_tokens", "input"),
            "output": tokens("measured_tokens", "output"),
        },
        "estimated_tokens": {
            "input": tokens("estimated_tokens", "input"),
            "output": tokens("estimated_tokens", "output"),
        },
        "price_table_cost_usd": (
            str(sum(priced_runs, ZERO).quantize(Decimal("0.000001"))) if every_run_priced else None
        ),
        "price_table_cost_partial_usd": (
            str(partial.quantize(Decimal("0.000001")))
            if priced_runs and not every_run_priced
            else None
        ),
        "price_table_cost_unknown_runs": len(arm_runs) - len(priced_runs),
        "billed_cost_usd": (
            str(sum(billed_runs, ZERO).quantize(Decimal("0.000001")))
            if len(billed_runs) == len(arm_runs) and arm_runs
            else None
        ),
        "billed_cost_unknown_runs": len(unknown_runs),
        "billed_cost_unknown_reasons": dict(sorted(reasons.items())),
        "timed_out_calls": sum(int(run.cost.get("timed_out_calls", 0)) for run in arm_runs),
        "unreconciled_timeout_calls": sum(
            int(run.cost.get("unreconciled_timeout_calls", 0)) for run in arm_runs
        ),
        "strict_billed_ceiling": False,
        "note": (
            "Measured tokens, estimated tokens, price-table cost and billed cost are "
            "reported separately. A null billed total means at least one run's provider "
            "reported no charge; it is not a zero."
        ),
    }


def _aggregate_cost_across_arms(arm_costs: Sequence[dict]) -> dict:
    billed = [cost["billed_cost_usd"] for cost in arm_costs if cost["billed_cost_usd"] is not None]
    priced = [
        cost["price_table_cost_usd"]
        for cost in arm_costs
        if cost["price_table_cost_usd"] is not None
    ]
    reasons: dict[str, int] = {}
    for cost in arm_costs:
        for reason, count in (cost.get("billed_cost_unknown_reasons") or {}).items():
            reasons[reason] = reasons.get(reason, 0) + count
    return {
        "measured_tokens": {
            "input": tokens_with(arm_costs, "measured_tokens", "input"),
            "output": tokens_with(arm_costs, "measured_tokens", "output"),
        },
        "estimated_tokens": {
            "input": tokens_with(arm_costs, "estimated_tokens", "input"),
            "output": tokens_with(arm_costs, "estimated_tokens", "output"),
        },
        "price_table_cost_usd": (
            str(sum(Decimal(value) for value in priced).quantize(Decimal("0.000001")))
            if arm_costs and len(priced) == len(arm_costs)
            else None
        ),
        "billed_cost_usd": (
            str(sum(Decimal(value) for value in billed).quantize(Decimal("0.000001")))
            if arm_costs and len(billed) == len(arm_costs)
            else None
        ),
        "billed_cost_unknown_runs": sum(
            int(cost.get("billed_cost_unknown_runs", 0)) for cost in arm_costs
        ),
        "billed_cost_unknown_reasons": dict(sorted(reasons.items())),
        "timed_out_calls": sum(int(cost.get("timed_out_calls", 0)) for cost in arm_costs),
        "unreconciled_timeout_calls": sum(
            int(cost.get("unreconciled_timeout_calls", 0)) for cost in arm_costs
        ),
        "strict_billed_ceiling": False,
        "note": (
            "A null total means at least one arm's cost was not reported. The report never "
            "substitutes the price-table estimate for a billed figure, and never zero for "
            "unknown."
        ),
    }


def tokens_with(arm_costs: Sequence[dict], key: str, field: str) -> int:
    return sum(int(cost.get(key, {}).get(field, 0)) for cost in arm_costs)


# ---------------------------------------------------------------------------
# The comparison report
# ---------------------------------------------------------------------------
RISK_RULE_FIELDS = (
    "max_trade_allocation_pct",
    "max_portfolio_exposure_pct",
    "max_ticker_concentration_pct",
    "max_sector_concentration_pct",
    "max_turnover_pct",
    "min_cash_buffer_pct",
    "daily_loss_limit_usd",
)


def _risk_rules_check(opportunity_set: OpportunitySet, portfolio: Portfolio) -> dict:
    """Compare the declared rules with the portfolio's current configuration.

    Historical risk configuration is not versioned, so this is the same caveat the
    experiment report makes: a match is necessary, not sufficient.
    """
    checks: dict[str, dict] = {}
    statuses = []
    for key in RISK_RULE_FIELDS:
        if key not in opportunity_set.risk_rules:
            continue
        declared = _decimal(opportunity_set.risk_rules[key], field_name=f"risk_rules.{key}")
        current = getattr(portfolio, key)
        matches = declared == current
        statuses.append(matches)
        checks[key] = {
            "declared": str(declared),
            "portfolio_current": str(current),
            "status": "match" if matches else "mismatch",
        }
    if not checks:
        status = "not_declared"
    elif all(statuses):
        status = "match"
    else:
        status = "mismatch"
    return {
        "status": status,
        "checks": checks,
        "note": (
            "The declared rules are what each arm's guard evaluated. They are compared "
            "with the portfolio's current configuration because historical risk "
            "configuration is not versioned; a match is necessary, not sufficient."
        ),
    }


def _portfolio_snapshot(portfolio: Portfolio | None) -> dict:
    if portfolio is None:
        return {"available": False}
    return {
        "available": True,
        "portfolio_id": portfolio.id,
        "risk_profile": portfolio.risk_profile,
        "limits": {
            key: str(getattr(portfolio, key)) for key in RISK_RULE_FIELDS if hasattr(portfolio, key)
        },
    }


def methods_block(metric: dict) -> dict:
    from services.portfolio_accounting import METHOD_VERSION as ACCOUNTING_METHOD_VERSION

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "archive_format": ARCHIVE_FORMAT,
        "simulation": SIMULATION_METHOD_VERSION,
        "fill_timing": FILL_TIMING_RULE,
        "fill_rule": FILL_RULE,
        "fill_validation": FILL_VALIDATION_POLICY,
        "cost_basis": COST_BASIS_POLICY,
        "fee_policy": FEE_POLICY,
        "trading_day": TRADING_DAY_POLICY,
        "benchmark_match": BENCHMARK_MATCH_POLICY,
        "attempt_identity": ATTEMPT_IDENTITY_VERSION,
        "accounting": ACCOUNTING_METHOD_VERSION,
        "metric": "registered_signal_metric.v1",
        "registered_metric_id": metric.get("id"),
    }


def build_comparison_report(
    *,
    opportunity_set: OpportunitySet,
    arm_runs: dict[str, list[ArmRun]],
    trajectories: dict[str, dict],
    provider_mode: str,
    generated_at: datetime | None = None,
    portfolio: Portfolio | None = None,
    execution: dict | None = None,
) -> dict:
    """Assemble the V0-V3 comparison from the runs that actually happened.

    The report's job is to make the denominators and the provenance impossible to
    misread: which arms ran, under which protocol id, against which snapshot, and
    which numbers are measured, estimated or unknown.
    """
    now = generated_at or timezone.now()
    arms: list[dict] = []
    scheduled_total = completed_total = failed_total = not_executed_total = 0
    cost_by_arm: list[dict] = []
    protocol_ids: dict[str, str] = {}
    reference_snapshots: dict[str, str] | None = None
    identical_snapshots = True
    snapshot_sets: dict[str, dict[str, dict]] = {}

    for arm in ARM_ORDER:
        runs = arm_runs.get(arm, [])
        implemented = arm != "V4"
        snapshots: dict[str, dict] = {}
        if implemented:
            for run in runs:
                scheduled_total += 1
                if run.status == "completed":
                    completed_total += 1
                elif run.status == "failed":
                    failed_total += 1
                else:
                    not_executed_total += 1
                snapshot = (
                    {
                        "snapshot_id": run.snapshot.id,
                        "content_hash": run.snapshot.content_hash,
                        "opportunity": run.opportunity,
                    }
                    if run.snapshot is not None
                    else None
                )
                if snapshot is not None:
                    snapshots[run.opportunity] = snapshot
            snapshot_sets[arm] = snapshots
            if reference_snapshots is None:
                reference_snapshots = {
                    key: value["content_hash"] for key, value in snapshots.items()
                }
            else:
                comparable = {key: value["content_hash"] for key, value in snapshots.items()}
                if comparable != reference_snapshots:
                    identical_snapshots = False
        cost = _aggregate_costs(runs) if runs else _aggregate_costs(())
        recorded_protocols = sorted({run.protocol_version for run in runs if run.protocol_version})
        expected_protocol = ARM_PROTOCOLS.get(arm, "")
        if recorded_protocols:
            protocol_ids[arm] = recorded_protocols[0]
        else:
            protocol_ids[arm] = expected_protocol
        arm_status = "not_executed"
        if any(run.status == "completed" for run in runs):
            arm_status = "executed"
        if any(run.status == "failed" for run in runs):
            arm_status = "partially_failed" if arm_status == "executed" else "failed"
        trajectory = trajectories.get(arm)
        reason = ""
        if arm == "V4":
            reason = UNEXECUTED_REASONS["V4"]
        elif arm_status == "not_executed":
            reason = next(
                (
                    run.failure_detail or run.failure_code
                    for run in runs
                    if run.failure_detail or run.failure_code
                ),
                "the arm produced no recorded run",
            )
        arms.append(
            {
                "key": arm,
                "label": ARM_LABELS[arm],
                "engine": ARM_ENGINES[arm],
                "implemented": implemented,
                "status": arm_status,
                "reason": reason or None,
                "protocol_version": protocol_ids[arm],
                "expected_protocol_version": expected_protocol,
                "protocol_matches_declaration": (
                    None if not recorded_protocols else recorded_protocols == [expected_protocol]
                ),
                "requires_rebuttals": arm == "V3",
                "provider": (
                    "deterministic"
                    if arm == "V0"
                    else ("stub" if provider_mode == "stub" else provider_mode)
                ),
                "scheduled_runs": len(runs) if implemented else 0,
                "completed_runs": sum(1 for run in runs if run.status == "completed"),
                "failed_runs": sum(1 for run in runs if run.status == "failed"),
                "not_executed_runs": sum(1 for run in runs if run.status == "not_executed"),
                "excluded_from_denominator": not implemented,
                "cost": cost,
                "runs": [run.as_dict() for run in runs],
                "snapshots": list(snapshots.values()),
                "trajectory": trajectory,
            }
        )
        if implemented:
            cost_by_arm.append(cost)

    distinct_protocol_ids = len({protocol_ids[arm] for arm in ("V0", "V1", "V2", "V3")}) == 4
    sample_plan = opportunity_set.sample_plan
    completed_arms = sum(1 for arm in arms if arm["implemented"] and arm["status"] == "executed")
    if reference_snapshots is None:
        identical_snapshots = False
        reference_snapshots = {}

    decisions_by_arm = {
        arm: [run.decision for run in arm_runs.get(arm, []) if run.decision is not None]
        for arm in ("V0", "V1", "V2", "V3")
    }
    run_states_by_arm = {
        arm: {
            run.opportunity: {
                "status": run.status,
                "failure_code": run.failure_code,
            }
            for run in arm_runs.get(arm, [])
        }
        for arm in ("V0", "V1", "V2", "V3")
    }
    metric_result = compute_registered_metric(
        metric=opportunity_set.primary_metric,
        opportunities=opportunity_set.opportunities,
        marks=opportunity_set.marks,
        decisions_by_arm=decisions_by_arm,
        trajectories=trajectories,
        as_of=opportunity_set.as_of,
        uncertainty=dict(opportunity_set.pre_registration.get("uncertainty") or {}),
        benchmark=opportunity_set.benchmark,
        benchmark_marks=opportunity_set.benchmark_marks,
        run_states_by_arm=run_states_by_arm,
    )
    for arm in arms:
        arm["primary_metric"] = metric_result["by_arm"].get(arm["key"])
        arm["synthetic"] = True
        if arm["key"] == "V4":
            arm["primary_metric"] = {
                "arm": "V4",
                "status": "not_executed",
                "value": None,
                "unit": "percent",
                "in_denominator": False,
                "reason": UNEXECUTED_REASONS["V4"],
                "denominator_eligible": False,
            }

    pre_registration = {
        "registered_at": opportunity_set.pre_registration.get("registered_at"),
        "registered_by": opportunity_set.pre_registration.get("registered_by"),
        "declared_before_first_opportunity": (
            parse_at(opportunity_set.pre_registration["registered_at"], field_name="registered_at")
            <= opportunity_set.opportunities[0].scheduled_at
        ),
        "sample_plan": sample_plan,
        "primary_metric": opportunity_set.primary_metric,
        "cost_ceiling_usd": str(
            _decimal(
                opportunity_set.pre_registration.get("cost_ceiling_usd"),
                field_name="cost_ceiling_usd",
            )
        ),
        "acceptable_drawdown_pct": str(
            _decimal(
                opportunity_set.pre_registration.get("acceptable_drawdown_pct"),
                field_name="acceptable_drawdown_pct",
            )
        ),
        "honoured": {
            "opportunities": len(opportunity_set.opportunities)
            >= int(sample_plan.get("minimum_opportunities") or 0),
            "completed_arms": completed_arms >= int(sample_plan.get("minimum_completed_arms") or 0),
            "primary_metric": {
                "computed": metric_result["status"] == "computed",
                "status": metric_result["status"],
                "value": metric_result["value"],
                "denominator": metric_result["denominator"],
                "reason": metric_result["reason"],
                "missing_input": metric_result["missing_input"],
            },
        },
        "sample_gate": {
            "minimum_opportunities": sample_plan.get("minimum_opportunities"),
            "minimum_completed_arms": sample_plan.get("minimum_completed_arms"),
            "minimum_observations": sample_plan.get("minimum_observations"),
            "opportunities": len(opportunity_set.opportunities),
            "completed_arms": completed_arms,
            "observations": metric_result["denominator"]["usable"],
            "observations_met": (
                None
                if not sample_plan.get("minimum_observations")
                else metric_result["denominator"]["usable"]
                >= int(sample_plan.get("minimum_observations") or 0)
            ),
            "met": (
                len(opportunity_set.opportunities)
                >= int(sample_plan.get("minimum_opportunities") or 0)
                and completed_arms >= int(sample_plan.get("minimum_completed_arms") or 0)
                and (
                    not sample_plan.get("minimum_observations")
                    or metric_result["denominator"]["usable"]
                    >= int(sample_plan.get("minimum_observations") or 0)
                )
            ),
        },
        "primary_metric_result": metric_result,
    }

    drawdown_gate = {
        "acceptable_drawdown_pct": pre_registration["acceptable_drawdown_pct"],
        "per_arm": {},
        "note": (
            "Drawdowns are from each arm's simulated trajectory under the frozen marks, not "
            "from realised account history. They are an engineering check, not evidence of "
            "risk control."
        ),
    }
    acceptable = _decimal(
        opportunity_set.pre_registration.get("acceptable_drawdown_pct"),
        field_name="acceptable_drawdown_pct",
    )
    for arm in arms:
        if not arm["implemented"] or not arm["trajectory"]:
            continue
        value = arm["trajectory"].get("max_drawdown_pct")
        drawdown_gate["per_arm"][arm["key"]] = {
            "max_drawdown_pct": value,
            "within_acceptable": (
                None if value is None else abs(_decimal(value, field_name="drawdown")) <= acceptable
            ),
        }

    aggregate_cost = _aggregate_cost_across_arms(cost_by_arm)
    run_ids = {
        arm: [
            run.decision_run_id or (str(run.run.id) if run.run is not None else "") for run in runs
        ]
        for arm, runs in arm_runs.items()
    }
    aggregate_cost["scope"] = {
        "execution_id": (execution or {}).get("execution_id"),
        "namespace": (execution or {}).get("namespace"),
        "arms": ["V0", "V1", "V2", "V3"],
        "run_count": sum(len([run_id for run_id in ids if run_id]) for ids in run_ids.values()),
        "run_ids": {arm: [run_id for run_id in ids if run_id] for arm, ids in run_ids.items()},
        "note": "The ledger is scoped to this execution's namespaced runs only.",
    }
    methods = methods_block(opportunity_set.primary_metric)
    manifest_payload = json_safe(opportunity_set.document)
    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "archive_format": ARCHIVE_FORMAT,
        "generated_at": utc_iso(now),
        "opportunity_set_id": opportunity_set.opportunity_set_id,
        "label": opportunity_set.label,
        "provider_mode": provider_mode,
        "synthetic": True,
        "methods": methods,
        "pre_registration": pre_registration,
        "primary_metric_result": metric_result,
        "frozen": {
            "opportunity_set": json_safe(opportunity_set.document),
            "opportunity_set_hash": opportunity_set.content_hash,
            "source_path": opportunity_set.source_path or None,
            "declared_at": utc_iso(opportunity_set.declared_at),
            "observation_window": {
                "start": utc_iso(opportunity_set.window_start),
                "end": utc_iso(opportunity_set.window_end),
            },
            "starting_state": {
                "cash": str(opportunity_set.starting_state["cash"]),
                "positions": {
                    ticker: str(quantity)
                    for ticker, quantity in opportunity_set.starting_state["positions"].items()
                },
                "average_cost": {
                    ticker: str(cost)
                    for ticker, cost in opportunity_set.starting_state.get(
                        "average_cost", {}
                    ).items()
                },
            },
            "portfolio": _portfolio_snapshot(portfolio),
            "execution_assumptions": opportunity_set.execution_assumptions,
            "risk_rules": opportunity_set.risk_rules,
            "benchmark": opportunity_set.benchmark,
            "benchmark_mark_count": len(opportunity_set.benchmark_marks),
            "benchmark_mark_sources": PriceBook(opportunity_set.benchmark_marks).source_counts(),
            "methods": methods,
            "mark_count": len(opportunity_set.marks),
            "mark_sources": PriceBook(opportunity_set.marks).source_counts(),
            "mark_provenance": (
                "Daily marks declared in the opportunity set. Their observation instants are "
                "recorded per mark; they are authored, not fetched, and are labelled synthetic."
            ),
        },
        "arms": arms,
        "benchmark": {
            "declared": opportunity_set.benchmark,
            "matching_policy": BENCHMARK_MATCH_POLICY,
            "target_policy": "benchmark_first_mark_at_or_after_target_session.v1",
            "marks_recorded": len(opportunity_set.benchmark_marks),
            "pairs": metric_result.get("benchmark_pairs"),
            "relative_metric_id": "mean_relative_forward_return_pct",
            "note": (
                "The benchmark's entry and horizon observations are recorded beside each "
                "instrument observation. A relative result uses matched pairs only; an "
                "excluded pair is published with its reason and is never a zero."
            ),
        },
        "execution": execution or {},
        "verification": {
            "distinct_protocol_ids": distinct_protocol_ids,
            "protocol_ids": protocol_ids,
            "identical_snapshots_across_arms": identical_snapshots,
            "reference_snapshots": reference_snapshots,
            "snapshot_sets": snapshot_sets,
            "execution_id": (execution or {}).get("execution_id"),
            "configuration_id": (execution or {}).get("configuration_id"),
            "namespace": (execution or {}).get("namespace"),
            "run_ids": run_ids,
            "run_count": aggregate_cost["scope"]["run_count"],
            "recompute_command": (
                "python manage.py rebuild_experiment_report --path <archived-report.json>"
            ),
            "metric_path": "pre_registration.primary_metric_result",
            "content_hash_path": "contents_hash",
            "archive_format": ARCHIVE_FORMAT,
            "same_opportunity_set": all(
                arm["scheduled_runs"] == len(opportunity_set.opportunities)
                for arm in arms
                if arm["implemented"] and arm["scheduled_runs"]
            ),
            "note": (
                "Protocol ids are read from the runs, not copied from the declaration. "
                "Snapshot identity is compared by content hash per opportunity across arms. "
                "Run identity is namespaced to this execution."
            ),
        },
        "denominators": {
            "rule": "all_scheduled_runs_of_implemented_arms",
            "implemented_arms": ["V0", "V1", "V2", "V3"],
            "excluded_unimplemented_arms": ["V4"],
            "opportunities": len(opportunity_set.opportunities),
            "scheduled": scheduled_total,
            "completed": completed_total,
            "failed": failed_total,
            "not_executed": not_executed_total,
            "completion_rate_pct": (
                None
                if not scheduled_total
                else str(
                    (Decimal(completed_total) / Decimal(scheduled_total) * 100).quantize(
                        Decimal("0.0001")
                    )
                )
            ),
            "metric_observations": metric_result["denominator"]["usable"],
            "metric_scheduled_observations": metric_result["denominator"]["scheduled"],
            "metric_excluded_observations": metric_result["denominator"]["excluded"],
            "metric_by_state": metric_result["denominator"]["by_state"],
            "metric_excluded_by_reason": metric_result["denominator"]["excluded_by_reason"],
            "v4": {
                "status": "not_executed",
                "implemented": False,
                "in_denominator": False,
                "reason": UNEXECUTED_REASONS["V4"],
            },
            "note": (
                "A missing or failed run is counted in the denominator. Dropping it would "
                "make an arm look complete by omission. V4 is excluded because it did not "
                "execute, not because its result was zero."
            ),
        },
        "fairness": {
            "equal_opportunity": {
                "opportunities_per_implemented_arm": len(opportunity_set.opportunities),
                "implemented_arms": ["V0", "V1", "V2", "V3"],
                "v4_excluded": True,
            },
            "same_snapshots": identical_snapshots,
            "same_risk_rules": (
                _risk_rules_check(opportunity_set, portfolio)
                if portfolio is not None
                else {
                    "status": "not_verifiable",
                    "checks": {},
                    "reason": "the portfolio configuration was not recorded",
                }
            ),
            "same_execution_assumptions": {
                "declared": opportunity_set.execution_assumptions,
                "applied_by_all_arms": True,
                "fill_rule": FILL_RULE,
                "fill_validation": FILL_VALIDATION_POLICY,
                "cost_basis": COST_BASIS_POLICY,
                "fee_policy": FEE_POLICY,
                "trading_day": TRADING_DAY_POLICY,
            },
            "same_accounting_method": "portfolio_accounting.v1",
            "note": (
                "The arms share the opportunity set, snapshots, risk rules, execution "
                "assumptions and accounting method. They differ only in protocol and in "
                "the decisions that follow from it."
            ),
        },
        "cost_breakdown": aggregate_cost,
        "drawdown_gate": drawdown_gate,
        "honesty": {
            "engineering_validation_only": True,
            "no_profitability_claim": True,
            "profitability_threshold": None,
            "simulated_provider": provider_mode == "stub",
            "simulated_content": True,
            "synthetic_label": "synthetic_simulation",
            "real_market_outcome": False,
            "comparative_protocol_claim": False,
            "reason": (
                "The opportunity set, marks and arm trajectories are authored/simulated. "
                "With the scripted provider the model outputs are not LLM samples, and even "
                "with a live provider a single opportunity set is engineering validation, "
                "not a comparative result."
            ),
            "what_is_real": (
                "The protocol code paths, the shared snapshots and their hashes, the guard "
                "evaluation, the fill rule, the accounting method and the cost separation."
            ),
            "what_is_not_real": (
                "The prices, the model outputs (with the scripted provider), and therefore "
                "any return, drawdown or protocol ranking derived from them."
            ),
        },
        "manifest": {
            "experiment_id": opportunity_set.opportunity_set_id,
            "declared_at": utc_iso(opportunity_set.declared_at),
            "observation_window": {
                "start": utc_iso(opportunity_set.window_start),
                "end": utc_iso(opportunity_set.window_end),
            },
            "primary_metric": opportunity_set.primary_metric,
            "sample_requirements": sample_plan,
            "cost_ceiling_usd": pre_registration["cost_ceiling_usd"],
            "acceptable_drawdown_pct": pre_registration["acceptable_drawdown_pct"],
            "checksum": content_hash(manifest_payload),
        },
        "contents_hash": "",
    }
    report["contents_hash"] = content_hash(
        {key: value for key, value in report.items() if key != "contents_hash"}
    )
    return json_safe(report)


# ---------------------------------------------------------------------------
# Schedule the arms
# ---------------------------------------------------------------------------
def execute_opportunity_set(
    *,
    opportunity_set: OpportunitySet,
    portfolio: Portfolio,
    client=None,
    provider_mode: str = "auto",
    force: bool = False,
    budget: ProtocolBudget | None = None,
    now: datetime | None = None,
) -> dict:
    """Schedule V0-V3 against the frozen opportunities and return the report.

    Idempotent per ``(namespace, arm, opportunity)``: a run whose namespaced
    fixture id already exists is reused rather than re-executed, so re-running
    the command neither double-spends nor duplicates a decision. A different
    portfolio, universe or provider mode has a different namespace and cannot
    reuse these runs.
    """
    started = now or timezone.now()
    registered_at = parse_at(
        opportunity_set.pre_registration.get("registered_at"),
        field_name="pre_registration.registered_at",
    )
    if registered_at > started:
        raise ExecutionError(
            f"pre_registration.registered_at ({utc_iso(registered_at)}) is after this "
            f"execution ({utc_iso(started)}): a registration written in the future cannot "
            "govern a run that has already started."
        )
    namespace = execution_namespace(
        portfolio_id=portfolio.id,
        opportunity_set_id=opportunity_set.opportunity_set_id,
        opportunity_set_hash=opportunity_set.content_hash,
        provider_mode=provider_mode,
        client_identity=(client.__class__.__name__ if client is not None else ""),
    )
    snapshots: dict[str, InputSnapshot] = {}
    evidence: dict[str, dict] = {}
    for opportunity in opportunity_set.opportunities:
        snapshot = ensure_snapshot(opportunity_set, opportunity, portfolio)
        snapshots[opportunity.key] = snapshot
        evidence[opportunity.key] = ensure_evidence(opportunity, snapshot)

    arm_runs: dict[str, list[ArmRun]] = {arm: [] for arm in ARM_ORDER}
    arm_mode: dict[str, str] = {}
    arm_identity: dict[str, AttemptIdentity] = {}
    resolved_budget = budget or ProtocolBudget.from_settings()
    for arm in ("V0", "V1", "V2", "V3"):
        arm_client = None
        reason = ""
        if arm != "V0":
            arm_client, used_mode, reason = _client_for_arm(
                arm, client=client, provider_mode=provider_mode, portfolio=portfolio
            )
            arm_mode[arm] = used_mode
        else:
            arm_mode[arm] = "deterministic"
        identity = resolve_attempt_identity(
            arm=arm,
            protocol_version=ARM_PROTOCOLS[arm],
            client=arm_client,
            provider_mode=arm_mode[arm],
            budget=None if arm == "V0" else resolved_budget,
            execution_assumptions=opportunity_set.execution_assumptions,
            simulation_method_version=SIMULATION_METHOD_VERSION,
            fill_timing_rule=FILL_TIMING_RULE,
            trading_day_policy=TRADING_DAY_POLICY,
            cost_basis_policy=COST_BASIS_POLICY,
            fee_policy=FEE_POLICY,
        )
        arm_identity[arm] = identity
        if arm != "V0" and arm_client is None:
            for opportunity in opportunity_set.opportunities:
                prefix = attempt_prefix(
                    namespace=namespace, arm=arm, opportunity_key=opportunity.key
                )
                arm_runs[arm].append(
                    ArmRun(
                        arm=arm,
                        opportunity=opportunity.key,
                        fixture_id=fixture_id_for_attempt(
                            prefix=prefix,
                            identity_hash=identity.identity_hash,
                            attempt=1,
                        ),
                        status="not_executed",
                        failure_code="provider_not_configured",
                        failure_detail=reason,
                        protocol_version=ARM_PROTOCOLS[arm],
                        identity=identity,
                        attempt=1,
                        cost=_cost_of_run(None),
                    )
                )
            continue
        for opportunity in opportunity_set.opportunities:
            arm_runs[arm].append(
                _run_arm_once(
                    arm=arm,
                    opportunity=opportunity,
                    portfolio=portfolio,
                    snapshot=snapshots[opportunity.key],
                    client=arm_client,
                    provider_mode=arm_mode[arm],
                    identity=identity,
                    budget=resolved_budget,
                    force=force,
                    namespace=namespace,
                )
            )
    arm_runs["V4"] = [
        ArmRun(
            arm="V4",
            opportunity=opportunity.key,
            fixture_id=fixture_id_for_attempt(
                prefix=attempt_prefix(
                    namespace=namespace, arm="V4", opportunity_key=opportunity.key
                ),
                identity_hash="unimplemented",
                attempt=1,
            ),
            status="not_executed",
            failure_code="unimplemented",
            failure_detail=UNEXECUTED_REASONS["V4"],
            protocol_version="",
            cost=_cost_of_run(None),
        )
        for opportunity in opportunity_set.opportunities
    ]

    trajectories: dict[str, dict] = {}
    for arm in ("V0", "V1", "V2", "V3"):
        if any(run.status == "completed" or run.run is not None for run in arm_runs[arm]):
            trajectories[arm] = simulate_arm_accounting(
                arm=arm,
                opportunity_set=opportunity_set,
                runs=arm_runs[arm],
                portfolio=portfolio,
                snapshots_by_opportunity=snapshots,
            )

    used = set(arm_mode.values())
    if "live" in used:
        provider_used = "live"
    elif "stub" in used:
        provider_used = "stub"
    else:
        provider_used = "auto"
    configuration_id = content_hash(
        {
            "version": ATTEMPT_IDENTITY_VERSION,
            "arms": {arm: arm_identity[arm].identity_hash for arm in ("V0", "V1", "V2", "V3")},
        }
    )
    execution = {
        "execution_id": namespace,
        "execution_id_kind": "deterministic_namespace.v1",
        "configuration_id": configuration_id,
        "configuration_id_kind": "resolved_attempt_identities.v1",
        "namespace": namespace,
        "namespace_version": EXECUTION_NAMESPACE_VERSION,
        "portfolio_id": portfolio.id,
        "provider_mode_requested": provider_mode,
        "provider_mode_used": provider_used,
        "started_at": utc_iso(started),
        "snapshots_used": {
            key: _snapshot_input(snapshot).as_dict() for key, snapshot in snapshots.items()
        },
        "evidence": evidence,
        "client_modes": arm_mode,
        "attempt_identity_version": ATTEMPT_IDENTITY_VERSION,
        "attempt_identities": {arm: arm_identity[arm].as_dict() for arm in arm_identity},
        "run_ids": {
            arm: [
                run.decision_run_id or (str(run.run.id) if run.run is not None else "")
                for run in arm_runs[arm]
            ]
            for arm in ("V0", "V1", "V2", "V3")
        },
        "run_count": sum(
            len([run for run in arm_runs[arm] if run.decision_run_id or run.run is not None])
            for arm in ("V0", "V1", "V2", "V3")
        ),
        "idempotent": True,
        "note": (
            "Re-running with the same inputs and provider mode reuses this namespace's runs. "
            "A different portfolio, opportunity-set content or provider mode has its own "
            "namespace and cannot read or overwrite these decisions or costs."
        ),
    }
    report = build_comparison_report(
        opportunity_set=opportunity_set,
        arm_runs=arm_runs,
        trajectories=trajectories,
        provider_mode=provider_used,
        generated_at=started,
        portfolio=portfolio,
        execution=execution,
    )
    return json_safe(report)
