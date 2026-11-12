"""Portfolio CIO, plan creation and plan execution (PORT-01 - PORT-05).

The CIO is not a new persona. It is the existing chief-investment-officer role
extended to its natural boundary: one call sees **all** candidates, the whole
portfolio, the limits and the evidence, and returns one coherent allocation
instead of a sequence of independent ticker decisions. :class:`PortfolioCIO` is
the model seam; everything that turns its answer into trades is deterministic and
lives below it.

The lifecycle is:

``capture snapshot -> build brief -> CIO proposal -> validate contract ->
constrain weights -> size legs -> create plan -> apply plan``

Two rules are enforced here rather than trusted:

1. **The authoritative execution service remains the only route to the ledger.**
   Under one lock, ``apply_plan`` re-projects the whole plan against current
   marks, limits and classifications, verifies the projected final state and each
   leg's funding, and writes every leg through
   :func:`services.execution.execute_trade`. The allocation layer adds
   constraints; it does not replace the guard.
2. **A plan is bound to what was approved.** Limits, classifications, cost
   assumptions and the requested allocation are hashed on the plan. A change to
   any of them stops application and requires explicit revalidation; a changed
   price or derived quantity likewise requires a new approval rather than
   executing a different decision under the old one.
3. **A plan is applied against the portfolio version it was sized against.**
   If the portfolio changed, ``apply_plan`` refuses and sets
   ``revalidation_required``; ``revalidate_plan`` re-derives the legs from the
   stored requested weights against the new state. A duplicate apply returns the
   existing result and never adds a second ledger row.

Live application requires current, timestamped, finite marks for every material
holding and leg. The plan's own marks are accepted only in explicit ``paper``
mode, where the mode is recorded on the plan. Average cost is never substituted
for a missing mark in authoritative risk.

The whole application is one database transaction: selling legs are processed
first, every remaining leg is revalidated, and any failure rolls the transaction
back and marks the plan failed. No buying leg is attempted after a sale fails, so
proceeds from a failed sale are never treated as available cash.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.allocation_models import (
    EXECUTION_MODE_LIVE,
    EXECUTION_MODE_PAPER,
    PlanExecutionAttempt,
    PlanLegStatus,
    PlanSide,
    PlanStatus,
    PortfolioPlan,
    PortfolioPlanLeg,
)
from core.decision_json import content_hash, decimal_string, json_safe, utc_iso
from core.models import Asset, Portfolio, Transaction
from services.event_outbox import deliver_now, enqueue_event
from services.events import EventType
from services.execution import (
    ExecutionRequest,
    Mark,
    MaterialChangeError,
    ValuationUnavailableError,
    execute_trade,
    normalise_marks,
    portfolio_policy_version,
)
from services.market_data import PriceQuote
from services.portfolio_brief import (
    CandidateCase,
    PortfolioBrief,
    PortfolioState,
    build_allocation_brief,
    build_portfolio_state,
    capture_allocation_snapshot,
    load_classifications,
    portfolio_state_version,
)
from services.portfolio_policy import (
    CENT,
    ONE,
    WEIGHT_QUANTUM,
    ZERO,
    AllocationPolicy,
    CostModel,
)
from services.portfolio_targets import (
    TARGET_SCHEMA_VERSION,
    AllocationContractError,
    AllocationProposal,
    TargetAction,
    constrain_targets,
    decode_json_object,
    derive_legs,
    validate_target_proposal,
)

logger = logging.getLogger("alphaagent.portfolio_allocation")

__all__ = [
    "BINDING_SCHEMA",
    "PROMPT_TEMPLATE_VERSION",
    "PROTOCOL_VERSION",
    "AllocationClient",
    "AllocationError",
    "AllocationRun",
    "ApprovalReference",
    "BriefIncompleteError",
    "ModelCall",
    "PlanApplication",
    "PlanError",
    "PlanLegFailed",
    "PlanNotApplicableError",
    "PlanQuoteUnavailable",
    "PlanReapprovalRequired",
    "PlanStaleError",
    "PortfolioCIO",
    "StaticAllocationClient",
    "SubmissionConflictError",
    "apply_plan",
    "build_allocation_prompt",
    "build_hold_proposal",
    "create_plan",
    "plan_as_dict",
    "revalidate_plan",
    "run_allocation",
]

BINDING_SCHEMA = "portfolio_plan.binding.v1"

PROTOCOL_VERSION = "portfolio_allocation.v1"
PortfolioStateLike = PortfolioState
PROMPT_TEMPLATE_VERSION = "portfolio_targets.v1"
MAX_REPAIR_ATTEMPTS = 1

PROMPT_INSTRUCTIONS = """\
You are the chief investment officer (CIO) of this portfolio.

Decision boundary: ONE portfolio, ALL candidates together, one coherent capital
allocation. You are not analysing a single ticker and you are not writing
independent per-ticker orders: the weights you return are the whole portfolio
after this decision, and they must fit the constraints as a single plan.

Objective: allocate capital under the stated constraints. This is not a forecast
and not a guarantee of profitability. A decision to hold cash or make no change
is a valid and complete answer.

Hard contract (a response that violates it is rejected):
- target_weights MUST include a "CASH" key and cover every instrument you change.
- Every weight is a decimal STRING, not a JSON number, and is >= 0 (long-only,
  unlevered, no negative weights, no borrowing).
- The weights MUST sum to 1.0 within 0.000001. CASH is the uninvested residual.
- Every instrument with a target weight or a current holding needs one entry in
  "actions" with an action, a rationale, and claim_ids when the action increases
  risk.
- "increase" actions MUST cite at least one claim_id from the brief's
  claim_catalog. Do not invent a claim id.
- Include "rejected_alternatives" (with a reason each) and
  "unresolved_uncertainties".
- "funding_source" is one of: cash, sales, cash_and_sales, none.
- Echo the brief's "portfolio_version" exactly.
- Return ONE JSON object only. No markdown, no commentary, no code fences.

Use ONLY the statistics in the brief. Do NOT invent expected returns, forecasts,
correlations, covariances, volatilities, prices, sector labels or transaction
costs. A value that is null is unknown; say so rather than filling it in. Sector
exposure marked incomplete is incomplete, not zero.

Constraints you must respect (the deterministic layer re-checks them and records
every difference as a deviation): per-name weight, per-sector weight, total
exposure, turnover, cash buffer, and the per-trade execution ceiling. If a target
is infeasible, propose the feasible weight and explain the difference; never
scale a position up to fill a gap.

Response schema (exact keys):
{
  "schema_version": "portfolio_targets.v1",
  "portfolio_version": "<echo the brief value>",
  "objective": "<one sentence>",
  "target_weights": {"<TICKER>": "<decimal string>", "CASH": "<decimal string>"},
  "actions": [
    {"ticker": "<TICKER>", "action": "increase|reduce|maintain|exit|no_change",
     "rationale": "<why>", "claim_ids": ["<claim id>"]}
  ],
  "funding_source": "cash|sales|cash_and_sales|none",
  "rationale": "<portfolio-level rationale linked to the decisive claims>",
  "rejected_alternatives": [{"ticker": "<TICKER or null>", "reason": "<why not>"}],
  "unresolved_uncertainties": ["<what remains unknown>"],
  "data_sufficiency": "sufficient|insufficient"
}
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class AllocationError(Exception):
    """Base class for allocation failures."""


class BriefIncompleteError(AllocationError):
    """The brief cannot support sizing (e.g. equity is unknown)."""


class PlanError(AllocationError):
    """Base class for plan lifecycle failures."""


class PlanNotFoundError(PlanError):
    pass


class PlanStaleError(PlanError):
    """The portfolio version changed since the plan was evaluated."""

    def __init__(self, *, expected: str, current: str, detail: str = "") -> None:
        self.expected = expected
        self.current = current
        super().__init__(
            detail
            or (
                f"Plan was evaluated against {expected}, but the portfolio is now {current}. "
                "Revalidate the plan before applying it."
            )
        )


class PlanNotApplicableError(PlanError):
    """The plan is not in a state that can be applied."""


class PlanQuoteUnavailable(PlanError):
    """A current, timestamped valuation is unavailable for a material holding.

    A plan is never applied against planned marks, acquisition cost or a stale
    quote unless the caller explicitly selected ``paper`` mode. No new risk is
    taken when the portfolio cannot be valued.
    """

    def __init__(self, reason: str) -> None:
        self.detail = reason
        super().__init__(reason)


class PlanReapprovalRequired(PlanError):
    """An input that decides permission changed after the plan was approved.

    The plan is not executed and no ledger row is written. It is left in a state
    that ``revalidate_plan`` can re-project and a human can approve again.
    """

    def __init__(
        self,
        reason: str,
        *,
        code: str = "reapproval_required",
        revised_terms: dict | None = None,
    ) -> None:
        self.code = code
        self.detail = reason
        self.revised_terms = revised_terms or {}
        super().__init__(reason)


class SubmissionConflictError(PlanError):
    """A submission key was reused with a different request body."""


class PlanLegFailed(PlanError):
    """One leg failed revalidation or execution. The plan is halted."""

    def __init__(
        self,
        *,
        leg_sequence: int,
        ticker: str,
        code: str,
        detail: str,
    ) -> None:
        self.leg_sequence = leg_sequence
        self.ticker = ticker
        self.code = code
        self.detail = detail
        self.revised_terms: dict = {}
        super().__init__(f"leg {leg_sequence} ({ticker}) failed: {code}: {detail}")


# ---------------------------------------------------------------------------
# Model seam
# ---------------------------------------------------------------------------
class AllocationClient(Protocol):
    """The model surface the CIO needs. Tests substitute a stub.

    Implementations must be safe to call from the protocol's caller and may
    raise. Nothing here imports ``ai_agent``, so the module is testable with no
    API key, no network and no CrewAI import.
    """

    provider: str
    model: str
    parameters: dict
    last_usage: Any

    def complete(self, stage: str, prompt: str) -> str:
        raise NotImplementedError("AllocationClient is a Protocol; use a concrete client.")


@dataclass
class ModelCall:
    stage: str
    prompt_hash: str
    response_hash: str
    status: str
    elapsed_ms: int
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


class StaticAllocationClient:
    """A deterministic, explicitly-labelled stub client.

    Used by the paper command and the tests. It is not a fallback: a plan built
    with it records ``provider: "stub"`` and ``model: "stub-model"`` in its model
    metadata, so it can never be mistaken for a live decision.
    """

    provider = "stub"
    model = "stub-model"
    last_usage = None

    def __init__(self, response: Any, *, provider: str = "stub", model: str = "stub-model"):
        self.responses = list(response) if isinstance(response, list) else [response]
        self.provider = provider
        self.model = model
        self.parameters = {"mode": "static_stub", "network": False}
        self.calls: list[dict] = []

    def complete(self, stage: str, prompt: str) -> str:
        index = min(len(self.calls), len(self.responses) - 1)
        response = self.responses[index]
        self.calls.append({"stage": stage, "prompt": prompt})
        if isinstance(response, str):
            return response
        return json.dumps(response, indent=2, ensure_ascii=False)


def build_allocation_prompt(
    brief_payload: Mapping[str, Any],
    *,
    repair_note: str | None = None,
) -> str:
    """Render the one prompt that sees the whole portfolio and every candidate."""
    body = json.dumps(json_safe(dict(brief_payload)), indent=2, ensure_ascii=False)
    prompt = PROMPT_INSTRUCTIONS + "\nBRIEF (the only permitted source of facts):\n" + body + "\n"
    if repair_note:
        prompt += (
            "\nYour previous response was rejected: "
            + repair_note
            + "\nReturn only a corrected JSON object.\n"
        )
    return prompt


class PortfolioCIO:
    """The CIO at the portfolio decision boundary.

    One call, one validated proposal. A repairable contract violation gets one
    bounded repair attempt; arithmetic and version failures are not repaired by
    re-prompting, because the state or the numbers would not change.
    """

    def __init__(
        self,
        *,
        client: AllocationClient,
        policy: AllocationPolicy | None = None,
        max_repairs: int = MAX_REPAIR_ATTEMPTS,
    ) -> None:
        self.client = client
        self.policy = policy
        self.max_repairs = max(0, max_repairs)

    @property
    def provider(self) -> str:
        return str(getattr(self.client, "provider", "") or "unknown")

    @property
    def model(self) -> str:
        return str(getattr(self.client, "model", "") or "unknown")

    def decide(self, brief: PortfolioBrief) -> AllocationProposal:
        payload = brief.as_dict()
        policy = self.policy
        if policy is None:
            policy = AllocationPolicy.from_portfolio(
                # The policy only supplies limits; the brief already carries them.
                # Reconstructing from the brief keeps this method portfolio-free.
                _BriefPolicyCarrier(payload),
            )
        base_prompt = build_allocation_prompt(payload)
        prompt = base_prompt
        calls: list[ModelCall] = []
        last_error: AllocationContractError | None = None

        for attempt in range(self.max_repairs + 1):
            stage = "portfolio_allocation" if attempt == 0 else "portfolio_allocation.repair"
            started = time.monotonic()
            try:
                raw = self.client.complete(stage, prompt)
            except Exception as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                calls.append(
                    ModelCall(
                        stage=stage,
                        prompt_hash=content_hash(prompt),
                        response_hash="",
                        status="provider_error",
                        elapsed_ms=elapsed,
                        error=str(exc),
                    )
                )
                # Budget/deadline/provider failures carry a typed ``code``. Keep
                # them rather than flattening every provider failure into one
                # string, so the submission record names the budget that stopped.
                if getattr(exc, "code", ""):
                    raise
                raise AllocationError(f"Allocation provider call failed: {exc}") from exc
            elapsed = int((time.monotonic() - started) * 1000)
            try:
                decoded = decode_json_object(raw)
                proposal = validate_target_proposal(decoded, brief, policy=policy)
            except AllocationContractError as exc:
                last_error = exc
                calls.append(
                    ModelCall(
                        stage=stage,
                        prompt_hash=content_hash(prompt),
                        response_hash=content_hash(raw),
                        status="rejected",
                        elapsed_ms=elapsed,
                        error=f"{exc.code}: {exc.detail}",
                    )
                )
                if not exc.repairable or attempt >= self.max_repairs:
                    raise
                prompt = build_allocation_prompt(payload, repair_note=f"{exc.code}: {exc.detail}")
                continue
            calls.append(
                ModelCall(
                    stage=stage,
                    prompt_hash=content_hash(prompt),
                    response_hash=content_hash(raw),
                    status="accepted",
                    elapsed_ms=elapsed,
                )
            )
            proposal.model_metadata = self._metadata(
                base_prompt=base_prompt,
                raw=raw,
                calls=calls,
                repairs=attempt,
            )
            return proposal

        raise last_error or AllocationError("The CIO produced no valid proposal.")

    def _metadata(
        self, *, base_prompt: str, raw: Any, calls: list[ModelCall], repairs: int
    ) -> dict:
        usage = getattr(self.client, "last_usage", None)
        if usage is None:
            input_tokens = max(len(base_prompt) // 4, 0)
            output_tokens = max(len(str(raw)) // 4, 0)
            cost = None
            cost_reported = False
            method = "estimated_chars_per_4_tokens"
        else:
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            reported_cost = getattr(usage, "cost_usd", None)
            cost = str(reported_cost) if reported_cost is not None else None
            # ``cost_reported`` describes the *cost*, not whether the provider
            # reported a token count. A provider that reports tokens but no
            # dollars has an unknown cost (FIX-07A); the token basis travels
            # separately in ``token_accounting``.
            cost_reported = reported_cost is not None
            tokens_measured = bool(getattr(usage, "measured_tokens", False))
            method = "provider_reported" if tokens_measured else "estimated_chars_per_4_tokens"
        # A budgeted provider adapter publishes its limits and the separated
        # usage quantities. A static stub publishes nothing, which is itself a
        # signal that no provider was involved.
        reporter = getattr(self.client, "report", None)
        adapter_report = reporter() if callable(reporter) else None
        provider_name = self.provider.strip().lower()
        return {
            "provider": self.provider,
            "model": self.model,
            "parameters": json_safe(dict(getattr(self.client, "parameters", {}) or {})),
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "prompt_hash": content_hash(base_prompt),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "cost_reported": cost_reported,
            "token_accounting": method,
            "repair_attempts": repairs,
            "calls": [call.as_dict() for call in calls],
            "adapter": json_safe(adapter_report),
            "synthetic_response": provider_name in {"stub", "fixture", "static_stub"},
            "note": (
                "The brief is recorded verbatim in the plan and the validated response is "
                "stored as weights, actions and rationales. Replaying the arithmetic is "
                "deterministic; regenerating model output is not."
            ),
        }


class _BriefPolicyCarrier:
    """The minimal portfolio-shaped object ``AllocationPolicy`` reads.

    ``PortfolioCIO.decide`` receives a brief, not a portfolio. If the caller did
    not pass a policy, this carrier lets the policy be rebuilt from the brief's
    recorded limits without a database query. It deliberately exposes only the
    limit fields.
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        limits = dict((payload.get("policy") or {}).get("limits") or {})
        self.max_ticker_concentration_pct = Decimal(limits.get("max_name_weight", "0.30")) * 100
        self.max_sector_concentration_pct = Decimal(limits.get("max_sector_weight", "0.40")) * 100
        self.max_portfolio_exposure_pct = Decimal(limits.get("max_total_exposure", "0.90")) * 100
        self.min_cash_buffer_pct = Decimal(limits.get("min_cash_weight", "0.10")) * 100
        self.max_turnover_pct = Decimal(limits.get("max_turnover", "0.25")) * 100
        self.max_trade_allocation_pct = (
            Decimal(limits.get("max_trade_fraction_of_cash", "0.05")) * 100
        )


# ---------------------------------------------------------------------------
# Plan creation
# ---------------------------------------------------------------------------
def _sector_maps(brief: PortfolioBrief) -> tuple[dict[str, str], dict[str, str]]:
    sectors: dict[str, str] = {}
    asset_classes: dict[str, str] = {}
    for ticker, info in (brief.classifications or {}).items():
        if info.get("sector"):
            sectors[str(ticker).upper()] = str(info["sector"])
        if info.get("asset_class"):
            asset_classes[str(ticker).upper()] = str(info["asset_class"])
    return sectors, asset_classes


def _cost_reserve_fraction(
    requested: Mapping[str, Decimal],
    state: PortfolioStateLike,
    *,
    policy: AllocationPolicy,
    cost_model: CostModel,
) -> Decimal:
    """Reserve enough pre-cost equity for the plan's estimated transaction costs.

    The limits are stated on post-cost equity. A plan whose weights are at a
    ceiling before costs can cross it after costs are paid, so the projection
    scales the ceilings by a conservative reserve: the fee on the *requested*
    turnover (which is at least the executed turnover) plus the fixed per-leg
    charge. The reserve is capped at 5% and is an estimate from the declared cost
    model; the fill's ``Transaction.fee`` remains NULL.
    """
    if state.equity_usd is None or state.equity_usd <= ZERO:
        return ZERO
    equity = state.equity_usd
    tickers = set(requested) | {holding.ticker for holding in state.holdings}
    turn = ZERO
    legs = 0
    for ticker in tickers:
        if ticker == "CASH":
            continue
        delta = abs(requested.get(ticker, ZERO) - state.weight(ticker))
        turn += delta
        if delta * equity >= policy.min_trade_notional_usd:
            legs += 1
    if turn <= ZERO:
        return ZERO
    notional = turn * equity
    reserve = cost_model.estimate("BUY", notional)
    if legs > 1:
        reserve += cost_model.fixed_per_trade_usd * (legs - 1)
    return min(reserve / equity, Decimal("0.05"))


def _submission_request_hash(proposal: AllocationProposal) -> str:
    """Canonical hash of the request body a submission key claims to identify."""
    return content_hash({"schema": "portfolio_plan.request.v1", "proposal": proposal.as_dict()})


def _resolve_existing_plan(
    existing: PortfolioPlan, request_hash: str, submission_key: str
) -> tuple[PortfolioPlan, bool]:
    """A repeated key returns the first plan only for the *same* request."""
    if existing.submission_request_hash and existing.submission_request_hash != request_hash:
        raise SubmissionConflictError(
            f"Submission key {submission_key!r} was already used for a different plan "
            f"({existing.id}). Use a new key for a different request."
        )
    return existing, False


def _recorded_classifications(brief: PortfolioBrief, tickers: Sequence[str]) -> dict[str, dict]:
    recorded = brief.classifications or {}
    result: dict[str, dict] = {}
    for ticker in tickers:
        symbol = str(ticker).upper()
        info = recorded.get(symbol) or {}
        result[symbol] = {
            "sector": (str(info.get("sector")) if info.get("sector") else ""),
            "asset_class": (str(info.get("asset_class")) if info.get("asset_class") else ""),
        }
    return result


def _effective_classifications(
    plan: PortfolioPlan, portfolio: Portfolio, tickers: Sequence[str]
) -> dict[str, dict]:
    """Classifications that decide permission, now.

    The explicit lookup in the database wins where it exists; where it does not,
    the classification recorded on the approved plan is retained (it is what the
    approval was based on). A changed database label is a changed permission and
    therefore a reapproval, not a silent reclassification.
    """
    recorded = _recorded_classifications_from_payload(plan.brief, tickers)
    raw_recorded = (plan.brief or {}).get("classifications") or {}
    current = load_classifications(portfolio, tickers)
    result: dict[str, dict] = {}
    for ticker in tickers:
        symbol = str(ticker).upper()
        if symbol in current:
            info = current[symbol]
            result[symbol] = {
                "sector": str(info.get("sector") or ""),
                "asset_class": str(info.get("asset_class") or ""),
            }
        elif (raw_recorded.get(symbol) or {}).get("source"):
            # The plan recorded an explicit classification and the lookup no
            # longer has one. A deleted permission input is a changed permission
            # input, not an invitation to fall back to the plan's snapshot.
            result[symbol] = {"sector": "", "asset_class": ""}
        else:
            result[symbol] = recorded.get(symbol, {"sector": "", "asset_class": ""})
    return result


def _recorded_classifications_from_payload(brief: Mapping[str, Any], tickers: Sequence[str]):
    recorded = (brief or {}).get("classifications") or {}
    result: dict[str, dict] = {}
    for ticker in tickers:
        symbol = str(ticker).upper()
        info = recorded.get(symbol) or {}
        result[symbol] = {
            "sector": (str(info.get("sector")) if info.get("sector") else ""),
            "asset_class": (str(info.get("asset_class")) if info.get("asset_class") else ""),
        }
    return result


def _permission_payload(
    policy: AllocationPolicy,
    classifications: Mapping[str, dict],
    cost_model: CostModel,
    requested: Mapping[str, Any],
) -> dict:
    """Everything that decides whether the approved allocation is permitted."""
    return {
        "schema": BINDING_SCHEMA,
        "policy": json_safe(policy.as_dict()),
        "classifications": {
            ticker: {
                "sector": classifications.get(ticker, {}).get("sector", ""),
                "asset_class": classifications.get(ticker, {}).get("asset_class", ""),
            }
            for ticker in sorted(classifications)
        },
        "cost_model": json_safe(cost_model.as_dict()),
        "requested_allocation": {
            str(ticker).upper(): decimal_string(Decimal(str(weight)))
            for ticker, weight in sorted((requested or {}).items())
        },
    }


def _plan_binding(
    *,
    policy: AllocationPolicy,
    classifications: Mapping[str, dict],
    cost_model: CostModel,
    requested: Mapping[str, Any],
    target_weights: Mapping[str, str],
    legs: Sequence[Any],
    marks: Mapping[str, Decimal],
    execution_mode: str,
    constraints: dict,
    post_trade: dict,
) -> dict:
    permission = _permission_payload(policy, classifications, cost_model, requested)
    legs_payload = [
        {
            "sequence": int(leg.sequence),
            "ticker": str(leg.ticker).upper(),
            "side": str(leg.side),
            "quantity": decimal_string(Decimal(str(leg.quantity))),
            "price": decimal_string(Decimal(str(leg.price))),
        }
        for leg in legs
    ]
    return {
        **permission,
        "permission_hash": content_hash(permission),
        "target_weights": {
            str(ticker): decimal_string(Decimal(str(weight)))
            for ticker, weight in sorted((target_weights or {}).items())
        },
        "planned_marks": {
            str(ticker).upper(): decimal_string(Decimal(str(price)))
            for ticker, price in sorted((marks or {}).items())
        },
        "legs": legs_payload,
        "constraints": json_safe(constraints),
        "post_trade": json_safe(post_trade),
        "execution_mode": execution_mode,
    }


def _existing_plan(portfolio: Portfolio, submission_key: str) -> PortfolioPlan | None:
    key = (submission_key or "").strip()
    if not key:
        return None
    return PortfolioPlan.objects.filter(portfolio=portfolio, submission_key=key).first()


def create_plan(
    portfolio: Portfolio,
    *,
    brief: PortfolioBrief,
    proposal: AllocationProposal,
    submission_key: str = "",
    policy: AllocationPolicy | None = None,
    cost_model: CostModel | None = None,
    execution_mode: str = EXECUTION_MODE_LIVE,
    quote_observations: Mapping[str, Any] | None = None,
) -> tuple[PortfolioPlan, bool]:
    """Validate the deterministic projection and persist a plan and its legs.

    Returns ``(plan, created)``. A repeated ``submission_key`` with the *same*
    request returns the first plan; the same key with a different request is a
    conflict. The approved request, permissions, marks and legs are bound into a
    hash that ``apply_plan`` rechecks under the lock.
    """
    request_hash = _submission_request_hash(proposal)
    existing = _existing_plan(portfolio, submission_key)
    if existing is not None:
        return _resolve_existing_plan(existing, request_hash, submission_key)
    if brief.equity_usd is None or brief.equity_usd <= ZERO:
        raise BriefIncompleteError(
            "The brief has no usable equity: "
            + (", ".join(brief.data_quality.get("unpriced_tickers", [])) or "unknown")
        )

    policy = policy or AllocationPolicy.from_portfolio(portfolio)
    cost_model = cost_model or CostModel.from_config()
    sectors, asset_classes = _sector_maps(brief)
    constrained = constrain_targets(
        proposal.target_weights,
        brief.state,
        sectors=sectors,
        policy=policy,
        data_quality=brief.data_quality,
        prices=brief.state.prices,
        cost_reserve_fraction=_cost_reserve_fraction(
            proposal.target_weights, brief.state, policy=policy, cost_model=cost_model
        ),
    )
    sized = derive_legs(
        constrained,
        brief.state,
        policy=policy,
        cost_model=cost_model,
        sectors=sectors,
        asset_classes=asset_classes,
        quotes=brief.state.prices,
    )
    deviations = list(constrained.deviations) + list(sized.deviations)
    target_weights = constrained.weights_json
    actions = []
    for action in proposal.actions:
        row = action.as_dict()
        row["applied_target_weight"] = target_weights.get(action.ticker, "0")
        actions.append(row)
    if proposal.funding_source != sized.funding["source"]:
        deviations.append(
            {
                "code": "funding_source_differs",
                "detail": (
                    f"The proposal declared funding_source={proposal.funding_source!r}, but "
                    f"the executable plan uses {sized.funding['source']!r}."
                ),
            }
        )

    material_tickers = sorted(
        {holding.ticker for holding in brief.state.holdings}
        | {leg.ticker.upper() for leg in sized.legs}
        | {
            str(ticker).upper()
            for ticker, weight in proposal.target_weights.items()
            if str(ticker).upper() != "CASH" and weight > ZERO
        }
    )
    classifications = _recorded_classifications(brief, material_tickers)
    binding = _plan_binding(
        policy=policy,
        classifications=classifications,
        cost_model=cost_model,
        requested=proposal.target_weights,
        target_weights=target_weights,
        legs=sized.legs,
        marks={
            ticker: brief.state.prices[ticker]
            for ticker in material_tickers
            if ticker in brief.state.prices
        },
        execution_mode=execution_mode,
        constraints=constrained.constraints,
        post_trade=sized.post_trade,
    )

    try:
        with transaction.atomic():
            locked = Portfolio.objects.select_for_update().get(pk=portfolio.id)
            evaluated_version = portfolio_state_version(locked)
            concurrent = _existing_plan(locked, submission_key)
            if concurrent is not None:
                return _resolve_existing_plan(concurrent, request_hash, submission_key)
            payload = brief.as_dict()
            plan = PortfolioPlan.objects.create(
                portfolio=locked,
                snapshot_id=brief.snapshot_id,
                submission_key=(submission_key or "").strip(),
                submission_request_hash=request_hash,
                execution_mode=execution_mode,
                status=PlanStatus.PROPOSED,
                protocol_version=PROTOCOL_VERSION,
                policy_version=policy.policy_version,
                proposal_portfolio_version=proposal.portfolio_version,
                evaluated_portfolio_version=evaluated_version,
                investment_horizon_days=brief.investment_horizon_days,
                data_sufficiency=proposal.data_sufficiency or "",
                objective=proposal.objective or payload.get("objective", ""),
                requested_target_weights=proposal.target_weights_json(),
                target_weights=target_weights,
                actions=json_safe(actions),
                funding_source=proposal.funding_source,
                funding=json_safe(sized.funding),
                rationale=proposal.rationale,
                rejected_alternatives=json_safe(proposal.rejected_alternatives),
                unresolved_uncertainties=json_safe(proposal.unresolved_uncertainties),
                deviations=json_safe(deviations),
                constraints=json_safe(constrained.constraints),
                data_quality=json_safe(brief.data_quality),
                brief=json_safe(payload),
                brief_hash=content_hash(payload),
                cost_model=json_safe(cost_model.as_dict()),
                quote_observations=json_safe(dict(quote_observations or {})),
                model_metadata=json_safe(proposal.model_metadata),
                approval_binding=json_safe(binding),
                approval_hash=content_hash(binding),
                equity_usd=brief.equity_usd,
                cash_usd=brief.state.cash_usd,
                min_cash_usd=(policy.min_cash_weight * brief.equity_usd).quantize(CENT),
                estimated_cost_usd=sized.estimated_cost_usd,
                estimated_turnover_pct=sized.estimated_turnover_pct,
                revalidation_required=proposal.portfolio_version != evaluated_version,
            )
            for leg in sized.legs:
                PortfolioPlanLeg.objects.create(
                    plan=plan,
                    sequence=leg.sequence,
                    ticker=leg.ticker,
                    side=leg.side,
                    quantity=leg.quantity,
                    price=leg.price,
                    notional_usd=leg.notional_usd,
                    estimated_cost_usd=leg.estimated_cost_usd,
                    priority=leg.priority,
                    sector=leg.sector,
                    asset_class=leg.asset_class,
                    status=PlanLegStatus.PLANNED,
                    reason=leg.reason,
                )
        return plan, True
    except IntegrityError:
        # A concurrent submission with the same key won the unique constraint.
        existing = _existing_plan(portfolio, submission_key)
        if existing is not None:
            return _resolve_existing_plan(existing, request_hash, submission_key)
        raise


# ---------------------------------------------------------------------------
# Plan serialisation
# ---------------------------------------------------------------------------
def plan_as_dict(plan: PortfolioPlan, *, include_brief: bool = False) -> dict:
    """JSON-ready view of a plan, with Decimal values as strings."""
    legs = list(plan.legs.order_by("sequence"))
    weights = plan.target_weights or {}
    weights_sum = sum((Decimal(str(value)) for value in weights.values()), ZERO)
    payload = {
        "plan_id": str(plan.id),
        "portfolio_id": plan.portfolio_id,
        "snapshot_id": str(plan.snapshot_id),
        "submission_key": plan.submission_key or None,
        "submission_request_hash": plan.submission_request_hash or None,
        "execution_mode": plan.execution_mode,
        "status": plan.status,
        "protocol_version": plan.protocol_version,
        "policy_version": plan.policy_version,
        "proposal_portfolio_version": plan.proposal_portfolio_version,
        "evaluated_portfolio_version": plan.evaluated_portfolio_version,
        "investment_horizon_days": plan.investment_horizon_days,
        "plan_version": plan.plan_version,
        "data_sufficiency": plan.data_sufficiency or None,
        "objective": plan.objective,
        "requested_target_weights": plan.requested_target_weights,
        "target_weights": weights,
        "target_weights_sum": str(weights_sum),
        "actions": plan.actions,
        "funding_source": plan.funding_source,
        "funding": plan.funding,
        "rationale": plan.rationale,
        "rejected_alternatives": plan.rejected_alternatives,
        "unresolved_uncertainties": plan.unresolved_uncertainties,
        "deviations": plan.deviations,
        "constraints": plan.constraints,
        "data_quality": plan.data_quality,
        "brief_hash": plan.brief_hash,
        "cost_model": plan.cost_model,
        "model_metadata": plan.model_metadata,
        "equity_usd": str(plan.equity_usd) if plan.equity_usd is not None else None,
        "cash_usd": str(plan.cash_usd) if plan.cash_usd is not None else None,
        "min_cash_usd": str(plan.min_cash_usd) if plan.min_cash_usd is not None else None,
        "estimated_cost_usd": str(plan.estimated_cost_usd),
        "estimated_turnover_pct": (
            str(plan.estimated_turnover_pct) if plan.estimated_turnover_pct is not None else None
        ),
        "approval_hash": plan.approval_hash or None,
        "approval_binding": plan.approval_binding,
        "approval_version": plan.approval_version,
        "approved_by": plan.approved_by_id,
        "approved_at": utc_iso(plan.approved_at),
        "approval_expires_at": utc_iso(plan.approval_expires_at),
        "applied_approval_version": plan.applied_approval_version,
        "applied_by": plan.applied_by_id,
        "revalidation_required": plan.revalidation_required,
        "revalidation_count": plan.revalidation_count,
        "executed_target_weights": plan.executed_target_weights,
        "execution_reasons": plan.execution_reasons,
        "failure_code": plan.failure_code or None,
        "failure_detail": plan.failure_detail or None,
        "created_at": utc_iso(plan.created_at),
        "applied_at": utc_iso(plan.applied_at),
        "legs": [
            {
                "leg_id": leg.id,
                "sequence": leg.sequence,
                "ticker": leg.ticker,
                "side": leg.side,
                "quantity": str(leg.quantity),
                "price": str(leg.price),
                "notional_usd": str(leg.notional_usd),
                "estimated_cost_usd": str(leg.estimated_cost_usd),
                "priority": leg.priority,
                "sector": leg.sector or None,
                "asset_class": leg.asset_class or None,
                "status": leg.status,
                "transaction_id": leg.transaction_id,
                "reason": leg.reason,
            }
            for leg in legs
        ],
    }
    if include_brief:
        payload["brief"] = plan.brief
    return payload


# ---------------------------------------------------------------------------
# Plan application
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ApprovalReference:
    """The exact plan version, permission hash and terms an execution is approved under.

    Passed into :func:`apply_plan` so the check happens *under the plan's lock*,
    not in a caller-side window where a revalidation could slip in between.
    ``approved_legs``/``terms_hash``/tolerances are the terms the reviewer saw;
    execution compares the refreshed plan with them rather than with whatever
    the plan currently says.
    """

    plan_version: int
    approval_hash: str
    approval_version: int
    expires_at: datetime
    approval_id: int | None = None
    terms_hash: str = ""
    approved_legs: tuple[dict, ...] = ()
    price_tolerance_pct: Decimal | None = None
    quantity_tolerance: Decimal | None = None
    max_quote_age_seconds: int | None = None
    policy_version: str = ""
    classification_version: str = ""


@dataclass
class PlanApplication:
    plan: PortfolioPlan
    legs: list[PortfolioPlanLeg]
    transactions: list[Transaction]
    already_applied: bool = False


def _record_plan_failure(
    plan_id,
    failure: PlanLegFailed,
    *,
    attempt_id=None,
    attempt_number: int | None = None,
    mode: str = "",
    expected_plan_version: int | None = None,
    expected_portfolio_version: str = "",
) -> bool:
    """Record a failed attempt without overwriting a plan another attempt applied.

    The conditional update names the attempt token written before the application
    transaction. If another attempt has since written a newer token, or the plan
    is already applied/no-change, the update matches zero rows. This is the lock
    the delayed-failure race needs: the unsuccessful attempt is still recorded as
    a ``PlanExecutionAttempt``, but it cannot change the successful plan or its
    filled legs.
    """
    try:
        filters: dict[str, Any] = {
            "pk": plan_id,
            "status": PlanStatus.PROPOSED,
            "applied_at__isnull": True,
        }
        if attempt_id is not None:
            filters["execution_attempt_id"] = attempt_id
        if expected_plan_version is not None:
            filters["plan_version"] = expected_plan_version
        updated = PortfolioPlan.objects.filter(**filters).update(
            status=PlanStatus.FAILED,
            failure_code=failure.code,
            failure_detail=failure.detail,
            revalidation_required=False,
        )
        if updated:
            if failure.leg_sequence > 0:
                PortfolioPlanLeg.objects.filter(
                    plan_id=plan_id, sequence=failure.leg_sequence
                ).update(status=PlanLegStatus.FAILED, reason=failure.detail)
                PortfolioPlanLeg.objects.filter(
                    plan_id=plan_id, sequence__gt=failure.leg_sequence
                ).update(
                    status=PlanLegStatus.SKIPPED,
                    reason="Not attempted: the plan halted on an earlier leg.",
                )
            # Earlier legs were rolled back with the transaction and hold no fill.
            PortfolioPlanLeg.objects.filter(
                plan_id=plan_id, sequence__lt=max(failure.leg_sequence, 0)
            ).update(status=PlanLegStatus.PLANNED)
        if attempt_id is not None:
            plan = PortfolioPlan.objects.filter(pk=plan_id).only("portfolio_id").first()
            if plan is not None:
                PlanExecutionAttempt.objects.update_or_create(
                    attempt_id=attempt_id,
                    defaults={
                        "plan_id": plan_id,
                        "attempt_number": attempt_number or 1,
                        "status": "failed" if updated else "superseded",
                        "mode": mode,
                        "expected_plan_version": expected_plan_version or 1,
                        "expected_portfolio_version": expected_portfolio_version,
                        "failure_code": failure.code,
                        "failure_detail": failure.detail,
                        "finished_at": timezone.now(),
                    },
                )
        if not updated:
            logger.warning(
                "Ignoring stale failure for plan %s attempt %s; a newer attempt owns it",
                plan_id,
                attempt_id,
            )
        return bool(updated)
    except Exception:  # pragma: no cover - failure recording must never mask the cause
        logger.exception("Could not record allocation plan failure for %s", plan_id)
        return False


def _mark_plan_for_reapproval(
    plan_id,
    reason: str,
    *,
    attempt_id=None,
    expected_plan_version: int | None = None,
) -> None:
    """Leave a materially changed plan proposed, but requiring explicit review.

    The update is conditional on the attempt that produced the change. A
    delayed handler from an older attempt cannot clear the approval that a newer
    attempt is executing under.
    """
    try:
        filters: dict[str, Any] = {
            "pk": plan_id,
            "status": PlanStatus.PROPOSED,
            "applied_at__isnull": True,
        }
        if attempt_id is not None:
            filters["execution_attempt_id"] = attempt_id
        if expected_plan_version is not None:
            filters["plan_version"] = expected_plan_version
        PortfolioPlan.objects.filter(**filters).update(
            revalidation_required=True,
            status=PlanStatus.PROPOSED,
            failure_code="reapproval_required",
            failure_detail=reason[:4000],
            approval_version=0,
            approved_at=None,
            approval_expires_at=None,
        )
    except Exception:  # pragma: no cover - the raised error is the contract
        logger.exception("Could not flag allocation plan %s for reapproval", plan_id)


def _capture_after_plan(portfolio: Portfolio, quotes: Mapping[str, Decimal]) -> None:
    """Record the post-trade equity point; never let a snapshot failure undo a fill."""
    try:
        from services.snapshots import capture_snapshot

        price_map: dict[str, PriceQuote] = {}
        for asset in portfolio.assets.all():
            symbol = asset.ticker.upper()
            price = quotes.get(symbol)
            if price is not None and price > ZERO:
                price_map[symbol] = PriceQuote(
                    ticker=symbol, symbol=symbol, price=price, source="allocation_apply"
                )
        capture_snapshot(portfolio, price_map)
    except Exception as exc:  # pragma: no cover - operational safety net
        logger.warning("Post-plan snapshot failed for %s: %s", portfolio.id, exc)


def _material_tickers(plan: PortfolioPlan, legs: Sequence[PortfolioPlanLeg]) -> list[str]:
    """Every name whose price or classification can change the plan's permission.

    That includes names the model asked for but the projection declined. They are
    not legs, but their classification decides *whether* declining them was
    correct; dropping them from the valuation would let the same proposal be
    re-admitted at apply time under an unknown sector.
    """
    held = {
        asset.ticker.upper()
        for asset in Asset.objects.filter(portfolio_id=plan.portfolio_id, amount__gt=ZERO)
    }
    allocated = {
        str(ticker).upper()
        for ticker, weight in (plan.requested_target_weights or {}).items()
        if str(ticker).upper() != "CASH" and Decimal(str(weight)) > ZERO
    }
    projected = {
        str(ticker).upper()
        for ticker, weight in (plan.target_weights or {}).items()
        if str(ticker).upper() != "CASH" and Decimal(str(weight)) > ZERO
    }
    return sorted(held | allocated | projected | {leg.ticker.upper() for leg in legs})


def _require_marks(
    plan: PortfolioPlan,
    legs: Sequence[PortfolioPlanLeg],
    marks: Mapping[str, Any],
    *,
    max_quote_age_seconds: int,
    now,
    execution_mode: str = EXECUTION_MODE_LIVE,
) -> list[dict]:
    """Demand a current, timestamped mark for every material holding and leg.

    No acquisition-cost substitution is allowed here. A missing, future-dated,
    stale or untimed mark stops the plan before the first ledger write, unless
    the declared risk-reducing exception covers a *non-traded* holding in a
    sale-only plan. That exception is returned so it can be recorded; it is
    never silent and it never covers the price of the instrument being traded.

    ``max_quote_age_seconds`` is retained as the caller's ceiling; the
    per-source/per-instrument policy may only narrow it.
    """
    from services.quote_policy import ExecutionFreshnessPolicy, validate_mark

    policy = ExecutionFreshnessPolicy.from_settings()
    required = _material_tickers(plan, legs)
    leg_tickers = {leg.ticker.upper() for leg in legs}
    selling_only = bool(legs) and all(leg.side == PlanSide.SELL for leg in legs)
    paper = (execution_mode or "").strip().lower() in {"paper", "estimate", "estimation"}
    exemptions: list[dict] = []
    for ticker in required:
        mark = marks.get(ticker)
        first = next((leg for leg in legs if leg.ticker.upper() == ticker), None)
        sequence = first.sequence if first is not None else 0
        traded = ticker in leg_tickers
        if mark is None:
            raise PlanLegFailed(
                leg_sequence=sequence,
                ticker=ticker,
                code="price_unavailable",
                detail=(
                    f"No current quote was supplied for {ticker}; refusing to value the "
                    "plan from a purchase cost or a stale mark."
                ),
            )
        effective_max = min(
            max_quote_age_seconds,
            policy.max_age_seconds(ticker, mark.source),
        )
        result = validate_mark(
            ticker=ticker,
            price=mark.price,
            as_of=mark.as_of,
            retrieved_at=mark.retrieved_at,
            source=mark.source,
            now=now,
            mode=execution_mode,
            require_current=not paper,
            risk_reducing=selling_only and not traded,
            policy=policy,
        )
        _ = effective_max
        if not result.ok:
            raise PlanLegFailed(
                leg_sequence=sequence,
                ticker=ticker,
                code=result.code,
                detail=result.detail,
            )
        if result.rule.startswith("risk_reducing_"):
            exemptions.append(
                {
                    "ticker": ticker,
                    "rule": result.rule,
                    "reason": result.reason,
                    "observed_at": result.observed_at.isoformat() if result.observed_at else None,
                    "max_age_seconds": result.max_age_seconds,
                }
            )
    return exemptions


def _check_permission_binding(
    plan: PortfolioPlan,
    portfolio: Portfolio,
    *,
    policy: AllocationPolicy,
    cost_model: CostModel,
    material_tickers: Sequence[str],
) -> tuple[dict[str, dict], dict]:
    """Recompute the permission hash and refuse a plan whose inputs moved."""
    classifications = _effective_classifications(plan, portfolio, material_tickers)
    permission = _permission_payload(
        policy, classifications, cost_model, plan.requested_target_weights or {}
    )
    current_hash = content_hash(permission)
    stored = (plan.approval_binding or {}) if isinstance(plan.approval_binding, dict) else {}
    stored_hash = str(stored.get("permission_hash") or "")
    if stored_hash and stored_hash != current_hash:
        differences = _permission_differences(stored, permission)
        detail = (
            "An input that decides permission changed after this plan was approved "
            f"({', '.join(differences) or 'unknown'}). Revalidate and approve the "
            "current plan before applying it."
        )
        raise PlanReapprovalRequired(detail, code="permission_changed")
    return classifications, permission


def _permission_differences(stored: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    if (stored or {}).get("policy") != current.get("policy"):
        labels.append("policy limits")
    if (stored or {}).get("classifications") != current.get("classifications"):
        labels.append("instrument classifications")
    if (stored or {}).get("cost_model") != current.get("cost_model"):
        labels.append("cost assumptions")
    if (stored or {}).get("requested_allocation") != current.get("requested_allocation"):
        labels.append("approved allocation")
    return labels


def _project_plan(
    plan: PortfolioPlan,
    portfolio: Portfolio,
    state: PortfolioState,
    *,
    policy: AllocationPolicy,
    cost_model: CostModel,
    classifications: Mapping[str, dict],
) -> tuple[Any, Any]:
    """Re-run the deterministic projection against the locked current state."""
    sectors = {
        ticker: info["sector"] for ticker, info in classifications.items() if info.get("sector")
    }
    asset_classes = {
        ticker: info["asset_class"]
        for ticker, info in classifications.items()
        if info.get("asset_class")
    }
    requested = {
        str(ticker).upper(): Decimal(str(weight))
        for ticker, weight in (plan.requested_target_weights or {}).items()
    }
    constrained = constrain_targets(
        requested,
        state,
        sectors=sectors,
        policy=policy,
        data_quality=plan.data_quality,
        prices=state.prices,
        cost_reserve_fraction=_cost_reserve_fraction(
            requested, state, policy=policy, cost_model=cost_model
        ),
    )
    sized = derive_legs(
        constrained,
        state,
        policy=policy,
        cost_model=cost_model,
        sectors=sectors,
        asset_classes=asset_classes,
        quotes=state.prices,
    )
    return constrained, sized


def _leg_differences(plan: PortfolioPlan, sized) -> list[str]:
    """Names for every leg that the current state would execute differently."""
    stored = {leg.sequence: leg for leg in plan.legs.order_by("sequence")}
    derived = {leg.sequence: leg for leg in sized.legs}
    differences: list[str] = []
    for sequence in sorted(set(stored) | set(derived)):
        old = stored.get(sequence)
        new = derived.get(sequence)
        if old is None or new is None:
            differences.append(f"leg {sequence} appears/disappears")
            continue
        if (
            old.ticker.upper() != new.ticker.upper()
            or old.side != new.side
            or old.quantity != Decimal(str(new.quantity))
            or old.price != Decimal(str(new.price))
        ):
            differences.append(
                f"leg {sequence} {old.side} {old.quantity} {old.ticker}@{old.price} -> "
                f"{new.side} {new.quantity} {new.ticker}@{new.price}"
            )
    return differences


def _weight_differences(plan: PortfolioPlan, constrained) -> list[str]:
    stored = {
        str(ticker).upper(): Decimal(str(weight))
        for ticker, weight in (plan.target_weights or {}).items()
    }
    current = {str(ticker).upper(): weight for ticker, weight in constrained.weights.items()}
    differences: list[str] = []
    for ticker in sorted(set(stored) | set(current)):
        if abs(stored.get(ticker, ZERO) - current.get(ticker, ZERO)) > Decimal("0.000001"):
            differences.append(
                f"{ticker} {stored.get(ticker, ZERO)} -> {current.get(ticker, ZERO)}"
            )
    return differences


def _verify_projected_limits(constrained, sized, *, policy: AllocationPolicy) -> None:
    checks = (constrained.constraints or {}).get("checks") or {}
    for name in (
        "max_name_weight",
        "max_sector_weight",
        "max_total_exposure",
        "cash_buffer",
        "max_turnover",
    ):
        if (checks.get(name) or {}).get("status") == "fail":
            raise PlanReapprovalRequired(
                f"The projected final state breaches {name}; a new plan is required.",
                code="projected_limit_breach",
            )
    post = sized.post_trade or {}
    if post.get("status") == "violation":
        raise PlanReapprovalRequired(
            "The projected final state would increase an exposure past a hard limit.",
            code="projected_limit_breach",
        )
    _ = policy


def _verify_funding(
    legs: Sequence[Any],
    *,
    starting_cash: Decimal,
    min_cash: Decimal,
    policy: AllocationPolicy,
) -> None:
    """Simulate the post-cost cash path, sells first, and refuse a shortfall."""
    cash = starting_cash
    for leg in legs:
        if leg.side == PlanSide.BUY:
            cash -= leg.notional_usd + leg.estimated_cost_usd
        else:
            cash += leg.notional_usd - leg.estimated_cost_usd
        if cash < ZERO:
            raise PlanLegFailed(
                leg_sequence=int(leg.sequence),
                ticker=leg.ticker,
                code="funding_gap",
                detail=(
                    f"Leg {leg.sequence} ({leg.ticker}) would leave cash at ${cash}; the "
                    "post-cost funding path is not covered."
                ),
            )
    if cash + Decimal("0.01") < min_cash:
        raise PlanReapprovalRequired(
            f"The projected final cash ${cash} is below the policy cash buffer "
            f"${min_cash}; a new plan is required.",
            code="cash_buffer_breach",
        )
    _ = policy


def _mark_prices(marks: Mapping[str, Any]) -> dict[str, Decimal]:
    return {ticker: mark.price for ticker, mark in marks.items()}


def _record_plan_attempt(
    *,
    plan: PortfolioPlan,
    attempt_id,
    attempt_number: int,
    status: str,
    mode: str,
    expected_plan_version: int,
    expected_portfolio_version: str,
    price_tolerance_pct: Decimal | None,
    transaction_ids: list[int],
    detail: dict | None = None,
) -> PlanExecutionAttempt:
    attempt, _created = PlanExecutionAttempt.objects.update_or_create(
        attempt_id=attempt_id,
        defaults={
            "plan": plan,
            "attempt_number": attempt_number,
            "status": status,
            "mode": mode,
            "expected_plan_version": expected_plan_version,
            "expected_portfolio_version": expected_portfolio_version,
            "price_tolerance_pct": price_tolerance_pct,
            "transaction_ids": transaction_ids,
            "detail": json_safe(detail or {}),
            "finished_at": timezone.now(),
        },
    )
    return attempt


def _enqueue_plan_event(
    plan: PortfolioPlan, event_type: str, payload: dict, dedupe_key: str
) -> None:
    """Queue the durable plan event in the caller's transaction.

    Delivery is attempted only after commit; a notification outage cannot fail a
    plan that has already decided. The outbox row is the durable record.
    """
    row = enqueue_event(
        plan.portfolio.user_id,
        "portfolio",
        event_type,
        json_safe(payload),
        dedupe_key=dedupe_key,
    )
    if row is not None:
        transaction.on_commit(lambda row_id=row.id: deliver_now(row_id))


def _revised_plan_terms(plan: PortfolioPlan, constrained, sized, *, reason_code: str) -> dict:
    return {
        "reason_code": reason_code,
        "plan_id": str(plan.id),
        "plan_version": plan.plan_version,
        "target_weights": {
            str(ticker).upper(): decimal_string(weight)
            for ticker, weight in constrained.weights.items()
        },
        "legs": [
            {
                "sequence": leg.sequence,
                "ticker": leg.ticker,
                "side": leg.side,
                "quantity": str(leg.quantity),
                "reference_price": str(leg.price),
            }
            for leg in sized.legs
        ],
        "requires_new_approval": True,
        "message": "Revalidate the current terms and approve them before applying.",
    }


def _require_approval_reference(
    plan: PortfolioPlan,
    approval: ApprovalReference,
    *,
    now: datetime,
) -> None:
    """Refuse execution when the named approval does not cover this plan version."""
    if int(plan.plan_version) != int(approval.plan_version):
        raise PlanReapprovalRequired(
            (
                f"The approval names plan version {approval.plan_version}, but the plan is "
                f"now version {plan.plan_version}. A materially changed plan requires a new "
                "approval."
            ),
            code="approval_version_changed",
        )
    if plan.approval_hash != approval.approval_hash:
        raise PlanReapprovalRequired(
            (
                "The approval names a different permission hash than the current plan. "
                "Revalidate and approve the current plan before applying it."
            ),
            code="approval_binding_changed",
        )
    if int(plan.approval_version) != int(approval.approval_version):
        raise PlanReapprovalRequired(
            (
                f"The approval was recorded for version {approval.approval_version}, but the "
                f"plan's current approval is version {plan.approval_version}."
            ),
            code="approval_stale",
        )
    if plan.approval_expires_at is None:
        raise PlanReapprovalRequired(
            "The plan has no approval expiry; an unbounded approval is not executable.",
            code="approval_missing_expiry",
        )
    if plan.approval_expires_at <= now:
        raise PlanReapprovalRequired(
            (
                f"The approval expired at {utc_iso(plan.approval_expires_at)}. Approve the "
                "current plan version again before applying it."
            ),
            code="approval_expired",
        )
    if approval.expires_at != plan.approval_expires_at:
        raise PlanReapprovalRequired(
            "The supplied approval expiry does not match the recorded approval.",
            code="approval_expiry_changed",
        )
    if plan.revalidation_required:
        raise PlanReapprovalRequired(
            "The plan is marked for revalidation; it cannot execute under the old approval.",
            code="revalidation_required",
        )
    if not approval.approved_legs and plan.legs.exists():
        raise PlanReapprovalRequired(
            "This approval has no recorded side, quantity or reference-price terms. Its "
            "terms cannot be validated; approve the current plan version again.",
            code="approval_terms_unknown",
        )
    stored_legs = {int(leg.sequence): leg for leg in plan.legs.order_by("sequence")}
    approved_legs = {int(row.get("sequence") or 0): row for row in approval.approved_legs}
    if set(stored_legs) != set(approved_legs):
        raise PlanReapprovalRequired(
            "The approved terms name a different set of legs than the current plan. "
            "A new approval is required.",
            code="approval_terms_changed",
        )
    for sequence, leg in stored_legs.items():
        row = approved_legs[sequence]
        try:
            approved_quantity = Decimal(str(row.get("quantity")))
            approved_price = Decimal(str(row.get("reference_price")))
        except Exception:
            raise PlanReapprovalRequired(
                "The approval's leg terms are not decimal values; a new approval is required.",
                code="approval_terms_unknown",
            ) from None
        if (
            str(row.get("ticker") or "").upper() != leg.ticker.upper()
            or str(row.get("side") or "").upper() != leg.side.upper()
            or approved_quantity != leg.quantity
            or approved_price != leg.price
        ):
            raise PlanReapprovalRequired(
                (
                    f"Approved leg {sequence} is {row.get('side')} {row.get('quantity')} "
                    f"{row.get('ticker')}@{row.get('reference_price')}, but the plan now "
                    f"holds {leg.side} {leg.quantity} {leg.ticker}@{leg.price}. A new "
                    "approval is required."
                ),
                code="approval_terms_changed",
            )
    if approval.policy_version:
        from services.execution import portfolio_policy_version

        current_policy = portfolio_policy_version(plan.portfolio)
        if current_policy != approval.policy_version:
            raise PlanReapprovalRequired(
                "The portfolio policy changed after this plan was approved. A new "
                "approval is required for the current policy.",
                code="policy_changed",
            )


def apply_plan(
    plan_id,
    *,
    quotes: Mapping[str, Any] | None = None,
    mode: str | None = None,
    max_quote_age_seconds: int | None = None,
    policy: AllocationPolicy | None = None,
    cost_model: CostModel | None = None,
    approval: ApprovalReference | None = None,
) -> PlanApplication:
    """Apply a validated plan, atomically, with sells before buys.

    Under the lock the whole plan is re-projected against current marks and the
    current policy. Per-name, sector, total-exposure, turnover and cash-buffer
    limits are checked on the projected *final* state, each leg's funding is
    simulated, and the resulting legs must still be the approved decision. A
    material difference requires a new approval and writes no ledger row.

    ``quotes`` must cover every material holding and leg with a finite, positive,
    timestamped price. ``mode="paper"`` is the only mode allowed to fall back to
    the plan's own marks; that fallback is recorded on the plan and is never an
    implicit production default.
    """
    plan: PortfolioPlan | None = None
    applications: list[PlanApplication] = []
    attempt_id = uuid4()
    expected_plan_version: int | None = None
    expected_portfolio_version = ""
    # Record the attempt identity before the application transaction. A failure
    # handler runs after a rollback, so an identity written only inside that
    # transaction would vanish and the delayed handler would have nothing to
    # condition on.
    with transaction.atomic():
        try:
            pre = (
                PortfolioPlan.objects.select_for_update()
                .select_related("portfolio")
                .get(pk=plan_id)
            )
        except PortfolioPlan.DoesNotExist:
            raise PlanNotFoundError(f"Portfolio plan {plan_id} does not exist.") from None
        if pre.status == PlanStatus.APPLIED:
            existing_legs = list(pre.legs.order_by("sequence"))
            return PlanApplication(
                plan=pre,
                legs=existing_legs,
                transactions=[
                    leg.transaction for leg in existing_legs if leg.transaction_id is not None
                ],
                already_applied=True,
            )
        if pre.status == PlanStatus.NO_CHANGE:
            return PlanApplication(plan=pre, legs=[], transactions=[], already_applied=True)
        if pre.status != PlanStatus.PROPOSED:
            raise PlanNotApplicableError(f"Plan {pre.id} is {pre.status!r} and cannot be applied.")
        attempt_number = int(pre.execution_attempt_number or 0) + 1
        expected_plan_version = int(pre.plan_version)
        expected_portfolio_version = pre.evaluated_portfolio_version
        execution_mode_value = (mode or pre.execution_mode or EXECUTION_MODE_LIVE).lower()
        PortfolioPlan.objects.filter(pk=pre.id).update(
            execution_attempt_id=attempt_id,
            execution_attempt_number=attempt_number,
        )
    try:
        with transaction.atomic():
            try:
                plan = (
                    PortfolioPlan.objects.select_for_update()
                    .select_related("portfolio")
                    .get(pk=plan_id)
                )
            except PortfolioPlan.DoesNotExist:
                raise PlanNotFoundError(f"Portfolio plan {plan_id} does not exist.") from None

            if plan.status == PlanStatus.APPLIED:
                existing_legs = list(plan.legs.order_by("sequence"))
                return PlanApplication(
                    plan=plan,
                    legs=existing_legs,
                    transactions=[
                        leg.transaction for leg in existing_legs if leg.transaction_id is not None
                    ],
                    already_applied=True,
                )
            if plan.status == PlanStatus.NO_CHANGE:
                return PlanApplication(plan=plan, legs=[], transactions=[], already_applied=True)
            if plan.status != PlanStatus.PROPOSED:
                raise PlanNotApplicableError(
                    f"Plan {plan.id} is {plan.status!r} and cannot be applied."
                )

            if approval is not None:
                # Checked inside the same transaction and after the plan row is
                # locked, so a revalidation cannot change the version between the
                # caller's check and the first ledger write.
                _require_approval_reference(plan, approval, now=timezone.now())

            portfolio = Portfolio.objects.select_for_update().get(pk=plan.portfolio_id)
            current_version = portfolio_state_version(portfolio)
            if plan.revalidation_required:
                raise PlanStaleError(
                    expected=plan.evaluated_portfolio_version,
                    current=current_version,
                    detail=(
                        "The plan was built from a snapshot that no longer matches the "
                        "portfolio; revalidate it before applying."
                    ),
                )
            if current_version != plan.evaluated_portfolio_version:
                raise PlanStaleError(
                    expected=plan.evaluated_portfolio_version, current=current_version
                )

            execution_mode = (mode or plan.execution_mode or EXECUTION_MODE_LIVE).lower()
            policy = policy or AllocationPolicy.from_portfolio(portfolio)
            cost_model = cost_model or CostModel.from_config()
            legs = list(plan.legs.order_by("sequence"))
            now = timezone.now()
            max_age = (
                int(max_quote_age_seconds)
                if max_quote_age_seconds is not None
                else int(
                    getattr(
                        settings,
                        "EXECUTION_MAX_QUOTE_AGE_SECONDS",
                        policy.max_snapshot_age_seconds,
                    )
                )
            )
            if quotes is None:
                if execution_mode != EXECUTION_MODE_PAPER:
                    raise PlanQuoteUnavailable(
                        "Live plan application requires current quotes for every material "
                        "holding and leg. Planned marks are accepted only in explicit "
                        "paper mode."
                    )
                try:
                    paper_marks = _paper_marks(plan)
                except ValuationUnavailableError as exc:
                    raise PlanQuoteUnavailable(str(exc)) from exc
                quotes = {
                    # Explicit paper marks have no market observation instant.
                    # Recording "now" here would fabricate a fresh market quote.
                    ticker: Mark(price=price, as_of=None, source="paper_plan_mark")
                    for ticker, price in paper_marks.items()
                }
            if approval is not None and approval.max_quote_age_seconds is not None:
                max_age = min(max_age, int(approval.max_quote_age_seconds))
            try:
                marks = normalise_marks(quotes, now=now)
            except ValuationUnavailableError as exc:
                raise PlanLegFailed(
                    leg_sequence=0,
                    ticker="",
                    code=getattr(exc, "code", "price_unavailable"),
                    detail=str(exc),
                ) from exc

            exemptions = _require_marks(
                plan,
                legs,
                marks,
                max_quote_age_seconds=max_age,
                now=now,
                execution_mode=execution_mode,
            )

            material = _material_tickers(plan, legs)
            classifications, _permission = _check_permission_binding(
                plan,
                portfolio,
                policy=policy,
                cost_model=cost_model,
                material_tickers=material,
            )

            if not legs:
                # Validated as "nothing to do" against the *current* permissions.
                state = build_portfolio_state(portfolio, quotes=_mark_prices(marks))
                plan.status = PlanStatus.NO_CHANGE
                plan.applied_at = now
                plan.execution_mode = execution_mode
                plan.executed_target_weights = _weights_json(state)
                plan.execution_reasons = json_safe(
                    [{"reason": "No leg was required; no ledger row was written."}]
                )
                if approval is not None:
                    plan.applied_approval_version = int(approval.approval_version)
                plan.execution_attempt_id = attempt_id
                plan.execution_attempt_number = attempt_number
                plan.save(
                    update_fields=[
                        "status",
                        "applied_at",
                        "execution_mode",
                        "executed_target_weights",
                        "execution_reasons",
                        "applied_approval_version",
                        "execution_attempt_id",
                        "execution_attempt_number",
                        "updated_at",
                    ]
                )
                _record_plan_attempt(
                    plan=plan,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    status="no_change",
                    mode=execution_mode,
                    expected_plan_version=int(expected_plan_version or plan.plan_version),
                    expected_portfolio_version=expected_portfolio_version,
                    price_tolerance_pct=(
                        approval.price_tolerance_pct if approval is not None else None
                    ),
                    transaction_ids=[],
                    detail={"exemptions": exemptions, "no_leg": True},
                )
                _enqueue_plan_event(
                    plan,
                    EventType.PORTFOLIO_PLAN_APPLIED,
                    {
                        "plan_id": str(plan.id),
                        "status": plan.status,
                        "mode": execution_mode,
                        "already_applied": False,
                        "transaction_ids": [],
                        "no_change": True,
                    },
                    dedupe_key=f"plan-applied:{plan.id}:{plan.applied_approval_version}",
                )
                return PlanApplication(plan=plan, legs=[], transactions=[])

            prices = _mark_prices(marks)
            state = build_portfolio_state(portfolio, quotes=prices)
            if state.unpriced_tickers:
                raise PlanLegFailed(
                    leg_sequence=0,
                    ticker=",".join(state.unpriced_tickers),
                    code="valuation_unavailable",
                    detail=(
                        "The portfolio cannot be fully valued from current marks: "
                        + ", ".join(state.unpriced_tickers)
                    ),
                )

            constrained, sized = _project_plan(
                plan,
                portfolio,
                state,
                policy=policy,
                cost_model=cost_model,
                classifications=classifications,
            )
            _verify_projected_limits(constrained, sized, policy=policy)
            changes = _weight_differences(plan, constrained) + _leg_differences(plan, sized)
            if changes:
                raise PlanReapprovalRequired(
                    "The plan projected against current marks and policy no longer "
                    "matches the approved decision: " + "; ".join(changes[:6]),
                    code="plan_changed",
                    revised_terms=_revised_plan_terms(
                        plan, constrained, sized, reason_code="plan_changed"
                    ),
                )

            min_cash = (policy.min_cash_weight * (state.equity_usd or ZERO)).quantize(CENT)
            _verify_funding(
                sized.legs,
                starting_cash=state.cash_usd,
                min_cash=min_cash,
                policy=policy,
            )

            current_policy_version = portfolio_policy_version(portfolio)
            transactions: list[Transaction] = []
            reasons: list[dict] = []
            stored_legs = {leg.sequence: leg for leg in legs}
            approval_tolerance = approval.price_tolerance_pct if approval is not None else ZERO
            approval_quantity_tolerance = (
                approval.quantity_tolerance if approval is not None else Decimal("0.00000001")
            )
            for leg in sized.legs:
                reference = stored_legs[leg.sequence]
                traded_mark = marks.get(leg.ticker.upper())
                request = ExecutionRequest(
                    portfolio_id=portfolio.id,
                    ticker=leg.ticker,
                    side=leg.side,
                    quantity=leg.quantity,
                    quotes=marks,
                    requested_quantity=leg.quantity,
                    execution_key=f"plan:{plan.id}:leg:{leg.sequence}",
                    source="plan_leg",
                    approval_ref=f"plan:{plan.id}",
                    approved_policy_version=current_policy_version,
                    approved_price=reference.price,
                    approved_quantity=reference.quantity,
                    price_tolerance_pct=approval_tolerance,
                    quantity_tolerance=approval_quantity_tolerance,
                    quote_time=traded_mark.as_of if traded_mark is not None else None,
                    quote_source=traded_mark.source if traded_mark is not None else "",
                    quote_retrieved_at=(
                        traded_mark.retrieved_at if traded_mark is not None else None
                    ),
                    mode=execution_mode,
                    max_quote_age_seconds=max_age,
                    require_full_valuation=leg.side == PlanSide.BUY,
                )
                try:
                    execution = execute_trade(request)
                except MaterialChangeError as exc:
                    raise PlanLegFailed(
                        leg_sequence=leg.sequence,
                        ticker=leg.ticker,
                        code=getattr(exc, "code", "guard_clamped"),
                        detail=(
                            f"{exc}. The plan's funding arithmetic would no longer hold; "
                            "revalidate before applying."
                        ),
                    ) from exc
                except ValuationUnavailableError as exc:
                    raise PlanLegFailed(
                        leg_sequence=leg.sequence,
                        ticker=leg.ticker,
                        code=getattr(exc, "code", "price_unavailable"),
                        detail=str(exc),
                    ) from exc
                if not execution.approved or execution.transaction is None:
                    raise PlanLegFailed(
                        leg_sequence=leg.sequence,
                        ticker=leg.ticker,
                        code="guard_rejected",
                        detail=execution.reason,
                    )
                stored = stored_legs[leg.sequence]
                stored.transaction = execution.transaction
                stored.status = PlanLegStatus.APPLIED
                stored.applied_at = now
                stored.reason = execution.reason
                stored.save(
                    update_fields=[
                        "transaction",
                        "status",
                        "applied_at",
                        "reason",
                        "updated_at",
                    ]
                )
                transactions.append(execution.transaction)
                reasons.append(
                    {
                        "sequence": leg.sequence,
                        "ticker": leg.ticker,
                        "side": leg.side,
                        "requested_quantity": str(leg.quantity),
                        "executed_quantity": str(execution.decision.amount),
                        "price": str(execution.transaction.price),
                        "reason": execution.reason,
                        "transaction_id": execution.transaction.id,
                    }
                )

            state_after = build_portfolio_state(portfolio, quotes=prices)
            plan.status = PlanStatus.APPLIED
            plan.applied_at = now
            plan.execution_mode = execution_mode
            plan.failure_code = ""
            plan.failure_detail = ""
            plan.executed_target_weights = _weights_json(state_after)
            plan.execution_reasons = json_safe(
                [
                    *reasons,
                    *([{"risk_reducing_exception": exemptions}] if exemptions else []),
                ]
            )
            plan.execution_attempt_id = attempt_id
            plan.execution_attempt_number = attempt_number
            if approval is not None:
                plan.applied_approval_version = int(approval.approval_version)
            plan.save(
                update_fields=[
                    "status",
                    "applied_at",
                    "execution_mode",
                    "failure_code",
                    "failure_detail",
                    "executed_target_weights",
                    "execution_reasons",
                    "applied_approval_version",
                    "execution_attempt_id",
                    "execution_attempt_number",
                    "updated_at",
                ]
            )
            _record_plan_attempt(
                plan=plan,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                status="applied",
                mode=execution_mode,
                expected_plan_version=int(expected_plan_version or plan.plan_version),
                expected_portfolio_version=expected_portfolio_version,
                price_tolerance_pct=approval_tolerance,
                transaction_ids=[row.id for row in transactions],
                detail={"exemptions": exemptions},
            )
            _enqueue_plan_event(
                plan,
                EventType.PORTFOLIO_PLAN_APPLIED,
                {
                    "plan_id": str(plan.id),
                    "status": plan.status,
                    "mode": execution_mode,
                    "already_applied": False,
                    "transaction_ids": [row.id for row in transactions],
                    "applied_approval_version": plan.applied_approval_version,
                    "execution_attempt_id": str(attempt_id),
                },
                dedupe_key=f"plan-applied:{plan.id}:{plan.applied_approval_version}",
            )
            applications.append(PlanApplication(plan=plan, legs=legs, transactions=transactions))
    except PlanReapprovalRequired as exc:
        _mark_plan_for_reapproval(
            plan_id,
            str(exc),
            attempt_id=attempt_id,
            expected_plan_version=expected_plan_version,
        )
        raise
    except PlanStaleError:
        PortfolioPlan.objects.filter(
            pk=plan_id,
            status=PlanStatus.PROPOSED,
            execution_attempt_id=attempt_id,
        ).update(revalidation_required=True)
        raise
    except PlanLegFailed as failure:
        _record_plan_failure(
            plan_id,
            failure,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            mode=execution_mode_value,
            expected_plan_version=expected_plan_version,
            expected_portfolio_version=expected_portfolio_version,
        )
        raise
    except (PlanNotFoundError, PlanNotApplicableError):
        raise
    except Exception as exc:
        wrapped = PlanLegFailed(
            leg_sequence=0,
            ticker="",
            code="execution_failed",
            detail=f"Execution failed and was rolled back: {exc}",
        )
        _record_plan_failure(
            plan_id,
            wrapped,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            mode=execution_mode_value,
            expected_plan_version=expected_plan_version,
            expected_portfolio_version=expected_portfolio_version,
        )
        raise wrapped from exc

    application = applications[0]
    _capture_after_plan(portfolio, _mark_prices(marks))
    return application


def _weights_json(state: PortfolioState) -> dict:
    return {
        ticker: decimal_string(weight.quantize(WEIGHT_QUANTUM))
        for ticker, weight in sorted(state.weights.items())
    }


def _paper_marks(plan: PortfolioPlan) -> dict[str, Decimal]:
    """The plan's own prices, for explicit paper mode only."""
    marks = {leg.ticker.upper(): leg.price for leg in plan.legs.all()}
    for holding in (plan.brief or {}).get("holdings") or []:
        ticker = str(holding.get("ticker") or "").upper()
        price = holding.get("price")
        if ticker and price is not None and ticker not in marks:
            try:
                parsed = Decimal(str(price))
            except Exception:
                logger.debug("Ignoring non-decimal planned mark for %s: %r", ticker, price)
                continue
            if parsed > ZERO:
                marks[ticker] = parsed
    missing = set()
    for ticker in _material_tickers(plan, list(plan.legs.all())):
        if ticker not in marks:
            missing.add(ticker)
    if missing:
        raise ValuationUnavailableError(
            "Paper mode still needs a planned mark for every holding: " + ", ".join(sorted(missing))
        )
    return marks


def revalidate_plan(
    plan_id,
    *,
    quotes: Mapping[str, Any] | None = None,
    policy: AllocationPolicy | None = None,
    cost_model: CostModel | None = None,
    mode: str | None = None,
    max_quote_age_seconds: int | None = None,
) -> PortfolioPlan:
    """Re-derive a plan's legs against the current portfolio state.

    Revalidation is the explicit act that replaces the plan's approval with a new
    one: the requested weights are projected again against current holdings,
    marks, classifications, cost assumptions and limits, and the binding hash is
    refreshed. The previous binding is recorded in the deviations so a reviewer
    can see that the permission set changed.
    """
    with transaction.atomic():
        try:
            plan = (
                PortfolioPlan.objects.select_for_update()
                .select_related("portfolio")
                .get(pk=plan_id)
            )
        except PortfolioPlan.DoesNotExist:
            raise PlanNotFoundError(f"Portfolio plan {plan_id} does not exist.") from None
        if plan.status in {
            PlanStatus.APPLIED,
            PlanStatus.NO_CHANGE,
            PlanStatus.CANCELLED,
            PlanStatus.SUPERSEDED,
        }:
            raise PlanNotApplicableError(
                f"Plan {plan.id} is {plan.status!r}; it cannot be revalidated."
            )
        portfolio = Portfolio.objects.select_for_update().get(pk=plan.portfolio_id)
        policy = policy or AllocationPolicy.from_portfolio(portfolio)
        cost_model = cost_model or CostModel.from_config()
        execution_mode = (mode or plan.execution_mode or EXECUTION_MODE_LIVE).lower()
        now = timezone.now()
        if quotes is None:
            if execution_mode != EXECUTION_MODE_PAPER:
                raise PlanQuoteUnavailable(
                    "Live revalidation requires current quotes; planned marks are paper-only."
                )
            try:
                paper_marks = _paper_marks(plan)
            except ValuationUnavailableError as exc:
                raise PlanQuoteUnavailable(str(exc)) from exc
            quotes = {
                ticker: Mark(price=price, as_of=None, source="paper_plan_mark")
                for ticker, price in paper_marks.items()
            }
        try:
            marks = normalise_marks(quotes, now=now)
        except ValuationUnavailableError as exc:
            raise PlanQuoteUnavailable(str(exc)) from exc
        legs = list(plan.legs.order_by("sequence"))
        from services.quote_policy import ExecutionFreshnessPolicy, validate_mark

        freshness = ExecutionFreshnessPolicy.from_settings()
        paper_mode = execution_mode == EXECUTION_MODE_PAPER
        for ticker in _material_tickers(plan, legs):
            mark = marks.get(ticker)
            if mark is None:
                raise PlanQuoteUnavailable(
                    f"No current quote for {ticker}; revalidation needs a full valuation."
                )
            result = validate_mark(
                ticker=ticker,
                price=mark.price,
                as_of=mark.as_of,
                retrieved_at=mark.retrieved_at,
                source=mark.source,
                now=now,
                mode=execution_mode,
                require_current=not paper_mode,
                policy=freshness,
            )
            if not result.ok:
                raise PlanQuoteUnavailable(result.detail)
        state = build_portfolio_state(portfolio, quotes=_mark_prices(marks))
        if state.equity_usd is None or state.equity_usd <= ZERO:
            raise BriefIncompleteError(
                "Cannot revalidate: the current portfolio has no usable equity."
            )
        material = _material_tickers(plan, legs)
        classifications = _effective_classifications(plan, portfolio, material)
        constrained, sized = _project_plan(
            plan,
            portfolio,
            state,
            policy=policy,
            cost_model=cost_model,
            classifications=classifications,
        )
        binding = _plan_binding(
            policy=policy,
            classifications=classifications,
            cost_model=cost_model,
            requested=plan.requested_target_weights or {},
            target_weights=constrained.weights_json,
            legs=sized.legs,
            marks=_mark_prices(marks),
            execution_mode=execution_mode,
            constraints=constrained.constraints,
            post_trade=sized.post_trade,
        )
        deviations = list(constrained.deviations) + list(sized.deviations)
        deviations.append(
            {
                "code": "revalidated",
                "detail": (
                    "The portfolio, marks or permissions changed after this plan was "
                    "proposed. The requested weights were projected against the current "
                    "holdings, cash, limits and classifications; the legs below replace "
                    "the previous legs and are a new approval basis."
                ),
            }
        )
        had_approval = plan.approval_version > 0
        if had_approval:
            deviations.append(
                {
                    "code": "approval_invalidated",
                    "detail": (
                        f"The previous approval covered plan version {plan.plan_version}; "
                        "this revalidation changes the material decision and requires a new "
                        "approval."
                    ),
                }
            )
        plan.legs.all().delete()
        target_weights = constrained.weights_json
        for leg in sized.legs:
            PortfolioPlanLeg.objects.create(
                plan=plan,
                sequence=leg.sequence,
                ticker=leg.ticker,
                side=leg.side,
                quantity=leg.quantity,
                price=leg.price,
                notional_usd=leg.notional_usd,
                estimated_cost_usd=leg.estimated_cost_usd,
                priority=leg.priority,
                sector=leg.sector,
                asset_class=leg.asset_class,
                status=PlanLegStatus.PLANNED,
                reason=leg.reason,
            )
        plan.status = PlanStatus.PROPOSED
        plan.target_weights = target_weights
        plan.actions = json_safe(
            [
                {**row, "applied_target_weight": target_weights.get(row.get("ticker"), "0")}
                for row in (plan.actions or [])
            ]
        )
        plan.evaluated_portfolio_version = portfolio_state_version(portfolio)
        plan.revalidation_required = False
        plan.revalidation_count += 1
        plan.revalidated_at = now
        plan.plan_version = int(plan.plan_version or 1) + 1
        # A materially changed plan cannot inherit the previous permission.
        plan.approval_version = 0
        plan.approved_by = None
        plan.approved_at = None
        plan.approval_expires_at = None
        plan.applied_approval_version = 0
        plan.execution_mode = execution_mode
        plan.deviations = json_safe(deviations)
        plan.constraints = json_safe(constrained.constraints)
        plan.funding = json_safe(sized.funding)
        plan.approval_binding = json_safe(binding)
        plan.approval_hash = content_hash(binding)
        plan.executed_target_weights = {}
        plan.execution_reasons = []
        plan.equity_usd = state.equity_usd
        plan.cash_usd = state.cash_usd
        plan.min_cash_usd = (policy.min_cash_weight * state.equity_usd).quantize(CENT)
        plan.estimated_cost_usd = sized.estimated_cost_usd
        plan.estimated_turnover_pct = sized.estimated_turnover_pct
        plan.failure_code = ""
        plan.failure_detail = ""
        plan.quote_observations = json_safe(
            {
                ticker: {
                    "price": str(mark.price),
                    "source": mark.source,
                    "as_of": mark.as_of.isoformat() if mark.as_of else None,
                    "retrieved_at": mark.retrieved_at.isoformat() if mark.retrieved_at else None,
                    "currency": mark.currency,
                }
                for ticker, mark in marks.items()
            }
        )
        plan.execution_attempt_id = None
        plan.save()
    return plan


# ---------------------------------------------------------------------------
# Hold proposal
# ---------------------------------------------------------------------------
def build_hold_proposal(brief: PortfolioBrief, reason: str) -> AllocationProposal:
    """A valid no-change proposal: keep current weights and cash, trade nothing."""
    target: dict[str, Decimal] = {}
    risky_total = ZERO
    for ticker, weight in brief.state.weights.items():
        if ticker == "CASH":
            continue
        value = weight.quantize(WEIGHT_QUANTUM, rounding=ROUND_DOWN)
        target[ticker] = value
        risky_total += value
    target["CASH"] = ONE - risky_total
    if target["CASH"] < ZERO:
        raise BriefIncompleteError("Current weights do not sum to one; cannot build a hold.")
    actions = []
    for ticker in sorted(target):
        if ticker == "CASH":
            actions.append(TargetAction(ticker="CASH", action="maintain_cash", rationale=reason))
        else:
            actions.append(
                TargetAction(
                    ticker=ticker,
                    action="maintain",
                    rationale=reason,
                    target_weight=target[ticker],
                )
            )
    return AllocationProposal(
        schema_version=TARGET_SCHEMA_VERSION,
        portfolio_version=brief.portfolio_version,
        target_weights=target,
        actions=actions,
        funding_source="none",
        rationale=reason,
        rejected_alternatives=[],
        unresolved_uncertainties=[reason],
        data_sufficiency="insufficient",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
@dataclass
class AllocationRun:
    plan: PortfolioPlan
    created: bool
    brief: PortfolioBrief | None = None
    proposal: AllocationProposal | None = None


def run_allocation(
    portfolio: Portfolio,
    *,
    client: AllocationClient,
    quotes: Mapping[str, Any],
    price_history: Mapping[str, Any] | None = None,
    candidates: Sequence[CandidateCase] | None = None,
    classifications: Mapping[str, dict] | None = None,
    submission_key: str = "",
    policy: AllocationPolicy | None = None,
    cost_model: CostModel | None = None,
    as_of: datetime | None = None,
    horizon_days: int | None = None,
    evidence_sufficient: bool | None = None,
    execution_mode: str = EXECUTION_MODE_LIVE,
) -> AllocationRun:
    """Run the whole pipeline and create a plan, with no automatic execution."""
    policy = policy or AllocationPolicy.from_portfolio(portfolio)
    cost_model = cost_model or CostModel.from_config()
    existing = _existing_plan(portfolio, submission_key)
    if existing is not None:
        # The proposal body does not exist yet at this point (it is produced by
        # the model below). ``create_plan`` is where a conflicting reuse of a key
        # is detected and rejected; this early return avoids paying for a model
        # call that would only be discarded.
        return AllocationRun(plan=existing, created=False)
    snapshot = capture_allocation_snapshot(portfolio, quotes=quotes, as_of=as_of)
    brief = build_allocation_brief(
        portfolio,
        snapshot,
        quotes=quotes,
        price_history=price_history,
        candidates=candidates,
        classifications=classifications,
        policy=policy,
        cost_model=cost_model,
        as_of=as_of,
        horizon_days=horizon_days,
        evidence_sufficient=evidence_sufficient,
    )
    proposal = PortfolioCIO(client=client, policy=policy).decide(brief)
    plan, created = create_plan(
        portfolio,
        brief=brief,
        proposal=proposal,
        submission_key=submission_key,
        policy=policy,
        cost_model=cost_model,
        execution_mode=execution_mode,
    )
    return AllocationRun(plan=plan, created=created, brief=brief, proposal=proposal)
