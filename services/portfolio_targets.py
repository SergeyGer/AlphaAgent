"""Target-weight contract, constrained projection and deterministic sizing (PORT-03/04).

The model proposes weights. This module decides what the portfolio can actually
support, then turns that into quantities. Two properties matter more than the
specific arithmetic:

* **The model is never the optimizer.** The proposal is a *request*. A
  deterministic projection applies per-name, sector, total-exposure, turnover
  and cash-buffer limits, and every difference between the request and the
  constrained target is recorded as a deviation. A deficit goes to cash; it is
  never silently redistributed to another name.
* **The executable plan is derived, not trusted.** Quantities round **down** to
  the lot size, costs are included in funding, and the per-trade guard ceiling is
  applied at plan time. If rounding or costs make a target infeasible, the plan
  says so and leaves cash rather than scaling the position up past a limit.

The contract is long-only, unlevered and non-negative, with weights including
``CASH`` that sum to one within ``0.000001``. The request is normalised to
exactly one at six decimals (the residual is assigned to cash) so the stored
plan cannot drift on serialisation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import Any

from core.decision_json import content_hash, decimal_string
from services.portfolio_brief import PortfolioBrief, PortfolioState
from services.portfolio_policy import (
    ACTION_BLOCK_NEW_RISK,
    ACTION_RETAIN_CASH,
    CENT,
    ONE,
    WEIGHT_QUANTUM,
    ZERO,
    AllocationPolicy,
    CostModel,
)

__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_FUNDING_SOURCES",
    "TARGET_SCHEMA_VERSION",
    "WEIGHT_TOLERANCE",
    "AllocationContractError",
    "AllocationProposal",
    "ConstraintResult",
    "LegDraft",
    "LegResult",
    "ProposalStaleError",
    "TargetAction",
    "constrain_targets",
    "derive_legs",
    "validate_target_proposal",
]

TARGET_SCHEMA_VERSION = "portfolio_targets.v1"
PROJECTION_METHOD_VERSION = "deterministic_constrained_projection.v1"
#: PORT-03 tolerance. The request is validated against it and then normalised to
#: exactly one at six decimals.
WEIGHT_TOLERANCE = Decimal("0.000001")
WEIGHT_DECIMALS = WEIGHT_QUANTUM
ALLOWED_ACTIONS = frozenset(
    {"increase", "reduce", "maintain", "exit", "no_change", "maintain_cash"}
)
ALLOWED_FUNDING_SOURCES = frozenset({"cash", "sales", "cash_and_sales", "none", "mixed"})
FORBIDDEN_KEYS = frozenset(
    {
        "expected_returns",
        "expected_return",
        "expected_return_pct",
        "forecast_return",
        "forecast_returns",
        "correlations",
        "covariance",
        "risk_statistics",
        "volatility",
    }
)
MAX_PROJECTION_ITERATIONS = 12


class AllocationContractError(ValueError):
    """The model's target-weight payload is not a valid PORT-03 contract."""

    def __init__(self, code: str, detail: str, *, repairable: bool = True) -> None:
        self.code = code
        self.detail = detail
        self.repairable = repairable
        super().__init__(f"{code}: {detail}")


class ProposalStaleError(AllocationContractError):
    """The proposal was made against a different portfolio version."""

    def __init__(self, detail: str) -> None:
        super().__init__("portfolio_version_mismatch", detail, repairable=False)


@dataclass(frozen=True)
class TargetAction:
    ticker: str
    action: str
    rationale: str
    claim_ids: list[str] = field(default_factory=list)
    target_weight: Decimal = ZERO

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "action": self.action,
            "target_weight": str(self.target_weight),
            "rationale": self.rationale,
            "claim_ids": list(self.claim_ids),
        }


@dataclass
class AllocationProposal:
    """A validated PORT-03 proposal, before deterministic projection."""

    schema_version: str
    portfolio_version: str
    target_weights: dict[str, Decimal]
    actions: list[TargetAction]
    funding_source: str
    rationale: str
    rejected_alternatives: list[dict]
    unresolved_uncertainties: list[str]
    objective: str = ""
    data_sufficiency: str = ""
    model_metadata: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def cash_weight(self) -> Decimal:
        return self.target_weights.get("CASH", ZERO)

    def target_weights_json(self) -> dict[str, str]:
        return {
            ticker: decimal_string(weight) for ticker, weight in sorted(self.target_weights.items())
        }

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "portfolio_version": self.portfolio_version,
            "objective": self.objective,
            "target_weights": self.target_weights_json(),
            "weights_sum": str(sum(self.target_weights.values(), ZERO)),
            "actions": [action.as_dict() for action in self.actions],
            "funding_source": self.funding_source,
            "rationale": self.rationale,
            "rejected_alternatives": list(self.rejected_alternatives),
            "unresolved_uncertainties": list(self.unresolved_uncertainties),
            "data_sufficiency": self.data_sufficiency or None,
        }


def _reject_forbidden_keys(payload: Mapping[str, Any], *, where: str = "response") -> None:
    present = sorted(FORBIDDEN_KEYS & {str(key).lower() for key in payload})
    if present:
        raise AllocationContractError(
            "invented_risk_statistics",
            f"{where} contains {present}. Expected returns, correlations and risk "
            "statistics are supplied deterministically in the brief; the model must "
            "not invent them.",
        )


def _weight_value(value: Any, *, ticker: str) -> Decimal:
    if isinstance(value, (bool, float)):
        # A float target is a DATA-02 violation: it cannot represent a weight
        # exactly, and 0.1 + 0.2 != 0.3 in binary floating point.
        raise AllocationContractError(
            "decimal_string_required",
            f"target_weights[{ticker!r}] must be a decimal string, not {type(value).__name__}.",
        )
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        raise AllocationContractError(
            "invalid_weight", f"target_weights[{ticker!r}] is not a decimal: {value!r}."
        ) from None
    if parsed.is_nan() or parsed.is_infinite():
        raise AllocationContractError(
            "invalid_weight", f"target_weights[{ticker!r}] is not finite."
        )
    return parsed


def _normalise_exact(weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Quantise risky weights down to six decimals and make CASH the residual.

    Rounding risky weights down can only increase cash, so a non-negative cash
    request stays non-negative and the sum is exact by construction.
    """
    normalised: dict[str, Decimal] = {}
    risky_total = ZERO
    for ticker, weight in weights.items():
        if ticker == "CASH":
            continue
        # Keep zero-weight holdings: a held name with a zero target is an exit,
        # and dropping it here would silently remove the sell leg.
        value = weight.quantize(WEIGHT_DECIMALS, rounding=ROUND_DOWN)
        normalised[ticker] = value
        risky_total += value
    cash = ONE - risky_total
    if cash < ZERO:
        raise AllocationContractError(
            "weights_do_not_sum_to_one",
            f"Quantised risky weights total {risky_total}, which exceeds 1.0.",
            repairable=False,
        )
    normalised["CASH"] = cash
    return normalised


def validate_target_proposal(
    payload: Any,
    brief: PortfolioBrief,
    *,
    policy: AllocationPolicy | None = None,
) -> AllocationProposal:
    """Validate one model response against PORT-03 and the brief's universe.

    Every rejection is typed and names the rule. The caller may spend one repair
    attempt on a repairable error; ``portfolio_version_mismatch`` and the
    arithmetic invariants are not repairable by re-prompting the same state.
    """
    if not isinstance(payload, Mapping):
        raise AllocationContractError("invalid_payload", "Response must be a JSON object.")
    _reject_forbidden_keys(payload)

    schema_version = str(payload.get("schema_version") or "").strip()
    if schema_version != TARGET_SCHEMA_VERSION:
        raise AllocationContractError(
            "unsupported_schema_version",
            f"schema_version must be {TARGET_SCHEMA_VERSION!r}, got {schema_version!r}.",
        )

    proposed_version = str(payload.get("portfolio_version") or "").strip()
    if not proposed_version:
        raise AllocationContractError(
            "portfolio_version_required",
            "portfolio_version must echo the brief so a stale proposal cannot be applied.",
            repairable=False,
        )
    if proposed_version != brief.portfolio_version:
        raise ProposalStaleError(
            f"Proposal is for portfolio_version {proposed_version!r}, but the brief is "
            f"{brief.portfolio_version!r}. Re-run the allocation against the current state."
        )

    raw_weights = payload.get("target_weights")
    if not isinstance(raw_weights, Mapping) or not raw_weights:
        raise AllocationContractError(
            "target_weights_required", "target_weights must be a non-empty object."
        )
    weights: dict[str, Decimal] = {}
    permitted = set(brief.permitted_tickers) | {"CASH"}
    for raw_ticker, raw_weight in raw_weights.items():
        ticker = str(raw_ticker).strip().upper()
        if not ticker:
            raise AllocationContractError("blank_ticker", "target_weights contains a blank ticker.")
        if ticker in weights:
            raise AllocationContractError(
                "duplicate_ticker", f"target_weights contains {ticker!r} more than once."
            )
        if ticker not in permitted:
            raise AllocationContractError(
                "unknown_instrument",
                f"{ticker!r} is not in the permitted universe {sorted(permitted)}.",
            )
        weight = _weight_value(raw_weight, ticker=ticker)
        if weight < ZERO:
            raise AllocationContractError(
                "negative_weight",
                f"{ticker!r} weight {weight} is negative; the release is long-only.",
            )
        if weight > ONE:
            raise AllocationContractError(
                "weight_above_one", f"{ticker!r} weight {weight} exceeds 1.0."
            )
        weights[ticker] = weight

    if "CASH" not in weights:
        raise AllocationContractError(
            "cash_weight_required",
            "target_weights must include CASH. A weights vector without cash cannot state "
            "how much is deliberately uninvested.",
        )
    total = sum(weights.values(), ZERO)
    if abs(total - ONE) > WEIGHT_TOLERANCE:
        raise AllocationContractError(
            "weights_do_not_sum_to_one",
            f"target_weights sum to {total}; the tolerance is {WEIGHT_TOLERANCE}.",
            repairable=False,
        )
    normalised = _normalise_exact(weights)

    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise AllocationContractError(
            "actions_required", "actions must be a non-empty array with a rationale per action."
        )
    actions: list[TargetAction] = []
    seen_actions: set[str] = set()
    for item in raw_actions:
        if not isinstance(item, Mapping):
            raise AllocationContractError("invalid_action", "every action must be an object.")
        _reject_forbidden_keys(item, where="an action")
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            raise AllocationContractError("invalid_action", "an action has a blank ticker.")
        if ticker in seen_actions:
            raise AllocationContractError(
                "duplicate_action", f"{ticker!r} is described by more than one action."
            )
        seen_actions.add(ticker)
        if ticker not in permitted:
            raise AllocationContractError(
                "unknown_instrument", f"action for {ticker!r} is outside the permitted universe."
            )
        action = str(item.get("action") or "").strip().lower()
        if action not in ALLOWED_ACTIONS:
            raise AllocationContractError(
                "invalid_action",
                f"{ticker!r} action {action!r} is not one of {sorted(ALLOWED_ACTIONS)}.",
            )
        rationale = str(item.get("rationale") or "").strip()
        if not rationale:
            raise AllocationContractError(
                "action_rationale_required", f"{ticker!r} action has no rationale."
            )
        claim_ids_raw = item.get("claim_ids", [])
        if claim_ids_raw is None:
            claim_ids_raw = []
        if not isinstance(claim_ids_raw, list):
            raise AllocationContractError(
                "invalid_claim_ids", f"{ticker!r} claim_ids must be an array."
            )
        claim_ids = [str(claim_id).strip() for claim_id in claim_ids_raw if str(claim_id).strip()]
        unknown_claims = sorted(set(claim_ids) - brief.claim_ids)
        if unknown_claims:
            raise AllocationContractError(
                "unknown_claim_id",
                f"{ticker!r} cites claim ids that are not in the brief: {unknown_claims}.",
            )

        target_weight = normalised.get(ticker, ZERO)
        if ticker == "CASH":
            if action not in {"maintain_cash", "no_change"}:
                raise AllocationContractError(
                    "invalid_cash_action",
                    f"CASH action must be maintain_cash/no_change, got {action!r}.",
                )
        else:
            current_weight = brief.state.weight(ticker)
            delta = target_weight - current_weight
            if delta > WEIGHT_TOLERANCE:
                expected = "increase"
            elif delta < -WEIGHT_TOLERANCE:
                expected = "exit" if target_weight == ZERO else "reduce"
            else:
                expected = "maintain"
            compatible = (
                action == expected
                or (expected in {"reduce", "exit"} and action in {"reduce", "exit"})
                or (expected == "maintain" and action in {"maintain", "no_change"})
            )
            if not compatible:
                raise AllocationContractError(
                    "action_weight_mismatch",
                    f"{ticker!r} action {action!r} contradicts its weights: current "
                    f"{current_weight}, target {target_weight} implies {expected!r}.",
                )
            if expected == "increase":
                if not claim_ids:
                    raise AllocationContractError(
                        "claims_required_for_increase",
                        f"{ticker!r} increases risk but cites no claim. An unlinked "
                        "increase is an assertion without evidence.",
                    )
                if not brief.claim_catalog:
                    raise AllocationContractError(
                        "claims_required_for_increase",
                        f"{ticker!r} increases risk but the brief has no claims to cite.",
                    )
        actions.append(
            TargetAction(
                ticker=ticker,
                action=action,
                rationale=rationale,
                claim_ids=claim_ids,
                target_weight=target_weight,
            )
        )

    required_actions = {
        ticker
        for ticker in permitted
        if ticker != "CASH"
        and (normalised.get(ticker, ZERO) > ZERO or brief.state.weight(ticker) > ZERO)
    }
    missing = sorted(required_actions - seen_actions)
    if missing:
        raise AllocationContractError(
            "missing_action",
            f"Every allocated or currently held instrument needs an action; missing {missing}.",
        )

    funding_source = str(payload.get("funding_source") or "").strip().lower()
    if funding_source not in ALLOWED_FUNDING_SOURCES:
        raise AllocationContractError(
            "invalid_funding_source",
            f"funding_source must be one of {sorted(ALLOWED_FUNDING_SOURCES)}, got "
            f"{funding_source!r}.",
        )
    if funding_source == "mixed":
        funding_source = "cash_and_sales"

    rationale = str(payload.get("rationale") or "").strip()
    if not rationale:
        raise AllocationContractError(
            "rationale_required", "The proposal needs a portfolio-level rationale."
        )

    rejected = payload.get("rejected_alternatives", [])
    if not isinstance(rejected, list):
        raise AllocationContractError(
            "invalid_rejected_alternatives", "rejected_alternatives must be an array."
        )
    rejected_normalised: list[dict] = []
    for item in rejected:
        if not isinstance(item, Mapping):
            raise AllocationContractError(
                "invalid_rejected_alternatives",
                "each rejected alternative must be an object with a reason.",
            )
        ticker = str(item.get("ticker") or item.get("name") or "").strip().upper()
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise AllocationContractError(
                "invalid_rejected_alternatives",
                f"rejected alternative {ticker or '(unnamed)'} has no reason.",
            )
        if ticker and ticker not in permitted:
            raise AllocationContractError(
                "unknown_instrument",
                f"rejected alternative {ticker!r} is outside the permitted universe.",
            )
        rejected_normalised.append({"ticker": ticker or None, "reason": reason})

    uncertainties = payload.get("unresolved_uncertainties", [])
    if not isinstance(uncertainties, list):
        raise AllocationContractError(
            "invalid_unresolved_uncertainties", "unresolved_uncertainties must be an array."
        )
    uncertainties_normalised = [str(item).strip() for item in uncertainties if str(item).strip()]

    data_sufficiency = str(payload.get("data_sufficiency") or "").strip().lower()
    if data_sufficiency and data_sufficiency not in {"sufficient", "insufficient"}:
        raise AllocationContractError(
            "invalid_data_sufficiency",
            "data_sufficiency must be 'sufficient', 'insufficient' or absent.",
        )

    _ = policy  # the policy is applied by the projection; kept for a symmetric call shape
    return AllocationProposal(
        schema_version=schema_version,
        portfolio_version=proposed_version,
        target_weights=normalised,
        actions=actions,
        funding_source=funding_source,
        rationale=rationale,
        rejected_alternatives=rejected_normalised,
        unresolved_uncertainties=uncertainties_normalised,
        objective=str(payload.get("objective") or "").strip(),
        data_sufficiency=data_sufficiency,
        raw=dict(payload),
    )


# ---------------------------------------------------------------------------
# Constrained projection
# ---------------------------------------------------------------------------
@dataclass
class ConstraintResult:
    requested_weights: dict[str, Decimal]
    weights: dict[str, Decimal]
    deviations: list[dict]
    policy_actions: list[dict]
    constraints: dict
    turnover_fraction: Decimal

    @property
    def weights_json(self) -> dict[str, str]:
        return {ticker: decimal_string(weight) for ticker, weight in sorted(self.weights.items())}

    @property
    def requested_weights_json(self) -> dict[str, str]:
        return {
            ticker: decimal_string(weight)
            for ticker, weight in sorted(self.requested_weights.items())
        }


def _add_deviation(
    deviations: list[dict],
    *,
    code: str,
    detail: str,
    ticker: str | None = None,
    sector: str | None = None,
    requested: Decimal | None = None,
    applied: Decimal | None = None,
    limit: Decimal | None = None,
) -> None:
    record: dict[str, Any] = {"code": code, "detail": detail}
    if ticker:
        record["ticker"] = ticker
    if sector:
        record["sector"] = sector
    if requested is not None:
        record["requested_weight"] = decimal_string(requested)
    if applied is not None:
        record["applied_weight"] = decimal_string(applied)
    if limit is not None:
        record["limit"] = decimal_string(limit)
    deviations.append(record)


def _retain_cash(
    weights: dict[str, Decimal],
    cash: Decimal,
    *,
    required_cash: Decimal,
    current: Mapping[str, Decimal],
) -> tuple[Decimal, list[str], bool]:
    """Raise cash to ``required_cash`` by removing increases first, then scaling.

    Increases are removed down to the current weight in descending order, so a
    policy that says "retain cash" does not sell an existing holding before it
    cancels a new purchase. If that is not enough, all risky weights are scaled
    down proportionally - a risk-reducing move - and the second return flag says
    so.
    """
    deficit = required_cash - cash
    if deficit <= ZERO:
        return cash, [], False
    removed: list[str] = []
    candidates = [
        ticker
        for ticker, weight in weights.items()
        if weight > current.get(ticker, ZERO) + WEIGHT_TOLERANCE
    ]
    candidates.sort(key=lambda ticker: (-(weights[ticker] - current.get(ticker, ZERO)), ticker))
    for ticker in candidates:
        if deficit <= ZERO:
            break
        floor = current.get(ticker, ZERO)
        available = weights[ticker] - floor
        take = min(available, deficit)
        if take > ZERO:
            weights[ticker] -= take
            cash += take
            deficit -= take
            removed.append(ticker)
    scaled = False
    if deficit > WEIGHT_TOLERANCE:
        risky_total = sum(weights.values(), ZERO)
        if risky_total > ZERO:
            keep = max(ONE - required_cash, ZERO)
            factor = min(keep / risky_total, ONE)
            if factor < ONE:
                for ticker in weights:
                    weights[ticker] *= factor
                cash = ONE - sum(weights.values(), ZERO)
                scaled = True
    return cash, removed, scaled


def _block_new_risk(
    weights: dict[str, Decimal],
    cash: Decimal,
    *,
    current: Mapping[str, Decimal],
    tickers: set[str] | None = None,
) -> tuple[Decimal, list[str]]:
    blocked: list[str] = []
    for ticker, weight in list(weights.items()):
        if tickers is not None and ticker not in tickers:
            continue
        floor = current.get(ticker, ZERO)
        if weight > floor + WEIGHT_TOLERANCE:
            cash += weight - floor
            weights[ticker] = floor
            blocked.append(ticker)
    return cash, blocked


def _cap_names(
    weights: dict[str, Decimal],
    cash: Decimal,
    policy: AllocationPolicy,
    deviations: list[dict],
    limit_scale: Decimal = ONE,
) -> Decimal:
    limit = policy.max_name_weight * limit_scale
    for ticker in sorted(weights):
        weight = weights[ticker]
        if weight > limit + WEIGHT_TOLERANCE:
            cash += weight - limit
            weights[ticker] = limit
            _add_deviation(
                deviations,
                code="max_name_weight",
                ticker=ticker,
                detail=(
                    f"{ticker} requested {weight} of equity; the single-name ceiling is "
                    f"{policy.max_name_weight} (effective {limit} after the cost reserve). "
                    "The difference went to cash."
                ),
                requested=weight,
                applied=limit,
                limit=limit,
            )
    return cash


def _cap_sectors(
    weights: dict[str, Decimal],
    cash: Decimal,
    sectors: Mapping[str, str],
    policy: AllocationPolicy,
    deviations: list[dict],
    current: Mapping[str, Decimal],
    limit_scale: Decimal = ONE,
) -> Decimal:
    limit = policy.max_sector_weight * limit_scale
    grouped: dict[str, list[str]] = {}
    for ticker, weight in weights.items():
        if weight <= ZERO:
            continue
        sector = sectors.get(ticker)
        if not sector:
            continue
        grouped.setdefault(sector, []).append(ticker)
    for sector in sorted(grouped):
        members = grouped[sector]
        total = sum((weights[ticker] for ticker in members), ZERO)
        if total <= limit + WEIGHT_TOLERANCE:
            continue
        # Do not open a NEW position in a sector that is ALREADY over its
        # ceiling. Trimming an existing name is a reduction of risk; adding a
        # new one to an over-weight sector increases it, however small the new
        # weight. Declining it first is the deterministic version of that rule.
        # A sector that is merely brought over its cap by this plan is scaled
        # to the cap instead, so a new sector is not refused outright.
        current_sector_total = sum((current.get(ticker, ZERO) for ticker in members), ZERO)
        if current_sector_total <= limit + WEIGHT_TOLERANCE:
            factor = limit / total
            for ticker in members:
                original = weights[ticker]
                weights[ticker] = original * factor
                cash += original - weights[ticker]
                _add_deviation(
                    deviations,
                    code="max_sector_weight",
                    ticker=ticker,
                    sector=sector,
                    detail=(
                        f"Sector {sector!r} would reach {total} of equity; the ceiling is "
                        f"{policy.max_sector_weight} (effective {limit} after the cost "
                        f"reserve). {ticker} was reduced proportionally and the difference "
                        "went to cash."
                    ),
                    requested=original,
                    applied=weights[ticker],
                    limit=policy.max_sector_weight,
                )
            continue
        for ticker in sorted(members):
            if current.get(ticker, ZERO) > ZERO or weights[ticker] <= ZERO:
                continue
            requested = weights[ticker]
            weights[ticker] = ZERO
            cash += requested
            total -= requested
            _add_deviation(
                deviations,
                code="max_sector_weight_new_risk",
                ticker=ticker,
                sector=sector,
                detail=(
                    f"{ticker} would open a new position in sector {sector!r}, which is "
                    f"already over the {policy.max_sector_weight} ceiling (effective "
                    f"{limit} after the cost reserve). New risk in that sector was "
                    "declined; the weight went to cash."
                ),
                requested=requested,
                applied=ZERO,
                limit=limit,
            )
        if total <= limit + WEIGHT_TOLERANCE:
            continue
        factor = limit / total
        for ticker in members:
            original = weights[ticker]
            weights[ticker] = original * factor
            cash += original - weights[ticker]
            _add_deviation(
                deviations,
                code="max_sector_weight",
                ticker=ticker,
                sector=sector,
                detail=(
                    f"Sector {sector!r} would reach {total} of equity; the ceiling is "
                    f"{policy.max_sector_weight} (effective {limit} after the cost "
                    f"reserve). {ticker} was reduced proportionally and the difference "
                    "went to cash."
                ),
                requested=original,
                applied=weights[ticker],
                limit=limit,
            )
    return cash


def _cap_total(
    weights: dict[str, Decimal],
    cash: Decimal,
    policy: AllocationPolicy,
    deviations: list[dict],
    limit_scale: Decimal = ONE,
) -> Decimal:
    effective_max = min(policy.max_total_exposure, ONE - policy.min_cash_weight) * limit_scale
    risky_total = sum(weights.values(), ZERO)
    if risky_total <= effective_max + WEIGHT_TOLERANCE:
        return cash
    factor = effective_max / risky_total if risky_total > ZERO else ZERO
    for ticker in weights:
        weights[ticker] *= factor
    cash = ONE - sum(weights.values(), ZERO)
    code = (
        "cash_buffer"
        if ONE - policy.min_cash_weight < policy.max_total_exposure
        else ("total_exposure")
    )
    _add_deviation(
        deviations,
        code=code,
        detail=(
            f"Risky weights totaled {risky_total} of equity; the enforceable maximum is "
            f"{effective_max} (total exposure {policy.max_total_exposure}, cash buffer "
            f"{policy.min_cash_weight}). All risky weights were scaled down; the "
            "difference went to cash."
        ),
        requested=risky_total,
        applied=effective_max,
        limit=effective_max,
    )
    return cash


def _cap_turnover(
    weights: dict[str, Decimal],
    current: Mapping[str, Decimal],
    cash: Decimal,
    policy: AllocationPolicy,
    deviations: list[dict],
) -> tuple[Decimal, Decimal]:
    turnover = sum(
        (abs(weight - current.get(ticker, ZERO)) for ticker, weight in weights.items()), ZERO
    )
    if turnover <= policy.max_turnover + WEIGHT_TOLERANCE:
        return cash, turnover
    factor = policy.max_turnover / turnover if turnover > ZERO else ZERO
    for ticker, weight in list(weights.items()):
        weights[ticker] = current.get(ticker, ZERO) + (weight - current.get(ticker, ZERO)) * factor
    cash = ONE - sum(weights.values(), ZERO)
    new_turnover = sum(
        (abs(weight - current.get(ticker, ZERO)) for ticker, weight in weights.items()), ZERO
    )
    _add_deviation(
        deviations,
        code="max_turnover",
        detail=(
            f"Requested rebalance turnover was {turnover} of equity; the ceiling is "
            f"{policy.max_turnover}. All weight changes were scaled toward the current "
            f"portfolio by {factor:.6f}; no trade was scaled beyond its requested size."
        ),
        requested=turnover,
        applied=new_turnover,
        limit=policy.max_turnover,
    )
    return cash, new_turnover


def _apply_quality_policy(
    weights: dict[str, Decimal],
    cash: Decimal,
    *,
    current: Mapping[str, Decimal],
    policy: AllocationPolicy,
    data_quality: Mapping[str, Any],
    sectors: Mapping[str, str],
    deviations: list[dict],
) -> tuple[Decimal, list[dict]]:
    applied: list[dict] = []
    for entry in data_quality.get("policy_actions", []) or []:
        flag = str(entry.get("flag") or "")
        action = str(entry.get("action") or "")
        if action == ACTION_BLOCK_NEW_RISK:
            if flag == "sector_exposure_incomplete" and policy.unknown_sector_action == action:
                # Block new risk only for names whose sector is unknown; a
                # classified increase is untouched by this particular flag.
                unknown = {ticker for ticker in weights if not sectors.get(ticker)}
                cash, blocked = _block_new_risk(weights, cash, current=current, tickers=unknown)
                detail = (
                    "Policy blocks new risk in instruments without a sector "
                    f"classification; increases removed for {blocked or 'none'}."
                )
            else:
                cash, blocked = _block_new_risk(weights, cash, current=current)
                detail = (
                    f"Policy blocks new risk while {flag} is present; increases removed "
                    f"for {blocked or 'none'}."
                )
            _add_deviation(deviations, code=f"policy:{flag}", detail=detail)
            applied.append({"flag": flag, "action": action})
        elif action == ACTION_RETAIN_CASH:
            required = max(cash, current.get("CASH", ZERO))
            cash, removed, scaled = _retain_cash(
                weights, cash, required_cash=required, current=current
            )
            detail = (
                f"Policy retains cash while {flag} is present; cash floor raised to "
                f"{required}. Increases removed for {removed or 'none'}"
                + ("; remaining risky weights scaled down." if scaled else ".")
            )
            _add_deviation(deviations, code=f"policy:{flag}", detail=detail)
            applied.append({"flag": flag, "action": action})
    return cash, applied


def constrain_targets(
    requested_weights: Mapping[str, Decimal],
    state: PortfolioState,
    *,
    sectors: Mapping[str, str],
    policy: AllocationPolicy,
    data_quality: Mapping[str, Any] | None = None,
    prices: Mapping[str, Decimal] | None = None,
    cost_reserve_fraction: Decimal = ZERO,
) -> ConstraintResult:
    """Project a requested weight vector into the feasible set, deterministically.

    Order: policy actions -> unpriceable targets -> per-name -> sector -> total
    exposure/cash buffer -> turnover, repeated until stable. A deficit always
    goes to cash; the projection never moves weight to a different risky name to
    "use up" a cap.
    """
    if state.equity_usd is None or state.equity_usd <= ZERO:
        raise AllocationContractError(
            "equity_unavailable",
            "The portfolio equity is unknown (an unpriced holding), so weights cannot be "
            "converted into a plan.",
            repairable=False,
        )
    prices = prices if prices is not None else state.prices
    reserve_fraction = min(max(cost_reserve_fraction, ZERO), Decimal("0.05"))
    limit_scale = ONE - reserve_fraction
    current = {ticker: state.weight(ticker) for ticker in state.weights if ticker != "CASH"}
    current["CASH"] = state.weights.get("CASH", ZERO)

    weights: dict[str, Decimal] = {
        ticker: weight for ticker, weight in requested_weights.items() if ticker != "CASH"
    }
    # Every current holding must be present even when the request omitted it:
    # an omitted holding is a zero target, which is an exit, not a no-op.
    for holding in state.holdings:
        weights.setdefault(holding.ticker, ZERO)
    cash = requested_weights.get("CASH", ZERO)
    deviations: list[dict] = []

    for ticker in sorted(weights):
        if weights[ticker] > ZERO and not prices.get(ticker):
            cash += weights[ticker]
            _add_deviation(
                deviations,
                code="unpriceable_target",
                ticker=ticker,
                detail=(
                    f"{ticker} had a target weight but no price; it cannot be sized. The "
                    "weight went to cash rather than being guessed."
                ),
                requested=weights[ticker],
                applied=ZERO,
            )
            weights[ticker] = ZERO

    cash, policy_actions = _apply_quality_policy(
        weights,
        cash,
        current=current,
        policy=policy,
        data_quality=data_quality or {},
        sectors=sectors,
        deviations=deviations,
    )

    previous_signature: tuple | None = None
    converged = False
    for _iteration in range(MAX_PROJECTION_ITERATIONS):
        cash = _cap_names(weights, cash, policy, deviations, limit_scale)
        cash = _cap_sectors(weights, cash, sectors, policy, deviations, current, limit_scale)
        cash = _cap_total(weights, cash, policy, deviations, limit_scale)
        cash, _turnover = _cap_turnover(weights, current, cash, policy, deviations)
        risky_total = sum(weights.values(), ZERO)
        signature_parts = [(ticker, round(weights[ticker], 9)) for ticker in sorted(weights)]
        signature_parts.append(("__total__", round(risky_total, 9)))
        signature = tuple(signature_parts)
        checks_pass = (
            all(weight <= policy.max_name_weight + WEIGHT_TOLERANCE for weight in weights.values())
            and risky_total
            <= min(policy.max_total_exposure, ONE - policy.min_cash_weight) + WEIGHT_TOLERANCE
            and _turnover <= policy.max_turnover + WEIGHT_TOLERANCE
        )
        if checks_pass:
            converged = True
            break
        if signature == previous_signature:
            break
        previous_signature = signature

    if not converged:
        # Last-resort safe action: hold the current portfolio. This can still be
        # outside a limit that was already breached, which is recorded below.
        # Cash is not assigned here: ``_normalise_exact`` derives it from the
        # weights that survive rounding, and that is the value returned below.
        weights = {ticker: current.get(ticker, ZERO) for ticker in weights}
        _add_deviation(
            deviations,
            code="constrained_projection_no_feasible_change",
            detail=(
                "The requested targets could not be projected into the limit set within "
                f"{MAX_PROJECTION_ITERATIONS} iterations. The plan holds the current "
                "weights; no trade is proposed."
            ),
        )

    # Final exact sum at six decimals. Rounding risky weights down can only
    # increase cash, so this cannot violate the cash floor in the other direction.
    weights = _normalise_exact(weights)
    cash = weights.pop("CASH")
    weights["CASH"] = cash

    # --- Post-projection constraint report ---------------------------------
    risky_total = sum(weight for ticker, weight in weights.items() if ticker != "CASH")
    effective_max = min(policy.max_total_exposure, ONE - policy.min_cash_weight) * limit_scale
    cash_floor = policy.min_cash_weight + reserve_fraction * (ONE - policy.min_cash_weight)
    name_limit = policy.max_name_weight * limit_scale
    sector_limit = policy.max_sector_weight * limit_scale
    per_name = []
    for ticker in sorted(t for t in weights if t != "CASH"):
        per_name.append(
            {
                "ticker": ticker,
                "limit": str(name_limit),
                "policy_limit": str(policy.max_name_weight),
                "observed": decimal_string(weights[ticker]),
                "status": ("pass" if weights[ticker] <= name_limit + WEIGHT_TOLERANCE else "fail"),
            }
        )
    sector_totals: dict[str, Decimal] = {}
    unevaluated: list[dict] = []
    for ticker, weight in weights.items():
        if ticker == "CASH":
            continue
        sector = sectors.get(ticker)
        if not sector:
            if weight > ZERO:
                unevaluated.append(
                    {
                        "limit": "max_sector_weight",
                        "ticker": ticker,
                        "reason": "no_classification",
                    }
                )
            continue
        sector_totals[sector] = sector_totals.get(sector, ZERO) + weight
    per_sector = [
        {
            "sector": sector,
            "limit": str(sector_limit),
            "policy_limit": str(policy.max_sector_weight),
            "observed": decimal_string(total),
            "status": "pass" if total <= sector_limit + WEIGHT_TOLERANCE else "fail",
        }
        for sector, total in sorted(sector_totals.items())
    ]
    turnover = sum(
        (
            abs(weight - current.get(ticker, ZERO))
            for ticker, weight in weights.items()
            if ticker != "CASH"
        ),
        ZERO,
    )
    checks = {
        "max_name_weight": {
            "limit": str(name_limit),
            "policy_limit": str(policy.max_name_weight),
            "status": "fail" if any(row["status"] == "fail" for row in per_name) else "pass",
            "per_name": per_name,
        },
        "max_sector_weight": {
            "limit": str(sector_limit),
            "policy_limit": str(policy.max_sector_weight),
            "status": "fail" if any(row["status"] == "fail" for row in per_sector) else "pass",
            "per_sector": per_sector,
            "not_evaluated": unevaluated,
        },
        "max_total_exposure": {
            "limit": str(policy.max_total_exposure),
            "effective_limit": str(effective_max),
            "observed": decimal_string(risky_total),
            "status": "pass" if risky_total <= effective_max + WEIGHT_TOLERANCE else "fail",
        },
        "cash_buffer": {
            "min_cash_weight": str(cash_floor),
            "policy_min_cash_weight": str(policy.min_cash_weight),
            "observed_cash_weight": decimal_string(weights["CASH"]),
            "status": "pass" if weights["CASH"] >= cash_floor - WEIGHT_TOLERANCE else "fail",
        },
        "max_turnover": {
            "limit": str(policy.max_turnover),
            "observed": decimal_string(turnover),
            "status": "pass" if turnover <= policy.max_turnover + WEIGHT_TOLERANCE else "fail",
        },
    }
    constraints = {
        "method_version": PROJECTION_METHOD_VERSION,
        "objective_version": "constrained_capital_allocation.v1",
        "inputs": [
            "input_snapshot_holdings",
            "input_snapshot_cash",
            "input_snapshot_quotes",
            "explicit_instrument_classification",
            "deterministic_price_statistics",
            "candidate_cases_and_claims",
        ],
        "solver": {
            "method": PROJECTION_METHOD_VERSION,
            "type": "deterministic_projection",
            "not_an_optimizer": True,
            "note": (
                "The model proposes weights; this layer projects them into the feasible "
                "set. There is no mean-variance or expected-return objective."
            ),
        },
        "covariance_method_version": "covariance.sample_daily_returns.v1",
        "policy_version": policy.policy_version,
        "limits": policy.as_dict()["limits"],
        "checks": checks,
        "unevaluated": unevaluated,
        "binding_constraints": sorted(
            {deviation["code"] for deviation in deviations if deviation.get("code")}
        ),
        "cost_reserve_fraction": str(reserve_fraction),
        "limit_scale": str(limit_scale),
        "note": (
            "Limits are applied to weights of pre-cost equity scaled by the cost reserve, "
            "so post-cost weights stay inside the policy limits. The reserve is an "
            "estimate from the declared cost model, not a broker fee."
        ),
    }
    return ConstraintResult(
        requested_weights=dict(requested_weights),
        weights=weights,
        deviations=deviations,
        policy_actions=policy_actions,
        constraints=constraints,
        turnover_fraction=turnover,
    )


# ---------------------------------------------------------------------------
# Deterministic sizing
# ---------------------------------------------------------------------------
@dataclass
class LegDraft:
    sequence: int
    ticker: str
    side: str
    quantity: Decimal
    price: Decimal
    notional_usd: Decimal
    estimated_cost_usd: Decimal
    priority: str
    sector: str = ""
    asset_class: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "ticker": self.ticker,
            "side": self.side,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "notional_usd": str(self.notional_usd),
            "estimated_cost_usd": str(self.estimated_cost_usd),
            "priority": self.priority,
            "sector": self.sector or None,
            "asset_class": self.asset_class or None,
            "reason": self.reason,
        }


@dataclass
class LegResult:
    legs: list[LegDraft]
    deviations: list[dict]
    estimated_cost_usd: Decimal
    turnover_fraction: Decimal
    post_trade: dict
    funding: dict

    @property
    def estimated_turnover_pct(self) -> Decimal:
        return (self.turnover_fraction * Decimal("100")).quantize(Decimal("0.0001"))


def _floor_to_lot(quantity: Decimal, lot: Decimal) -> Decimal:
    if quantity <= ZERO or lot <= ZERO:
        return ZERO
    return ((quantity / lot).to_integral_value(rounding=ROUND_FLOOR) * lot).quantize(
        Decimal("0.00000001")
    )


def derive_legs(
    constrained: ConstraintResult,
    state: PortfolioState,
    *,
    policy: AllocationPolicy,
    cost_model: CostModel,
    sectors: Mapping[str, str],
    asset_classes: Mapping[str, str] | None = None,
    quotes: Mapping[str, Decimal] | None = None,
) -> LegResult:
    """Turn constrained weights into lot-rounded, cost-aware quantities.

    Sells are built first and sequenced before buys. The cash available to buys
    includes the **planned** net sale proceeds, but no plan assumes proceeds from
    a sale that has not executed: :func:`services.portfolio_allocation.apply_plan`
    validates and applies each selling leg before any buying leg, and stops on a
    failure.
    """
    if state.equity_usd is None or state.equity_usd <= ZERO:
        raise AllocationContractError(
            "equity_unavailable", "Portfolio equity is unknown; cannot size legs.", repairable=False
        )
    equity = state.equity_usd
    prices = dict(state.prices)
    for ticker, price in (quotes or {}).items():
        parsed = Decimal(str(price))
        if parsed > ZERO:
            prices[str(ticker).upper()] = parsed
    for ticker, price in prices.items():
        prices[ticker] = price.quantize(CENT)

    asset_classes = asset_classes or {}
    deviations: list[dict] = []
    sells: list[LegDraft] = []
    buys: list[dict] = []

    for ticker in sorted(constrained.weights):
        if ticker == "CASH":
            continue
        weight = constrained.weights[ticker]
        price = prices.get(ticker)
        quantity = state.quantity(ticker)
        current_value = (quantity * price).quantize(CENT) if price is not None else ZERO
        target_value = (weight * equity).quantize(CENT)
        if weight > ZERO and price is None:
            _add_deviation(
                deviations,
                code="no_price_for_target",
                ticker=ticker,
                detail=f"{ticker} has a target weight but no price; no leg was created.",
                requested=weight,
                applied=ZERO,
            )
            continue
        delta = target_value - current_value
        if delta < -policy.min_trade_notional_usd:
            if price is None or quantity <= ZERO:
                continue
            raw_quantity = min((-delta) / price, quantity)
            rounded = _floor_to_lot(raw_quantity, policy.lot_size_for(ticker))
            if rounded <= ZERO:
                _add_deviation(
                    deviations,
                    code="rounding_no_sell",
                    ticker=ticker,
                    detail=(
                        f"{ticker} reduction of {abs(delta)} is smaller than one lot at "
                        f"{price}; no sell was created."
                    ),
                    requested=weight,
                    applied=state.weight(ticker),
                )
                continue
            notional = (rounded * price).quantize(CENT)
            priority = "funding" if weight == ZERO else "risk_reducing"
            sells.append(
                LegDraft(
                    sequence=0,
                    ticker=ticker,
                    side="SELL",
                    quantity=rounded,
                    price=price,
                    notional_usd=notional,
                    estimated_cost_usd=cost_model.estimate("SELL", notional),
                    priority=priority,
                    sector=sectors.get(ticker, ""),
                    asset_class=asset_classes.get(ticker, ""),
                    reason=(f"Reduce {ticker} from {state.weight(ticker)} to {weight} of equity."),
                )
            )
        elif delta > policy.min_trade_notional_usd:
            if weight <= ZERO:
                continue
            buys.append(
                {
                    "ticker": ticker,
                    "weight": weight,
                    "price": price,
                    "desired": delta,
                    "current_value": current_value,
                }
            )
        elif abs(delta) > WEIGHT_TOLERANCE and weight == ZERO and quantity > ZERO:
            # A target of exactly zero with a residue below the minimum trade is
            # still a deliberate exit request; record why it did not trade.
            _add_deviation(
                deviations,
                code="below_min_trade",
                ticker=ticker,
                detail=(
                    f"{ticker} exit leaves {abs(delta)} below the minimum trade "
                    f"{policy.min_trade_notional_usd}; no sell was created."
                ),
                requested=weight,
                applied=state.weight(ticker),
            )

    sells.sort(key=lambda leg: (leg.priority != "funding", leg.ticker))
    buys.sort(key=lambda item: (-item["weight"], item["ticker"]))

    min_cash = (policy.min_cash_weight * equity).quantize(CENT)
    sale_proceeds = sum((leg.notional_usd - leg.estimated_cost_usd for leg in sells), ZERO)
    available_cash = (state.cash_usd + sale_proceeds).quantize(CENT)

    buy_legs: list[LegDraft] = []
    for item in buys:
        ticker = item["ticker"]
        price = item["price"]
        if price is None:
            continue
        desired = item["desired"]
        lot = policy.lot_size_for(ticker)
        budget = (available_cash * policy.max_trade_fraction_of_cash).quantize(CENT)
        max_spend = available_cash - min_cash
        if policy.max_trade_fraction_of_cash <= ZERO:
            _add_deviation(
                deviations,
                code="per_trade_budget_zero",
                ticker=ticker,
                detail=(
                    "max_trade_allocation_pct resolves to a zero per-trade budget; no "
                    f"buy was created for {ticker}."
                ),
                requested=item["weight"],
                applied=state.weight(ticker),
            )
            continue
        if max_spend <= ZERO:
            _add_deviation(
                deviations,
                code="cash_buffer_no_buy",
                ticker=ticker,
                detail=(
                    f"Available cash {available_cash} is at or below the required cash "
                    f"buffer {min_cash}; no buy was created for {ticker}."
                ),
                requested=item["weight"],
                applied=state.weight(ticker),
            )
            continue
        max_notional = min(desired, budget, max_spend)
        quantity = _floor_to_lot(max_notional / price, lot)
        cost = cost_model.estimate("BUY", (quantity * price).quantize(CENT))
        while quantity > ZERO and (
            quantity * price + cost > max_spend or quantity * price > budget
        ):
            overshoot = (quantity * price + cost) - min(max_spend, budget)
            reduce_lots = (overshoot / (price * lot)).to_integral_value(rounding=ROUND_FLOOR) * lot
            if reduce_lots <= ZERO:
                reduce_lots = lot
            quantity = (quantity - reduce_lots).quantize(Decimal("0.00000001"))
            if quantity < lot:
                quantity = ZERO
                break
            cost = cost_model.estimate("BUY", (quantity * price).quantize(CENT))
        if quantity <= ZERO:
            code = "rounding_no_buy" if desired <= budget else "per_trade_budget_no_buy"
            _add_deviation(
                deviations,
                code=code,
                ticker=ticker,
                detail=(
                    f"{ticker} target of {item['weight']} of equity needs {desired}, but "
                    f"after lot rounding ({lot}) and costs nothing fits the available "
                    f"budget ({budget}) and cash buffer ({min_cash}). The target was not "
                    "scaled up past a limit."
                ),
                requested=item["weight"],
                applied=state.weight(ticker),
            )
            continue
        notional = (quantity * price).quantize(CENT)
        cost = cost_model.estimate("BUY", notional)
        if notional < desired - CENT:
            binding = (
                "per_trade_budget"
                if budget < min(desired, max_spend)
                else "cash_buffer"
                if max_spend < desired
                else "lot_rounding"
            )
            _add_deviation(
                deviations,
                code=binding,
                ticker=ticker,
                detail=(
                    f"{ticker} requested {desired} of new value; the plan buys {notional} "
                    f"because {binding} limited it. Cash buffer {min_cash}, per-trade "
                    f"budget {budget}."
                ),
                requested=item["weight"],
                applied=(item["current_value"] + notional) / equity,
            )
        available_cash = (available_cash - notional - cost).quantize(CENT)
        buy_legs.append(
            LegDraft(
                sequence=0,
                ticker=ticker,
                side="BUY",
                quantity=quantity,
                price=price,
                notional_usd=notional,
                estimated_cost_usd=cost,
                priority="new_risk" if state.quantity(ticker) == ZERO else "rebalance",
                sector=sectors.get(ticker, ""),
                asset_class=asset_classes.get(ticker, ""),
                reason=f"Increase {ticker} from {state.weight(ticker)} to {item['weight']} of equity.",
            )
        )

    legs = sells + buy_legs
    for index, leg in enumerate(legs, start=1):
        leg.sequence = index

    def _compute_post() -> dict:
        nonlocal legs
        legs = sells + buy_legs
        for index, leg in enumerate(legs, start=1):
            leg.sequence = index
        estimated_cost = sum((leg.estimated_cost_usd for leg in legs), ZERO)
        turnover = sum((leg.notional_usd for leg in legs), ZERO) / equity if equity > ZERO else ZERO
        quantities = {holding.ticker: holding.quantity for holding in state.holdings}
        for leg in legs:
            sign = ONE if leg.side == "BUY" else -ONE
            quantities[leg.ticker] = quantities.get(leg.ticker, ZERO) + sign * leg.quantity
        post_equity = equity - estimated_cost
        sell_proceeds_net = sum(
            (leg.notional_usd - leg.estimated_cost_usd for leg in sells), ZERO
        ).quantize(CENT)
        post_cash = (
            state.cash_usd
            + sell_proceeds_net
            - sum((leg.notional_usd + leg.estimated_cost_usd for leg in buy_legs), ZERO)
        ).quantize(CENT)
        post_positions = ZERO
        post_weights: dict[str, Decimal] = {}
        post_sector: dict[str, Decimal] = {}
        post_unknown_value = ZERO
        for ticker, quantity in sorted(quantities.items()):
            price = prices.get(ticker)
            if price is None or quantity <= ZERO:
                continue
            value = (quantity * price).quantize(CENT)
            post_positions += value
            weight = value / post_equity if post_equity > ZERO else ZERO
            post_weights[ticker] = weight
            sector = sectors.get(ticker)
            if sector:
                post_sector[sector] = post_sector.get(sector, ZERO) + weight
            else:
                post_unknown_value += weight
        if post_equity > ZERO:
            post_weights["CASH"] = post_cash / post_equity
        post_total = sum(
            (weight for ticker, weight in post_weights.items() if ticker != "CASH"), ZERO
        )
        post_cash_weight = post_weights.get("CASH", ZERO)
        current_total = sum((state.weight(holding.ticker) for holding in state.holdings), ZERO)
        breaches: list[dict] = []
        for ticker, weight in sorted(post_weights.items()):
            if ticker == "CASH":
                continue
            if weight > policy.max_name_weight + WEIGHT_TOLERANCE:
                breaches.append(
                    {
                        "dimension": "name",
                        "ticker": ticker,
                        "observed": weight,
                        "limit_value": policy.max_name_weight,
                        "current": state.weight(ticker),
                        "increased": weight > state.weight(ticker) + WEIGHT_TOLERANCE,
                    }
                )
        for sector, weight in sorted(post_sector.items()):
            if weight > policy.max_sector_weight + WEIGHT_TOLERANCE:
                current_sector = sum(
                    state.weight(ticker) for ticker in post_weights if sectors.get(ticker) == sector
                )
                breaches.append(
                    {
                        "dimension": "sector",
                        "sector": sector,
                        "observed": weight,
                        "limit_value": policy.max_sector_weight,
                        "current": current_sector,
                        "increased": weight > current_sector + WEIGHT_TOLERANCE,
                    }
                )
        if post_cash_weight < policy.min_cash_weight - WEIGHT_TOLERANCE:
            breaches.append(
                {
                    "dimension": "cash",
                    "observed": post_cash_weight,
                    "limit_value": policy.min_cash_weight,
                    "current": state.weight("CASH"),
                    "increased": True,
                }
            )
        effective_total_limit = min(policy.max_total_exposure, ONE - policy.min_cash_weight)
        if post_total > effective_total_limit + WEIGHT_TOLERANCE:
            breaches.append(
                {
                    "dimension": "total",
                    "observed": post_total,
                    "limit_value": effective_total_limit,
                    "current": current_total,
                    "increased": post_total > current_total + WEIGHT_TOLERANCE,
                }
            )
        return {
            "legs": legs,
            "estimated_cost": estimated_cost,
            "turnover": turnover,
            "post_equity": post_equity,
            "post_cash": post_cash,
            "sale_proceeds": sell_proceeds_net,
            "post_positions": post_positions,
            "post_weights": post_weights,
            "post_sector": post_sector,
            "post_unknown": post_unknown_value,
            "post_total": post_total,
            "post_cash_weight": post_cash_weight,
            "breaches": breaches,
        }

    post = _compute_post()
    trim_rounds = 0
    while post["breaches"] and trim_rounds < 12:
        trimmed_any = False
        for breach in list(post["breaches"]):
            if breach["dimension"] == "name":
                candidates = [leg for leg in buy_legs if leg.ticker == breach["ticker"]]
            elif breach["dimension"] == "sector":
                candidates = [
                    leg for leg in buy_legs if sectors.get(leg.ticker) == breach["sector"]
                ]
            else:
                candidates = list(buy_legs)
            if not candidates:
                continue
            if breach["dimension"] == "cash":
                excess_value = max(
                    (policy.min_cash_weight * post["post_equity"]) - post["post_cash"],
                    ZERO,
                )
            else:
                excess_value = (breach["observed"] - breach["limit_value"]) * post["post_equity"]
            if excess_value <= ZERO:
                continue
            for leg in sorted(candidates, key=lambda item: (-item.notional_usd, item.ticker)):
                if excess_value <= CENT:
                    break
                lot = policy.lot_size_for(leg.ticker)
                lots = (excess_value / (leg.price * lot)).to_integral_value(rounding=ROUND_CEILING)
                reduce_quantity = min(leg.quantity, lots * lot)
                if reduce_quantity <= ZERO:
                    continue
                new_quantity = (leg.quantity - reduce_quantity).quantize(Decimal("0.00000001"))
                removed_notional = (reduce_quantity * leg.price).quantize(CENT)
                if new_quantity <= ZERO:
                    buy_legs.remove(leg)
                else:
                    leg.quantity = new_quantity
                    leg.notional_usd = (new_quantity * leg.price).quantize(CENT)
                    leg.estimated_cost_usd = cost_model.estimate("BUY", leg.notional_usd)
                excess_value -= removed_notional
                trimmed_any = True
                _add_deviation(
                    deviations,
                    code="post_trade_trimmed",
                    ticker=leg.ticker,
                    sector=breach.get("sector"),
                    detail=(
                        f"Post-trade {breach['dimension']} would exceed its limit after lot "
                        f"rounding and costs; the {leg.ticker} buy was reduced by "
                        f"{reduce_quantity} units. The plan does not scale beyond the limit."
                    ),
                    applied=new_quantity,
                    limit=breach["limit_value"],
                )
        if not trimmed_any:
            break
        trim_rounds += 1
        post = _compute_post()

    legs = post["legs"]
    estimated_cost = post["estimated_cost"]
    turnover = post["turnover"]
    post_equity = post["post_equity"]
    post_cash = post["post_cash"]
    post_positions = post["post_positions"]
    post_weights = post["post_weights"]
    post_sector = post["post_sector"]
    post_unknown_value = post["post_unknown"]
    post_total_exposure = post["post_total"]
    post_cash_weight = post["post_cash_weight"]
    existing = post["breaches"]
    for breach in existing:
        if breach["dimension"] == "name":
            _add_deviation(
                deviations,
                code="post_trade_name_breach",
                ticker=breach["ticker"],
                detail=(
                    f"After rounding and costs, {breach['ticker']} would be "
                    f"{breach['observed']} of equity against a {breach['limit_value']} "
                    "ceiling. The position was not increased by this plan."
                ),
                applied=breach["observed"],
                limit=breach["limit_value"],
            )
        elif breach["dimension"] == "sector":
            _add_deviation(
                deviations,
                code="post_trade_sector_breach",
                sector=breach["sector"],
                detail=(
                    f"After rounding and costs, sector {breach['sector']!r} would be "
                    f"{breach['observed']} of equity against a {breach['limit_value']} "
                    "ceiling."
                ),
                applied=breach["observed"],
                limit=breach["limit_value"],
            )
        elif breach["dimension"] == "cash":
            _add_deviation(
                deviations,
                code="post_trade_cash_breach",
                detail=(
                    f"After costs, cash would be {breach['observed']} of equity against a "
                    f"{breach['limit_value']} buffer."
                ),
                applied=breach["observed"],
                limit=breach["limit_value"],
            )
        else:
            _add_deviation(
                deviations,
                code="post_trade_total_breach",
                detail=(
                    f"After rounding and costs, total exposure would be "
                    f"{breach['observed']} against a {breach['limit_value']} ceiling."
                ),
                applied=breach["observed"],
                limit=breach["limit_value"],
            )
    increases = [entry for entry in existing if entry["increased"]]
    status = "violation" if increases else ("existing_breach" if existing else "within_limits")
    post_trade = {
        "status": status,
        "equity_usd": str(post_equity.quantize(CENT)),
        "cash_usd": str(post_cash),
        "cash_weight": str(post_cash_weight),
        "positions_value_usd": str(post_positions.quantize(CENT)),
        "total_exposure": str(post_total_exposure),
        "sector_weights": {
            sector: decimal_string(weight) for sector, weight in sorted(post_sector.items())
        },
        "unclassified_weight": decimal_string(post_unknown_value),
        "weights": {
            ticker: decimal_string(weight) for ticker, weight in sorted(post_weights.items())
        },
        "breaches": existing,
    }
    if status == "violation":
        raise AllocationContractError(
            "post_trade_limit_violation",
            "The rounded plan would increase an exposure past a hard limit; refusing to "
            f"emit it ({increases}).",
            repairable=False,
        )

    sale_proceeds = post["sale_proceeds"]
    buy_spend = sum((leg.notional_usd + leg.estimated_cost_usd for leg in buy_legs), ZERO)
    if buy_legs and sells:
        source = "cash_and_sales" if buy_spend > state.cash_usd else "cash"
    elif buy_legs:
        source = "cash"
    else:
        source = "none"
    funding = {
        "source": source,
        "starting_cash_usd": str(state.cash_usd),
        "sell_proceeds_usd": str(sale_proceeds),
        "buy_spend_usd": str(buy_spend.quantize(CENT)),
        "estimated_costs_usd": str(estimated_cost.quantize(CENT)),
        "ending_cash_usd": str(post_cash),
        "min_cash_usd": str(min_cash),
        "assumes_sell_proceeds": bool(buy_legs and sells),
        "note": (
            "Buy legs are funded from the planned sell proceeds in sequence. At apply "
            "time every selling leg is revalidated and applied before any buying leg; a "
            "failed sale halts the plan and its proceeds are not treated as available."
        ),
    }
    return LegResult(
        legs=legs,
        deviations=deviations,
        estimated_cost_usd=estimated_cost.quantize(CENT),
        turnover_fraction=turnover,
        post_trade=post_trade,
        funding=funding,
    )


def proposal_fingerprint(proposal: AllocationProposal) -> str:
    """Stable hash of the validated proposal, used in idempotency checks."""
    return content_hash(proposal.as_dict())


def decode_json_object(text: Any) -> dict:
    """Parse one JSON object from a model response, tolerating a fenced block.

    Kept local so the allocation seam does not depend on the debate protocol's
    internals. Markdown fences are stripped because models emit them; an array
    or scalar is rejected rather than coerced.
    """
    if isinstance(text, Mapping):
        return dict(text)
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AllocationContractError(
            "invalid_json", f"Response is not valid JSON: {exc.msg} at {exc.pos}."
        ) from exc
    if not isinstance(parsed, dict):
        raise AllocationContractError(
            "invalid_json", "Response must be a single JSON object, not an array or scalar."
        )
    return parsed
