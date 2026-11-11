"""Bounded, recorded adversarial debate protocol (DEB-01 - DEB-05).

Why this module exists
----------------------
The pre-Phase-2 pipeline was one ``crew.kickoff()``: Bull, Bear and CIO ran
sequentially, the Bull read nothing (it ran first), the Bear read the Bull's
output, and the two "cases" were recovered after the fact by slicing
``tasks_output[0]`` and ``tasks_output[1]``. There was no rebuttal stage at all -
which is why every Phase 1 fixture had to be labelled ``synthetic``.

This module replaces that with an explicit protocol whose inputs, bounds and
outputs are recorded as they happen:

1. **Independent first round (DEB-01).** The Bull and Bear prompts are built
   from the same brief and the same evidence catalog, and neither contains the
   other's output. Independence is a *context* property here, not a scheduling
   one: running the two calls in parallel would not make them independent if the
   Bear's prompt included the Bull's case. Both cases are persisted before the
   rebuttal stage starts, and the event log shows that ordering.
2. **One rebuttal round (DEB-02).** Each agent receives the opponent's
   *validated claims* with their evidence references, and answers through the
   existing :class:`core.decision_models.Rebuttal` contract. A concession
   (``agree``, or an empty Bear case when there is no credible objection) is a
   valid output; nothing in this module manufactures an objection to fill a
   quota. Output is bounded: at most three material claims per agent and at most
   two targeted responses per agent. Extra items are excluded **and recorded**,
   never silently dropped. A missing or invalid ``target_claim_id`` gets one
   bounded repair attempt, then the stage fails.
3. **Adjudication (DEB-03).** The CIO sees the brief, both cases and the
   rebuttals. It must name the strongest counterargument and the unresolved
   uncertainty, and it may return HOLD. The recorded verdict carries whether the
   analysts actually converged, so a split debate cannot be reported as a
   consensus.
4. **Budgets and failures (DEB-04).** Per-stage timeout, run deadline, token
   caps, schema repairs, provider retries and a monetary ceiling are enforced
   here and recorded on the run. A failure is typed, stops the protocol, and
   leaves the failed stage visible through ``stage_states``. A heuristic
   fallback is recorded as a **separate run** (``mode="heuristic"``) with no
   claims and no rebuttals, so a fallback can never appear to have held a debate.
5. **Reproducibility (DEB-05).** Provider, model id, parameters, prompt-template
   version, tool versions, snapshot hash, limits, usage and the raw validated
   stage outputs are stored in ``DecisionRun.protocol_metadata``. The note
   stored alongside them says plainly that events replay deterministically but
   regenerating model output does not.

The model seam
--------------
:class:`StageClient` is the whole surface this module needs from a model::

    client.complete(stage, prompt) -> str

plus optional ``provider``, ``model``, ``parameters`` and ``last_usage``
attributes. Tests substitute a stub; ``ai_agent`` wires the real CrewAI client.
Nothing here imports ``ai_agent``, so the module is testable with no API key, no
network and no CrewAI import.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from string import Template
from typing import Any, Protocol

from django.conf import settings
from django.utils import timezone

from core.decision_errors import ClaimValidationError, DecisionError, EvidenceError
from core.decision_json import content_hash, json_safe, utc_iso
from core.decision_models import (
    MAX_EXCERPT_CHARS,
    MAX_STATEMENT_CHARS,
    AgentRole,
    Claim,
    ClaimStance,
    ClaimType,
    DecisionAction,
    DecisionRecord,
    DecisionRun,
    DecisionStatus,
    EvidenceItem,
    InputSnapshot,
    Rebuttal,
    RebuttalResponseType,
    RunEventType,
    RunMode,
    RunSource,
    RunStage,
)
from core.models import Portfolio
from services.cost_accounting import (
    UNKNOWN_MODEL_PRICING,
    UNKNOWN_NO_CALL,
    UNKNOWN_PROVIDER_COST,
    UNKNOWN_UNRECONCILED_TIMEOUT,
)
from services.cost_accounting import (
    price_table_cost as price_table_cost,
)
from services.decision_runs import (
    append_event,
    create_run,
    record_failure,
    transition_status,
    update_protocol_metadata,
)
from services.evidence import (
    create_claim,
    create_evidence_item,
    create_rebuttal,
    create_snapshot,
)

logger = logging.getLogger("alphaagent.debate_protocol")

__all__ = [
    "BASELINE_PROTOCOL_VERSION",
    "HEURISTIC_PROTOCOL_VERSION",
    "MAX_INITIAL_CLAIMS",
    "MAX_REBUTTALS_PER_AGENT",
    "NO_REBUTTAL_PROTOCOL_VERSION",
    "PROMPT_TEMPLATE_VERSION",
    "PROTOCOL_VERSION",
    "REBUTTAL_ROUNDS",
    "TOOL_VERSIONS",
    "Adjudication",
    "BudgetExhausted",
    "DeadlineExceeded",
    "DebateProtocol",
    "DebateProtocolError",
    "MissingRequiredCase",
    "ModelUsage",
    "ProtocolBudget",
    "ProtocolOutcome",
    "ProtocolUsage",
    "ProviderError",
    "ProviderTransientError",
    "RebuttalTargetError",
    "StageClient",
    "StageFailure",
    "StageSchemaError",
    "StageTimeout",
    "classify_provider_exception",
    "record_heuristic_attempt",
]

# ---------------------------------------------------------------------------
# Protocol identity and bounds
# ---------------------------------------------------------------------------
#: Bumped only when the stage sequence or an output contract changes
#: incompatibly. Stored on every run, so two runs are comparable only when they
#: agree on it.
PROTOCOL_VERSION = "debate.v1"

#: The one-agent baseline: same snapshot, same validated proposal contract, no
#: debate stages.
BASELINE_PROTOCOL_VERSION = "single_agent_baseline.v1"

#: The deterministic engine, recorded as an attempt of its own (DEB-04).
HEURISTIC_PROTOCOL_VERSION = "heuristic_fallback.v1"

#: The same producer with the explicit rebuttal round omitted (FIX-07B). V2 in
#: the experiment manifest needs a genuinely distinct configuration from V3, and
#: V3 is the rebuttal protocol; recording both as ``debate.v1`` would make the
#: comparison a relabelling of one arm as two.
NO_REBUTTAL_PROTOCOL_VERSION = "debate.no_rebuttal.v1"

#: Version of the wording below. A prompt change that could move model behaviour
#: is a version change; the raw prompts are not stored (they contain the whole
#: evidence catalog) but their template identity is.
PROMPT_TEMPLATE_VERSION = "debate.prompts.v2"

#: DEB-02 hard bounds. NOT configurable: a configuration flag that raises these
#: would turn "bounded output" into a wish. The values actually used are recorded
#: on the run.
MAX_INITIAL_CLAIMS = 3
MAX_REBUTTALS_PER_AGENT = 2
REBUTTAL_ROUNDS = 1

#: Identity of the read-only gatherers whose output becomes the evidence
#: catalog. These are not semantic versions of published libraries; they are
#: the module identities a reviewer needs in order to know which implementation
#: produced the evidence. Bumped when the shape of what a gatherer returns
#: changes.
TOOL_VERSIONS: dict[str, str] = {
    "news": "services.news.v1",
    "fundamentals": "services.fundamentals.v1",
    "price_history": "services.fundamentals.v1",
    "market_data": "services.market_data.v1",
}

#: Model-stage keys used in prompts, usage records and retry bookkeeping. They
#: are deliberately not ``RunStage`` values: several model stages belong to one
#: run stage (the two independent cases are both ``analysing``).
STAGE_BULL_CASE = "bull_case"
STAGE_BEAR_CASE = "bear_case"
STAGE_BULL_REBUTTAL = "bull_rebuttal"
STAGE_BEAR_REBUTTAL = "bear_rebuttal"
STAGE_ADJUDICATION = "adjudication"
STAGE_BASELINE = "baseline_analysis"

_JSON_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)
_OUTER_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

#: Strings a provider error must contain to be treated as transient. Matched
#: case-insensitively against the exception text. Timeouts are deliberately NOT
#: on this list: a timed-out stage is over (see ``StageTimeout``), and retrying
#: it doubles the wall-clock budget the run deadline exists to cap.
_TRANSIENT_HINTS = (
    "rate limit",
    "rate_limit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "overloaded",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "try again",
)
_TIMEOUT_HINTS = ("timeout", "timed out", "deadline exceeded", "read timed out")


# ---------------------------------------------------------------------------
# Typed failures
# ---------------------------------------------------------------------------
class DebateProtocolError(DecisionError):
    """Base class for every expected refusal inside the protocol.

    ``code`` is the machine-readable failure code written to
    ``DecisionRun.error_code``; ``detail`` is the text a reviewer reads;
    ``stage`` is the ``RunStage`` the failure belongs to (used by
    ``stage_states`` so the UI can point at the stage that failed); ``run`` is
    the persisted run, when there is one, so the caller can link a fallback
    attempt to the attempt it replaces.
    """

    code = "debate_protocol_error"

    def __init__(self, detail: str, *, stage: str = "", run: DecisionRun | None = None):
        super().__init__(detail)
        self.detail = str(detail)
        self.stage = stage
        self.run = run

    def as_payload(self) -> dict:
        return {"code": self.code, "stage": self.stage or None, "detail": self.detail}


class StageFailure(DebateProtocolError):
    """A stage could not produce a usable output."""

    code = "stage_failed"


class StageTimeout(StageFailure):
    """The stage did not return within ``stage_timeout_seconds``."""

    code = "stage_timeout"


class ProviderError(StageFailure):
    """The provider failed in a way retrying will not fix."""

    code = "provider_error"


class ProviderTransientError(StageFailure):
    """A provider failure that may succeed on retry.

    Raised by the client; the protocol retries it ``provider_retries`` times
    with exponential backoff and converts exhaustion into :class:`ProviderError`.
    """

    code = "provider_transient_error"


class BudgetExhausted(StageFailure):
    """A configured budget ran out before the protocol finished."""

    code = "budget_exhausted"

    def __init__(self, detail: str, *, budget: str = "", **kwargs):
        super().__init__(detail, **kwargs)
        self.budget = budget

    def as_payload(self) -> dict:
        return {**super().as_payload(), "budget": self.budget or None}


class DeadlineExceeded(BudgetExhausted):
    """The run deadline elapsed before the next stage started."""

    code = "run_deadline_exceeded"


class StageSchemaError(StageFailure):
    """The stage output did not satisfy its schema after the repair attempt."""

    code = "invalid_stage_output"


class RebuttalTargetError(StageSchemaError):
    """A rebuttal named no target, or a target that is not an opposing claim."""

    code = "invalid_rebuttal_target"


class MissingRequiredCase(StageFailure):
    """A required case (Bull or Bear) could not be produced."""

    code = "missing_required_case"


def classify_provider_exception(exc: Exception, *, stage: str = "") -> StageFailure:
    """Map a provider exception onto the protocol's typed failures.

    Used by the real client in ``ai_agent`` so the retry policy is decided by
    this module and not by the adapter. Timeouts become :class:`StageTimeout`
    (not retried); recognisably transient errors become
    :class:`ProviderTransientError` (retried with backoff); anything else is a
    fatal :class:`ProviderError`.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    detail = f"{type(exc).__name__}: {exc}"[:500]
    if any(hint in text for hint in _TIMEOUT_HINTS):
        return StageTimeout(detail, stage=stage)
    if any(hint in text for hint in _TRANSIENT_HINTS):
        return ProviderTransientError(detail, stage=stage)
    return ProviderError(detail, stage=stage)


# ---------------------------------------------------------------------------
# Budgets, usage and the model seam
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProtocolBudget:
    """DEB-04 ceilings, with the specification's starting defaults.

    ``monetary_budget_usd=None`` disables the monetary ceiling. When enabled it is
    checked before **every** model attempt (retries included) against the best
    available basis: the provider-reported charge where one exists, otherwise the
    price-table estimate over measured or estimated tokens. It is deliberately
    **not** a strict billed-dollar ceiling. A provider may report tokens without
    dollars, a timed-out call may be billed after the run ends, and a price table
    may be stale; :meth:`DebateProtocol._cost_accounting` records
    ``strict_billed_ceiling: false`` so no reader is told otherwise.
    """

    stage_timeout_seconds: float = 90.0
    run_deadline_seconds: float = 300.0
    max_input_tokens: int = 120_000
    max_output_tokens: int = 16_000
    schema_repair_attempts: int = 1
    provider_retries: int = 2
    provider_retry_backoff_seconds: float = 0.5
    monetary_budget_usd: Decimal | None = Decimal("1.00")

    def __post_init__(self) -> None:
        for name in (
            "stage_timeout_seconds",
            "run_deadline_seconds",
            "max_input_tokens",
            "max_output_tokens",
            "schema_repair_attempts",
            "provider_retries",
            "provider_retry_backoff_seconds",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"ProtocolBudget.{name} must not be negative.")
        if self.monetary_budget_usd is not None and self.monetary_budget_usd < 0:
            raise ValueError("ProtocolBudget.monetary_budget_usd must not be negative.")

    @classmethod
    def from_settings(cls, config: dict | None = None) -> ProtocolBudget:
        """Build the budget from ``settings.DEBATE_CONFIG`` (or an override)."""
        config = (
            dict(config) if config is not None else dict(getattr(settings, "DEBATE_CONFIG", {}))
        )

        def as_int(key: str, default: int) -> int:
            try:
                return max(0, int(config.get(key, default)))
            except (TypeError, ValueError):
                logger.warning("DEBATE_CONFIG[%r] is not an integer; using %s", key, default)
                return default

        def as_float(key: str, default: float) -> float:
            try:
                return max(0.0, float(config.get(key, default)))
            except (TypeError, ValueError):
                logger.warning("DEBATE_CONFIG[%r] is not a number; using %s", key, default)
                return default

        raw_money = config.get("MONETARY_BUDGET_USD", "1.00")
        try:
            money: Decimal | None = None if raw_money in (None, "") else Decimal(str(raw_money))
        except InvalidOperation:
            logger.warning("DEBATE_CONFIG['MONETARY_BUDGET_USD'] is not a decimal; using 1.00")
            money = Decimal("1.00")

        return cls(
            stage_timeout_seconds=as_float("STAGE_TIMEOUT_SECONDS", 90.0),
            run_deadline_seconds=as_float("RUN_DEADLINE_SECONDS", 300.0),
            max_input_tokens=as_int("MAX_INPUT_TOKENS", 120_000),
            max_output_tokens=as_int("MAX_OUTPUT_TOKENS", 16_000),
            schema_repair_attempts=as_int("SCHEMA_REPAIR_ATTEMPTS", 1),
            provider_retries=as_int("PROVIDER_RETRIES", 2),
            provider_retry_backoff_seconds=as_float("PROVIDER_RETRY_BACKOFF_SECONDS", 0.5),
            monetary_budget_usd=money,
        )

    def as_dict(self) -> dict:
        return {
            "stage_timeout_seconds": self.stage_timeout_seconds,
            "run_deadline_seconds": self.run_deadline_seconds,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "schema_repair_attempts": self.schema_repair_attempts,
            "provider_retries": self.provider_retries,
            "provider_retry_backoff_seconds": self.provider_retry_backoff_seconds,
            "monetary_budget_usd": (
                str(self.monetary_budget_usd) if self.monetary_budget_usd is not None else None
            ),
        }


@dataclass
class ModelUsage:
    """What one model call used, and on what basis.

    ``reported``/``tokens_measured`` distinguish provider-reported token counts
    from character estimates. ``cost_usd`` is the **billed** charge when the
    client knows it. A price-table number is never placed in ``cost_usd``: it is
    carried in ``price_table_cost_usd`` and labelled an estimate. ``None`` in
    either money field means unknown, with ``cost_unknown_reason`` saying why.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal | None = None
    reported: bool = False
    #: True when the token counts came from the provider rather than a character
    #: estimate. ``None`` falls back to ``reported`` so existing clients keep
    #: their meaning.
    tokens_measured: bool | None = None
    #: Price-table arithmetic over the tokens above. An estimate, even when the
    #: tokens are measured.
    price_table_cost_usd: Decimal | None = None
    cost_unknown_reason: str = ""

    @property
    def measured_tokens(self) -> bool:
        if self.tokens_measured is None:
            return bool(self.reported)
        return bool(self.tokens_measured)


class StageClient(Protocol):
    """The entire model surface the protocol depends on.

    Implementations must be safe to call from a worker thread (the per-stage
    timeout runs the call in one) and must not rely on the caller's database
    transaction. They may raise :class:`StageTimeout`, :class:`ProviderError`
    or :class:`ProviderTransientError`; any other exception is treated as a
    fatal provider error rather than retried blindly.
    """

    provider: str
    model: str
    parameters: dict
    last_usage: ModelUsage | None

    def complete(self, stage: str, prompt: str) -> str:
        raise NotImplementedError("StageClient is a Protocol; use a concrete client.")


@dataclass
class ProtocolUsage:
    """Token/cost totals for one run, with every quantity kept separate.

    Five things are recorded and never interchanged:

    * **measured tokens** - provider-reported counts;
    * **estimated tokens** - character-based inference;
    * **price-table cost** - arithmetic over either token source, published only
      when every call could be priced (a partial sum is named as partial);
    * **billed cost** - the provider-reported charge, published only when every
      call reported one;
    * **unknown** - ``None`` plus a reason, never ``Decimal("0")``.

    ``cost_usd``/``cost_reported`` are retained as the billed-cost view for
    existing consumers: a run with any unreported charge has ``cost_usd = None``
    rather than a total that silently omits it.
    """

    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    price_table_cost_usd: Decimal = Decimal("0")
    price_table_priced_calls: int = 0
    price_table_unpriced_calls: int = 0
    price_table_unknown_reason: str = ""
    billed_cost_usd: Decimal = Decimal("0")
    billed_cost_calls: int = 0
    billed_cost_unknown_calls: int = 0
    billed_cost_unknown_reason: str = ""
    calls: int = 0
    retries: int = 0
    repairs: int = 0
    timed_out_calls: int = 0
    reconciled_timeout_calls: int = 0
    unreconciled_timeout_calls: int = 0
    budget_basis_counts: dict[str, int] = field(default_factory=dict)
    budget_spent_usd: Decimal = Decimal("0")

    @property
    def input_tokens(self) -> int:
        return self.measured_input_tokens + self.estimated_input_tokens

    @property
    def output_tokens(self) -> int:
        return self.measured_output_tokens + self.estimated_output_tokens

    @property
    def accounting_method(self) -> str:
        if not self.calls:
            return UNKNOWN_NO_CALL
        measured = self.measured_input_tokens + self.measured_output_tokens
        estimated = self.estimated_input_tokens + self.estimated_output_tokens
        if measured and estimated:
            return "mixed_measured_and_estimated"
        if measured:
            return "provider_reported"
        return "estimated_chars_per_4_tokens"

    @property
    def cost_reported(self) -> bool:
        """True only when every call reported an actual charge."""
        return self.calls > 0 and self.billed_cost_unknown_calls == 0

    @property
    def cost_usd(self) -> Decimal | None:
        return self.billed_cost_usd if self.cost_reported else None

    @property
    def price_table_cost_complete(self) -> bool:
        return self.price_table_priced_calls > 0 and self.price_table_unpriced_calls == 0

    @property
    def billed_cost_complete(self) -> bool:
        return self.cost_reported

    def as_dict(self) -> dict:
        return {
            # Legacy view, now explicitly the *billed* view.
            "cost_usd": (str(self.cost_usd) if self.cost_usd is not None else None),
            "cost_reported": self.cost_reported,
            "calls": self.calls,
            "provider_retries_used": self.retries,
            "schema_repairs_used": self.repairs,
            "token_accounting": self.accounting_method,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            # The five separated quantities.
            "measured_tokens": {
                "input": self.measured_input_tokens,
                "output": self.measured_output_tokens,
            },
            "estimated_tokens": {
                "input": self.estimated_input_tokens,
                "output": self.estimated_output_tokens,
            },
            "price_table_cost_usd": (
                str(self.price_table_cost_usd.quantize(Decimal("0.000001")))
                if self.price_table_cost_complete
                else None
            ),
            "price_table_cost_partial_usd": (
                str(self.price_table_cost_usd.quantize(Decimal("0.000001")))
                if self.price_table_priced_calls and not self.price_table_cost_complete
                else None
            ),
            "price_table_cost_complete": self.price_table_cost_complete,
            "price_table_cost_unknown_calls": self.price_table_unpriced_calls,
            "price_table_cost_unknown_reason": (
                self.price_table_unknown_reason or UNKNOWN_MODEL_PRICING
                if self.price_table_unpriced_calls
                else None
            ),
            "billed_cost_usd": (
                str(self.billed_cost_usd.quantize(Decimal("0.000001")))
                if self.cost_reported
                else None
            ),
            "billed_cost_unknown_calls": self.billed_cost_unknown_calls,
            "billed_cost_unknown_reason": (
                self.billed_cost_unknown_reason or UNKNOWN_PROVIDER_COST
                if self.billed_cost_unknown_calls
                else None
            ),
            "timed_out_calls": self.timed_out_calls,
            "reconciled_timeout_calls": self.reconciled_timeout_calls,
            "unreconciled_timeout_calls": self.unreconciled_timeout_calls,
            "unreconciled_charge_note": (
                "A timed-out call may still be billed by the provider after this run "
                "ended; its charge is not included in any figure here."
                if self.unreconciled_timeout_calls
                else None
            ),
            "monetary_ceiling_basis": self.budget_basis,
            "monetary_ceiling_spent_usd": str(self.budget_spent_usd.quantize(Decimal("0.000001"))),
            "strict_billed_ceiling": False,
        }

    @property
    def budget_basis(self) -> str:
        bases = set(self.budget_basis_counts)
        if not bases:
            return "unknown"
        if len(bases) == 1:
            return next(iter(bases))
        return "mixed_billed_and_price_table_estimate"


# ---------------------------------------------------------------------------
# Validated outputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Adjudication:
    """The CIO's validated proposal.

    Deliberately not ``ai_agent.TradeProposal``: this module must not import the
    orchestrator (the orchestrator imports this module). ``run_alpha_agent``
    converts this into the public ``TradeProposal`` shape, so the execution
    guard's contract is unchanged.
    """

    action: str
    amount: Decimal
    sentiment: str
    reasoning: str
    decisive_claim_ids: tuple[str, ...] = ()
    strongest_objection: str = ""
    unresolved_uncertainty: str = ""
    thesis: str = ""
    invalidation_condition: str = ""

    def requested_quantity(self) -> Decimal | None:
        """Quantity recorded on the decision, or ``None`` when none was asked for."""
        if self.action == DecisionAction.HOLD or self.amount <= 0:
            return None
        return self.amount


@dataclass
class ClaimDraft:
    statement: str
    claim_type: str
    evidence_ids: list[str]
    horizon: str = ""
    limitations: str = ""


@dataclass
class RebuttalDraft:
    target_claim_id: str
    response_type: str
    explanation: str
    evidence_ids: list[str]
    position_change: str


@dataclass
class ExcludedItem:
    """Something the model produced that was not accepted, and why.

    Recorded on the run (and as a ``limitation.recorded`` event) so a claim that
    failed its evidence check is *flagged*, never quietly deleted.
    """

    kind: str
    reason: str
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "reason": self.reason, "payload": json_safe(self.payload)}


@dataclass
class ProtocolOutcome:
    """What a completed protocol produced, including what it did and did not record."""

    proposal: Adjudication
    cases: dict[str, list[dict]] = field(default_factory=dict)
    run: DecisionRun | None = None
    snapshot: InputSnapshot | None = None
    usage: ProtocolUsage = field(default_factory=ProtocolUsage)
    limits: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    recorded: bool = False


@dataclass
class _EvidenceRef:
    """One citable item, usable whether or not it was persisted."""

    id: str
    key: str
    source_type: str
    publisher: str
    published_at: str | None
    available_at: str | None
    text: str
    units: str = ""
    currency: str = ""

    def catalog_entry(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "source_type": self.source_type,
            "publisher": self.publisher or None,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "units": self.units or None,
            "currency": self.currency or None,
            "text": self.text[:500],
        }


@dataclass
class _RunState:
    run: DecisionRun | None = None
    snapshot: InputSnapshot | None = None
    refs: list[_EvidenceRef] = field(default_factory=list)
    claims: dict[str, Claim] = field(default_factory=dict)
    claims_by_stance: dict[str, list[dict]] = field(default_factory=dict)
    rebuttals: list[Rebuttal] = field(default_factory=list)
    raw_outputs: dict = field(default_factory=dict)
    excluded: list[ExcludedItem] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    current_stage: str = RunStage.QUEUED
    rebuttal_rounds: int = 0

    def evidence_ids(self) -> set[str]:
        return {ref.id for ref in self.refs}


# ---------------------------------------------------------------------------
# Prompts
#
# ``string.Template`` rather than ``str.format`` so the JSON schemas embedded in
# the prompts do not have to escape every brace. The templates avoid ``$`` in
# prose for the same reason.
# ---------------------------------------------------------------------------
CASE_PROMPT_TEMPLATE = Template(
    """ALPHAAGENT DEBATE PROTOCOL - model stage $stage - first round (independent)
Prompt template: $template_version
Ticker: $ticker
Agent role: $role, arguing the $stance side
Round: 1 of $rounds ($rounds rebuttal round in total)

You are producing the $stance case from the evidence catalog below. You have NOT seen and
will NOT be shown the opposing analyst's response before you commit this case. Do not guess
what the other side says, and do not argue against a case you have not read.

BRIEF
$brief

EVIDENCE CATALOG - cite only these ids
$catalog

RULES
1. State at most $max_claims material claims. Fewer is better; one strong claim beats three
   weak ones.
2. Every claim MUST cite at least one catalog evidence id in "evidence_ids". A statement you
   cannot support from the catalog must be left out, not asserted.
3. If you cannot find a credible $stance case, return an empty "claims" list and say in
   "summary" that you found no credible case. Do not manufacture a claim to fill the quota.
   An explicit concession is a valid output.
4. "statement" is at most $max_statement characters. "claim_type" is exactly one of
   observed_fact, interpretation, forecast.
5. "limitations" names what would weaken the claim, or is an empty string.

OUTPUT - one JSON object, no markdown fence, no prose around it:
{
  "summary": "<two or three sentences: the case, and how strong the evidence actually is>",
  "claims": [
    {
      "statement": "<at most 280 characters>",
      "claim_type": "observed_fact|interpretation|forecast",
      "evidence_ids": ["<catalog id>"],
      "horizon": "<e.g. 30d, or empty>",
      "limitations": "<what weakens this, or empty>"
    }
  ]
}
"""
)

REBUTTAL_PROMPT_TEMPLATE = Template(
    """ALPHAAGENT DEBATE PROTOCOL - model stage $stage - rebuttal round 1 of $rounds
Prompt template: $template_version
Ticker: $ticker
Agent role: $role, responding to the $opposing_stance side

You have now read the opposing analyst's validated claims. They are listed below with the
evidence each one cites. Respond to the material ones.

OPPOSING VALIDATED CLAIMS
$opposing

EVIDENCE CATALOG - you may cite any of these ids
$catalog

BRIEF
$brief

RULES
1. Respond to at most $max_responses claims. Choose the ones that matter to the decision.
2. "target_claim_id" MUST be one of the ids in OPPOSING VALIDATED CLAIMS.
3. "response_type" is exactly one of:
   - agree: the claim stands and you concede it. This is a valid, expected answer.
   - dispute: the claim is wrong or unsupported; explain with evidence.
   - qualify: the claim is partly right; state the qualification.
   - insufficient_evidence: the claim cannot be assessed from the available evidence.
4. "position_change" is REQUIRED for every response. State what you now believe and why, or
   "unchanged" and why the claim did not move you. The declaration is part of the record.
5. Cite at least one catalog evidence id for agree, dispute and qualify.
   insufficient_evidence may cite none.
6. If you genuinely have no material response, return an empty "responses" list and put the
   reason in "no_response_reason". Do not invent an objection to fill a quota.

OUTPUT - one JSON object, no markdown fence, no prose around it:
{
  "responses": [
    {
      "target_claim_id": "<one of the opposing claim ids>",
      "response_type": "agree|dispute|qualify|insufficient_evidence",
      "explanation": "<what you accept, reject or qualify, and on what evidence>",
      "evidence_ids": ["<catalog id>"],
      "position_change": "<what you now believe and why, or 'unchanged: ...'>"
    }
  ],
  "no_response_reason": "<only when responses is empty>"
}
"""
)

ADJUDICATION_PROMPT_TEMPLATE = Template(
    """ALPHAAGENT DEBATE PROTOCOL - model stage $stage - adjudication
Prompt template: $template_version
Ticker: $ticker

You are the Chief Investment Officer. $protocol_note

BRIEF
$brief

BULL CASE
$bull_case

BEAR CASE
$bear_case

REBUTTALS
$rebuttals

DISAGREEMENT SUMMARY
$round_note
$dispute_count dispute, $qualify_count qualify, $concession_count agree,
$insufficient_count insufficient_evidence, $position_change_count position changes.
$disagreement_note

RULES
- Decide from the evidence, not from how confident the prose sounds.
- HOLD is a valid decision when the evidence is insufficient or the two cases are balanced.
  Do not force a trade to look decisive.
- "decisive_claim_ids" names the claims the decision turns on. Use only ids from the cases
  above; name the disputed claim when the decision turns on it.
- "strongest_objection" is the single strongest point on the losing side. It is required.
- "unresolved_uncertainty" is what this debate did NOT settle. It is required even when you
  are confident, and it is required to be specific when the rebuttals still disagree.
- "thesis" explains why a position is worth holding, on BUY. "invalidation_condition" is the
  observable condition that would prove the thesis wrong. Empty on HOLD unless useful.

OUTPUT - one JSON object, no markdown fence, no prose around it:
{
  "action": "BUY|SELL|HOLD",
  "amount": <quantity of the asset in units; 0 for HOLD>,
  "sentiment": "BULLISH|BEARISH|NEUTRAL",
  "reasoning": "<why this decision, which argument won, and what remains unresolved>",
  "decisive_claim_ids": ["<claim id>"],
  "strongest_objection": "<the losing side's strongest point>",
  "unresolved_uncertainty": "<what the debate did not settle>",
  "thesis": "<why this is worth holding, or empty>",
  "invalidation_condition": "<what would prove this wrong, or empty>"
}
"""
)

BASELINE_PROMPT_TEMPLATE = Template(
    """ALPHAAGENT DEBATE PROTOCOL - model stage $stage - single-agent baseline
Prompt template: $template_version
Ticker: $ticker

You are the only analyst in this run. There is no opposing case and no rebuttal round: this
is the one-agent baseline, recorded for comparison against the debate protocol. Produce the
same validated proposal a CIO would produce, but say plainly in "reasoning" that no debate
was held and that no counter-case was argued.

BRIEF
$brief

EVIDENCE CATALOG - cite only these ids
$catalog

RULES
- "decisive_claim_ids" must be an empty list: the baseline records no claims.
- HOLD is a valid decision when the evidence is insufficient or balanced.
- "strongest_objection" is required: name the strongest counter-argument you can see even
  though no one argued it against you.
- "unresolved_uncertainty" is required.
- Do not claim a consensus; there was no second analyst.

OUTPUT - one JSON object, no markdown fence, no prose around it:
{
  "action": "BUY|SELL|HOLD",
  "amount": <quantity of the asset in units; 0 for HOLD>,
  "sentiment": "BULLISH|BEARISH|NEUTRAL",
  "reasoning": "<why this decision, and that no debate was held>",
  "decisive_claim_ids": [],
  "strongest_objection": "<the strongest counter-argument you can see>",
  "unresolved_uncertainty": "<what is still unknown>",
  "thesis": "<why this is worth holding, or empty>",
  "invalidation_condition": "<what would prove this wrong, or empty>"
}
"""
)

REPAIR_PROMPT_TEMPLATE = Template(
    """ALPHAAGENT DEBATE PROTOCOL - model stage $stage - bounded schema repair
Prompt template: $template_version
Attempt: 1 of $attempts

Your previous reply could not be accepted.

PROBLEM
$problems
$context_block
PREVIOUS REPLY
$previous

Return the corrected JSON object only. Correct what is wrong and keep the rest unchanged. Do
not add claims or responses that were not in the previous reply.
"""
)


def _templates() -> dict[str, str]:
    return {
        "case": CASE_PROMPT_TEMPLATE.template,
        "rebuttal": REBUTTAL_PROMPT_TEMPLATE.template,
        "adjudication": ADJUDICATION_PROMPT_TEMPLATE.template,
        "baseline": BASELINE_PROMPT_TEMPLATE.template,
        "repair": REPAIR_PROMPT_TEMPLATE.template,
    }


def prompt_template_hash() -> str:
    """Stable hash of the wording that produced a run's outputs."""
    return content_hash(
        {"version": PROMPT_TEMPLATE_VERSION, "templates": sorted(_templates().items())}
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _estimate_tokens(text: str) -> int:
    """Character-based token estimate.

    Four characters per token is the usual English approximation. It is recorded
    as an estimate (``token_accounting``), never as billed usage.
    """
    return max(1, len(text or "") // 4)


def _parse_timestamp(value: Any) -> Any:
    """Parse an article timestamp into an aware UTC datetime, or ``None``.

    RSS feeds publish ISO-8601 and RFC-2822 in roughly equal measure, and some
    publish nothing usable. An unparseable value stays ``None`` (unknown
    publication time) rather than being replaced with "now", which would turn a
    guess into an availability claim.
    """
    if value is None or value == "":
        return None
    if hasattr(value, "tzinfo") and callable(getattr(value, "isoformat", None)):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, UTC)
    return parsed.astimezone(UTC)


def _call_with_timeout(fn, timeout: float, *, stage: str, timeout_basis: str = "stage_timeout"):
    """Run ``fn`` and stop *waiting* after ``timeout`` seconds.

    A timed-out call is not cancelled - Python cannot kill a thread - so the
    provider may still finish it and bill for it. The protocol stops consuming
    its output, which is the guarantee that matters here: the run fails typed
    instead of hanging. ``timeout_basis`` says whether the bound was the stage
    timeout or the remaining run deadline, so a truncated stage is diagnosable.
    """
    if timeout is None or timeout <= 0:
        return fn()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="debate-stage")
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeout as exc:
        future.cancel()
        basis = (
            "the remaining run deadline"
            if timeout_basis == "remaining_deadline"
            else "the stage timeout"
        )
        raise StageTimeout(
            f"stage {stage!r} did not return within {timeout:g}s ({basis}); the call "
            "was abandoned (it may still complete and be billed by the provider).",
            stage=stage,
        ) from exc
    finally:
        # Never wait: a still-running provider call must not block the worker or
        # interpreter shutdown.
        executor.shutdown(wait=False)


def _decode_json_object(text: Any, *, stage: str) -> dict:
    """Extract one JSON object from a model reply, tolerating fences and prose."""
    if not isinstance(text, str):
        raise StageSchemaError(f"stage {stage!r} did not return text.", stage=stage)
    raw = text.strip()
    if not raw:
        raise StageSchemaError(f"stage {stage!r} returned an empty reply.", stage=stage)

    candidates = [raw, _JSON_FENCE_RE.sub("", raw).strip()]
    match = _OUTER_OBJECT_RE.search(raw)
    if match:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    raise StageSchemaError(
        f"stage {stage!r} did not return a JSON object (got {raw[:120]!r}).", stage=stage
    )


def _as_text(value: Any, *, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return str(value).strip()[:limit]


def _uuid_strings(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [str(value).strip() for value in values if str(value or "").strip()]


def _brief(context: Any) -> str:
    """The factual brief both sides see. Never includes either side's output."""
    as_brief = getattr(context, "as_brief", None)
    if callable(as_brief):
        return str(as_brief())
    return f"Ticker: {getattr(context, 'ticker', '')}\nRisk profile: {getattr(context, 'risk_profile', '')}"


def _render_catalog(refs: list[_EvidenceRef]) -> str:
    return json.dumps(
        [ref.catalog_entry() for ref in refs], indent=2, sort_keys=True, ensure_ascii=False
    )


def _render_opposing(claims: list[dict]) -> str:
    entries = []
    for claim in claims:
        entries.append(
            {
                "claim_id": claim["claim_id"],
                "agent_role": claim["agent_role"],
                "claim_type": claim["claim_type"],
                "statement": claim["statement"],
                "horizon": claim.get("horizon") or None,
                "limitations": claim.get("limitations") or None,
                "evidence": claim.get("evidence", []),
            }
        )
    return json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False)


def _render_cases(cases: dict[str, list[dict]], summaries: dict[str, str]) -> tuple[str, str]:
    def render(stance: str) -> str:
        summary = summaries.get(stance) or "(no summary recorded)"
        claims = cases.get(stance) or []
        lines = [summary]
        if not claims:
            lines.append("No material claims were recorded.")
        for index, claim in enumerate(claims, start=1):
            evidence = ", ".join(claim.get("evidence_ids") or []) or "none"
            lines.append(
                f"{index}. [{claim['claim_type']}] {claim['statement']} "
                f"(claim_id {claim['claim_id']}; evidence: {evidence})"
            )
        return "\n".join(lines)

    return render(ClaimStance.BULL), render(ClaimStance.BEAR)


def _render_rebuttals(rebuttals: list[dict]) -> str:
    if not rebuttals:
        return "No rebuttals were recorded."
    lines = []
    for item in rebuttals:
        lines.append(
            f"- {item['responding_agent']} {item['response_type']} claim "
            f"{item['target_claim_id']}: {item['explanation']} "
            f"(position_change: {item['position_change'] or 'not declared'}; "
            f"evidence: {', '.join(item.get('evidence_ids') or []) or 'none'})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output validators
#
# Structural problems raise ``StageSchemaError`` (repairable, then fatal).
# Semantic problems that can be contained - a claim citing evidence the catalog
# does not hold, an item over a bound - are returned as exclusions with a repair
# hint, so the caller can repair once and then accept-and-flag instead of
# discarding a whole case.
# ---------------------------------------------------------------------------
def validate_case_payload(
    payload: dict,
    *,
    stance: str,
    known_evidence_ids: set[str],
    stage: str,
) -> tuple[str, list[ClaimDraft], list[ExcludedItem], list[str]]:
    summary = _as_text(payload.get("summary"), limit=2000)
    if not summary:
        raise StageSchemaError(f"stage {stage!r} returned no case summary.", stage=stage)

    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        raise StageSchemaError(
            f"stage {stage!r} must return a 'claims' list (an empty list is a valid concession).",
            stage=stage,
        )

    drafts: list[ClaimDraft] = []
    excluded: list[ExcludedItem] = []
    hints: list[str] = []

    for index, item in enumerate(raw_claims):
        if not isinstance(item, dict):
            excluded.append(
                ExcludedItem(
                    kind="claim", reason="claim is not a JSON object", payload={"index": index}
                )
            )
            continue
        if len(drafts) >= MAX_INITIAL_CLAIMS:
            excluded.append(
                ExcludedItem(
                    kind="claim",
                    reason=f"over the {MAX_INITIAL_CLAIMS}-claim bound for the initial case",
                    payload={
                        "index": index,
                        "statement": _as_text(item.get("statement"), limit=280),
                    },
                )
            )
            continue

        statement = _as_text(item.get("statement"))
        if not statement:
            excluded.append(
                ExcludedItem(
                    kind="claim", reason="claim has no statement", payload={"index": index}
                )
            )
            continue

        claim_type = str(item.get("claim_type") or "").strip().lower()
        if claim_type not in ClaimType.values:
            excluded.append(
                ExcludedItem(
                    kind="claim",
                    reason=f"unknown claim_type {claim_type!r}",
                    payload={"index": index, "statement": statement},
                )
            )
            continue

        evidence_ids = _uuid_strings(item.get("evidence_ids"))
        unknown = [value for value in evidence_ids if value not in known_evidence_ids]
        if not evidence_ids or unknown:
            reason = (
                "claim cites no evidence"
                if not evidence_ids
                else f"claim cites evidence outside the catalog: {', '.join(unknown)}"
            )
            excluded.append(
                ExcludedItem(
                    kind="claim",
                    reason=reason,
                    payload={"index": index, "statement": statement, "evidence_ids": evidence_ids},
                )
            )
            hints.append(
                f"claim {index + 1} ({statement[:60]!r}) {reason}. Cite an id from the "
                "catalog, or remove the claim."
            )
            continue

        limitations = _as_text(item.get("limitations"), limit=2000)
        if len(statement) > MAX_STATEMENT_CHARS:
            hints.append(
                f"claim {index + 1} is {len(statement)} characters; the bound is "
                f"{MAX_STATEMENT_CHARS}."
            )
            statement = statement[: MAX_STATEMENT_CHARS - 1].rstrip() + "\u2026"
            limitations = (
                limitations + " [statement was trimmed to the 280-character bound]"
            ).strip()

        drafts.append(
            ClaimDraft(
                statement=statement,
                claim_type=claim_type,
                evidence_ids=evidence_ids,
                horizon=_as_text(item.get("horizon"), limit=64),
                limitations=limitations,
            )
        )

    return summary, drafts, excluded, hints


def validate_rebuttal_payload(
    payload: dict,
    *,
    stage: str,
    opponent_claim_ids: set[str],
    known_evidence_ids: set[str],
) -> tuple[list[RebuttalDraft], list[ExcludedItem], list[str], list[str], str]:
    """Validate one rebuttal round output.

    Returns ``(drafts, excluded, hints, bad_targets, no_response_reason)``.
    ``bad_targets`` is separate from the other hints because it is the one
    problem the protocol treats as fatal after the repair attempt (DEB-02).
    """
    raw_responses = payload.get("responses")
    if not isinstance(raw_responses, list):
        raise StageSchemaError(f"stage {stage!r} must return a 'responses' list.", stage=stage)

    drafts: list[RebuttalDraft] = []
    excluded: list[ExcludedItem] = []
    hints: list[str] = []
    bad_targets: list[str] = []

    for index, item in enumerate(raw_responses):
        if not isinstance(item, dict):
            excluded.append(
                ExcludedItem(
                    kind="rebuttal",
                    reason="response is not a JSON object",
                    payload={"index": index},
                )
            )
            continue
        if len(drafts) >= MAX_REBUTTALS_PER_AGENT:
            excluded.append(
                ExcludedItem(
                    kind="rebuttal",
                    reason=f"over the {MAX_REBUTTALS_PER_AGENT}-response bound",
                    payload={"index": index, "target_claim_id": item.get("target_claim_id")},
                )
            )
            continue

        target = str(item.get("target_claim_id") or "").strip()
        if target not in opponent_claim_ids:
            bad_targets.append(target or "<missing>")
            hint = (
                f"response {index + 1} target_claim_id {target or '<missing>'!r} is not one of "
                f"the opposing claim ids: {', '.join(sorted(opponent_claim_ids))}."
            )
            hints.append(hint)
            continue

        response_type = str(item.get("response_type") or "").strip().lower()
        if response_type not in RebuttalResponseType.values:
            excluded.append(
                ExcludedItem(
                    kind="rebuttal",
                    reason=f"unknown response_type {response_type!r}",
                    payload={"index": index, "target_claim_id": target},
                )
            )
            hints.append(
                f"response {index + 1} response_type {response_type!r} is not one of "
                f"{', '.join(RebuttalResponseType.values)}."
            )
            continue

        explanation = _as_text(item.get("explanation"))
        if not explanation:
            excluded.append(
                ExcludedItem(
                    kind="rebuttal", reason="response has no explanation", payload={"index": index}
                )
            )
            continue

        position_change = _as_text(item.get("position_change"), limit=280)
        if not position_change:
            excluded.append(
                ExcludedItem(
                    kind="rebuttal",
                    reason="response did not declare position_change",
                    payload={"index": index, "target_claim_id": target},
                )
            )
            hints.append(
                f"response {index + 1} left position_change empty. Declare what you now "
                "believe and why, or say 'unchanged' and why."
            )
            continue

        evidence_ids = _uuid_strings(item.get("evidence_ids"))
        unknown = [value for value in evidence_ids if value not in known_evidence_ids]
        if response_type != RebuttalResponseType.INSUFFICIENT_EVIDENCE and not evidence_ids:
            excluded.append(
                ExcludedItem(
                    kind="rebuttal",
                    reason="a non-insufficient_evidence response must cite evidence",
                    payload={"index": index, "target_claim_id": target},
                )
            )
            hints.append(
                f"response {index + 1} cites no evidence; cite a catalog id or use "
                "insufficient_evidence."
            )
            continue
        if unknown:
            excluded.append(
                ExcludedItem(
                    kind="rebuttal",
                    reason=f"response cites evidence outside the catalog: {', '.join(unknown)}",
                    payload={"index": index, "target_claim_id": target},
                )
            )
            hints.append(f"response {index + 1} cites unknown evidence ids {', '.join(unknown)}.")
            continue

        drafts.append(
            RebuttalDraft(
                target_claim_id=target,
                response_type=response_type,
                explanation=explanation,
                evidence_ids=evidence_ids,
                position_change=position_change,
            )
        )

    no_response_reason = _as_text(payload.get("no_response_reason"), limit=500)
    return drafts, excluded, hints, bad_targets, no_response_reason


def validate_adjudication_payload(
    payload: dict,
    *,
    stage: str,
    known_claim_ids: set[str],
) -> tuple[Adjudication, list[ExcludedItem], list[str], bool]:
    """Validate the CIO's proposal.

    Returns ``(adjudication, excluded, hints, hold_amount_forced)``.
    """
    action = str(payload.get("action") or "").strip().upper()
    if action not in DecisionAction.values:
        raise StageSchemaError(
            f"stage {stage!r} returned action {action!r}; expected BUY, SELL or HOLD.",
            stage=stage,
        )

    sentiment = str(payload.get("sentiment") or "").strip().upper()
    if sentiment not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        raise StageSchemaError(
            f"stage {stage!r} returned sentiment {sentiment!r}; expected BULLISH, BEARISH "
            "or NEUTRAL.",
            stage=stage,
        )

    reasoning = _as_text(payload.get("reasoning"))
    if not reasoning:
        raise StageSchemaError(f"stage {stage!r} returned no reasoning.", stage=stage)

    raw_amount = payload.get("amount", 0)
    try:
        amount = Decimal(str(raw_amount if raw_amount not in (None, "") else "0"))
    except (InvalidOperation, ValueError) as exc:
        raise StageSchemaError(
            f"stage {stage!r} returned a non-numeric amount {raw_amount!r}.", stage=stage
        ) from exc
    if amount < 0:
        raise StageSchemaError(f"stage {stage!r} returned a negative amount.", stage=stage)

    excluded: list[ExcludedItem] = []
    hints: list[str] = []

    hold_amount_forced = False
    if action == DecisionAction.HOLD and amount != 0:
        hold_amount_forced = True
        excluded.append(
            ExcludedItem(
                kind="adjudication",
                reason="HOLD carried a non-zero amount; recorded as 0",
                payload={"amount": str(amount)},
            )
        )
        hints.append("HOLD must use amount 0.")
        amount = Decimal("0")

    decisive_ids = _uuid_strings(payload.get("decisive_claim_ids"))
    unknown_decisive = [value for value in decisive_ids if value not in known_claim_ids]
    if unknown_decisive:
        excluded.append(
            ExcludedItem(
                kind="adjudication",
                reason=f"decisive_claim_ids are not claims in this run: {unknown_decisive}",
                payload={"decisive_claim_ids": unknown_decisive},
            )
        )
        hints.append("decisive_claim_ids may only contain ids of claims recorded in this run.")
    decisive_ids = [value for value in decisive_ids if value in known_claim_ids]

    strongest_objection = _as_text(payload.get("strongest_objection"))
    if not strongest_objection:
        raise StageSchemaError(
            f"stage {stage!r} did not acknowledge the strongest counterargument "
            "('strongest_objection' is required).",
            stage=stage,
        )

    unresolved = _as_text(payload.get("unresolved_uncertainty"))
    if not unresolved:
        raise StageSchemaError(
            f"stage {stage!r} did not name the unresolved uncertainty "
            "('unresolved_uncertainty' is required).",
            stage=stage,
        )

    adjudication = Adjudication(
        action=action,
        amount=amount,
        sentiment=sentiment,
        reasoning=reasoning,
        decisive_claim_ids=tuple(decisive_ids),
        strongest_objection=strongest_objection,
        unresolved_uncertainty=unresolved,
        thesis=_as_text(payload.get("thesis")),
        invalidation_condition=_as_text(payload.get("invalidation_condition")),
    )
    return adjudication, excluded, hints, hold_amount_forced


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------
class DebateProtocol:
    """Run the recorded Bull / Bear / CIO protocol for one snapshot.

    ``client`` is the only model dependency. ``portfolio=None`` runs the same
    protocol without writing anything (used by ``dry_run_agent``); a recording
    run needs a portfolio because a ``DecisionRun`` belongs to one.
    """

    def __init__(
        self,
        *,
        client: StageClient,
        budget: ProtocolBudget | None = None,
        config: dict | None = None,
        mode: str = RunMode.LIVE,
        source: str = RunSource.CREWAI,
        provider: str | None = None,
        model: str | None = None,
        parameters: dict | None = None,
    ) -> None:
        self.client = client
        self.config = (
            dict(config) if config is not None else dict(getattr(settings, "DEBATE_CONFIG", {}))
        )
        self.budget = budget or ProtocolBudget.from_settings(self.config)
        self.mode = mode
        self.source = source
        self.provider = (provider or getattr(client, "provider", "") or "unknown").strip()
        self.model = (model or getattr(client, "model", "") or "unknown").strip()
        self.parameters = dict(
            parameters if parameters is not None else getattr(client, "parameters", {}) or {}
        )

        if self.mode == RunMode.SYNTHETIC:
            raise DebateProtocolError(
                "The debate protocol cannot write a synthetic run: a live protocol run "
                "is 'live' (or 'heuristic' for a fallback attempt), and only a fixture "
                "loader may create 'synthetic' content."
            )

        self._usage = ProtocolUsage()
        self._attempts_items: list[dict] = []
        self._started_monotonic = 0.0
        #: The client's last usage object that has already been accounted, so a
        #: failed call is not counted twice and a previously successful call is
        #: not re-read as if it belonged to the failure.
        self._accounted_client_usage: ModelUsage | None = None

    # -- public -----------------------------------------------------------
    @property
    def protocol_version(self) -> str:
        return PROTOCOL_VERSION

    def run(
        self,
        context: Any,
        *,
        portfolio: Portfolio | None = None,
        snapshot: InputSnapshot | None = None,
        record: bool = True,
        fixture_id: str = "",
    ) -> ProtocolOutcome:
        """Execute the full debate protocol and return the validated proposal.

        ``record=False`` runs the protocol without persisting anything: no
        snapshot, no run, no evidence, no claims, no decision. It is the
        read-only path (the ``dry_run_agent`` contract), not a degraded mode.
        ``fixture_id`` gives a scheduled experiment run its stable natural key.
        """
        return self._execute(
            context,
            portfolio=portfolio,
            snapshot=snapshot,
            baseline=False,
            rebuttal=True,
            record=record,
            fixture_id=fixture_id,
        )

    def run_without_rebuttal(
        self,
        context: Any,
        *,
        portfolio: Portfolio | None = None,
        snapshot: InputSnapshot | None = None,
        record: bool = True,
        fixture_id: str = "",
    ) -> ProtocolOutcome:
        """Bull / Bear / CIO with **no explicit response round** (FIX-07B).

        This is the same producer with one stage omitted, not a second protocol
        pretending to be one. It is recorded as ``debate.no_rebuttal.v1`` so a
        report cannot present it as the rebuttal arm.
        """
        return self._execute(
            context,
            portfolio=portfolio,
            snapshot=snapshot,
            baseline=False,
            rebuttal=False,
            record=record,
            fixture_id=fixture_id,
        )

    def run_single_agent_baseline(
        self,
        context: Any,
        *,
        portfolio: Portfolio | None = None,
        snapshot: InputSnapshot | None = None,
        record: bool = True,
        fixture_id: str = "",
    ) -> ProtocolOutcome:
        """One analyst, no cases and no rebuttal round (acceptance scenario 4).

        Uses the same snapshot (pass the debate run's snapshot to reuse it) and
        the same validated proposal contract, so a baseline and a debate run are
        comparable without executing the debate stages.
        """
        return self._execute(
            context,
            portfolio=portfolio,
            snapshot=snapshot,
            baseline=True,
            rebuttal=False,
            record=record,
            fixture_id=fixture_id,
        )

    # -- execution --------------------------------------------------------
    def _execute(
        self,
        context: Any,
        *,
        portfolio: Portfolio | None,
        snapshot: InputSnapshot | None,
        baseline: bool,
        rebuttal: bool,
        record: bool,
        fixture_id: str = "",
    ) -> ProtocolOutcome:
        self._usage = ProtocolUsage()
        self._attempts_items = []
        self._accounted_client_usage = None
        self._started_monotonic = time.monotonic()
        self._rebuttal_rounds = REBUTTAL_ROUNDS if rebuttal else 0
        state = _RunState()
        if baseline:
            protocol_version = BASELINE_PROTOCOL_VERSION
        elif rebuttal:
            protocol_version = PROTOCOL_VERSION
        else:
            protocol_version = NO_REBUTTAL_PROTOCOL_VERSION

        try:
            state = self._prepare(
                context,
                portfolio=portfolio if record else None,
                snapshot=snapshot if record else None,
                protocol_version=protocol_version,
                record=record,
                fixture_id=fixture_id,
            )
            if baseline:
                return self._run_baseline(context, state)
            return self._run_debate(context, state, rebuttal=rebuttal)
        except StageFailure as exc:
            self._fail(state, exc)
            raise

    def _prepare(
        self,
        context: Any,
        *,
        portfolio: Portfolio | None,
        snapshot: InputSnapshot | None,
        protocol_version: str,
        record: bool = True,
        fixture_id: str = "",
    ) -> _RunState:
        state = _RunState()

        if record and portfolio is None and getattr(context, "portfolio_id", None):
            portfolio = (
                Portfolio.objects.select_related("user").filter(pk=context.portfolio_id).first()
            )

        if portfolio is None:
            # Read-only mode: the protocol still runs and validates, but nothing
            # is persisted, so evidence ids are stable local tokens. This is the
            # path ``dry_run_agent`` uses; it is the only path that writes no
            # rows.
            state.refs = self._synthetic_refs(context)
            state.current_stage = RunStage.COLLECTING
            return state

        if snapshot is not None:
            if snapshot.portfolio_id != portfolio.id:
                raise DebateProtocolError("The input snapshot belongs to a different portfolio.")
            refs, skipped = self._refs_from_snapshot(snapshot)
            state.snapshot = snapshot
            state.refs = refs
            state.run = self._create_run(
                context,
                portfolio=portfolio,
                snapshot=snapshot,
                protocol_version=protocol_version,
                started_at=timezone.now(),
                fixture_id=fixture_id,
            )
            state.current_stage = RunStage.COLLECTING
            self._transition(state, DecisionStatus.COLLECTING)
            self._stage_event(
                state,
                RunEventType.STAGE_STARTED,
                RunStage.COLLECTING,
                payload={
                    "reused_snapshot": True,
                    "evidence_count": len(state.refs),
                    "snapshot_hash": snapshot.content_hash,
                },
            )
        else:
            prepared, skipped = self._gather_evidence(context)
            cutoff = timezone.now()
            state.snapshot = self._capture_snapshot(context, portfolio, cutoff)
            state.run = self._create_run(
                context,
                portfolio=portfolio,
                snapshot=state.snapshot,
                protocol_version=protocol_version,
                started_at=cutoff,
                fixture_id=fixture_id,
            )
            state.current_stage = RunStage.COLLECTING
            self._transition(state, DecisionStatus.COLLECTING)
            self._stage_event(
                state,
                RunEventType.STAGE_STARTED,
                RunStage.COLLECTING,
                payload={
                    "tool_versions": dict(TOOL_VERSIONS),
                    "snapshot_hash": state.snapshot.content_hash,
                },
            )
            state.refs, evidence_skipped = self._persist_evidence(state, prepared, cutoff)
            skipped = [*skipped, *evidence_skipped]

        self._exclude(state, skipped)
        self._stage_event(
            state,
            RunEventType.STAGE_COMPLETED,
            RunStage.COLLECTING,
            payload={
                "evidence_count": len(state.refs),
                "evidence_skipped": len(skipped),
                "snapshot_hash": state.snapshot.content_hash if state.snapshot else None,
            },
        )
        return state

    # -- baseline ---------------------------------------------------------
    def _run_baseline(self, context: Any, state: _RunState) -> ProtocolOutcome:
        self._transition(state, DecisionStatus.ANALYSING)
        self._stage_event(
            state,
            RunEventType.STAGE_STARTED,
            RunStage.ANALYSING,
            payload={"protocol": BASELINE_PROTOCOL_VERSION, "debate": "not_run"},
        )
        self._limitation(
            state,
            "Single-agent baseline: no bull case, no bear case and no rebuttal round "
            "were executed. The run is recorded for comparison against the debate "
            "protocol, not as a debate.",
        )
        self._stage_event(
            state,
            RunEventType.STAGE_COMPLETED,
            RunStage.ANALYSING,
            payload={"protocol": BASELINE_PROTOCOL_VERSION, "claims": 0},
        )

        self._transition(state, DecisionStatus.ADJUDICATING)
        self._stage_event(
            state,
            RunEventType.STAGE_STARTED,
            RunStage.ADJUDICATING,
            payload={"single_agent": True},
        )
        prompt = BASELINE_PROMPT_TEMPLATE.substitute(
            stage=STAGE_BASELINE,
            template_version=PROMPT_TEMPLATE_VERSION,
            ticker=getattr(context, "ticker", ""),
            brief=_brief(context),
            catalog=_render_catalog(state.refs),
        )
        payload = self._stage_json(
            state,
            stage_key=STAGE_BASELINE,
            prompt=prompt,
            stage=RunStage.ADJUDICATING,
            agent_role=AgentRole.CIO,
        )
        state.raw_outputs[STAGE_BASELINE] = payload
        adjudication, excluded, hints, _forced = validate_adjudication_payload(
            payload,
            stage=STAGE_BASELINE,
            known_claim_ids=set(),
        )
        # The baseline records no claims, so a decisive id can only be a
        # hallucinated reference. Drop any that survived validation.
        self._exclude(state, excluded)
        if hints:
            self._limitation(state, "adjudication repair hints: " + " ".join(hints))

        decision = self._record_decision(state, adjudication, baseline=True, consensus=None)
        self._stage_event(
            state,
            RunEventType.STAGE_COMPLETED,
            RunStage.ADJUDICATING,
            payload={
                "decision_id": str(decision.id) if decision is not None else None,
                "action": adjudication.action,
            },
        )

        completed_run = self._complete(state, adjudication)
        return ProtocolOutcome(
            proposal=adjudication,
            cases={ClaimStance.BULL: [], ClaimStance.BEAR: []},
            run=completed_run,
            snapshot=state.snapshot,
            usage=self._usage,
            limits=self._limits(),
            metadata=dict(completed_run.protocol_metadata or {}) if completed_run else {},
            recorded=completed_run is not None,
        )

    # -- debate -----------------------------------------------------------
    def _run_debate(
        self, context: Any, state: _RunState, *, rebuttal: bool = True
    ) -> ProtocolOutcome:
        brief = _brief(context)
        rounds = REBUTTAL_ROUNDS if rebuttal else 0

        # ---- Round 1: two independent cases (DEB-01) --------------------
        self._transition(state, DecisionStatus.ANALYSING)
        self._stage_event(
            state,
            RunEventType.STAGE_STARTED,
            RunStage.ANALYSING,
            payload={
                "protocol": PROTOCOL_VERSION if rebuttal else NO_REBUTTAL_PROTOCOL_VERSION,
                "independence": "isolated_context",
                "rounds": rounds,
                "rebuttal_round_executed": rebuttal,
                "limits": self._limits(),
                "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                "provider": self.provider,
                "model": self.model,
                "parameters": json_safe(self.parameters),
            },
        )

        summaries: dict[str, str] = {}
        for agent_role, stance, stage_key, role_name in (
            (AgentRole.BULL, ClaimStance.BULL, STAGE_BULL_CASE, "Bullish Research Analyst"),
            (AgentRole.BEAR, ClaimStance.BEAR, STAGE_BEAR_CASE, "Risk Assessor (Short Seller)"),
        ):
            prompt = CASE_PROMPT_TEMPLATE.substitute(
                stage=stage_key,
                template_version=PROMPT_TEMPLATE_VERSION,
                ticker=getattr(context, "ticker", ""),
                role=role_name,
                stance=stance,
                rounds=rounds,
                brief=brief,
                catalog=_render_catalog(state.refs),
                max_claims=MAX_INITIAL_CLAIMS,
                max_statement=MAX_STATEMENT_CHARS,
            )
            summary, claims = self._case_stage(
                state,
                stage_key=stage_key,
                prompt=prompt,
                agent_role=agent_role,
                stance=stance,
            )
            summaries[stance] = summary
            # The case is persisted here, before the second stage runs and long
            # before the rebuttal stage starts. Independence is preserved by the
            # prompt, and the event log shows both cases landing before
            # ``status.changed`` to ``rebutting``.
            self._stage_event(
                state,
                RunEventType.STAGE_COMPLETED,
                RunStage.ANALYSING,
                agent_role=agent_role,
                payload={
                    "case": stance,
                    "claims": len(claims),
                    "summary": summary[:1000],
                    "concession": not claims,
                },
            )

        self._stage_event(
            state,
            RunEventType.STAGE_COMPLETED,
            RunStage.ANALYSING,
            payload={
                "bull_claims": len(state.claims_by_stance.get(ClaimStance.BULL, [])),
                "bear_claims": len(state.claims_by_stance.get(ClaimStance.BEAR, [])),
                "excluded_items": len(state.excluded),
            },
        )

        rebuttal_payloads: list[dict] = []
        if rebuttal:
            # ---- Round 2: one explicit rebuttal round (DEB-02) ----------
            self._transition(state, DecisionStatus.REBUTTING)
            state.rebuttal_rounds = 1
            self._stage_event(
                state,
                RunEventType.STAGE_STARTED,
                RunStage.REBUTTING,
                payload={
                    "round": 1,
                    "rounds_configured": rounds,
                    "max_responses_per_agent": MAX_REBUTTALS_PER_AGENT,
                    "bull_claim_ids": [
                        item["claim_id"]
                        for item in state.claims_by_stance.get(ClaimStance.BULL, [])
                    ],
                    "bear_claim_ids": [
                        item["claim_id"]
                        for item in state.claims_by_stance.get(ClaimStance.BEAR, [])
                    ],
                },
            )
            for agent_role, opposing_stance, stage_key, role_name in (
                (
                    AgentRole.BULL,
                    ClaimStance.BEAR,
                    STAGE_BULL_REBUTTAL,
                    "Bullish Research Analyst",
                ),
                (
                    AgentRole.BEAR,
                    ClaimStance.BULL,
                    STAGE_BEAR_REBUTTAL,
                    "Risk Assessor (Short Seller)",
                ),
            ):
                opposing = state.claims_by_stance.get(opposing_stance, [])
                if not opposing:
                    self._limitation(
                        state,
                        f"No {opposing_stance} claims were recorded, so the {agent_role} had "
                        "no validated claim to rebut. The stage was skipped rather than "
                        "inventing a target.",
                    )
                    continue
                prompt = REBUTTAL_PROMPT_TEMPLATE.substitute(
                    stage=stage_key,
                    template_version=PROMPT_TEMPLATE_VERSION,
                    ticker=getattr(context, "ticker", ""),
                    role=role_name,
                    opposing_stance=opposing_stance,
                    rounds=rounds,
                    opposing=_render_opposing(opposing),
                    catalog=_render_catalog(state.refs),
                    brief=brief,
                    max_responses=MAX_REBUTTALS_PER_AGENT,
                )
                round_payloads = self._rebuttal_stage(
                    state,
                    stage_key=stage_key,
                    prompt=prompt,
                    agent_role=agent_role,
                    opposing=opposing,
                )
                rebuttal_payloads.extend(round_payloads)

            self._stage_event(
                state,
                RunEventType.STAGE_COMPLETED,
                RunStage.REBUTTING,
                payload={
                    "round": 1,
                    "rebuttals": len(rebuttal_payloads),
                    "disputes": sum(
                        1 for item in rebuttal_payloads if item["response_type"] == "dispute"
                    ),
                    "concessions": sum(
                        1 for item in rebuttal_payloads if item["response_type"] == "agree"
                    ),
                },
            )
        else:
            # No rebuttal round is a configuration, not an omission. Say so where
            # a reader looks for the debate: in the event log and on the run.
            self._limitation(
                state,
                "No rebuttal round was executed: this run is the Bull/Bear/CIO "
                "configuration without an explicit response round "
                f"({NO_REBUTTAL_PROTOCOL_VERSION}).",
            )

        # ---- Adjudication (DEB-03) --------------------------------------
        self._transition(state, DecisionStatus.ADJUDICATING)
        disagreement = self._disagreement_summary(rebuttal_payloads)
        consensus = disagreement["analyst_consensus"] if rebuttal else None
        self._stage_event(
            state,
            RunEventType.STAGE_STARTED,
            RunStage.ADJUDICATING,
            payload={
                "claim_count": len(state.claims),
                "rebuttal_count": len(rebuttal_payloads),
                "rebuttal_round_executed": rebuttal,
                "analyst_consensus": consensus,
            },
        )
        bull_case, bear_case = _render_cases(state.claims_by_stance, summaries)
        if rebuttal:
            disagreement_note = (
                "The rebuttals still dispute or qualify claims, so the analysts have NOT "
                "converged; do not describe the outcome as a consensus."
                if not disagreement["analyst_consensus"]
                else "Every recorded rebuttal was a concession, so the record shows convergence."
            )
            protocol_note = (
                "Two analysts produced independent cases, then one rebuttal round. Decide."
            )
            round_note = "Round 1 recorded the following responses:"
        else:
            disagreement_note = (
                "No explicit response round was executed, so the analysts expressed no "
                "measured consensus. Do not describe the outcome as agreement or as a debate."
            )
            protocol_note = (
                "Two analysts produced independent cases. No rebuttal round was executed "
                "in this configuration. Decide."
            )
            round_note = "No rebuttal round was executed; the response counts are all zero."
        prompt = ADJUDICATION_PROMPT_TEMPLATE.substitute(
            stage=STAGE_ADJUDICATION,
            template_version=PROMPT_TEMPLATE_VERSION,
            ticker=getattr(context, "ticker", ""),
            brief=brief,
            bull_case=bull_case,
            bear_case=bear_case,
            protocol_note=protocol_note,
            rebuttals=_render_rebuttals(rebuttal_payloads),
            rebuttal_count=len(rebuttal_payloads),
            dispute_count=disagreement["counts"]["dispute"],
            qualify_count=disagreement["counts"]["qualify"],
            concession_count=disagreement["counts"]["agree"],
            insufficient_count=disagreement["counts"]["insufficient_evidence"],
            position_change_count=disagreement["position_changes"],
            round_note=round_note,
            disagreement_note=disagreement_note,
        )
        payload = self._stage_json(
            state,
            stage_key=STAGE_ADJUDICATION,
            prompt=prompt,
            stage=RunStage.ADJUDICATING,
            agent_role=AgentRole.CIO,
        )
        state.raw_outputs[STAGE_ADJUDICATION] = payload
        adjudication, excluded, hints, _forced = validate_adjudication_payload(
            payload,
            stage=STAGE_ADJUDICATION,
            known_claim_ids=self._known_claim_ids(state),
        )
        self._exclude(state, excluded)
        if hints:
            self._limitation(state, "adjudication repair hints: " + " ".join(hints))

        if (
            rebuttal
            and not disagreement["analyst_consensus"]
            and not adjudication.unresolved_uncertainty
        ):
            self._limitation(
                state,
                "The CIO returned an empty unresolved_uncertainty field while the rebuttals "
                "still disputed or qualified claims.",
            )

        decision = self._record_decision(
            state,
            adjudication,
            baseline=False,
            consensus=consensus,
            disagreement=disagreement,
        )
        self._stage_event(
            state,
            RunEventType.STAGE_COMPLETED,
            RunStage.ADJUDICATING,
            payload={
                "decision_id": str(decision.id) if decision is not None else None,
                "action": adjudication.action,
                "analyst_consensus": consensus,
            },
        )

        completed_run = self._complete(state, adjudication)
        metadata = dict(completed_run.protocol_metadata or {}) if completed_run else {}
        if completed_run is not None:
            metadata = update_protocol_metadata(
                completed_run,
                rebuttal_rounds_run=rounds,
                rebuttal_executed=rebuttal,
                analyst_consensus=consensus,
                disagreement=disagreement,
            )
        return ProtocolOutcome(
            proposal=adjudication,
            cases=dict(state.claims_by_stance),
            run=completed_run,
            snapshot=state.snapshot,
            usage=self._usage,
            limits=self._limits(),
            metadata=metadata,
            recorded=completed_run is not None,
        )

    # -- stage helpers ----------------------------------------------------
    def _case_stage(
        self,
        state: _RunState,
        *,
        stage_key: str,
        prompt: str,
        agent_role: str,
        stance: str,
    ) -> tuple[str, list[dict]]:
        current_prompt = prompt
        hints: list[str] = []
        excluded: list[ExcludedItem] = []
        summary = ""
        drafts: list[ClaimDraft] = []
        known = state.evidence_ids()

        for attempt in range(self.budget.schema_repair_attempts + 1):
            raw = self._call_model(state, stage_key, current_prompt, stage=RunStage.ANALYSING)
            try:
                payload = _decode_json_object(raw, stage=stage_key)
            except StageSchemaError as exc:
                if attempt < self.budget.schema_repair_attempts:
                    self._usage.repairs += 1
                    current_prompt = self._repair_prompt(
                        stage_key=stage_key,
                        previous=raw,
                        problems=[str(exc)],
                        evidence=state.refs,
                    )
                    continue
                raise MissingRequiredCase(
                    f"The {stance} case could not be produced: {exc.detail}",
                    stage=RunStage.ANALYSING,
                ) from exc

            try:
                summary, drafts, excluded, hints = validate_case_payload(
                    payload,
                    stance=stance,
                    known_evidence_ids=known,
                    stage=stage_key,
                )
            except StageSchemaError as exc:
                if attempt < self.budget.schema_repair_attempts:
                    self._usage.repairs += 1
                    current_prompt = self._repair_prompt(
                        stage_key=stage_key,
                        previous=raw,
                        problems=[str(exc)],
                        evidence=state.refs,
                    )
                    continue
                raise MissingRequiredCase(
                    f"The {stance} case could not be produced: {exc.detail}",
                    stage=RunStage.ANALYSING,
                ) from exc

            state.raw_outputs[stage_key] = payload
            if hints and attempt < self.budget.schema_repair_attempts:
                # A claim citing evidence outside the catalog is repairable;
                # spend the bounded repair on it before excluding anything.
                self._usage.repairs += 1
                current_prompt = self._repair_prompt(
                    stage_key=stage_key,
                    previous=raw,
                    problems=hints,
                    evidence=state.refs,
                )
                continue
            self._exclude(state, excluded)
            break

        claims = self._persist_claims(state, drafts, agent_role=agent_role, stance=stance)
        return summary, claims

    def _rebuttal_stage(
        self,
        state: _RunState,
        *,
        stage_key: str,
        prompt: str,
        agent_role: str,
        opposing: list[dict],
    ) -> list[dict]:
        current_prompt = prompt
        known = state.evidence_ids()
        opponent_ids = {item["claim_id"] for item in opposing}
        records: list[dict] = []

        for attempt in range(self.budget.schema_repair_attempts + 1):
            raw = self._call_model(state, stage_key, current_prompt, stage=RunStage.REBUTTING)
            try:
                payload = _decode_json_object(raw, stage=stage_key)
                drafts, excluded, hints, bad_targets, no_response_reason = (
                    validate_rebuttal_payload(
                        payload,
                        stage=stage_key,
                        opponent_claim_ids=opponent_ids,
                        known_evidence_ids=known,
                    )
                )
            except StageSchemaError as exc:
                if attempt < self.budget.schema_repair_attempts:
                    self._usage.repairs += 1
                    current_prompt = self._repair_prompt(
                        stage_key=stage_key,
                        previous=raw,
                        problems=[str(exc)],
                        claim_ids=sorted(opponent_ids),
                        claim_ids_label="VALID TARGET CLAIM IDS",
                        evidence=state.refs,
                    )
                    continue
                raise RebuttalTargetError(
                    f"The {agent_role} rebuttal could not be accepted: {exc.detail}",
                    stage=RunStage.REBUTTING,
                ) from exc

            state.raw_outputs[stage_key] = payload

            if bad_targets or hints:
                if attempt < self.budget.schema_repair_attempts:
                    self._usage.repairs += 1
                    problems = list(hints)
                    if bad_targets:
                        problems.insert(
                            0,
                            "target_claim_id must be one of: " + ", ".join(sorted(opponent_ids)),
                        )
                    current_prompt = self._repair_prompt(
                        stage_key=stage_key,
                        previous=raw,
                        problems=problems,
                        claim_ids=sorted(opponent_ids),
                        claim_ids_label="VALID TARGET CLAIM IDS",
                        evidence=state.refs,
                    )
                    continue
                if bad_targets:
                    # DEB-02: one bounded repair, then the stage fails. Dropping
                    # the response silently would hide an agent that never
                    # engaged with the opposing case.
                    raise RebuttalTargetError(
                        "The "
                        f"{agent_role} rebuttal still named invalid target_claim_id "
                        f"value(s) {bad_targets} after the bounded repair attempt; "
                        f"valid targets were {sorted(opponent_ids)}.",
                        stage=RunStage.REBUTTING,
                    )
                self._exclude(state, excluded)
            else:
                self._exclude(state, excluded)

            if not drafts:
                reason = no_response_reason or (
                    "the agent returned no responses and gave no reason"
                )
                self._limitation(
                    state,
                    f"The {agent_role} returned no rebuttal responses: {reason}",
                )
            for draft in drafts:
                records.append(
                    self._persist_rebuttal(
                        state,
                        draft,
                        agent_role=agent_role,
                    )
                )
            return records

        raise RebuttalTargetError(
            f"The {agent_role} rebuttal could not be accepted after the repair attempt.",
            stage=RunStage.REBUTTING,
        )

    def _stage_json(
        self,
        state: _RunState,
        *,
        stage_key: str,
        prompt: str,
        stage: str,
        agent_role: str,
        known_claim_ids: set[str] | None = None,
    ) -> dict:
        """Call a single-output stage (adjudication, baseline) with one repair."""
        current_prompt = prompt
        validator = validate_adjudication_payload
        for attempt in range(self.budget.schema_repair_attempts + 1):
            raw = self._call_model(state, stage_key, current_prompt, stage=stage)
            try:
                payload = _decode_json_object(raw, stage=stage_key)
                # Validation happens in the caller so the caller owns the
                # decision record; but structural errors must be repairable, so
                # run the validator here purely to detect them.
                validator(payload, stage=stage_key, known_claim_ids=known_claim_ids or set())
                return payload
            except StageSchemaError as exc:
                if attempt < self.budget.schema_repair_attempts:
                    self._usage.repairs += 1
                    current_prompt = self._repair_prompt(
                        stage_key=stage_key,
                        previous=raw,
                        problems=[str(exc)],
                        claim_ids=sorted(known_claim_ids or []),
                        evidence=state.refs,
                    )
                    continue
                # Re-stage the error: the validator names the model stage, but
                # the run records the RunStage the failure belongs to.
                raise StageSchemaError(exc.detail, stage=stage) from exc
        raise StageSchemaError(
            f"stage {stage_key!r} could not be accepted after the repair attempt.", stage=stage
        )

    def _repair_prompt(
        self,
        *,
        stage_key: str,
        previous: str,
        problems: list[str],
        claim_ids: list[str] | None = None,
        claim_ids_label: str = "VALID CLAIM IDS",
        evidence: list[_EvidenceRef] | None = None,
    ) -> str:
        """Build the one bounded repair prompt.

        A repair that only says "that evidence id is unknown" is not repairable:
        the model has to be able to see which ids are valid and what they are.
        The same catalog the original stage saw is therefore repeated here,
        alongside the valid claim ids where the stage needs them.
        """
        blocks: list[str] = []
        if claim_ids:
            blocks.append(
                f"\n{claim_ids_label}\n" + "\n".join(f"- {value}" for value in claim_ids) + "\n"
            )
        if evidence:
            blocks.append(
                "\nEVIDENCE CATALOG - cite only these ids\n" + _render_catalog(evidence) + "\n"
            )
        self._attempts_record(
            {
                "stage": stage_key,
                "kind": "schema_repair",
                "problems": problems,
            }
        )
        return REPAIR_PROMPT_TEMPLATE.substitute(
            stage=stage_key,
            template_version=PROMPT_TEMPLATE_VERSION,
            attempts=self.budget.schema_repair_attempts,
            problems="\n".join(f"- {problem}" for problem in problems),
            context_block="".join(blocks),
            previous=str(previous)[:4000],
        )

    # -- persistence ------------------------------------------------------
    def _create_run(
        self,
        context: Any,
        *,
        portfolio: Portfolio,
        snapshot: InputSnapshot,
        protocol_version: str,
        started_at,
        fixture_id: str = "",
    ) -> DecisionRun:
        run = create_run(
            portfolio=portfolio,
            ticker=getattr(context, "ticker", ""),
            protocol_version=protocol_version,
            mode=self.mode,
            source=self.source,
            snapshot=snapshot,
            started_at=started_at,
            model_hash=content_hash(
                {"provider": self.provider, "model": self.model, "parameters": self.parameters}
            ),
            config_hash=content_hash(
                {
                    "protocol": protocol_version,
                    "limits": self._limits(),
                    "budget": self.budget.as_dict(),
                    "tool_versions": TOOL_VERSIONS,
                }
            ),
            prompt_hash=prompt_template_hash(),
            fixture_id=fixture_id,
            protocol_metadata={
                "protocol": {
                    "name": protocol_version,
                    "version": protocol_version,
                    "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                    "provider": self.provider,
                    "model": self.model,
                    "parameters": json_safe(self.parameters),
                    "tool_versions": dict(TOOL_VERSIONS),
                    "snapshot_hash": snapshot.content_hash,
                    "rounds": self._rebuttal_rounds,
                    "limits": self._limits(),
                    "budget": self.budget.as_dict(),
                    "cost_ceiling": {
                        "limit_usd": (
                            str(self.budget.monetary_budget_usd)
                            if self.budget.monetary_budget_usd is not None
                            else None
                        ),
                        "checked_before_every_attempt": True,
                        "strict_billed_ceiling": False,
                        "note": (
                            "Enforced against the reported charge when one exists and "
                            "otherwise the price-table estimate. Late provider charges and "
                            "unreconciled timed-out calls are not visible to the check."
                        ),
                    },
                },
                "deterministic_replay": (
                    "Stored events and records replay deterministically. Regenerating "
                    "the model outputs does not: a second generation is a new sample, "
                    "not a reproduction of this one."
                ),
            },
        )
        logger.info(
            "Debate protocol run %s started for %s (%s, %s)",
            run.id,
            run.ticker,
            protocol_version,
            self.provider,
        )
        return run

    def _capture_snapshot(self, context: Any, portfolio: Portfolio, cutoff) -> InputSnapshot:
        now = cutoff
        holdings = [
            {
                "ticker": asset.ticker.upper(),
                "quantity": str(asset.amount),
                "avg_purchase_price": str(asset.avg_purchase_price),
            }
            for asset in portfolio.assets.all()
        ]
        quotes: dict[str, dict] = {}
        price = getattr(context, "current_price", None)
        if price is not None:
            change = getattr(context, "price_change_pct", None)
            quotes[str(getattr(context, "ticker", "")).upper()] = {
                "price": str(price),
                "currency": "USD",
                "change_pct": str(change) if change is not None else None,
                "source": "market_data",
                "as_of": utc_iso(now),
            }
        return create_snapshot(
            portfolio=portfolio,
            portfolio_version=f"live-{now.strftime('%Y%m%dT%H%M%SZ')}",
            analysis_cutoff=now,
            captured_at=now,
            holdings=holdings,
            cash={
                "balance_usd": str(portfolio.balance_usd),
                "currency": "USD",
            },
            quotes=quotes,
        )

    def _gather_evidence(self, context: Any) -> tuple[list[dict], list[ExcludedItem]]:
        """Build evidence payloads from the read-only gatherers' output.

        Nothing is written here: the snapshot has to exist first, and the
        analysis cutoff is the moment gathering *finishes*, so that every item
        the agents may cite was available at or before the cutoff.
        """
        entries: list[dict] = []
        skipped: list[ExcludedItem] = []
        limit = int(self.config.get("MAX_EVIDENCE_ITEMS", 20) or 20)

        # Reserve room for the three structured observations below: a long news
        # feed must not push the quote or the fundamentals out of the catalog.
        news_budget = max(0, limit - 3)
        news = getattr(context, "news_report", None)
        if news is not None:
            articles = list(getattr(news, "articles", []) or [])[:news_budget]
            for article in articles:
                title = str(getattr(article, "title", "") or "").strip()
                summary = str(getattr(article, "summary", "") or "").strip()
                if not title and not summary:
                    continue
                entries.append(
                    {
                        "key": f"news:{len(entries)}",
                        "source_type": "news_article",
                        "source_uri": str(getattr(article, "url", "") or ""),
                        "publisher": str(getattr(article, "source", "") or ""),
                        "published_at": _parse_timestamp(getattr(article, "published_at", None)),
                        "excerpt": f"{title}. {summary}".strip()[:MAX_EXCERPT_CHARS],
                    }
                )
            if getattr(news, "degraded", False):
                skipped.append(
                    ExcludedItem(
                        kind="source",
                        reason="news gathering reported a degraded source; coverage may be partial",
                    )
                )

        health = getattr(context, "financial_health", None)
        if health is not None:
            block = getattr(health, "as_prompt_block", None)
            entries.append(
                {
                    "key": "fundamentals",
                    "source_type": "fundamentals",
                    "publisher": "yfinance",
                    "observation": health.as_dict(),
                    "excerpt": str(block() if callable(block) else "")[:MAX_EXCERPT_CHARS],
                }
            )

        history = getattr(context, "price_history", None)
        if history is not None:
            payload = history.as_dict()
            payload.pop("points", None)
            entries.append(
                {
                    "key": "price_history",
                    "source_type": "derived_metric",
                    "publisher": "yfinance",
                    "observation": payload,
                    "excerpt": json.dumps(json_safe(payload), sort_keys=True)[:MAX_EXCERPT_CHARS],
                }
            )

        price = getattr(context, "current_price", None)
        if price is not None:
            change = getattr(context, "price_change_pct", None)
            entries.append(
                {
                    "key": "quote",
                    "source_type": "price_quote",
                    "publisher": "market_data",
                    "currency": "USD",
                    "observation": {
                        "ticker": str(getattr(context, "ticker", "")).upper(),
                        "price": str(price),
                        "change_pct": str(change) if change is not None else None,
                    },
                    "excerpt": (
                        f"{str(getattr(context, 'ticker', '')).upper()} last price {price}"
                        + (f" ({change:+.2f}%)" if change is not None else "")
                    )[:MAX_EXCERPT_CHARS],
                }
            )

        return entries[:limit], skipped

    def _persist_evidence(
        self, state: _RunState, prepared: list[dict], cutoff
    ) -> tuple[list[_EvidenceRef], list[ExcludedItem]]:
        refs: list[_EvidenceRef] = []
        skipped: list[ExcludedItem] = []
        assert state.snapshot is not None
        for entry in prepared:
            retrieved_at = entry.get("retrieved_at") or cutoff
            published_at = entry.get("published_at")
            available_at = max(published_at, retrieved_at) if published_at else retrieved_at
            if available_at > cutoff:
                skipped.append(
                    ExcludedItem(
                        kind="evidence",
                        reason=(
                            "became available after the analysis cutoff and was not offered "
                            "to the agents"
                        ),
                        payload={
                            "key": entry.get("key"),
                            "available_at": utc_iso(available_at),
                        },
                    )
                )
                continue
            try:
                item = create_evidence_item(
                    snapshot=state.snapshot,
                    source_type=entry["source_type"],
                    retrieved_at=retrieved_at,
                    published_at=published_at,
                    source_uri=entry.get("source_uri", ""),
                    publisher=entry.get("publisher", ""),
                    excerpt=entry.get("excerpt", ""),
                    observation=entry.get("observation"),
                    units=entry.get("units", ""),
                    currency=entry.get("currency", ""),
                    is_redistributable=bool(entry.get("is_redistributable", False)),
                )
            except EvidenceError as exc:
                skipped.append(
                    ExcludedItem(
                        kind="evidence",
                        reason=f"evidence rejected by the EVID-01 contract: {exc}",
                        payload={"key": entry.get("key")},
                    )
                )
                continue
            ref = _EvidenceRef(
                id=str(item.id),
                key=str(entry.get("key") or item.source_type),
                source_type=item.source_type,
                publisher=item.publisher,
                published_at=utc_iso(item.published_at),
                available_at=utc_iso(item.available_at),
                text=item.excerpt or json.dumps(json_safe(item.observation or {}), sort_keys=True),
                units=item.units,
                currency=item.currency,
            )
            refs.append(ref)
            if state.run is not None:
                append_event(
                    state.run,
                    RunEventType.EVIDENCE_RECORDED,
                    stage=RunStage.COLLECTING,
                    agent_role=AgentRole.SYSTEM,
                    payload={
                        "evidence_id": ref.id,
                        "key": ref.key,
                        "source_type": ref.source_type,
                        "published_at": ref.published_at,
                        "available_at": ref.available_at,
                    },
                )
        return refs, skipped

    def _refs_from_snapshot(
        self, snapshot: InputSnapshot
    ) -> tuple[list[_EvidenceRef], list[ExcludedItem]]:
        items = list(EvidenceItem.objects.filter(snapshot=snapshot).order_by("available_at", "id"))
        refs = []
        for index, item in enumerate(items):
            refs.append(
                _EvidenceRef(
                    id=str(item.id),
                    key=f"{item.source_type}:{index}",
                    source_type=item.source_type,
                    publisher=item.publisher,
                    published_at=utc_iso(item.published_at),
                    available_at=utc_iso(item.available_at),
                    text=item.excerpt
                    or json.dumps(json_safe(item.observation or {}), sort_keys=True),
                    units=item.units,
                    currency=item.currency,
                )
            )
        return refs, []

    def _synthetic_refs(self, context: Any) -> list[_EvidenceRef]:
        """Stable, non-persisted evidence ids for a read-only run."""
        prepared, _skipped = self._gather_evidence(context)
        return [
            _EvidenceRef(
                id=f"ev:{index}",
                key=str(entry.get("key") or index),
                source_type=entry["source_type"],
                publisher=entry.get("publisher", ""),
                published_at=utc_iso(entry.get("published_at")),
                available_at=None,
                text=entry.get("excerpt", ""),
                units=entry.get("units", ""),
                currency=entry.get("currency", ""),
            )
            for index, entry in enumerate(prepared, start=1)
        ]

    def _persist_claims(
        self,
        state: _RunState,
        drafts: list[ClaimDraft],
        *,
        agent_role: str,
        stance: str,
    ) -> list[dict]:
        payloads: list[dict] = []
        for index, draft in enumerate(drafts, start=1):
            if state.run is None:
                # Read-only run: the case still has to reach the next prompt, or
                # the dry run would exercise a protocol with empty inputs.
                payload = {
                    "claim_id": f"unrecorded-{stance}-{index}",
                    "agent_role": agent_role,
                    "stance": stance,
                    "statement": draft.statement,
                    "claim_type": draft.claim_type,
                    "evidence_ids": list(draft.evidence_ids),
                    "horizon": draft.horizon,
                    "limitations": draft.limitations,
                }
                payloads.append(payload)
                by_id = {ref.id: ref for ref in state.refs}
                state.claims_by_stance.setdefault(stance, []).append(
                    {
                        **payload,
                        "evidence": [
                            {
                                "id": evidence_id,
                                "source_type": by_id[evidence_id].source_type,
                                "publisher": by_id[evidence_id].publisher,
                                "published_at": by_id[evidence_id].published_at,
                                "text": by_id[evidence_id].text[:300],
                            }
                            for evidence_id in draft.evidence_ids
                            if evidence_id in by_id
                        ],
                    }
                )
                continue
            try:
                claim = create_claim(
                    run=state.run,
                    agent_role=agent_role,
                    stance=stance,
                    statement=draft.statement,
                    claim_type=draft.claim_type,
                    evidence_ids=draft.evidence_ids,
                    horizon=draft.horizon,
                    limitations=draft.limitations,
                    case_round=1,
                )
            except (ClaimValidationError, EvidenceError) as exc:
                self._exclude(
                    state,
                    [
                        ExcludedItem(
                            kind="claim",
                            reason=f"recorded claim rejected by the EVID-02 contract: {exc}",
                            payload={"statement": draft.statement},
                        )
                    ],
                )
                continue
            evidence_ids = [str(item.id) for item in claim.evidence.all()]
            state.claims[str(claim.claim_id)] = claim
            payload = {
                "claim_id": str(claim.claim_id),
                "agent_role": claim.agent_role,
                "stance": claim.stance,
                "statement": claim.statement,
                "claim_type": claim.claim_type,
                "evidence_ids": evidence_ids,
                "horizon": claim.horizon,
                "limitations": claim.limitations,
            }
            payloads.append(payload)
            state.claims_by_stance.setdefault(stance, []).append(
                {
                    **payload,
                    "evidence": [
                        {
                            "id": str(item.id),
                            "source_type": item.source_type,
                            "publisher": item.publisher,
                            "published_at": utc_iso(item.published_at),
                            "text": (item.excerpt or json.dumps(json_safe(item.observation or {})))[
                                :300
                            ],
                        }
                        for item in claim.evidence.all()
                    ],
                }
            )
            append_event(
                state.run,
                RunEventType.CLAIM_CREATED,
                stage=RunStage.ANALYSING,
                agent_role=claim.agent_role,
                payload={
                    "claim_id": payload["claim_id"],
                    "case": stance,
                    "claim_type": claim.claim_type,
                    "evidence_ids": evidence_ids,
                    "case_round": claim.case_round,
                },
            )
        return payloads

    def _persist_rebuttal(self, state: _RunState, draft: RebuttalDraft, *, agent_role: str) -> dict:
        if state.run is None:
            return {
                "rebuttal_id": "unrecorded",
                "responding_agent": agent_role,
                "target_claim_id": draft.target_claim_id,
                "response_type": draft.response_type,
                "explanation": draft.explanation,
                "evidence_ids": list(draft.evidence_ids),
                "position_change": draft.position_change,
            }
        target = state.claims[draft.target_claim_id]
        try:
            rebuttal = create_rebuttal(
                run=state.run,
                responding_agent=agent_role,
                target_claim=target,
                response_type=draft.response_type,
                explanation=draft.explanation,
                evidence_ids=draft.evidence_ids,
                position_change=draft.position_change,
            )
        except EvidenceError as exc:
            raise StageSchemaError(
                f"The {agent_role} rebuttal was rejected by the EVID-03 contract: {exc}",
                stage=RunStage.REBUTTING,
            ) from exc
        evidence_ids = [str(item.id) for item in rebuttal.evidence.all()]
        state.rebuttals.append(rebuttal)
        payload = {
            "rebuttal_id": str(rebuttal.id),
            "responding_agent": rebuttal.responding_agent,
            "target_claim_id": str(target.claim_id),
            "response_type": rebuttal.response_type,
            "explanation": rebuttal.explanation,
            "evidence_ids": evidence_ids,
            "position_change": rebuttal.position_change,
            "position_changed": not rebuttal.position_change.lower().startswith("unchanged"),
        }
        append_event(
            state.run,
            RunEventType.REBUTTAL_CREATED,
            stage=RunStage.REBUTTING,
            agent_role=rebuttal.responding_agent,
            payload={
                "rebuttal_id": payload["rebuttal_id"],
                "target_claim_id": payload["target_claim_id"],
                "response_type": payload["response_type"],
                "position_changed": payload["position_changed"],
                "round": 1,
            },
        )
        return payload

    def _record_decision(
        self,
        state: _RunState,
        adjudication: Adjudication,
        *,
        baseline: bool,
        consensus: bool | None,
        disagreement: dict | None = None,
    ) -> DecisionRecord | None:
        if state.run is None:
            # Read-only run: validate and return the proposal, write nothing.
            return None
        decision = DecisionRecord.objects.create(
            run=state.run,
            action=adjudication.action,
            requested_quantity=adjudication.requested_quantity(),
            concise_rationale=adjudication.reasoning,
            decisive_claim_ids=list(adjudication.decisive_claim_ids),
            strongest_objection=adjudication.strongest_objection,
            thesis=adjudication.thesis,
            invalidation_condition=adjudication.invalidation_condition,
            engine_source=self.source,
        )
        append_event(
            state.run,
            RunEventType.DECISION_PROPOSED,
            stage=RunStage.ADJUDICATING,
            agent_role=AgentRole.CIO,
            payload={
                "decision_id": str(decision.id),
                "action": decision.action,
                "requested_quantity": (
                    str(decision.requested_quantity)
                    if decision.requested_quantity is not None
                    else None
                ),
                "decisive_claim_ids": list(decision.decisive_claim_ids),
                "strongest_objection": decision.strongest_objection,
                "unresolved_uncertainty": adjudication.unresolved_uncertainty,
                "analyst_consensus": consensus,
                "baseline": baseline,
                "disagreement": disagreement,
            },
        )
        return decision

    def _complete(self, state: _RunState, adjudication: Adjudication) -> DecisionRun | None:
        """Finish the *analysis* and stop at ``validating``.

        Analysis completion is not decision completion. The run used to jump to
        ``completed`` here, before ``tasks.py`` had evaluated the guard, so a
        proposal could read as a finished decision while approval or execution
        was still outstanding. The execution lifecycle (``services.
        decision_lifecycle``) owns the next transition, and it only makes it once
        a real guard result exists.
        """
        if state.run is None:
            return None
        self._transition(state, DecisionStatus.VALIDATING)
        assert state.snapshot is not None
        metadata = update_protocol_metadata(
            state.run,
            status=DecisionStatus.VALIDATING,
            analysis_completed=True,
            completion_owner="services.decision_lifecycle",
            snapshot_hash=state.snapshot.content_hash,
            usage=self._usage.as_dict(),
            attempts=self._attempts_items,
            excluded_items=[item.as_dict() for item in state.excluded],
            raw_outputs=state.raw_outputs,
            counts={
                "bull_claims": len(state.claims_by_stance.get(ClaimStance.BULL, [])),
                "bear_claims": len(state.claims_by_stance.get(ClaimStance.BEAR, [])),
                "rebuttals": len(state.rebuttals),
                "rebuttal_rounds_run": state.rebuttal_rounds,
                "excluded_items": len(state.excluded),
            },
            limits=self._limits(),
            cost_accounting=self._cost_accounting(),
            deterministic_replay=(
                "Stored events and records replay deterministically. Regenerating the "
                "model outputs does not: a second generation is a new sample, not a "
                "reproduction of this one."
            ),
        )
        DecisionRun.objects.filter(pk=state.run.pk).update(
            total_cost_usd=self._usage.cost_usd,
            estimated_cost_usd=(
                self._usage.price_table_cost_usd if self._usage.price_table_cost_complete else None
            ),
            cost_basis=self._cost_basis(),
            input_tokens=self._usage.input_tokens,
            output_tokens=self._usage.output_tokens,
        )
        state.run.refresh_from_db()
        logger.info(
            "Debate protocol run %s validated (analysis complete, awaiting guard): %s (%s tokens)",
            state.run.id,
            adjudication.action,
            self._usage.input_tokens + self._usage.output_tokens,
        )
        state.run.protocol_metadata = metadata
        return state.run

    def _fail(self, state: _RunState, exc: StageFailure) -> None:
        # The exception may carry a model-stage key (``bull_case``) rather than a
        # run stage; the run stage is what the event log and ``stage_states``
        # need, so an unrecognised value falls back to the stage the run was in.
        if exc.stage in RunStage.values:
            stage = exc.stage
        elif state.current_stage in RunStage.values:
            stage = state.current_stage
        else:
            stage = RunStage.ANALYSING
        exc.stage = stage
        if state.run is None:
            return
        exc.run = state.run
        append_event(
            state.run,
            RunEventType.STAGE_FAILED,
            stage=stage,
            agent_role=AgentRole.SYSTEM,
            payload={
                **exc.as_payload(),
                "attempts": self._attempts_items,
                "fallback_allowed": self._fallback_allowed(),
            },
        )
        try:
            record_failure(
                state.run,
                error_code=exc.code,
                error_detail=exc.detail,
                payload={
                    "failed_stage": stage,
                    "protocol": self.protocol_version,
                    "fallback_allowed": self._fallback_allowed(),
                },
            )
        except DecisionError:
            logger.exception("Could not mark debate run %s failed", state.run.id)
        update_protocol_metadata(
            state.run,
            status=DecisionStatus.FAILED,
            failure={
                "code": exc.code,
                "stage": stage,
                "detail": exc.detail,
                "attempts": self._attempts_items,
            },
            usage=self._usage.as_dict(),
            attempts=self._attempts_items,
            excluded_items=[item.as_dict() for item in state.excluded],
            raw_outputs=state.raw_outputs,
            limits=self._limits(),
            cost_accounting=self._cost_accounting(),
        )
        DecisionRun.objects.filter(pk=state.run.pk).update(
            total_cost_usd=self._usage.cost_usd,
            estimated_cost_usd=(
                self._usage.price_table_cost_usd if self._usage.price_table_cost_complete else None
            ),
            cost_basis=self._cost_basis(),
            input_tokens=self._usage.input_tokens,
            output_tokens=self._usage.output_tokens,
        )
        logger.warning("Debate protocol run %s failed at %s: %s", state.run.id, stage, exc.detail)

    # -- plumbing ---------------------------------------------------------
    def _known_claim_ids(self, state: _RunState) -> set[str]:
        """Every claim id the CIO may cite, recorded or read-only."""
        ids = set(state.claims)
        for claims in state.claims_by_stance.values():
            ids.update(item["claim_id"] for item in claims)
        return ids

    def _limits(self) -> dict:
        return {
            "max_initial_claims": MAX_INITIAL_CLAIMS,
            "max_rebuttals_per_agent": MAX_REBUTTALS_PER_AGENT,
            "rebuttal_rounds": self._rebuttal_rounds,
            "max_statement_chars": MAX_STATEMENT_CHARS,
            "max_evidence_items": int(self.config.get("MAX_EVIDENCE_ITEMS", 20) or 20),
        }

    def _fallback_allowed(self) -> bool:
        return bool(getattr(settings, "AI_CONFIG", {}).get("ALLOW_HEURISTIC_FALLBACK", True))

    def _transition(
        self,
        state: _RunState,
        status: str,
        *,
        payload: dict | None = None,
    ) -> None:
        state.current_stage = status
        if state.run is None:
            return
        transition_status(state.run, status, payload=payload)

    def _stage_event(
        self,
        state: _RunState,
        event_type: str,
        stage: str,
        *,
        agent_role: str = AgentRole.SYSTEM,
        payload: dict | None = None,
    ) -> None:
        if state.run is None:
            return
        append_event(
            state.run,
            event_type,
            stage=stage,
            agent_role=agent_role,
            payload=payload or {},
        )

    def _limitation(self, state: _RunState, text: str) -> None:
        """Record a limitation on the run *and* keep it for the metadata.

        Called for every exclusion, every skipped stage and every degraded
        source, because a limitation that exists only in a Python variable is
        not a record.
        """
        text = str(text).strip()
        if not text:
            return
        state.limitations.append(text)
        if state.run is None:
            return
        append_event(
            state.run,
            RunEventType.LIMITATION_RECORDED,
            stage=state.current_stage
            if state.current_stage in RunStage.values
            else RunStage.COLLECTING,
            agent_role=AgentRole.SYSTEM,
            payload={"limitation": text[:2000]},
        )

    def _exclude(self, state: _RunState, items: list[ExcludedItem]) -> None:
        """Keep an excluded item on the record and announce it.

        "Unsupported claims are flagged or excluded - never silently dropped"
        (DEB-02). The item lands in ``protocol_metadata.excluded_items`` *and*
        in a ``limitation.recorded`` event, so neither the database nor the
        event stream can show a claim that quietly disappeared.
        """
        for item in items:
            state.excluded.append(item)
            self._limitation(state, f"{item.kind} excluded: {item.reason}")

    def _attempts_record(self, record: dict) -> None:
        self._attempts_items.append(json_safe(record))

    # -- model calls ------------------------------------------------------
    def _call_model(self, state: _RunState, stage_key: str, prompt: str, *, stage: str) -> str:
        """One model call with the budget checks, timeout and retries applied.

        **The budget is checked before every attempt, including every retry.**
        Checking once before the loop let a retry push past the monetary ceiling
        unchecked: the first call's cost was inside the budget, the second was
        not, and nothing looked again.

        **Every call's timeout is the smaller of the stage timeout and the
        remaining run deadline.** A stage timeout alone lets a run overrun its
        own deadline by a full stage; a retry's backoff is bounded the same way
        and becomes a typed :class:`DeadlineExceeded` rather than a sleep that
        outlives the run.

        A stage timeout is deliberately *not* retried: the stage is over, and
        retrying would double the wall-clock budget the run deadline caps. The
        abandoned call is reconciled from the client's counters when the client
        can do that, and labelled unreconciled when it cannot. A transient error
        is retried ``provider_retries`` times with exponential backoff.
        """
        retries = 0
        delay = self.budget.provider_retry_backoff_seconds
        while True:
            # Before *every* attempt, retries included.
            self._check_budgets(stage_key, prompt, stage=stage)
            timeout, basis = self._effective_timeout(stage, stage_key)
            started = time.monotonic()
            try:
                text = _call_with_timeout(
                    lambda: self.client.complete(stage_key, prompt),
                    timeout,
                    stage=stage_key,
                    timeout_basis=basis,
                )
            except StageTimeout as exc:
                self._account_timed_out(state, stage_key, stage=stage, exc=exc)
                if basis == "remaining_deadline":
                    # The call did not exceed the stage allowance; the run ran
                    # out of time. Reporting a stage timeout here would blame the
                    # provider for the deadline the run imposed on itself.
                    raise DeadlineExceeded(
                        f"the remaining run deadline bounded the call to stage {stage_key!r} "
                        f"at {timeout:g}s, which elapsed. The stage was abandoned and the "
                        "run stopped rather than overrunning its deadline.",
                        budget="run_deadline",
                        stage=stage,
                    ) from exc
                raise
            except ProviderTransientError as exc:
                # A failed call can still be billed. Account whatever the client
                # reported for it before deciding whether a retry is affordable.
                self._reconcile_failed_call(state, stage_key, stage=stage, exc=exc)
                retries += 1
                self._usage.retries += 1
                remaining = self._remaining_deadline()
                self._attempts_record(
                    {
                        "stage": stage_key,
                        "kind": "provider_retry",
                        "attempt": retries,
                        "error": exc.detail,
                        "remaining_deadline_seconds": (
                            None if remaining is None else round(remaining, 3)
                        ),
                    }
                )
                if retries > self.budget.provider_retries:
                    raise ProviderError(
                        f"stage {stage_key!r} still failing after {retries} attempt(s): "
                        f"{exc.detail}",
                        stage=stage,
                    ) from exc
                if delay:
                    remaining = self._remaining_deadline()
                    if remaining is not None and remaining <= delay:
                        raise DeadlineExceeded(
                            f"stage {stage_key!r} needs a retry after {delay:g}s of backoff, "
                            f"but only {remaining:.2f}s of the "
                            f"{self.budget.run_deadline_seconds:g}s run deadline remain. "
                            "The retry was not started.",
                            budget="run_deadline",
                            stage=stage,
                        ) from exc
                    logger.warning(
                        "Debate stage %s hit a transient provider error (attempt %s/%s); "
                        "retrying in %.2fs: %s",
                        stage_key,
                        retries,
                        self.budget.provider_retries,
                        delay,
                        exc.detail,
                    )
                    time.sleep(delay)
                delay *= 2
                continue
            except StageFailure:
                raise
            except Exception as exc:
                # An unexpected exception is a fatal provider error, not
                # something to retry blindly. Retries are reserved for errors the
                # client classified as transient.
                raise ProviderError(
                    f"stage {stage_key!r} raised {type(exc).__name__}: {exc}",
                    stage=stage,
                ) from exc

            usage = self._read_usage(prompt, text)
            self._account(
                state, stage_key, usage, elapsed_ms=int((time.monotonic() - started) * 1000)
            )
            self._accounted_client_usage = getattr(self.client, "last_usage", None)
            return text

    def _remaining_deadline(self) -> float | None:
        """Seconds left in the run deadline, or ``None`` when it is disabled."""
        if not self.budget.run_deadline_seconds:
            return None
        return self.budget.run_deadline_seconds - (time.monotonic() - self._started_monotonic)

    def _effective_timeout(self, stage: str, stage_key: str) -> tuple[float, str]:
        """The timeout to use for one call, bounded by the remaining deadline."""
        stage_timeout = self.budget.stage_timeout_seconds or 0.0
        remaining = self._remaining_deadline()
        if remaining is None:
            return stage_timeout, "stage_timeout"
        if remaining <= 0:
            raise DeadlineExceeded(
                f"run deadline of {self.budget.run_deadline_seconds:g}s elapsed before stage "
                f"{stage_key!r} could start.",
                budget="run_deadline",
                stage=stage,
            )
        if stage_timeout and stage_timeout <= remaining:
            return stage_timeout, "stage_timeout"
        return remaining, "remaining_deadline"

    def _check_budgets(self, stage_key: str, prompt: str, *, stage: str) -> None:
        elapsed = time.monotonic() - self._started_monotonic
        if self.budget.run_deadline_seconds and elapsed >= self.budget.run_deadline_seconds:
            raise DeadlineExceeded(
                f"run deadline of {self.budget.run_deadline_seconds:g}s elapsed before stage "
                f"{stage_key!r} (elapsed {elapsed:.1f}s).",
                budget="run_deadline",
                stage=stage,
            )
        if (
            self.budget.max_input_tokens
            and self._usage.input_tokens + _estimate_tokens(prompt) > self.budget.max_input_tokens
        ):
            raise BudgetExhausted(
                f"input token budget of {self.budget.max_input_tokens} would be exceeded before "
                f"stage {stage_key!r}.",
                budget="input_tokens",
                stage=stage,
            )
        if (
            self.budget.max_output_tokens
            and self._usage.output_tokens >= self.budget.max_output_tokens
        ):
            raise BudgetExhausted(
                f"output token budget of {self.budget.max_output_tokens} was exhausted before "
                f"stage {stage_key!r}.",
                budget="output_tokens",
                stage=stage,
            )
        if self.budget.monetary_budget_usd is not None:
            spend = self._usage.budget_spent_usd
            if spend >= self.budget.monetary_budget_usd:
                basis = self._usage.budget_basis
                raise BudgetExhausted(
                    f"monetary budget of {self.budget.monetary_budget_usd} USD was exhausted "
                    f"before stage {stage_key!r} (spend basis: {basis}; "
                    f"{self._usage.unreconciled_timeout_calls} unreconciled timed-out call(s)). "
                    "This ceiling is not a strict billed-dollar cap: a charge the provider "
                    "reports late is not visible here.",
                    budget="monetary",
                    stage=stage,
                )

    def _read_usage(self, prompt: str, text: str) -> ModelUsage:
        reported = getattr(self.client, "last_usage", None)
        if isinstance(reported, ModelUsage):
            if reported.price_table_cost_usd is None and reported.cost_usd is None:
                # Fill in the table price when the client did not, and only when
                # the model is actually priced. An unpriced model stays unknown.
                reported = ModelUsage(
                    input_tokens=reported.input_tokens,
                    output_tokens=reported.output_tokens,
                    cost_usd=reported.cost_usd,
                    reported=reported.reported,
                    tokens_measured=reported.tokens_measured,
                    price_table_cost_usd=price_table_cost(
                        reported.input_tokens + reported.output_tokens,
                        self.model,
                        input_tokens=reported.input_tokens,
                        output_tokens=reported.output_tokens,
                    ),
                    cost_unknown_reason=reported.cost_unknown_reason,
                )
            return reported
        input_tokens = _estimate_tokens(prompt)
        output_tokens = _estimate_tokens(text)
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=None,
            reported=False,
            tokens_measured=False,
            price_table_cost_usd=price_table_cost(
                input_tokens + output_tokens,
                self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            cost_unknown_reason=UNKNOWN_PROVIDER_COST,
        )

    def _account(
        self,
        state: _RunState,
        stage_key: str,
        usage: ModelUsage,
        *,
        elapsed_ms: int,
        timed_out: bool = False,
    ) -> None:
        self._usage.calls += 1
        if usage.measured_tokens:
            self._usage.measured_input_tokens += usage.input_tokens
            self._usage.measured_output_tokens += usage.output_tokens
        else:
            self._usage.estimated_input_tokens += usage.input_tokens
            self._usage.estimated_output_tokens += usage.output_tokens

        # Billed, price-table, or unknown - never a substitution.
        if usage.cost_usd is not None:
            self._usage.billed_cost_calls += 1
            self._usage.billed_cost_usd += usage.cost_usd
            self._usage.budget_basis_counts["billed"] = (
                self._usage.budget_basis_counts.get("billed", 0) + 1
            )
            self._usage.budget_spent_usd += usage.cost_usd
        else:
            self._usage.billed_cost_unknown_calls += 1
            if not self._usage.billed_cost_unknown_reason:
                self._usage.billed_cost_unknown_reason = (
                    usage.cost_unknown_reason or UNKNOWN_PROVIDER_COST
                )

        if usage.price_table_cost_usd is not None:
            self._usage.price_table_priced_calls += 1
            self._usage.price_table_cost_usd += usage.price_table_cost_usd
            if usage.cost_usd is None:
                self._usage.budget_basis_counts["price_table_estimate"] = (
                    self._usage.budget_basis_counts.get("price_table_estimate", 0) + 1
                )
                self._usage.budget_spent_usd += usage.price_table_cost_usd
        else:
            self._usage.price_table_unpriced_calls += 1
            if not self._usage.price_table_unknown_reason:
                self._usage.price_table_unknown_reason = UNKNOWN_MODEL_PRICING
            if usage.cost_usd is None:
                self._usage.budget_basis_counts["unknown"] = (
                    self._usage.budget_basis_counts.get("unknown", 0) + 1
                )

        self._attempts_record(
            {
                "stage": stage_key,
                "kind": "model_call",
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "token_accounting": ("provider_reported" if usage.measured_tokens else "estimated"),
                "cost_usd": str(usage.cost_usd) if usage.cost_usd is not None else None,
                "cost_basis": (
                    "billed_provider_reported"
                    if usage.cost_usd is not None
                    else (
                        "price_table_estimate"
                        if usage.price_table_cost_usd is not None
                        else "unknown"
                    )
                ),
                "price_table_cost_usd": (
                    str(usage.price_table_cost_usd)
                    if usage.price_table_cost_usd is not None
                    else None
                ),
                "cost_unknown_reason": usage.cost_unknown_reason or None,
                "timed_out": timed_out,
                "elapsed_ms": elapsed_ms,
            }
        )
        if (
            self.budget.max_output_tokens
            and self._usage.output_tokens > self.budget.max_output_tokens
        ):
            raise BudgetExhausted(
                f"output token budget of {self.budget.max_output_tokens} was exceeded at stage "
                f"{stage_key!r}.",
                budget="output_tokens",
                stage=state.current_stage if state.current_stage in RunStage.values else "",
            )

    def _reconcile_failed_call(
        self, state: _RunState, stage_key: str, *, stage: str, exc: StageFailure
    ) -> None:
        """Account a failed call's reported usage, if the client published any.

        A provider that charged for a call it then failed to answer has still
        spent the operator's money. When the client's ``last_usage`` changed
        during the failed attempt, it is accounted here so the retry budget check
        sees the spend; when it did not change, nothing is invented and the
        attempt is recorded as having no reported usage.
        """
        reported = getattr(self.client, "last_usage", None)
        if not isinstance(reported, ModelUsage) or reported is self._accounted_client_usage:
            self._attempts_record(
                {
                    "stage": stage_key,
                    "kind": "failed_attempt_usage",
                    "error": exc.detail,
                    "usage_reported": False,
                    "note": (
                        "The failed attempt reported no usage; any charge it incurred is "
                        "unknown and is not included in this run's cost."
                    ),
                }
            )
            return
        self._accounted_client_usage = reported
        self._account(
            state,
            stage_key,
            reported,
            elapsed_ms=0,
        )
        self._attempts_record(
            {
                "stage": stage_key,
                "kind": "failed_attempt_usage",
                "error": exc.detail,
                "usage_reported": True,
                "note": (
                    "Usage reported for a failed attempt is counted: a call that errored "
                    "can still be billed."
                ),
            }
        )

    def _account_timed_out(
        self, state: _RunState, stage_key: str, *, stage: str, exc: StageTimeout
    ) -> None:
        """Reconcile an abandoned call where the client can, label it where it cannot.

        The stage timeout stops waiting, not the provider. A client that can read
        its own cumulative counters may be able to attribute the abandoned call;
        when it cannot, the charge stays unknown and the run records an
        unreconciled count instead of a zero.
        """
        self._usage.timed_out_calls += 1
        reconciled: ModelUsage | None = None
        reconciler = getattr(self.client, "reconcile_timed_out_call", None)
        if callable(reconciler):
            try:
                reconciled = reconciler(stage=stage_key)
            except Exception as reconcile_error:  # pragma: no cover - defensive
                logger.warning(
                    "Could not reconcile timed-out stage %s: %s", stage_key, reconcile_error
                )
                reconciled = None
        if isinstance(reconciled, ModelUsage):
            self._usage.reconciled_timeout_calls += 1
            self._account(
                state,
                stage_key,
                reconciled,
                elapsed_ms=0,
                timed_out=True,
            )
            self._accounted_client_usage = getattr(self.client, "last_usage", None)
            self._attempts_record(
                {
                    "stage": stage_key,
                    "kind": "timeout_reconciled",
                    "detail": (
                        "The abandoned call was attributed from provider counters after the "
                        "stage timeout. Its billed cost is still whatever the provider reports."
                    ),
                }
            )
            return
        self._usage.unreconciled_timeout_calls += 1
        if not self._usage.billed_cost_unknown_reason:
            self._usage.billed_cost_unknown_reason = UNKNOWN_UNRECONCILED_TIMEOUT
        self._attempts_record(
            {
                "stage": stage_key,
                "kind": "timeout_unreconciled",
                "error": exc.detail,
                "note": (
                    "The provider may still bill this abandoned call; it is excluded from "
                    "every cost figure on this run."
                ),
            }
        )

    def _cost_basis(self) -> str:
        usage = self._usage
        if usage.cost_reported:
            return "billed_provider_reported"
        if usage.price_table_cost_complete:
            return "price_table_estimate"
        if usage.calls or usage.timed_out_calls:
            return "unknown"
        return ""

    def _cost_accounting(self) -> dict:
        """The cost contract stored on every run (FIX-07A).

        It states explicitly that the monetary ceiling is **not** a strict
        billed-dollar cap. A provider can report tokens without dollars, an
        abandoned call can still be billed, and a price table can be stale; a
        ceiling that claimed otherwise would be promising a measurement the
        system does not have.
        """
        usage = self._usage
        return {
            **usage.as_dict(),
            "monetary_ceiling": {
                "limit_usd": (
                    str(self.budget.monetary_budget_usd)
                    if self.budget.monetary_budget_usd is not None
                    else None
                ),
                "enforced_on": usage.budget_basis,
                "strict_billed_ceiling": False,
                "late_charges_possible": True,
                "note": (
                    "The ceiling is checked before every attempt (retries included) "
                    "against the best available basis: the provider-reported charge "
                    "where one exists, otherwise the price-table estimate. It is not "
                    "a guarantee that billed spend stayed under the limit: "
                    "unreconciled timed-out calls and charges the provider reports "
                    "later are not visible here."
                ),
            },
            "deterministic_replay": (
                "Cost figures replay from the recorded token and charge basis. A "
                "provider charge reported after the run is not part of this record."
            ),
        }

    @property
    def _attempts(self) -> list[dict]:
        return self._attempts_items

    def _disagreement_summary(self, rebuttals: list[dict]) -> dict:
        counts = {
            "agree": 0,
            "dispute": 0,
            "qualify": 0,
            "insufficient_evidence": 0,
        }
        for item in rebuttals:
            counts[item["response_type"]] = counts.get(item["response_type"], 0) + 1
        disputed = [
            item["target_claim_id"] for item in rebuttals if item["response_type"] == "dispute"
        ]
        position_changes = sum(1 for item in rebuttals if item.get("position_changed"))
        unresolved = bool(counts["dispute"] or counts["qualify"] or counts["insufficient_evidence"])
        return {
            # Consensus requires a debate to have happened and every recorded
            # response to be a concession. No rebuttals at all is "unknown", not
            # "agreed".
            "analyst_consensus": bool(rebuttals) and not unresolved,
            "counts": counts,
            "disputed_claim_ids": disputed,
            "position_changes": position_changes,
        }


# ---------------------------------------------------------------------------
# Heuristic fallback - a separate, honestly labelled attempt (DEB-04)
# ---------------------------------------------------------------------------
def record_heuristic_attempt(
    *,
    context: Any,
    portfolio: Portfolio,
    proposal: Any,
    reasoning: str,
    error: str,
    snapshot: InputSnapshot | None = None,
    failed_run: DecisionRun | None = None,
    fixture_id: str = "",
    baseline: bool = False,
    zero_cost: bool = False,
) -> DecisionRun:
    """Persist a deterministic-engine decision as its own run.

    This exists so a fallback is *separately identified* rather than appended to
    the attempt it replaces. The run has ``mode="heuristic"``, no claims and no
    rebuttals: a reviewer can never mistake it for a debate that happened. When
    it replaces a failed live attempt it reuses that attempt's snapshot (the
    inputs are identical by construction), and the failed run gets a
    ``limitation.recorded`` event naming the fallback so the UI can show both.

    ``proposal`` is duck-typed: ``action``, ``amount``, ``sentiment``,
    ``reasoning``, plus optional ``thesis`` and ``invalidation``. Importing
    ``ai_agent.TradeProposal`` here would be a circular import.

    ``baseline=True`` records the deterministic engine as an *experiment arm*
    (V0) rather than as a replacement for a failed model attempt: there is no
    ``fallback`` block and no failed run to name. ``zero_cost=True`` records the
    only genuinely measured zero this module can: no model call was made, so the
    provider charge is exactly zero by construction (``cost_basis`` says so).
    """
    protocol_version = HEURISTIC_PROTOCOL_VERSION
    reused_snapshot = snapshot is not None
    if snapshot is None:
        now = timezone.now()
        holdings = [
            {
                "ticker": asset.ticker.upper(),
                "quantity": str(asset.amount),
                "avg_purchase_price": str(asset.avg_purchase_price),
            }
            for asset in portfolio.assets.all()
        ]
        quotes: dict[str, dict] = {}
        price = getattr(context, "current_price", None)
        if price is not None:
            quotes[str(getattr(context, "ticker", "")).upper()] = {
                "price": str(price),
                "currency": "USD",
                "source": "market_data",
                "as_of": utc_iso(now),
            }
        snapshot = create_snapshot(
            portfolio=portfolio,
            portfolio_version=f"heuristic-{now.strftime('%Y%m%dT%H%M%SZ')}",
            analysis_cutoff=now,
            captured_at=now,
            holdings=holdings,
            cash={"balance_usd": str(portfolio.balance_usd), "currency": "USD"},
            quotes=quotes,
        )

    cost_basis = "deterministic_no_model_call" if zero_cost else ""
    run = create_run(
        portfolio=portfolio,
        ticker=getattr(context, "ticker", ""),
        protocol_version=protocol_version,
        mode=RunMode.HEURISTIC,
        source=RunSource.HEURISTIC_FALLBACK,
        snapshot=snapshot,
        started_at=timezone.now(),
        fixture_id=fixture_id,
        total_cost_usd=Decimal("0") if zero_cost else None,
        estimated_cost_usd=Decimal("0") if zero_cost else None,
        cost_basis=cost_basis,
        config_hash=content_hash(
            {"protocol": protocol_version, "engine": "HeuristicDecisionEngine"}
        ),
        protocol_metadata={
            "protocol": {
                "name": protocol_version,
                "version": protocol_version,
                "prompt_template_version": None,
                "provider": "deterministic",
                "model": "HeuristicDecisionEngine",
                "parameters": {},
                "tool_versions": dict(TOOL_VERSIONS),
                "snapshot_hash": snapshot.content_hash,
                "rounds": 0,
                "limits": {"max_initial_claims": 0, "max_rebuttals_per_agent": 0},
            },
            **(
                {
                    "baseline": {
                        "arm": "V0",
                        "engine": "HeuristicDecisionEngine",
                        "reason": str(error or "deterministic experiment baseline")[:2000],
                        "model_calls": 0,
                        "debate_executed": False,
                        "claims_recorded": 0,
                        "rebuttals_recorded": 0,
                        "cost_basis": cost_basis,
                    }
                }
                if baseline
                else {
                    "fallback": {
                        "reason": str(error or "heuristic fallback")[:2000],
                        "failed_run_id": (str(failed_run.id) if failed_run is not None else None),
                        "debate_executed": False,
                        "claims_recorded": 0,
                        "rebuttals_recorded": 0,
                        "reused_failed_attempt_snapshot": reused_snapshot,
                    }
                }
            ),
            "cost_accounting": {
                "measured_tokens": {"input": 0, "output": 0},
                "estimated_tokens": {"input": 0, "output": 0},
                "price_table_cost_usd": "0" if zero_cost else None,
                "billed_cost_usd": "0" if zero_cost else None,
                "billed_cost_unknown_calls": 0 if zero_cost else 1,
                "strict_billed_ceiling": False,
                "monetary_ceiling_basis": "no_model_call" if zero_cost else "unknown",
                "note": (
                    "No model call was made, so the provider charge is zero by "
                    "construction rather than unmeasured."
                    if zero_cost
                    else "No provider charge was reported; the billed cost is unknown."
                ),
            },
            "deterministic_replay": (
                "This proposal came from the deterministic engine: the same snapshot and "
                "the same code produce the same decision. No model output exists to regenerate."
            ),
        },
    )

    for status in (
        DecisionStatus.COLLECTING,
        DecisionStatus.ANALYSING,
        DecisionStatus.ADJUDICATING,
    ):
        transition_status(run, status)
    append_event(
        run,
        RunEventType.LIMITATION_RECORDED,
        stage=RunStage.ADJUDICATING,
        agent_role=AgentRole.SYSTEM,
        payload={
            "limitation": (
                "Deterministic experiment baseline: no model call was made, no debate was "
                "executed, and no claims or rebuttals were recorded. The proposal comes "
                "from the deterministic engine."
                if baseline
                else (
                    "Heuristic fallback: no debate was executed and no claims or rebuttals "
                    "were recorded. The proposal comes from the deterministic engine."
                )
            )
        },
    )

    amount = Decimal(str(getattr(proposal, "amount", 0) or 0))
    action = str(getattr(proposal, "action", "HOLD") or "HOLD").upper()
    decision = DecisionRecord.objects.create(
        run=run,
        action=action,
        requested_quantity=(amount if action != DecisionAction.HOLD and amount > 0 else None),
        concise_rationale=str(reasoning or getattr(proposal, "reasoning", "") or "")[:20000],
        decisive_claim_ids=[],
        strongest_objection="",
        thesis=str(getattr(proposal, "thesis", "") or ""),
        invalidation_condition=str(getattr(proposal, "invalidation", "") or ""),
        engine_source=RunSource.HEURISTIC_FALLBACK,
    )
    append_event(
        run,
        RunEventType.DECISION_PROPOSED,
        stage=RunStage.ADJUDICATING,
        agent_role=AgentRole.CIO,
        payload={
            "decision_id": str(decision.id),
            "action": decision.action,
            "requested_quantity": (
                str(decision.requested_quantity)
                if decision.requested_quantity is not None
                else None
            ),
            "decisive_claim_ids": [],
            "analyst_consensus": None,
            "fallback": True,
            "no_debate": True,
        },
    )
    # The fallback proposal is validated, not decided: the execution lifecycle
    # completes it after the guard has actually evaluated it.
    transition_status(run, DecisionStatus.VALIDATING)

    if failed_run is not None:
        append_event(
            failed_run,
            RunEventType.LIMITATION_RECORDED,
            stage=RunStage.FAILED,
            agent_role=AgentRole.SYSTEM,
            payload={
                "limitation": (
                    "A heuristic fallback attempt was recorded separately; it has no "
                    "claims and no rebuttals."
                ),
                "fallback_run_id": str(run.id),
                "fallback_mode": RunMode.HEURISTIC,
                "fallback_source": RunSource.HEURISTIC_FALLBACK,
            },
        )
        update_protocol_metadata(
            failed_run,
            fallback={
                "run_id": str(run.id),
                "mode": RunMode.HEURISTIC,
                "source": RunSource.HEURISTIC_FALLBACK,
                "protocol_version": protocol_version,
                "debate_executed": False,
                "claims_recorded": 0,
                "rebuttals_recorded": 0,
            },
        )
    logger.info(
        "Recorded heuristic fallback run %s (replaces %s)",
        run.id,
        failed_run.id if failed_run is not None else "no live attempt",
    )
    return run
