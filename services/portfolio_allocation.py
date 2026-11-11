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

1. **The guard remains the only route to the ledger.** Every leg is revalidated
   through :func:`services.execution.evaluate_proposal` and written with
   :func:`services.execution.apply_trade`. The portfolio allocation layer adds
   constraints; it does not replace the guard.
2. **A plan is applied against the portfolio version it was sized against.**
   If the portfolio changed, ``apply_plan`` refuses and sets
   ``revalidation_required``; ``revalidate_plan`` re-derives the legs from the
   stored requested weights against the new state. A duplicate apply returns the
   existing result and never adds a second ledger row.

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

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.allocation_models import (
    PlanLegStatus,
    PlanStatus,
    PortfolioPlan,
    PortfolioPlanLeg,
)
from core.decision_json import content_hash, json_safe, utc_iso
from core.models import Asset, Portfolio, Transaction
from services.execution import Exposure, apply_trade, evaluate_proposal
from services.ledger import PositionState, realised_pnl_today
from services.market_data import PriceQuote
from services.portfolio_brief import (
    CandidateCase,
    PortfolioBrief,
    PortfolioState,
    build_allocation_brief,
    build_portfolio_state,
    capture_allocation_snapshot,
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
    "PROMPT_TEMPLATE_VERSION",
    "PROTOCOL_VERSION",
    "AllocationClient",
    "AllocationError",
    "AllocationRun",
    "BriefIncompleteError",
    "ModelCall",
    "PlanApplication",
    "PlanError",
    "PlanLegFailed",
    "PlanNotApplicableError",
    "PlanStaleError",
    "PortfolioCIO",
    "StaticAllocationClient",
    "apply_plan",
    "build_allocation_prompt",
    "build_hold_proposal",
    "create_plan",
    "plan_as_dict",
    "revalidate_plan",
    "run_allocation",
]

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
            cost_reported = bool(getattr(usage, "reported", False))
            method = "provider_reported" if cost_reported else "estimated_chars_per_4_tokens"
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
            "note": (
                "The brief and this response are recorded verbatim in the plan. Replaying "
                "the arithmetic is deterministic; regenerating model output is not."
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
) -> tuple[PortfolioPlan, bool]:
    """Validate the deterministic projection and persist a plan and its legs.

    Returns ``(plan, created)``. A repeated ``submission_key`` returns the first
    plan and does not create a second one, even if the request body differs.
    """
    existing = _existing_plan(portfolio, submission_key)
    if existing is not None:
        return existing, False
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

    try:
        with transaction.atomic():
            locked = Portfolio.objects.select_for_update().get(pk=portfolio.id)
            evaluated_version = portfolio_state_version(locked)
            payload = brief.as_dict()
            plan = PortfolioPlan.objects.create(
                portfolio=locked,
                snapshot_id=brief.snapshot_id,
                submission_key=(submission_key or "").strip(),
                status=PlanStatus.PROPOSED,
                protocol_version=PROTOCOL_VERSION,
                policy_version=policy.policy_version,
                proposal_portfolio_version=proposal.portfolio_version,
                evaluated_portfolio_version=evaluated_version,
                investment_horizon_days=brief.investment_horizon_days,
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
                model_metadata=json_safe(proposal.model_metadata),
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
            return existing, False
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
        "status": plan.status,
        "protocol_version": plan.protocol_version,
        "policy_version": plan.policy_version,
        "proposal_portfolio_version": plan.proposal_portfolio_version,
        "evaluated_portfolio_version": plan.evaluated_portfolio_version,
        "investment_horizon_days": plan.investment_horizon_days,
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
        "revalidation_required": plan.revalidation_required,
        "revalidation_count": plan.revalidation_count,
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
class _Proposal:
    """The two attributes ``evaluate_proposal`` reads."""

    def __init__(self, action: str, amount: Decimal) -> None:
        self.action = action
        self.amount = amount


@dataclass
class PlanApplication:
    plan: PortfolioPlan
    legs: list[PortfolioPlanLeg]
    transactions: list[Transaction]
    already_applied: bool = False


def _parse_quote_map(quotes: Mapping[str, Any] | None) -> dict[str, Decimal]:
    parsed: dict[str, Decimal] = {}
    for ticker, value in (quotes or {}).items():
        symbol = str(ticker).strip().upper()
        raw = getattr(value, "price", value)
        try:
            price = Decimal(str(raw))
        except Exception as exc:
            logger.debug("Ignoring invalid quote for %s: %r (%s)", ticker, raw, exc)
            continue
        if symbol and price > ZERO:
            parsed[symbol] = price.quantize(CENT)
    return parsed


def _exposure_for(portfolio: Portfolio, price_map: Mapping[str, Decimal], ticker: str) -> Exposure:
    positions_value = ZERO
    ticker_value = ZERO
    wanted = ticker.upper()
    for asset in portfolio.assets.all():
        symbol = asset.ticker.upper()
        price = price_map.get(symbol) or asset.avg_purchase_price
        value = (asset.amount * price).quantize(CENT)
        positions_value += value
        if symbol == wanted:
            ticker_value = value
    equity = (portfolio.balance_usd + positions_value).quantize(CENT)
    return Exposure(
        positions_value_usd=positions_value.quantize(CENT),
        ticker_value_usd=ticker_value.quantize(CENT),
        equity_usd=equity,
    )


def _record_plan_failure(plan_id, failure: PlanLegFailed) -> None:
    """Persist a failure after the application transaction rolled back."""
    try:
        PortfolioPlan.objects.filter(pk=plan_id).update(
            status=PlanStatus.FAILED,
            failure_code=failure.code,
            failure_detail=failure.detail,
            revalidation_required=False,
        )
        if failure.leg_sequence > 0:
            PortfolioPlanLeg.objects.filter(plan_id=plan_id, sequence=failure.leg_sequence).update(
                status=PlanLegStatus.FAILED, reason=failure.detail
            )
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
    except Exception:  # pragma: no cover - failure recording must never mask the cause
        logger.exception("Could not record allocation plan failure for %s", plan_id)


def _capture_after_plan(portfolio: Portfolio, quotes: Mapping[str, Decimal]) -> None:
    """Record the post-trade equity point; never let a snapshot failure undo a fill."""
    try:
        from services.snapshots import capture_snapshot

        price_map: dict[str, PriceQuote] = {}
        for asset in portfolio.assets.all():
            symbol = asset.ticker.upper()
            price = quotes.get(symbol) or asset.avg_purchase_price
            if price is not None and price > ZERO:
                price_map[symbol] = PriceQuote(
                    ticker=symbol, symbol=symbol, price=price, source="allocation_apply"
                )
        capture_snapshot(portfolio, price_map)
    except Exception as exc:  # pragma: no cover - operational safety net
        logger.warning("Post-plan snapshot failed for %s: %s", portfolio.id, exc)


def apply_plan(plan_id, *, quotes: Mapping[str, Any] | None = None) -> PlanApplication:
    """Apply a validated plan, atomically, with sells before buys.

    Every leg is revalidated through the execution guard inside the same database
    transaction. If any leg fails, the transaction rolls back, the plan is marked
    failed, and no later leg is attempted. A repeated call on an applied plan
    returns the existing result and writes no ledger row.
    """
    plan: PortfolioPlan | None = None
    applications: list[PlanApplication] = []
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

            legs = list(plan.legs.order_by("sequence"))
            if not legs:
                plan.status = PlanStatus.NO_CHANGE
                plan.applied_at = timezone.now()
                plan.save(update_fields=["status", "applied_at", "updated_at"])
                return PlanApplication(plan=plan, legs=[], transactions=[])

            quote_map = _parse_quote_map(quotes)
            if quotes is not None:
                missing = sorted({leg.ticker for leg in legs} - set(quote_map))
                if missing:
                    first_missing = next(leg for leg in legs if leg.ticker in missing)
                    raise PlanLegFailed(
                        leg_sequence=first_missing.sequence,
                        ticker=first_missing.ticker,
                        code="price_unavailable",
                        detail=(
                            "No current quote was supplied for "
                            f"{', '.join(missing)}; refusing to size a fill from a stale "
                            "mark."
                        ),
                    )
            else:
                # Offline/paper path: the plan's own marks are the best available
                # prices. A live caller should pass current quotes.
                quote_map = {leg.ticker: leg.price.quantize(CENT) for leg in legs}

            transactions: list[Transaction] = []
            for leg in legs:
                price = quote_map.get(leg.ticker)
                if price is None or price <= ZERO:
                    raise PlanLegFailed(
                        leg_sequence=leg.sequence,
                        ticker=leg.ticker,
                        code="price_unavailable",
                        detail=f"No usable price for {leg.ticker}.",
                    )
                portfolio.refresh_from_db()
                asset = Asset.objects.filter(portfolio=portfolio, ticker=leg.ticker).first()
                position = PositionState(
                    ticker=leg.ticker, amount=asset.amount if asset is not None else ZERO
                )
                exposure = _exposure_for(portfolio, quote_map, leg.ticker)
                decision = evaluate_proposal(
                    portfolio,
                    _Proposal(leg.side, leg.quantity),
                    price,
                    position,
                    realised_pnl_today(portfolio),
                    exposure=exposure,
                )
                if not decision.approved:
                    raise PlanLegFailed(
                        leg_sequence=leg.sequence,
                        ticker=leg.ticker,
                        code="guard_rejected",
                        detail=decision.reason,
                    )
                if decision.amount != leg.quantity:
                    raise PlanLegFailed(
                        leg_sequence=leg.sequence,
                        ticker=leg.ticker,
                        code="guard_clamped",
                        detail=(
                            f"The guard allowed {decision.amount} of the planned "
                            f"{leg.quantity} {leg.ticker}; the plan's funding arithmetic "
                            "would no longer hold. Revalidate before applying."
                        ),
                    )
                row = apply_trade(portfolio.id, leg.ticker, decision)
                leg.transaction = row
                leg.status = PlanLegStatus.APPLIED
                leg.applied_at = timezone.now()
                leg.reason = decision.reason
                leg.save(
                    update_fields=[
                        "transaction",
                        "status",
                        "applied_at",
                        "reason",
                        "updated_at",
                    ]
                )
                transactions.append(row)

            portfolio.refresh_from_db()
            plan.status = PlanStatus.APPLIED
            plan.applied_at = timezone.now()
            plan.failure_code = ""
            plan.failure_detail = ""
            plan.save(
                update_fields=[
                    "status",
                    "applied_at",
                    "failure_code",
                    "failure_detail",
                    "updated_at",
                ]
            )
            applications.append(PlanApplication(plan=plan, legs=legs, transactions=transactions))
    except PlanStaleError:
        PortfolioPlan.objects.filter(pk=plan_id).update(revalidation_required=True)
        raise
    except PlanLegFailed as failure:
        _record_plan_failure(plan_id, failure)
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
        _record_plan_failure(plan_id, wrapped)
        raise wrapped from exc

    application = applications[0]
    _capture_after_plan(portfolio, quote_map)
    return application


def revalidate_plan(
    plan_id,
    *,
    quotes: Mapping[str, Any] | None = None,
    policy: AllocationPolicy | None = None,
    cost_model: CostModel | None = None,
) -> PortfolioPlan:
    """Re-derive a plan's legs against the current portfolio state.

    Called after ``apply_plan`` reports a changed portfolio version. The stored
    *requested* weights are projected again against current weights (so turnover
    and cash are recomputed), then the legs and their prices are rebuilt. The
    original snapshot remains the proposal's provenance; the evaluated version
    moves to the state the plan will actually run against.
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

        if quotes is None:
            # Use the plan's own marks for the names it trades; other holdings
            # fall back to cost, which the brief labels as an approximation.
            quotes = {leg.ticker: leg.price for leg in plan.legs.all()}
        state = build_portfolio_state(portfolio, quotes=quotes)
        if state.equity_usd is None or state.equity_usd <= ZERO:
            raise BriefIncompleteError(
                "Cannot revalidate: the current portfolio has no usable equity."
            )
        classifications = {
            ticker: info
            for ticker, info in (plan.brief.get("classifications") or {}).items()
            if info
        }
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
        deviations = list(constrained.deviations) + list(sized.deviations)
        deviations.append(
            {
                "code": "revalidated",
                "detail": (
                    "The portfolio version changed after this plan was proposed. The "
                    "requested weights were projected against the current holdings, cash "
                    "and limits; the legs below replace the previous legs."
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
        plan.revalidated_at = timezone.now()
        plan.deviations = json_safe(deviations)
        plan.constraints = json_safe(constrained.constraints)
        plan.funding = json_safe(sized.funding)
        plan.equity_usd = state.equity_usd
        plan.cash_usd = state.cash_usd
        plan.min_cash_usd = (policy.min_cash_weight * state.equity_usd).quantize(CENT)
        plan.estimated_cost_usd = sized.estimated_cost_usd
        plan.estimated_turnover_pct = sized.estimated_turnover_pct
        plan.failure_code = ""
        plan.failure_detail = ""
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
) -> AllocationRun:
    """Run the whole pipeline and create a plan, with no automatic execution."""
    policy = policy or AllocationPolicy.from_portfolio(portfolio)
    cost_model = cost_model or CostModel.from_config()
    existing = _existing_plan(portfolio, submission_key)
    if existing is not None:
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
    )
    return AllocationRun(plan=plan, created=created, brief=brief, proposal=proposal)
