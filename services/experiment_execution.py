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

``V4`` (portfolio allocation) is declared and **not executed**: its dependency
FIX-08 is not done. It is excluded from the equal-opportunity denominator rather
than counted as a zero-opportunity arm.

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
* The pre-registered primary metric needs matured outcomes. If none exist, its
  value is ``null`` with the reason, not a number built from something else.
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
from services.execution import Exposure, evaluate_proposal
from services.ledger import PositionState
from services.portfolio_accounting import (
    FILL_RULE,
    Fill,
    LlmCost,
    OpeningState,
    PriceBook,
    PriceMark,
    build_performance,
    select_fill_price,
)

__all__ = [
    "ARM_ORDER",
    "ExecutionError",
    "OpportunitySet",
    "build_comparison_report",
    "execute_opportunity_set",
    "load_opportunity_set",
    "simulate_arm_accounting",
    "validate_opportunity_set",
]

SCHEMA_VERSION = "1.0"
REPORT_SCHEMA_VERSION = "1.0"
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
        "V4 (portfolio allocation) is declared and not implemented: its dependency FIX-08 "
        "is not done. It is excluded from the equal-opportunity denominator rather than "
        "measured as a zero."
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
    content_hash: str = ""
    source_path: str = ""

    @property
    def primary_metric(self) -> dict:
        return dict(self.pre_registration.get("primary_metric") or {})

    @property
    def sample_plan(self) -> dict:
        return dict(self.pre_registration.get("sample_plan") or {})

    def price_book(self) -> PriceBook:
        return PriceBook(self.marks)


def _parse_at(value, *, field_name: str) -> datetime:
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
    declared_at = _parse_at(document.get("declared_at"), field_name="declared_at")

    pre = document.get("pre_registration")
    if not isinstance(pre, dict):
        raise ExecutionError(
            "pre_registration is required: the sample plan, primary metric, cost ceiling "
            "and drawdown criterion must be registered before any arm runs."
        )
    registered_at = _parse_at(pre.get("registered_at"), field_name="pre_registration.registered_at")
    if registered_at > declared_at:
        raise ExecutionError(
            "pre_registration.registered_at is after declared_at; a registration cannot be "
            "written after the experiment it governs."
        )
    primary = pre.get("primary_metric")
    if not isinstance(primary, dict) or not primary.get("id"):
        raise ExecutionError("pre_registration.primary_metric needs an id.")
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
    window_start = _parse_at(window.get("start"), field_name="observation_window.start")
    window_end = _parse_at(window.get("end"), field_name="observation_window.end")
    if window_end < window_start:
        raise ExecutionError("observation_window.end precedes its start.")

    starting = document.get("starting_state")
    if not isinstance(starting, dict) or "cash" not in starting:
        raise ExecutionError("starting_state needs at least cash.")
    starting_state = {
        "cash": _decimal(starting.get("cash"), field_name="starting_state.cash"),
        "positions": {
            str(ticker).upper(): _decimal(quantity, field_name="starting_state.positions")
            for ticker, quantity in (starting.get("positions") or {}).items()
        },
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
        scheduled_at = _parse_at(
            raw.get("scheduled_at"), field_name=f"opportunity {key}.scheduled_at"
        )
        if not (window_start <= scheduled_at <= window_end):
            raise ExecutionError(
                f"opportunity {key!r} is scheduled outside the declared observation window."
            )
        snapshot_spec = raw.get("snapshot")
        if not isinstance(snapshot_spec, dict):
            raise ExecutionError(f"opportunity {key} needs a snapshot object.")
        evidence = raw.get("evidence") or []
        if not isinstance(evidence, list):
            raise ExecutionError(f"opportunity {key}.evidence must be a list.")
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
        at = _parse_at(raw.get("at"), field_name=f"mark {index}.at")
        observed_at = (
            _parse_at(raw.get("observed_at"), field_name=f"mark {index}.observed_at")
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
            "V4 is declared and unimplemented: its dependency FIX-08 is not done. It must not "
            "be marked implemented."
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
            _parse_at(document.get("as_of"), field_name="as_of") if document.get("as_of") else None
        ),
        opportunities=tuple(opportunities),
        marks=tuple(marks),
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
# Shared snapshots and evidence
# ---------------------------------------------------------------------------
def _snapshot_version(opportunity_set: OpportunitySet, opportunity: Opportunity) -> str:
    return f"{opportunity_set.opportunity_set_id}:{opportunity.key}"


def ensure_snapshot(
    opportunity_set: OpportunitySet, opportunity: Opportunity, portfolio: Portfolio
) -> InputSnapshot:
    """Return the one snapshot for this opportunity, creating it on first use.

    Every arm receives this exact row. A second snapshot for the same opportunity
    would make the comparison one of inputs rather than of protocols.
    """
    spec = opportunity.snapshot
    version = _snapshot_version(opportunity_set, opportunity)
    cutoff = _parse_at(
        spec.get("analysis_cutoff") or opportunity.scheduled_at,
        field_name=f"snapshot {opportunity.key}.analysis_cutoff",
    )
    captured_at = (
        _parse_at(spec.get("captured_at"), field_name=f"snapshot {opportunity.key}.captured_at")
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
            _parse_at(raw.get("retrieved_at"), field_name="evidence.retrieved_at")
            if raw.get("retrieved_at")
            else snapshot.analysis_cutoff
        )
        create_evidence_item(
            snapshot=snapshot,
            source_type=source_type,
            retrieved_at=retrieved_at,
            publisher=str(raw.get("publisher") or ""),
            published_at=(
                _parse_at(raw.get("published_at"), field_name="evidence.published_at")
                if raw.get("published_at")
                else None
            ),
            available_at=(
                _parse_at(raw.get("available_at"), field_name="evidence.available_at")
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

    def as_dict(self) -> dict:
        return {
            "opportunity": self.opportunity,
            "fixture_id": self.fixture_id,
            "status": self.status,
            "failure_code": self.failure_code or None,
            "failure_detail": self.failure_detail or None,
            "decision_run_id": str(self.run.id) if self.run is not None else None,
            "protocol_version": self.protocol_version or None,
            "snapshot_id": (
                str(self.run.snapshot_id) if self.run is not None and self.run.snapshot_id else None
            ),
            "snapshot_hash": (
                self.run.snapshot.content_hash
                if self.run is not None and self.run.snapshot is not None
                else None
            ),
            "cost": self.cost,
        }


def _fixture_id(opportunity_set: OpportunitySet, arm: str, opportunity: Opportunity) -> str:
    return f"{opportunity_set.opportunity_set_id}.{arm.lower()}.{opportunity.key}"


def _existing_run(fixture_id: str) -> DecisionRun | None:
    return (
        DecisionRun.objects.select_related("snapshot", "decision")
        .filter(fixture_id=fixture_id)
        .first()
    )


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


def _run_arm_once(
    *,
    arm: str,
    opportunity_set: OpportunitySet,
    opportunity: Opportunity,
    portfolio: Portfolio,
    snapshot: InputSnapshot,
    client,
    provider_mode: str,
    budget: ProtocolBudget | None = None,
) -> ArmRun:
    from services.debate_protocol import DebateProtocol

    fixture_id = _fixture_id(opportunity_set, arm, opportunity)
    result = ArmRun(
        arm=arm,
        opportunity=opportunity.key,
        fixture_id=fixture_id,
        protocol_version=ARM_PROTOCOLS[arm],
    )
    existing = _existing_run(fixture_id)
    if existing is not None:
        result.run = existing
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
        result.run = run
        result.protocol_version = run.protocol_version if run is not None else ""
        result.status = "completed" if run is not None else "not_executed"
        if run is None:
            result.failure_code = "run_not_recorded"
            result.failure_detail = "The protocol completed without persisting a run."
        result.cost = _cost_of_run(run)
    except StageFailure as exc:
        result.run = exc.run
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

    if action == "HOLD":
        outcome = "held"
        reason = "HOLD: no trade was proposed."
    elif not guard.approved:
        outcome = "rejected"
        reason = guard.reason
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
    snapshots_by_opportunity: dict[str, InputSnapshot],
) -> dict:
    """Replay one arm's decisions into its own fills and equity trajectory.

    The guard, fill rule, fee assumption and accounting method are the *same*
    objects the live path uses (``services.execution.evaluate_proposal``,
    ``first_executable_mark_at_or_after_decision.v1``,
    ``services.portfolio_accounting.build_performance``). What differs between
    arms is the decisions and therefore the fills - which is the comparison.

    The result is a simulation of that arm's decisions under frozen prices. It is
    not realised history, and its provenance says so.
    """
    book = opportunity_set.price_book()
    opening_at = opportunity_set.window_start - timedelta(seconds=1)
    cash = opportunity_set.starting_state["cash"]
    positions: dict[str, Decimal] = dict(opportunity_set.starting_state["positions"])
    average_cost: dict[str, Decimal] = dict.fromkeys(positions, ZERO)
    fills: list[Fill] = []
    orders: list[dict] = []
    assumptions = opportunity_set.execution_assumptions
    fee_bps = _decimal(assumptions.get("fee_bps") or 0, field_name="fee_bps")

    by_opportunity = {item.opportunity: item for item in runs}
    for opportunity in opportunity_set.opportunities:
        arm_run = by_opportunity.get(opportunity.key)
        decision: DecisionRecord | None = None
        if arm_run is not None and arm_run.run is not None:
            decision = DecisionRecord.objects.filter(run=arm_run.run).first()
        if decision is None:
            orders.append(
                {
                    "opportunity": opportunity.key,
                    "status": "not_executed",
                    "reason": (arm_run.failure_code or arm_run.failure_detail)
                    if arm_run is not None
                    else "no decision was recorded for this arm",
                }
            )
            continue
        action = str(decision.action or "HOLD").upper()
        requested = decision.requested_quantity or ZERO
        # The economic instant of the decision is the scheduled opportunity, not
        # the wall-clock time this replay happened to run. Using ``created_at``
        # (today) would place the order after every mark in the window, and using
        # any mark later than the scheduled instant would be look-ahead.
        decision_at = opportunity.scheduled_at
        recorded_at = decision.created_at
        marks = book
        # The price the decision actually saw is the snapshot quote; the mark book
        # is for the simulated fill and for valuation. Using a later mark as the
        # trade price would be look-ahead, and using it as the guard price would
        # misprice the exposure check the strategy faced.
        quote = _quote_for(snapshots_by_opportunity[opportunity.key], opportunity.ticker)
        quote_price = quote.get("price_usd") or quote.get("price")
        price = (
            _decimal(quote_price, field_name="quote.price")
            if quote_price not in (None, "")
            else (
                valuation.price
                if (valuation := marks.mark_at_or_before(opportunity.ticker, decision_at))
                is not None
                else None
            )
        )
        positions_value = ZERO
        for ticker, quantity in positions.items():
            mark = marks.mark_at_or_before(ticker, decision_at)
            if mark is not None:
                positions_value += quantity * mark.price
        equity = cash + positions_value
        state = PositionState(
            ticker=opportunity.ticker,
            amount=positions.get(opportunity.ticker, ZERO),
            avg_cost=average_cost.get(opportunity.ticker, ZERO),
        )
        from ai_agent import TradeProposal

        proposal = TradeProposal(
            action=action if action in {"BUY", "SELL", "HOLD"} else "HOLD",
            amount=float(requested),
            sentiment="NEUTRAL",
            reasoning=(decision.concise_rationale or "")[:2000],
        )
        view = _guard_view(portfolio, opportunity_set.risk_rules, cash, positions_value)
        exposure = Exposure(
            positions_value_usd=positions_value,
            ticker_value_usd=state.amount * price if price is not None else ZERO,
            equity_usd=equity,
            sector_value_usd=None,
        )
        guard = evaluate_proposal(
            view,
            proposal,
            price,
            state,
            realised_pnl_today=ZERO,
            exposure=exposure,
        )
        guard.policy_version = str(
            opportunity_set.execution_assumptions.get("guard_policy_version")
            or "services.execution.evaluate_proposal"
        )
        guard.evaluated_portfolio_version = f"{arm}:{opportunity.key}:cash={cash.quantize(CENT)}"
        guard.quote_time = decision_at
        guard_status = (
            "hold_no_trade" if action == "HOLD" else ("approved" if guard.approved else "rejected")
        )
        order = {
            "opportunity": opportunity.key,
            "decision_run_id": str(arm_run.run.id),
            "decision_at": utc_iso(decision_at),
            "decision_recorded_at": utc_iso(recorded_at),
            "proposal_action": action,
            "requested_quantity": str(requested) if requested else None,
            "price_at_decision_usd": None if price is None else str(price),
            "guard_status": guard_status,
            "guard_reason": guard.reason,
            "binding_rule": guard.binding_rule or None,
            "allowed_quantity": str(guard.amount) if guard.approved else "0",
            "fill": None,
            "equity_after_usd": None,
            "reason": None,
        }
        if guard.approved and action != "HOLD" and guard.amount > ZERO:
            selection = select_fill_price(
                ticker=opportunity.ticker,
                decision_at=decision_at,
                marks=marks,
            )
            if selection.price is None:
                order["reason"] = selection.reason or "no_executable_price"
            else:
                notional = (guard.amount * selection.price).quantize(CENT)
                fee = _fee_for(notional, assumptions)
                fill = Fill(
                    at=selection.at,
                    ticker=opportunity.ticker,
                    side=action,
                    quantity=guard.amount,
                    price=selection.price,
                    fee=fee,
                    executed_by="simulated",
                    source=f"simulated_arm:{arm}",
                )
                fills.append(fill)
                cash += fill.cash_effect
                current = positions.get(opportunity.ticker, ZERO)
                if action == "BUY":
                    new_quantity = current + guard.amount
                    if new_quantity > 0:
                        average_cost[opportunity.ticker] = (
                            (current * average_cost.get(opportunity.ticker, ZERO)) + notional
                        ) / new_quantity
                    positions[opportunity.ticker] = new_quantity
                else:
                    positions[opportunity.ticker] = max(current - guard.amount, ZERO)
                order["fill"] = fill.as_dict()
                order["fee_bps"] = str(fee_bps)
        closing_marks = {
            ticker: book.mark_at_or_before(ticker, decision_at) for ticker in positions
        }
        closing_value = sum(
            (
                quantity * mark.price
                for ticker, quantity in positions.items()
                if (mark := closing_marks.get(ticker)) is not None
            ),
            ZERO,
        )
        order["equity_after_usd"] = str((cash + closing_value).quantize(CENT))
        _complete_simulated_run(
            arm_run.run,
            action=action,
            guard=guard,
            order=order,
            decision_at=decision_at,
        )
        orders.append(order)

    # LLM costs: a deterministic arm's zero is measured (no call was made); a
    # scripted or live arm whose provider reported no charge is *unknown*, and
    # build_performance turns that into a null net return rather than a zero.
    llm_costs = []
    missing = 0
    for arm_run in runs:
        cost = arm_run.cost or {}
        billed = cost.get("billed_cost_usd")
        run_key = arm_run.fixture_id
        at = (
            arm_run.run.completed_at or arm_run.run.started_at
            if arm_run.run is not None
            else opportunity_set.window_start
        )
        if billed is None:
            missing += 1
            continue
        llm_costs.append(
            LlmCost(
                at=min(at, opportunity_set.window_end),
                amount=_decimal(billed, field_name="billed_cost_usd"),
                source=("deterministic_no_model_call" if arm == "V0" else "billed_provider"),
                run_key=run_key,
            )
        )

    opening = OpeningState(
        at=opening_at,
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
            "fee_model": (
                "declared_bps"
                if opportunity_set.execution_assumptions.get("fee_bps") not in (None, "")
                else "not_declared"
            ),
            "guard_policy": str(
                opportunity_set.execution_assumptions.get("guard_policy_version")
                or "evaluated_by_services.execution.evaluate_proposal"
            ),
            "llm_cost_timing": (
                "A known LLM cost is attributed at the decision instant (clamped to the "
                "window end) because it is external spend, not a portfolio flow."
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
    result["per_opportunity"] = [
        {
            "opportunity": item.key,
            "ticker": item.ticker,
            "scheduled_at": utc_iso(item.scheduled_at),
        }
        for item in opportunity_set.opportunities
    ]
    return result


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


def build_comparison_report(
    *,
    opportunity_set: OpportunitySet,
    arm_runs: dict[str, list[ArmRun]],
    trajectories: dict[str, dict],
    provider_mode: str,
    generated_at: datetime | None = None,
    portfolio: Portfolio | None = None,
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
                        "snapshot_id": str(run.run.snapshot_id),
                        "content_hash": run.run.snapshot.content_hash,
                        "opportunity": run.opportunity,
                    }
                    if run.run is not None and run.run.snapshot is not None
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

    pre_registration = {
        "registered_at": opportunity_set.pre_registration.get("registered_at"),
        "registered_by": opportunity_set.pre_registration.get("registered_by"),
        "declared_before_first_opportunity": (
            _parse_at(opportunity_set.pre_registration["registered_at"], field_name="registered_at")
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
                "computed": False,
                "reason": (
                    "The pre-registered primary metric is a forward-return signal metric. It "
                    "needs matured OutcomeRecord rows, and this execution produced none: the "
                    "opportunities are scheduled inside a historical window whose outcomes "
                    "were not evaluated. The registration is published as written and the "
                    "metric is null with this reason rather than replaced by a different "
                    "number."
                ),
            },
        },
        "sample_gate": {
            "minimum_opportunities": sample_plan.get("minimum_opportunities"),
            "minimum_completed_arms": sample_plan.get("minimum_completed_arms"),
            "opportunities": len(opportunity_set.opportunities),
            "completed_arms": completed_arms,
            "met": (
                len(opportunity_set.opportunities)
                >= int(sample_plan.get("minimum_opportunities") or 0)
                and completed_arms >= int(sample_plan.get("minimum_completed_arms") or 0)
            ),
        },
        "primary_metric_result": {
            "id": opportunity_set.primary_metric.get("id"),
            "value": None,
            "unit": "percent",
            "status": "not_computable",
            "reason": "no_matured_outcomes",
            "observations": 0,
        },
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
    manifest_payload = json_safe(opportunity_set.document)
    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": utc_iso(now),
        "opportunity_set_id": opportunity_set.opportunity_set_id,
        "label": opportunity_set.label,
        "provider_mode": provider_mode,
        "pre_registration": pre_registration,
        "frozen": {
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
            },
            "execution_assumptions": opportunity_set.execution_assumptions,
            "risk_rules": opportunity_set.risk_rules,
            "benchmark": opportunity_set.benchmark,
            "mark_count": len(opportunity_set.marks),
            "mark_sources": PriceBook(opportunity_set.marks).source_counts(),
            "mark_provenance": (
                "Daily marks declared in the opportunity set. Their observation instants are "
                "recorded per mark; they are authored, not fetched, and are labelled synthetic."
            ),
        },
        "arms": arms,
        "verification": {
            "distinct_protocol_ids": distinct_protocol_ids,
            "protocol_ids": protocol_ids,
            "identical_snapshots_across_arms": identical_snapshots,
            "reference_snapshots": reference_snapshots,
            "snapshot_sets": snapshot_sets,
            "same_opportunity_set": all(
                arm["scheduled_runs"] == len(opportunity_set.opportunities)
                for arm in arms
                if arm["implemented"] and arm["scheduled_runs"]
            ),
            "note": (
                "Protocol ids are read from the runs, not copied from the declaration. "
                "Snapshot identity is compared by content hash per opportunity across arms."
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
            "note": (
                "A missing or failed run is counted in the denominator. Dropping it would "
                "make an arm look complete by omission."
            ),
        },
        "fairness": {
            "equal_opportunity": {
                "opportunities_per_implemented_arm": len(opportunity_set.opportunities),
                "implemented_arms": ["V0", "V1", "V2", "V3"],
                "v4_excluded": True,
            },
            "same_snapshots": identical_snapshots,
            "same_risk_rules": _risk_rules_check(opportunity_set, portfolio),
            "same_execution_assumptions": {
                "declared": opportunity_set.execution_assumptions,
                "applied_by_all_arms": True,
                "fill_rule": FILL_RULE,
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
    budget: ProtocolBudget | None = None,
    now: datetime | None = None,
) -> dict:
    """Schedule V0-V3 against the frozen opportunities and return the report.

    Idempotent per ``(arm, opportunity)``: a run whose stable fixture id already
    exists is reused rather than re-executed, so re-running the command neither
    double-spends nor duplicates a decision.
    """
    started = now or timezone.now()
    registered_at = _parse_at(
        opportunity_set.pre_registration.get("registered_at"),
        field_name="pre_registration.registered_at",
    )
    if registered_at > started:
        raise ExecutionError(
            f"pre_registration.registered_at ({utc_iso(registered_at)}) is after this "
            f"execution ({utc_iso(started)}): a registration written in the future cannot "
            "govern a run that has already started."
        )
    snapshots: dict[str, InputSnapshot] = {}
    evidence: dict[str, dict] = {}
    for opportunity in opportunity_set.opportunities:
        snapshot = ensure_snapshot(opportunity_set, opportunity, portfolio)
        snapshots[opportunity.key] = snapshot
        evidence[opportunity.key] = ensure_evidence(opportunity, snapshot)

    arm_runs: dict[str, list[ArmRun]] = {arm: [] for arm in ARM_ORDER}
    arm_mode: dict[str, str] = {}
    for arm in ("V0", "V1", "V2", "V3"):
        arm_client = None
        if arm != "V0":
            arm_client, used_mode, reason = _client_for_arm(
                arm, client=client, provider_mode=provider_mode, portfolio=portfolio
            )
            arm_mode[arm] = used_mode
            if arm_client is None:
                for opportunity in opportunity_set.opportunities:
                    arm_runs[arm].append(
                        ArmRun(
                            arm=arm,
                            opportunity=opportunity.key,
                            fixture_id=_fixture_id(opportunity_set, arm, opportunity),
                            status="not_executed",
                            failure_code="provider_not_configured",
                            failure_detail=reason,
                            protocol_version=ARM_PROTOCOLS[arm],
                            cost=_cost_of_run(None),
                        )
                    )
                continue
        else:
            arm_mode[arm] = "deterministic"
        for opportunity in opportunity_set.opportunities:
            arm_runs[arm].append(
                _run_arm_once(
                    arm=arm,
                    opportunity_set=opportunity_set,
                    opportunity=opportunity,
                    portfolio=portfolio,
                    snapshot=snapshots[opportunity.key],
                    client=arm_client,
                    provider_mode=arm_mode[arm],
                    budget=budget,
                )
            )
    arm_runs["V4"] = [
        ArmRun(
            arm="V4",
            opportunity=opportunity.key,
            fixture_id=_fixture_id(opportunity_set, "V4", opportunity),
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
    report = build_comparison_report(
        opportunity_set=opportunity_set,
        arm_runs=arm_runs,
        trajectories=trajectories,
        provider_mode=provider_used,
        generated_at=started,
        portfolio=portfolio,
    )
    report["execution"] = {
        "started_at": utc_iso(started),
        "snapshots_used": {
            key: {"snapshot_id": str(snapshot.id), "content_hash": snapshot.content_hash}
            for key, snapshot in snapshots.items()
        },
        "evidence": evidence,
        "client_modes": arm_mode,
        "idempotent": True,
        "note": (
            "Re-running reuses a run whose stable fixture id exists. It does not re-call a "
            "provider for an arm/opportunity already recorded."
        ),
    }
    report["contents_hash"] = content_hash(
        {key: value for key, value in report.items() if key != "contents_hash"}
    )
    return json_safe(report)
