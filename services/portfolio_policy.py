"""Portfolio allocation policy and transaction-cost assumptions (PORT-04).

This module holds the numbers that turn a target-weight proposal into something
the deterministic layer can enforce. Keeping them out of the brief service and
the plan service means both read the same policy object, and a plan can record
the exact policy version it was built under.

Two rules are load-bearing:

* **The portfolio's own limits win.** ``max_name_weight``,
  ``max_sector_weight``, ``max_total_exposure``, ``max_turnover`` and the cash
  buffer are derived from the ``Portfolio`` row. The settings only supply
  estimation and evidence-policy defaults.
* **A policy action is a decision, not a hint.** Each data-quality flag maps to
  one of ``allow``, ``label_only``, ``retain_cash`` or ``block_new_risk``. The
  action is applied deterministically by the constrained projection and stored
  on the plan, so a reader can tell whether cash was retained because the model
  chose it or because the policy required it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.models import Portfolio

logger = logging.getLogger("alphaagent.allocation_policy")

__all__ = [
    "ACTION_ALLOW",
    "ACTION_BLOCK_NEW_RISK",
    "ACTION_LABEL_ONLY",
    "ACTION_RETAIN_CASH",
    "ACTION_VALUES",
    "CENT",
    "HUNDRED",
    "ONE",
    "WEIGHT_QUANTUM",
    "ZERO",
    "AllocationPolicy",
    "AllocationPolicyError",
    "CostModel",
]

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")
#: Weights are stored at six decimals. The PORT-03 tolerance is 1e-6, so a
#: quantised weight is the finest unit that still preserves the contract.
WEIGHT_QUANTUM = Decimal("0.000001")

ACTION_ALLOW = "allow"
ACTION_LABEL_ONLY = "label_only"
ACTION_RETAIN_CASH = "retain_cash"
ACTION_BLOCK_NEW_RISK = "block_new_risk"
ACTION_VALUES = frozenset(
    {ACTION_ALLOW, ACTION_LABEL_ONLY, ACTION_RETAIN_CASH, ACTION_BLOCK_NEW_RISK}
)

POLICY_VERSION = "portfolio_allocation.v1"


class AllocationPolicyError(ValueError):
    """A policy value is not usable, where guessing would change risk."""


def _decimal(value, *, default: Decimal | None = None) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        if default is None:
            raise AllocationPolicyError(f"{value!r} is not a decimal") from None
        return default
    if parsed.is_nan() or parsed.is_infinite():
        if default is None:
            raise AllocationPolicyError(f"{value!r} is not a finite decimal")
        return default
    return parsed


def _fraction(percent: Decimal) -> Decimal:
    """Percentage -> 0..1 fraction, clamped so a bad row cannot invert a limit."""
    return min(max(percent / HUNDRED, ZERO), ONE)


def _choice(value: str, *, name: str, default: str) -> str:
    chosen = (value or "").strip().lower()
    if chosen in ACTION_VALUES:
        return chosen
    logger.warning(
        "ALLOCATION_CONFIG[%r]=%r is not a policy action; using %r", name, value, default
    )
    return default


@dataclass(frozen=True)
class CostModel:
    """A declared, versioned transaction-cost assumption.

    ``linear_bps.v1`` is deliberately simple: proportional bps plus a fixed
    per-trade amount, floored at a minimum fee. It is an **estimate used for
    sizing**, not a broker fee. The execution guard records no fee today, so a
    planning estimate is never written into ``Transaction.fee``: that column's
    NULL/zero distinction is a Phase 3 contract and a guess would corrupt it.
    """

    version: str = "linear_bps.v1"
    bps: Decimal = Decimal("10")
    fixed_per_trade_usd: Decimal = ZERO
    minimum_fee_usd: Decimal = ZERO

    @classmethod
    def from_config(cls, config: dict | None = None) -> CostModel:
        config = dict(config if config is not None else getattr(settings, "ALLOCATION_CONFIG", {}))
        return cls(
            version=str(config.get("COST_MODEL", "linear_bps.v1")),
            bps=_decimal(config.get("COST_BPS", "10"), default=Decimal("10")),
            fixed_per_trade_usd=_decimal(config.get("FIXED_COST_USD", "0"), default=ZERO),
            minimum_fee_usd=_decimal(config.get("MINIMUM_FEE_USD", "0"), default=ZERO),
        )

    def estimate(self, side: str, notional_usd: Decimal) -> Decimal:
        """Estimated round-trip cost for one leg, rounded **up** to the cent.

        Rounding up is the conservative direction for funding: a plan that
        under-estimates a cost can spend cash it does not have, while one that
        over-estimates merely leaves a cent unused.
        """
        notional = max(notional_usd, ZERO)
        if notional <= ZERO:
            return ZERO
        proportional = notional * self.bps / Decimal("10000")
        raw = max(self.fixed_per_trade_usd + proportional, self.minimum_fee_usd)
        _ = side  # the model is currently symmetric; kept for a future asymmetric schedule
        return raw.quantize(CENT, rounding=ROUND_CEILING)

    def as_dict(self) -> dict:
        return {
            "model_version": self.version,
            "bps": str(self.bps),
            "fixed_per_trade_usd": str(self.fixed_per_trade_usd),
            "minimum_fee_usd": str(self.minimum_fee_usd),
            "source": "declared_assumption_not_broker_quote",
            "note": (
                "Costs are a planning estimate. Transaction.fee is left NULL because "
                "no broker fee was recorded; NULL is not zero."
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> CostModel:
        """Rebuild the exact declared cost model stored with a provider result."""
        data = dict(payload or {})
        return cls(
            version=str(data.get("model_version") or "linear_bps.v1"),
            bps=_decimal(data.get("bps", "10"), default=Decimal("10")),
            fixed_per_trade_usd=_decimal(data.get("fixed_per_trade_usd", "0"), default=ZERO),
            minimum_fee_usd=_decimal(data.get("minimum_fee_usd", "0"), default=ZERO),
        )


@dataclass(frozen=True)
class AllocationPolicy:
    """The enforceable constraints and evidence policy for one allocation.

    Construct with :meth:`from_portfolio`. The defaults come from
    ``settings.ALLOCATION_CONFIG`` and the portfolio's own limit columns; tests
    and the paper command pass ``overrides`` for scenario-specific values.
    """

    policy_version: str = POLICY_VERSION
    max_name_weight: Decimal = Decimal("0.30")
    max_sector_weight: Decimal = Decimal("0.40")
    max_total_exposure: Decimal = Decimal("0.90")
    min_cash_weight: Decimal = Decimal("0.10")
    max_turnover: Decimal = Decimal("0.25")
    max_trade_fraction_of_cash: Decimal = Decimal("0.05")
    min_trade_notional_usd: Decimal = Decimal("1")
    default_lot_size: Decimal = Decimal("1")
    lot_sizes: dict[str, Decimal] = field(default_factory=dict)
    covariance_min_observations: int = 20
    covariance_max_history_age_days: int = 7
    max_snapshot_age_seconds: int = 900
    investment_horizon_days: int = 30
    stale_snapshot_action: str = ACTION_BLOCK_NEW_RISK
    missing_correlation_action: str = ACTION_LABEL_ONLY
    insufficient_evidence_action: str = ACTION_RETAIN_CASH
    unknown_sector_action: str = ACTION_LABEL_ONLY
    max_candidate_cases: int = 20
    max_claims_per_case: int = 10

    @classmethod
    def from_portfolio(
        cls,
        portfolio: Portfolio,
        *,
        config: dict | None = None,
        overrides: dict | None = None,
    ) -> AllocationPolicy:
        config = dict(config if config is not None else getattr(settings, "ALLOCATION_CONFIG", {}))
        overrides = dict(overrides or {})

        max_name = _fraction(portfolio.max_ticker_concentration_pct)
        max_sector = _fraction(portfolio.max_sector_concentration_pct)
        max_total = _fraction(portfolio.max_portfolio_exposure_pct)
        # The stricter of the two: a 10% cash buffer and a 90% exposure ceiling
        # both imply at least 10% cash; whichever is larger binds.
        min_cash = max(_fraction(portfolio.min_cash_buffer_pct), ONE - max_total)

        values = {
            "max_name_weight": max_name,
            "max_sector_weight": max_sector,
            "max_total_exposure": max_total,
            "min_cash_weight": min_cash,
            "max_turnover": _fraction(portfolio.max_turnover_pct),
            "max_trade_fraction_of_cash": _fraction(portfolio.max_trade_allocation_pct),
            "min_trade_notional_usd": _decimal(
                config.get("MIN_TRADE_NOTIONAL_USD", "1"), default=Decimal("1")
            ),
            "default_lot_size": _decimal(config.get("DEFAULT_LOT_SIZE", "1"), default=Decimal("1")),
            "covariance_min_observations": int(config.get("COVARIANCE_MIN_OBSERVATIONS", 20)),
            "covariance_max_history_age_days": int(
                config.get("COVARIANCE_MAX_HISTORY_AGE_DAYS", 7)
            ),
            "max_snapshot_age_seconds": int(config.get("MAX_SNAPSHOT_AGE_SECONDS", 900)),
            "investment_horizon_days": int(config.get("INVESTMENT_HORIZON_DAYS", 30)),
            "stale_snapshot_action": _choice(
                config.get("STALE_SNAPSHOT_POLICY", ACTION_BLOCK_NEW_RISK),
                name="STALE_SNAPSHOT_POLICY",
                default=ACTION_BLOCK_NEW_RISK,
            ),
            "missing_correlation_action": _choice(
                config.get("MISSING_CORRELATION_POLICY", ACTION_LABEL_ONLY),
                name="MISSING_CORRELATION_POLICY",
                default=ACTION_LABEL_ONLY,
            ),
            "insufficient_evidence_action": _choice(
                config.get("INSUFFICIENT_EVIDENCE_POLICY", ACTION_RETAIN_CASH),
                name="INSUFFICIENT_EVIDENCE_POLICY",
                default=ACTION_RETAIN_CASH,
            ),
            "unknown_sector_action": _choice(
                config.get("UNKNOWN_SECTOR_POLICY", ACTION_LABEL_ONLY),
                name="UNKNOWN_SECTOR_POLICY",
                default=ACTION_LABEL_ONLY,
            ),
            "max_candidate_cases": int(config.get("MAX_CANDIDATE_CASES", 20)),
            "max_claims_per_case": int(config.get("MAX_CLAIMS_PER_CASE", 10)),
        }
        for key, value in overrides.items():
            if key == "lot_sizes":
                values[key] = {str(k).upper(): _decimal(v) for k, v in dict(value).items()}
            elif key in values:
                values[key] = _decimal(value) if isinstance(values[key], Decimal) else value
            else:
                raise AllocationPolicyError(f"Unknown allocation policy override: {key!r}")

        policy = cls(**values)
        policy.validate()
        return policy

    def validate(self) -> None:
        for name in (
            "max_name_weight",
            "max_sector_weight",
            "max_total_exposure",
            "min_cash_weight",
            "max_turnover",
            "max_trade_fraction_of_cash",
        ):
            value = getattr(self, name)
            if value < ZERO or value > ONE:
                raise AllocationPolicyError(f"{name}={value} must be between 0 and 1")
        if self.min_cash_weight + self.max_total_exposure < ONE - WEIGHT_QUANTUM:
            # This can only happen through an override; make it visible rather
            # than let the two constraints disagree silently.
            raise AllocationPolicyError(
                "min_cash_weight and max_total_exposure are inconsistent: together they "
                f"must cover 1.0 (got {self.min_cash_weight} + {self.max_total_exposure})"
            )
        if self.default_lot_size <= ZERO:
            raise AllocationPolicyError("default_lot_size must be positive")
        if self.covariance_min_observations < 2:
            raise AllocationPolicyError("covariance_min_observations must be at least 2")
        if self.max_candidate_cases < 0 or self.max_claims_per_case < 0:
            raise AllocationPolicyError("candidate/claim caps must be non-negative")

    def lot_size_for(self, ticker: str) -> Decimal:
        return self.lot_sizes.get((ticker or "").upper(), self.default_lot_size)

    def as_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "limits": {
                "max_name_weight": str(self.max_name_weight),
                "max_sector_weight": str(self.max_sector_weight),
                "max_total_exposure": str(self.max_total_exposure),
                "min_cash_weight": str(self.min_cash_weight),
                "max_turnover": str(self.max_turnover),
                "max_trade_fraction_of_cash": str(self.max_trade_fraction_of_cash),
            },
            "execution": {
                "min_trade_notional_usd": str(self.min_trade_notional_usd),
                "default_lot_size": str(self.default_lot_size),
                "lot_sizes": {k: str(v) for k, v in sorted(self.lot_sizes.items())},
                "price_quantum": str(CENT),
            },
            "risk_statistics": {
                "covariance_min_observations": self.covariance_min_observations,
                "covariance_max_history_age_days": self.covariance_max_history_age_days,
            },
            "evidence_policy": {
                "stale_snapshot": self.stale_snapshot_action,
                "missing_correlation_history": self.missing_correlation_action,
                "insufficient_evidence": self.insufficient_evidence_action,
                "unknown_sector": self.unknown_sector_action,
            },
            "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
            "investment_horizon_days": self.investment_horizon_days,
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> AllocationPolicy:
        """Rebuild the policy that was in force when a provider result was saved.

        A resume must not silently adopt today's portfolio limits: a limit moved
        after the call would change what the saved response is allowed to do.
        The stored policy is the authority for the resumed plan.
        """
        data = dict(payload or {})
        limits = dict(data.get("limits") or {})
        execution = dict(data.get("execution") or {})
        risk = dict(data.get("risk_statistics") or {})
        evidence = dict(data.get("evidence_policy") or {})
        defaults = cls()
        policy = cls(
            policy_version=str(data.get("policy_version") or POLICY_VERSION),
            max_name_weight=_decimal(
                limits.get("max_name_weight"), default=defaults.max_name_weight
            ),
            max_sector_weight=_decimal(
                limits.get("max_sector_weight"), default=defaults.max_sector_weight
            ),
            max_total_exposure=_decimal(
                limits.get("max_total_exposure"), default=defaults.max_total_exposure
            ),
            min_cash_weight=_decimal(
                limits.get("min_cash_weight"), default=defaults.min_cash_weight
            ),
            max_turnover=_decimal(limits.get("max_turnover"), default=defaults.max_turnover),
            max_trade_fraction_of_cash=_decimal(
                limits.get("max_trade_fraction_of_cash"),
                default=defaults.max_trade_fraction_of_cash,
            ),
            min_trade_notional_usd=_decimal(
                execution.get("min_trade_notional_usd"), default=defaults.min_trade_notional_usd
            ),
            default_lot_size=_decimal(
                execution.get("default_lot_size"), default=defaults.default_lot_size
            ),
            lot_sizes={
                str(ticker).upper(): _decimal(weight)
                for ticker, weight in dict(execution.get("lot_sizes") or {}).items()
            },
            covariance_min_observations=int(
                risk.get("covariance_min_observations", defaults.covariance_min_observations)
            ),
            covariance_max_history_age_days=int(
                risk.get(
                    "covariance_max_history_age_days", defaults.covariance_max_history_age_days
                )
            ),
            max_snapshot_age_seconds=int(
                data.get("max_snapshot_age_seconds", defaults.max_snapshot_age_seconds)
            ),
            investment_horizon_days=int(
                data.get("investment_horizon_days", defaults.investment_horizon_days)
            ),
            stale_snapshot_action=_choice(
                evidence.get("stale_snapshot", defaults.stale_snapshot_action),
                name="stale_snapshot",
                default=defaults.stale_snapshot_action,
            ),
            missing_correlation_action=_choice(
                evidence.get("missing_correlation_history", defaults.missing_correlation_action),
                name="missing_correlation_history",
                default=defaults.missing_correlation_action,
            ),
            insufficient_evidence_action=_choice(
                evidence.get("insufficient_evidence", defaults.insufficient_evidence_action),
                name="insufficient_evidence",
                default=defaults.insufficient_evidence_action,
            ),
            unknown_sector_action=_choice(
                evidence.get("unknown_sector", defaults.unknown_sector_action),
                name="unknown_sector",
                default=defaults.unknown_sector_action,
            ),
        )
        policy.validate()
        return policy
