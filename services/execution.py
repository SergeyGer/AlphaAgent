"""Trade execution: the guard and the only database-mutating path.

This module is deliberately free of Celery imports so the REST API, the
Telegram bot, the allocation service and the tests can all reach the *same*
implementation.

Two functions, two jobs:

* :func:`evaluate_proposal` is a **read-only simulation**. It is pure, performs
  no I/O, and an approved result from it is not an authorisation.
* :func:`execute_trade` is the authoritative path. It locks the portfolio and
  every position row, rebuilds cash/holdings/exposure, reruns the current policy
  and daily-loss checks against current marks, verifies the result is still the
  approved decision, and only then writes the ledger - in one transaction.

There is exactly one way for a proposal to become a ledger row, and it goes
through :func:`execute_trade` (or :func:`apply_trade`, which delegates to it and
revalidates rather than trusting a hand-built ``GuardDecision``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from core.decision_json import content_hash, json_safe
from core.models import (
    Asset,
    ExecutedBy,
    InstrumentClassification,
    Portfolio,
    Transaction,
)
from services.execution_types import (
    ZERO,
    ExecutionConflict,
    ExecutionError,
    ExecutionRejected,
    GuardDecision,
    GuardError,
    MaterialChangeError,
    PolicyChangedError,
    ValuationUnavailableError,
)
from services.quote_policy import (
    ESTIMATION_MODES,
    ExecutionFreshnessPolicy,
    is_estimate_source,
    validate_mark,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing CrewAI
    from ai_agent import TradeProposal
    from services.ledger import PositionState

# Deliberately no module-level logger: the guard is a pure function and does
# not log. A module-level `logger` here was flagged as py/unused-global-variable,
# which was correct.
# performs no I/O, so it has nothing to log. Callers log the reasons it returns.
# (An unused logger here was flagged as py/unused-global-variable.)

__all__ = [
    "CENT",
    "SATOSHI",
    "ZERO",
    "ExecutionConflict",
    "ExecutionError",
    "ExecutionRejected",
    "ExecutionRequest",
    "ExecutionResult",
    "Exposure",
    "GuardDecision",
    "GuardError",
    "Mark",
    "MaterialChangeError",
    "PolicyChangedError",
    "ValuationUnavailableError",
    "apply_trade",
    "evaluate_proposal",
    "execute_trade",
    "normalise_marks",
    "portfolio_policy_version",
]

HUNDRED = Decimal("100")
CENT = Decimal("0.01")
SATOSHI = Decimal("0.00000001")


# ---------------------------------------------------------------------------
# Execution guard
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Exposure:
    """Market values the guard needs to judge portfolio-level limits.

    The per-trade ceiling answers "is this trade too big on its own?". It cannot
    answer "is the portfolio already full?", because a sequence of individually
    permitted trades is unbounded - ADR-0001 says so in its own consequences.
    Twenty buys each inside a 5% ceiling reach 100% invested, and no single one of
    them looked wrong.

    Passed in rather than fetched, so ``evaluate_proposal`` stays free of I/O -
    which is what makes the safety-critical code testable without a database.

    ``equity_usd`` is the denominator for both ratios. When it is zero or absent
    the portfolio-level checks are skipped rather than guessed at: a percentage of
    an unknown total is not a limit, it is a number.
    """

    positions_value_usd: Decimal = ZERO
    #: This ticker's holding, at market, before the proposed trade.
    ticker_value_usd: Decimal = ZERO
    equity_usd: Decimal = ZERO
    #: Market value already held in the traded instrument's classified sector.
    #: ``None`` means "not known", not zero: an unclassified instrument must not
    #: silently pass a sector ceiling.
    sector_value_usd: Decimal | None = None
    sector_label: str = ""

    @property
    def usable(self) -> bool:
        return self.equity_usd > ZERO


def _portfolio_headroom(
    portfolio: Portfolio, exposure: Exposure | None, price: Decimal
) -> tuple[Decimal, str | None]:
    """How much more this portfolio may buy, in dollars.

    Returns the binding dollar ceiling across both portfolio-level limits, plus a
    note template naming the constraint that produced it - ``None`` when neither
    limit applies, so the caller falls back to reporting the per-trade ceiling.

    Both limits are expressed as percentages of equity, so the headroom is
    ``limit_value - current_value``. When two limits bind, the smaller wins and
    the note names that one; a clamp that does not say which rule it obeyed is
    harder to audit than no note at all.
    """
    if exposure is None or not exposure.usable:
        # No exposure means the portfolio-level limits cannot be evaluated. The
        # per-trade ceiling still applies in full, so this degrades to the
        # behaviour that existed before these limits - not to no limits at all.
        return Decimal("Infinity"), None

    equity = exposure.equity_usd
    ceilings: list[tuple[Decimal, str]] = []

    exposure_limit = portfolio.max_portfolio_exposure_pct
    if exposure_limit > 0:
        allowed_positions = (equity * exposure_limit / HUNDRED).quantize(CENT)
        ceilings.append(
            (
                max(allowed_positions - exposure.positions_value_usd, ZERO),
                "Clamped to {clamped} units: this would take invested capital past "
                f"the {exposure_limit}% portfolio exposure ceiling.",
            )
        )

    concentration_limit = portfolio.max_ticker_concentration_pct
    if concentration_limit > 0:
        allowed_ticker = (equity * concentration_limit / HUNDRED).quantize(CENT)
        ceilings.append(
            (
                max(allowed_ticker - exposure.ticker_value_usd, ZERO),
                "Clamped to {clamped} units: this would take the position past the "
                f"{concentration_limit}% single-instrument concentration ceiling.",
            )
        )

    sector_limit = portfolio.max_sector_concentration_pct
    if sector_limit > 0 and exposure.sector_value_usd is not None:
        allowed_sector = (equity * sector_limit / HUNDRED).quantize(CENT)
        ceilings.append(
            (
                max(allowed_sector - exposure.sector_value_usd, ZERO),
                "Clamped to {clamped} units: this would take the "
                f"{exposure.sector_label or 'classified'} sector past the "
                f"{sector_limit}% sector concentration ceiling.",
            )
        )

    if not ceilings:
        return Decimal("Infinity"), None

    headroom, note = min(ceilings, key=lambda pair: pair[0])
    # `price` is unused here but kept in the signature: callers pass it, and a
    # future limit expressed in units rather than dollars will need it.
    _ = price
    return headroom, note


def evaluate_proposal(
    portfolio: Portfolio,
    proposal: TradeProposal,
    price: Decimal | None,
    position: PositionState,
    realised_pnl_today: Decimal,
    *,
    exposure: Exposure | None = None,
) -> GuardDecision:
    """Apply every pre-trade guardrail. Pure function - performs no I/O.

    Enforced rules
    --------------
    1. ``HOLD`` never reaches the database.
    2. A live price is mandatory for any execution.
    3. ``notional = amount * price`` must fit ``max_trade_allocation_pct`` of cash
       (auto-clamped down when the model overshoots; rejected if nothing fits).
    4. BUY cannot exceed available cash; SELL cannot exceed the held position.
    5. Once ``daily_loss_limit_usd`` is breached, new risk (BUY) is blocked while
       de-risking (SELL) stays available.
    6. BUY cannot push total invested capital past ``max_portfolio_exposure_pct``
       of equity.
    7. BUY cannot push one instrument past ``max_ticker_concentration_pct`` of
       equity.

    Rules 6 and 7 need ``exposure`` and are skipped without it. That is a real
    gap rather than a graceful default, so the production caller is asserted to
    supply it by ``ExposureWiringTests`` - a new caller that forgets would
    silently lose the cumulative limits while every per-trade check still passed.
    """
    action = proposal.action
    decision = GuardDecision(approved=False, reason="", action=action, price=price)

    if action == "HOLD":
        decision.reason = "HOLD - no execution required."
        return decision

    if price is None or price <= 0:
        decision.reason = "Rejected: no reliable market price available."
        return decision

    try:
        amount = Decimal(str(proposal.amount)).quantize(SATOSHI, rounding=ROUND_DOWN)
    except Exception:
        decision.reason = f"Rejected: invalid amount {proposal.amount!r}."
        return decision

    decision.requested_amount = amount
    if amount <= 0:
        decision.reason = f"Rejected: {action} requires a positive amount."
        return decision

    # --- Rule 3: per-trade allocation ceiling ----------------------------
    budget = portfolio.max_trade_budget_usd
    if budget <= 0:
        decision.reason = (
            "Rejected: max_trade_allocation_pct resolves to a $0 budget "
            f"({portfolio.max_trade_allocation_pct}% of ${portfolio.balance_usd})."
        )
        return decision

    if action == "BUY":
        # --- Rule 5: portfolio stop-loss ---------------------------------
        loss_limit = portfolio.daily_loss_limit_usd
        if loss_limit > 0 and realised_pnl_today <= -loss_limit:
            decision.reason = (
                f"Blocked by daily loss limit: realised P&L today "
                f"${realised_pnl_today} breaches -${loss_limit}. New risk is frozen."
            )
            return decision

        # --- Rules 6 and 7: portfolio-level headroom ---------------------
        # Folded into the same ceiling as the per-trade limit rather than added as
        # a separate rejection path. A buy that does not fit the portfolio should
        # be sized down to what fits, exactly as one that does not fit the
        # per-trade budget is - rejecting outright would refuse trades the
        # portfolio can comfortably afford, just not at the size proposed.
        exposure_ceiling, exposure_note = _portfolio_headroom(portfolio, exposure, price)

        max_affordable = min(budget, portfolio.balance_usd, exposure_ceiling)
        notional = (amount * price).quantize(CENT)
        if notional > max_affordable:
            clamped_amount = (max_affordable / price).quantize(SATOSHI, rounding=ROUND_DOWN)
            if clamped_amount <= 0:
                # Name the limit that actually left no room. This message used to
                # say "per-trade ceiling" unconditionally, so a portfolio with a
                # $500 per-trade budget that was refused for being at its
                # concentration ceiling was told the per-trade ceiling refused it -
                # sending the reader to the wrong limit entirely, in the one place
                # they are most likely to be reading carefully.
                if exposure_note and max_affordable == exposure_ceiling:
                    # Reuse the clamping note's wording, minus its "Clamped to N
                    # units" prefix, so a refusal and a clamp describe the same
                    # ceiling in the same words. Joined with a colon rather than a
                    # full stop because the note's clause begins mid-sentence.
                    detail = exposure_note.format(clamped=0).split(": ", 1)[-1]
                    decision.reason = (
                        f"Rejected: requested ${notional} cannot be sized down: {detail}"
                    )
                else:
                    decision.reason = (
                        f"Rejected: requested ${notional} exceeds the ${max_affordable} "
                        "per-trade ceiling and cannot be sized down."
                    )
                return decision
            decision.clamped = True
            if exposure_note and max_affordable == exposure_ceiling:
                # Name the binding constraint. "Clamped to respect the ceiling"
                # without saying which ceiling sends the reader to the wrong one.
                decision.notes.append(exposure_note.format(clamped=clamped_amount))
            else:
                decision.notes.append(
                    f"Clamped {amount} -> {clamped_amount} units to respect the "
                    f"${max_affordable} ceiling "
                    f"(max_trade_allocation_pct={portfolio.max_trade_allocation_pct}%)."
                )
            amount = clamped_amount
            notional = (amount * price).quantize(CENT)

        if notional > portfolio.balance_usd:
            decision.reason = (
                f"Rejected: notional ${notional} exceeds cash balance ${portfolio.balance_usd}."
            )
            return decision

        decision.approved = True
        decision.amount = amount
        decision.notional = notional
        decision.reason = f"Approved BUY of {amount} {position.ticker}."
        return decision

    # --- SELL -------------------------------------------------------------
    if position.amount <= 0:
        decision.reason = f"Rejected: no open position in {position.ticker} to sell."
        return decision

    if amount > position.amount:
        decision.clamped = True
        decision.notes.append(f"Clamped SELL {amount} -> {position.amount} units (position size).")
        amount = position.amount

    notional = (amount * price).quantize(CENT)
    if amount <= 0 or notional <= 0:
        decision.reason = "Rejected: SELL resolves to a zero quantity."
        return decision

    decision.approved = True
    decision.amount = amount
    decision.notional = notional
    decision.reason = f"Approved SELL of {amount} {position.ticker}."
    return decision


# ---------------------------------------------------------------------------
# Authoritative execution (FIX-02)
#
# ``evaluate_proposal`` is a pure read-only simulation. It is deliberately not
# enough to execute: the state it read can change before the ledger write. The
# functions below take the portfolio and position rows under a lock, rebuild the
# state from the locked rows, run the *same* guard again against current policy
# and marks, and only then mutate the ledger - all inside one transaction.
#
# A ``GuardDecision`` is no longer an authorisation. It is a record of what one
# evaluation concluded. ``execute_trade`` cannot be handed an approved-looking
# decision and asked to write it: it re-derives the decision itself and refuses
# if the answer differs from the one the caller approved.
# ---------------------------------------------------------------------------
#: Portfolio columns that decide permission. A change to any of them between
#: approval and execution invalidates the approval.
POLICY_FIELDS: tuple[str, ...] = (
    "max_portfolio_exposure_pct",
    "max_ticker_concentration_pct",
    "max_sector_concentration_pct",
    "max_turnover_pct",
    "min_cash_buffer_pct",
    "max_trade_allocation_pct",
    "daily_loss_limit_usd",
    "risk_profile",
)


def portfolio_policy_version(portfolio: Portfolio) -> str:
    """A stable version of the limits that decide whether a trade is permitted."""
    payload = {name: str(getattr(portfolio, name, "")) for name in POLICY_FIELDS}
    return f"policy-{content_hash(payload)[:24]}"


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if parsed.is_nan() or parsed.is_infinite():
        return None
    return parsed


@dataclass(frozen=True)
class Mark:
    """One price observation and its provenance.

    ``as_of`` is the market observation instant (UTC), ``retrieved_at`` when this
    process obtained it, and ``source`` names the producer. ``as_of=None`` is a
    real, explicit state: the value exists but nobody recorded when it was
    observed. The freshness policy decides whether that is acceptable for the
    mode in which it is used.
    """

    price: Decimal
    as_of: datetime | None = None
    source: str = "caller_supplied"
    ticker: str = ""
    currency: str = "USD"
    retrieved_at: datetime | None = None
    is_estimate: bool = False

    @property
    def observed_at(self) -> datetime | None:
        return self.as_of

    def as_dict(self) -> dict:
        return {
            "price": str(self.price),
            "source": self.source,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "currency": self.currency,
            "ticker": self.ticker,
            "is_estimate": self.is_estimate,
        }


def normalise_marks(
    quotes: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Mark]:
    """Parse a quote map into strictly positive, finite marks with provenance.

    Accepted shapes: a :class:`Mark`, a market-data ``PriceQuote``, a mapping
    with ``price``/``as_of``/``source``/``retrieved_at``/``currency``, or a bare
    decimal value. A bare value carries no observation time; it is labelled
    ``caller_supplied_untimed`` and is *not* given a fabricated timestamp. A
    future-dated mark is refused here; a stale or untimed one is refused by the
    freshness policy where current valuation is required.
    """
    moment = now or timezone.now()
    parsed: dict[str, Mark] = {}
    for ticker, value in (quotes or {}).items():
        symbol = str(ticker).strip().upper()
        if not symbol:
            raise ValuationUnavailableError("A quote was supplied for an empty ticker.")
        raw_price: Any
        as_of: datetime | None
        retrieved_at: datetime | None
        source: str
        currency: str = "USD"
        explicit_estimate = False
        if isinstance(value, Mark):
            raw_price, as_of, source = value.price, value.as_of, value.source
            retrieved_at = value.retrieved_at
            currency = value.currency
            explicit_estimate = value.is_estimate
        elif isinstance(value, Mapping):
            raw_price = value.get("price")
            as_of = value.get("as_of") or value.get("quote_time")
            retrieved_at = value.get("retrieved_at")
            source = str(value.get("source") or "caller_supplied")
            currency = str(value.get("currency") or "USD")
            explicit_estimate = bool(value.get("is_estimate") or value.get("estimated"))
        elif hasattr(value, "price"):
            raw_price = getattr(value, "price", None)
            as_of = getattr(value, "as_of", None)
            retrieved_at = getattr(value, "retrieved_at", None)
            source = str(getattr(value, "source", "") or "caller_supplied")
            currency = str(getattr(value, "currency", "") or "USD")
        else:
            raw_price, as_of, retrieved_at = value, None, moment
            source = "caller_supplied_untimed"
        price = _as_decimal(raw_price)
        if price is None or price <= ZERO:
            raise ValuationUnavailableError(
                f"Quote for {symbol} is not a finite, positive price: {raw_price!r}.",
                code="invalid_quote",
            )
        as_of = _parse_timestamp(as_of, symbol, "as_of")
        retrieved_at = _parse_timestamp(retrieved_at, symbol, "retrieved_at")
        if as_of is not None and as_of > moment:
            raise ValuationUnavailableError(
                f"Quote for {symbol} is timestamped in the future ({as_of.isoformat()}).",
                code="future_quote",
            )
        parsed[symbol] = Mark(
            price=price,
            as_of=as_of,
            source=source,
            ticker=symbol,
            currency=currency,
            retrieved_at=retrieved_at or moment,
            is_estimate=explicit_estimate or is_estimate_source(source),
        )
    return parsed


def _parse_timestamp(value: Any, symbol: str, field: str) -> datetime | None:
    """Parse a UTC timestamp; a missing value stays missing."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValuationUnavailableError(
                f"Quote {field} for {symbol} is not ISO-8601: {value!r}.",
                code="invalid_quote",
            ) from None
    if not isinstance(value, datetime):
        raise ValuationUnavailableError(
            f"Quote {field} for {symbol} is not a timestamp: {value!r}.",
            code="invalid_quote",
        )
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _quote_age_seconds(mark: Mark, *, now: datetime) -> Decimal | None:
    if mark.as_of is None:
        return None
    return Decimal(str(max((now - mark.as_of).total_seconds(), 0.0)))


def _max_quote_age_seconds(requested: int | None = None) -> int:
    if requested is not None:
        return max(int(requested), 0)
    return ExecutionFreshnessPolicy.from_settings().default_max_age_seconds


def _max_price_drift_pct(requested: Decimal | None = None) -> Decimal:
    return ExecutionFreshnessPolicy.from_settings().drift_tolerance(requested)


def _validate_required_mark(
    mark: Mark,
    ticker: str,
    *,
    now: datetime,
    mode: str,
    require_current: bool,
    risk_reducing: bool = False,
    policy: ExecutionFreshnessPolicy | None = None,
) -> Any:
    """Apply the shared freshness policy and raise with a stable code."""
    result = validate_mark(
        ticker=ticker,
        price=mark.price,
        as_of=mark.as_of,
        retrieved_at=mark.retrieved_at,
        source=mark.source,
        now=now,
        mode=mode,
        require_current=require_current,
        risk_reducing=risk_reducing,
        policy=policy,
    )
    if not result.ok:
        raise ValuationUnavailableError(result.detail, code=result.code)
    return result


@dataclass
class _LockedState:
    portfolio: Portfolio
    assets: dict[str, Asset]
    marks: dict[str, Mark]
    positions_value_usd: Decimal
    equity_usd: Decimal | None
    unpriced_tickers: list[str]
    sector_values: dict[str, Decimal]

    def exposure_for(self, ticker: str) -> Exposure:
        wanted = ticker.upper()
        asset = self.assets.get(wanted)
        ticker_value = ZERO
        if asset is not None:
            mark = self.marks.get(wanted)
            if mark is not None:
                ticker_value = (asset.amount * mark.price).quantize(CENT)
        sector_value = None
        sector_label = ""
        sector = self._sector_of(wanted)
        if sector:
            sector_label = sector
            sector_value = self.sector_values.get(sector, ZERO)
        return Exposure(
            positions_value_usd=self.positions_value_usd,
            ticker_value_usd=ticker_value,
            equity_usd=self.equity_usd or ZERO,
            sector_value_usd=sector_value,
            sector_label=sector_label,
        )

    def _sector_of(self, ticker: str) -> str | None:
        return self._classifications.get(ticker.upper())

    _classifications: dict[str, str] = field(default_factory=dict)


def _lock_portfolio_state(
    portfolio: Portfolio,
    marks: Mapping[str, Mark],
    *,
    require_full_valuation: bool,
) -> _LockedState:
    """Lock and rebuild holdings, cash and exposure from the database rows.

    Portfolio and every position row are locked before any number is read, so
    two executions for the same portfolio cannot both evaluate a stale balance.
    """
    assets = {
        asset.ticker.upper(): asset
        for asset in Asset.objects.select_for_update()
        .filter(portfolio=portfolio)
        .order_by("ticker")
    }
    classifications = {
        row.ticker.upper(): row.sector
        for row in InstrumentClassification.objects.filter(portfolio=portfolio)
    }
    positions_value = ZERO
    sector_values: dict[str, Decimal] = {}
    unpriced: list[str] = []
    for symbol, asset in assets.items():
        if asset.amount <= ZERO:
            continue
        mark = marks.get(symbol)
        if mark is None:
            unpriced.append(symbol)
            continue
        value = (asset.amount * mark.price).quantize(CENT)
        positions_value += value
        sector = classifications.get(symbol)
        if sector:
            sector_values[sector] = sector_values.get(sector, ZERO) + value
    positions_value = positions_value.quantize(CENT)
    if require_full_valuation and unpriced:
        raise ValuationUnavailableError(
            "No current quote for material holding(s) "
            + ", ".join(sorted(unpriced))
            + "; authoritative exposure cannot be computed."
        )
    equity: Decimal | None = None
    if not unpriced:
        equity = (portfolio.balance_usd + positions_value).quantize(CENT)
    state = _LockedState(
        portfolio=portfolio,
        assets=assets,
        marks=dict(marks),
        positions_value_usd=positions_value,
        equity_usd=equity,
        unpriced_tickers=sorted(unpriced),
        sector_values=sector_values,
    )
    state._classifications = classifications
    return state


def _binding_rule(
    portfolio: Portfolio,
    decision: GuardDecision,
    exposure: Exposure | None,
    price: Decimal,
) -> str:
    """Name the single tightest ceiling for a clamped decision."""
    requested_notional = (decision.requested_amount * price).quantize(CENT)
    candidates: list[tuple[Decimal, str]] = [
        (
            max(portfolio.max_trade_budget_usd - requested_notional, ZERO),
            "max_trade_allocation_pct",
        ),
        (max(portfolio.balance_usd - requested_notional, ZERO), "cash_available"),
    ]
    if exposure is not None and exposure.usable:
        equity = exposure.equity_usd
        if portfolio.max_portfolio_exposure_pct > 0:
            headroom = (
                equity * portfolio.max_portfolio_exposure_pct / HUNDRED
                - exposure.positions_value_usd
            )
            candidates.append(
                (max(headroom - requested_notional, ZERO), "max_portfolio_exposure_pct")
            )
        if portfolio.max_ticker_concentration_pct > 0:
            headroom = (
                equity * portfolio.max_ticker_concentration_pct / HUNDRED
                - exposure.ticker_value_usd
            )
            candidates.append(
                (max(headroom - requested_notional, ZERO), "max_ticker_concentration_pct")
            )
        if portfolio.max_sector_concentration_pct > 0 and exposure.sector_value_usd is not None:
            headroom = (
                equity * portfolio.max_sector_concentration_pct / HUNDRED
                - exposure.sector_value_usd
            )
            candidates.append(
                (max(headroom - requested_notional, ZERO), "max_sector_concentration_pct")
            )
    if not decision.clamped:
        return ""
    _amount, rule = min(candidates, key=lambda pair: pair[0])
    return rule


def _check(
    rule_code: str,
    outcome: str,
    *,
    threshold: Any = None,
    actual: Any = None,
    unit: str = "",
) -> dict:
    return {
        "rule_code": rule_code,
        "outcome": outcome,
        "threshold": str(threshold) if threshold is not None else None,
        "actual": str(actual) if actual is not None else None,
        "unit": unit or None,
    }


def build_guard_checks(
    portfolio: Portfolio,
    decision: GuardDecision,
    *,
    price: Decimal | None,
    exposure: Exposure | None,
    realised_pnl_today: Decimal,
    position_amount: Decimal,
    requested_quantity: Decimal,
) -> list[dict]:
    """Numerical results of every rule, including the ones not evaluated.

    ``not_evaluated`` is a real outcome: a check the guard could not run is not
    a pass, and painting it as one would turn an unknown into a clean bill.
    """
    checks: list[dict] = []
    if decision.action == "HOLD":
        checks.append(_check("hold_no_execution", "pass", actual=0, unit="quantity"))
        return checks
    if price is None or price <= ZERO:
        checks.append(_check("price_available", "fail", actual=price, unit="USD"))
        return checks
    checks.append(_check("price_available", "pass", actual=price, unit="USD"))
    requested_notional = (requested_quantity * price).quantize(CENT)
    budget = portfolio.max_trade_budget_usd
    checks.append(
        _check(
            "max_trade_allocation_pct",
            "pass" if requested_notional <= budget else "fail",
            threshold=budget,
            actual=requested_notional,
            unit="USD",
        )
    )
    if decision.action == "BUY":
        checks.append(
            _check(
                "cash_available",
                "pass" if requested_notional <= portfolio.balance_usd else "fail",
                threshold=portfolio.balance_usd,
                actual=requested_notional,
                unit="USD",
            )
        )
        loss_limit = portfolio.daily_loss_limit_usd
        if loss_limit > 0:
            checks.append(
                _check(
                    "daily_loss_limit_usd",
                    "pass" if realised_pnl_today > -loss_limit else "fail",
                    threshold=loss_limit,
                    actual=abs(realised_pnl_today),
                    unit="USD",
                )
            )
        else:
            checks.append(_check("daily_loss_limit_usd", "not_evaluated", threshold=0, unit="USD"))
        if exposure is not None and exposure.usable:
            equity = exposure.equity_usd
            if portfolio.max_portfolio_exposure_pct > 0:
                threshold = (equity * portfolio.max_portfolio_exposure_pct / HUNDRED).quantize(CENT)
                actual = exposure.positions_value_usd + requested_notional
                checks.append(
                    _check(
                        "max_portfolio_exposure_pct",
                        "pass" if actual <= threshold else "fail",
                        threshold=threshold,
                        actual=actual,
                        unit="USD",
                    )
                )
            else:
                checks.append(_check("max_portfolio_exposure_pct", "not_evaluated", threshold=0))
            if portfolio.max_ticker_concentration_pct > 0:
                threshold = (equity * portfolio.max_ticker_concentration_pct / HUNDRED).quantize(
                    CENT
                )
                actual = exposure.ticker_value_usd + requested_notional
                checks.append(
                    _check(
                        "max_ticker_concentration_pct",
                        "pass" if actual <= threshold else "fail",
                        threshold=threshold,
                        actual=actual,
                        unit="USD",
                    )
                )
            else:
                checks.append(_check("max_ticker_concentration_pct", "not_evaluated", threshold=0))
            if portfolio.max_sector_concentration_pct > 0:
                if exposure.sector_value_usd is None:
                    checks.append(
                        _check(
                            "max_sector_concentration_pct",
                            "not_evaluated",
                            threshold=portfolio.max_sector_concentration_pct,
                            unit="USD",
                        )
                    )
                else:
                    threshold = (
                        equity * portfolio.max_sector_concentration_pct / HUNDRED
                    ).quantize(CENT)
                    actual = exposure.sector_value_usd + requested_notional
                    checks.append(
                        _check(
                            "max_sector_concentration_pct",
                            "pass" if actual <= threshold else "fail",
                            threshold=threshold,
                            actual=actual,
                            unit="USD",
                        )
                    )
            else:
                checks.append(_check("max_sector_concentration_pct", "not_evaluated", threshold=0))
    else:  # SELL
        checks.append(
            _check(
                "position_available",
                "pass" if requested_quantity <= position_amount else "fail",
                threshold=position_amount,
                actual=requested_quantity,
                unit="quantity",
            )
        )
    if decision.reason_codes:
        for code in decision.reason_codes:
            checks.append(_check(code, "fail", actual=decision.reason))
    return checks


def _request_fingerprint(payload: Mapping[str, Any]) -> str:
    return content_hash(json_safe(dict(payload)))


@dataclass
class ExecutionRequest:
    """One intent to execute, with the identity that makes retries safe."""

    portfolio_id: int
    ticker: str
    side: str
    quantity: Decimal
    quotes: Mapping[str, Any]
    execution_key: str = ""
    source: str = "direct"
    approval_ref: str = ""
    approved_policy_version: str | None = None
    approved_portfolio_version: str | None = None
    approved_price: Decimal | None = None
    approved_quantity: Decimal | None = None
    quote_time: datetime | None = None
    quote_source: str = ""
    quote_retrieved_at: datetime | None = None
    mode: str = "live"
    max_quote_age_seconds: int | None = None
    require_full_valuation: bool = True
    price_tolerance_pct: Decimal | None = None
    quantity_tolerance: Decimal = SATOSHI
    #: The amount the caller originally requested, before the read-only guard
    #: simulation clamped it. Kept separate from ``quantity`` (the approved
    #: amount) so the persisted guard record can show what was asked for.
    requested_quantity: Decimal | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ticker = (self.ticker or "").strip().upper()
        self.side = (self.side or "").strip().upper()
        self.execution_key = (self.execution_key or "").strip()
        self.quantity = _as_decimal(self.quantity) or ZERO
        self.requested_quantity = _as_decimal(self.requested_quantity)
        self.approved_price = _as_decimal(self.approved_price)
        self.approved_quantity = _as_decimal(self.approved_quantity)
        self.mode = (self.mode or "live").strip().lower()

    def fingerprint(self) -> str:
        """Canonical hash of the intent. Quote values are environment, not intent."""
        return _request_fingerprint(
            {
                "schema": "execution_request.v1",
                "portfolio_id": self.portfolio_id,
                "ticker": self.ticker,
                "side": self.side,
                "quantity": str(self.quantity),
                "execution_key": self.execution_key,
                "source": self.source,
                "approval_ref": self.approval_ref,
                "approved_price": str(self.approved_price) if self.approved_price else None,
                "mode": self.mode,
            }
        )


@dataclass
class ExecutionResult:
    approved: bool
    decision: GuardDecision
    request: ExecutionRequest
    transaction: Transaction | None = None
    already_executed: bool = False
    reason: str = ""
    policy_version: str = ""
    evaluated_portfolio_version: str = ""
    quote_time: datetime | None = None
    #: The durable attempt record when one was written. Direct callers get one
    #: too; it is what makes a repeated execution key explainable.
    execution_attempt: Any = None
    run_id: str | None = None
    terminal_event_id: int | None = None
    already_completed: bool = False

    @property
    def executed(self) -> bool:
        return self.transaction is not None


def _existing_execution(key: str) -> Transaction | None:
    if not key:
        return None
    return Transaction.objects.filter(execution_key=key).first()


def _reject_duplicate_key(
    existing: Transaction,
    request: ExecutionRequest,
    *,
    audit: Any = None,
) -> ExecutionResult:
    current_hash = request.fingerprint()
    if existing.execution_request_hash and existing.execution_request_hash != current_hash:
        raise ExecutionConflict(
            f"Execution key {request.execution_key!r} was already used for a different request."
        )
    from services.execution_audit import LegacyExecutionProvenance, resume_execution_audit

    try:
        attempt = resume_execution_audit(transaction_row=existing, request=request, audit=audit)
    except LegacyExecutionProvenance as exc:
        raise ExecutionRejected(str(exc), code=exc.code) from exc
    run = getattr(attempt, "decision_run", None) if attempt is not None else None
    decision = GuardDecision(
        approved=True,
        reason="Already executed; returning the existing ledger row.",
        action=existing.tx_type,
        amount=existing.amount,
        price=existing.price,
        notional=existing.gross_value_usd,
    )
    return ExecutionResult(
        approved=True,
        decision=decision,
        request=request,
        transaction=existing,
        already_executed=True,
        reason=decision.reason,
        execution_attempt=attempt,
        run_id=str(run.id) if run is not None else None,
        terminal_event_id=getattr(attempt, "terminal_event_id", None),
        already_completed=attempt is not None and attempt.terminal_event_id is not None,
    )


class _Proposal:
    """The two attributes the pure guard reads."""

    def __init__(self, action: str, amount: Decimal) -> None:
        self.action = action
        self.amount = amount


def _annotate_checks(
    decision: GuardDecision,
    *,
    mark: Mark,
    price_tolerance_pct: Decimal,
    approved_price: Decimal | None,
    approved_quantity: Decimal | None,
    quantity_tolerance: Decimal,
    approval_ref: str,
) -> None:
    """Show the observation and reference term each check was run against."""
    observed = mark.as_of.isoformat() if mark.as_of else None
    retrieved = mark.retrieved_at.isoformat() if mark.retrieved_at else None
    for check in decision.checks:
        check["observed_at"] = observed
        check["retrieved_at"] = retrieved
        check["quote_source"] = mark.source
        check["reference_price"] = str(approved_price) if approved_price is not None else None
        check["reference_quantity"] = (
            str(approved_quantity) if approved_quantity is not None else None
        )
        check["price_tolerance_pct"] = str(price_tolerance_pct)
        check["quantity_tolerance"] = str(quantity_tolerance)
        check["approval_ref"] = approval_ref or None


def _revised_terms(
    *,
    request: ExecutionRequest,
    price: Decimal,
    mark: Mark,
    tolerance: Decimal,
    reason_code: str,
    drift_pct: Decimal | None = None,
    policy_version: str = "",
) -> dict:
    return {
        "reason_code": reason_code,
        "ticker": request.ticker,
        "side": request.side,
        "quantity": str(request.quantity),
        "reference_price": str(price),
        "approved_price": (
            str(request.approved_price) if request.approved_price is not None else None
        ),
        "price_tolerance_pct": str(tolerance),
        "price_drift_pct": str(drift_pct) if drift_pct is not None else None,
        "quantity_tolerance": str(request.quantity_tolerance),
        "quote_observed_at": mark.as_of.isoformat() if mark.as_of else None,
        "quote_retrieved_at": mark.retrieved_at.isoformat() if mark.retrieved_at else None,
        "quote_source": mark.source,
        "policy_version": policy_version or request.approved_policy_version or None,
        "message": "A new approval is required for these refreshed terms.",
    }


def execute_trade(
    request: ExecutionRequest,
    *,
    audit: Any = None,
) -> ExecutionResult:
    """Validate and execute one trade under a lock, transactionally.

    The order of operations is the safety property: lock the portfolio and every
    position row, rebuild holdings and exposure, re-run the current policy and
    daily-loss checks against current marks, verify the result is still the
    decision that was approved, then write the ledger and its audit in the same
    transaction.

    When ``audit`` carries a decision run, the fill, the exact final guard
    evaluation, the run's terminal execution state and the durable event are all
    committed here. A repeated execution key repairs a missing attempt before it
    is reported as complete.
    """
    if request.side not in {"BUY", "SELL"}:
        raise ExecutionRejected(f"Unsupported side {request.side!r}.")
    if request.quantity <= ZERO:
        raise ExecutionRejected("An execution requires a positive quantity.")
    from services.ledger import PositionState, realised_pnl_today
    from services.quote_policy import validate_mark

    had_key = bool(request.execution_key)
    fingerprint = request.fingerprint()
    existing = _existing_execution(request.execution_key)
    if existing is not None:
        return _reject_duplicate_key(existing, request, audit=audit)

    with transaction.atomic():
        try:
            portfolio = (
                Portfolio.objects.select_for_update()
                .select_related("user")
                .get(pk=request.portfolio_id)
            )
        except Portfolio.DoesNotExist:
            raise ExecutionRejected(f"Portfolio {request.portfolio_id} does not exist.") from None

        existing = _existing_execution(request.execution_key)
        if existing is not None:
            return _reject_duplicate_key(existing, request, audit=audit)

        if request.source == "autonomous" and not portfolio.is_autonomous:
            raise ExecutionRejected(
                "Autonomous execution is disabled for this portfolio. The proposal "
                "requires a fresh human approval before any fill can be written.",
                code="autonomy_disabled",
            )

        now = timezone.now()
        current_policy_version = portfolio_policy_version(portfolio)
        if (
            request.approved_policy_version
            and request.approved_policy_version != current_policy_version
        ):
            raise PolicyChangedError(
                "The portfolio policy changed after this decision was approved "
                f"({request.approved_policy_version} -> {current_policy_version}). "
                "A new approval is required.",
                revised_terms={
                    "reason_code": "policy_changed",
                    "approved_policy_version": request.approved_policy_version,
                    "current_policy_version": current_policy_version,
                    "message": "A new approval is required for the current policy.",
                },
            )

        if request.approved_portfolio_version:
            from services.portfolio_brief import portfolio_state_version

            current_version = portfolio_state_version(portfolio)
            if current_version != request.approved_portfolio_version:
                raise MaterialChangeError(
                    "The portfolio changed after this decision was approved "
                    f"({request.approved_portfolio_version} -> {current_version}). "
                    "A new approval is required.",
                    code="portfolio_changed",
                    revised_terms={
                        "reason_code": "portfolio_changed",
                        "approved_portfolio_version": request.approved_portfolio_version,
                        "current_portfolio_version": current_version,
                        "message": "A new approval is required for the current portfolio.",
                    },
                )

        policy = ExecutionFreshnessPolicy.from_settings()
        paper_mode = request.mode in ESTIMATION_MODES
        marks = normalise_marks(request.quotes, now=now)
        traded_mark = marks.get(request.ticker)
        if traded_mark is None:
            raise ValuationUnavailableError(
                f"No current quote for {request.ticker}; refusing to execute.",
                code="missing_quote",
            )
        traded_validation = validate_mark(
            ticker=request.ticker,
            price=traded_mark.price,
            as_of=traded_mark.as_of,
            retrieved_at=traded_mark.retrieved_at,
            source=traded_mark.source,
            now=now,
            mode=request.mode,
            require_current=not paper_mode,
            risk_reducing=False,
            policy=policy,
        )
        if not traded_validation.ok:
            raise ValuationUnavailableError(traded_validation.detail, code=traded_validation.code)

        risk_exception: dict = {}
        valuation_marks = dict(marks)
        for asset in Asset.objects.filter(portfolio=portfolio, amount__gt=ZERO):
            symbol = asset.ticker.upper()
            if symbol == request.ticker:
                continue
            mark = valuation_marks.get(symbol)
            if mark is None:
                continue
            material_validation = validate_mark(
                ticker=symbol,
                price=mark.price,
                as_of=mark.as_of,
                retrieved_at=mark.retrieved_at,
                source=mark.source,
                now=now,
                mode=request.mode,
                require_current=not paper_mode,
                risk_reducing=(request.side == "SELL" and policy.allow_risk_reducing_exception),
                policy=policy,
            )
            if material_validation.ok:
                if material_validation.rule.startswith("risk_reducing_"):
                    risk_exception = material_validation.as_dict()
                continue
            if request.side == "SELL" and policy.allow_risk_reducing_exception and not paper_mode:
                # The sale reduces risk; an untimed/stale mark here values the
                # remainder, not the price being traded. Excluding it records the
                # exception and leaves the portfolio-level limits unevaluated
                # rather than silently valuing it at a stale number.
                risk_exception = {
                    **material_validation.as_dict(),
                    "excluded_from_valuation": True,
                    "rule": "risk_reducing_valuation_exception",
                }
                valuation_marks.pop(symbol, None)
                continue
            raise ValuationUnavailableError(
                material_validation.detail, code=material_validation.code
            )

        state = _lock_portfolio_state(
            portfolio,
            valuation_marks,
            require_full_valuation=request.require_full_valuation and request.side == "BUY",
        )
        if request.require_full_valuation and request.side == "BUY" and state.unpriced_tickers:
            raise ValuationUnavailableError(
                "No current quote for material holding(s) "
                + ", ".join(state.unpriced_tickers)
                + "; authoritative exposure cannot be computed.",
                code="missing_quote",
            )

        price = traded_mark.price
        tolerance = policy.drift_tolerance(request.price_tolerance_pct)
        if request.approved_price and request.approved_price > ZERO:
            drift = (abs(price - request.approved_price) / request.approved_price) * HUNDRED
            if drift > tolerance:
                raise MaterialChangeError(
                    f"The price moved {drift.quantize(CENT)}% between approval and execution "
                    f"(${request.approved_price} -> ${price}); the approved quantity is no "
                    "longer the same decision. A new approval is required for the refreshed "
                    "terms.",
                    code="price_drift_requires_approval",
                    revised_terms=_revised_terms(
                        request=request,
                        price=price,
                        mark=traded_mark,
                        tolerance=tolerance,
                        reason_code="price_drift_requires_approval",
                        drift_pct=drift.quantize(CENT),
                        policy_version=current_policy_version,
                    ),
                )

        asset = state.assets.get(request.ticker)
        position = PositionState(
            ticker=request.ticker,
            amount=asset.amount if asset is not None else ZERO,
            avg_cost=asset.avg_purchase_price if asset is not None else ZERO,
        )
        exposure = state.exposure_for(request.ticker) if state.equity_usd is not None else None
        realised = realised_pnl_today(portfolio)
        decision = evaluate_proposal(
            portfolio,
            _Proposal(request.side, request.quantity),
            price,
            position,
            realised,
            exposure=exposure,
        )
        from services.portfolio_brief import portfolio_state_version

        evaluated_version = portfolio_state_version(portfolio)
        decision.requested_amount = request.requested_quantity or request.quantity
        decision.policy_version = current_policy_version
        decision.evaluated_portfolio_version = evaluated_version
        decision.quote_time = traded_mark.as_of
        decision.quote_source = traded_mark.source
        decision.quote_retrieved_at = traded_mark.retrieved_at
        decision.approved_price = request.approved_price
        decision.approved_quantity = request.approved_quantity or request.quantity
        decision.price_tolerance_pct = tolerance
        decision.quantity_tolerance = request.quantity_tolerance
        decision.approval_ref = request.approval_ref
        decision.risk_exception = risk_exception
        decision.material_marks = {
            symbol: mark.as_dict()
            if hasattr(mark, "as_dict")
            else {
                "price": str(mark.price),
                "source": mark.source,
                "as_of": mark.as_of.isoformat() if mark.as_of else None,
                "retrieved_at": mark.retrieved_at.isoformat() if mark.retrieved_at else None,
            }
            for symbol, mark in marks.items()
        }
        decision.binding_rule = _binding_rule(portfolio, decision, exposure, price)
        decision.checks = build_guard_checks(
            portfolio,
            decision,
            price=price,
            exposure=exposure,
            realised_pnl_today=realised,
            position_amount=position.amount,
            requested_quantity=request.quantity,
        )
        _annotate_checks(
            decision,
            mark=traded_mark,
            price_tolerance_pct=tolerance,
            approved_price=request.approved_price,
            approved_quantity=request.approved_quantity,
            quantity_tolerance=request.quantity_tolerance,
            approval_ref=request.approval_ref,
        )

        if not decision.approved:
            decision.reason_codes = ["guard_rejected"]
            decision.revised_terms = _revised_terms(
                request=request,
                price=price,
                mark=traded_mark,
                tolerance=tolerance,
                reason_code="guard_rejected",
                policy_version=current_policy_version,
            )
            if audit is not None and getattr(audit, "run", None) is not None:
                from services.execution_audit import record_terminal_outcome

                record_terminal_outcome(
                    run=audit.run,
                    outcome="blocked",
                    recommendation=getattr(audit, "recommendation", None),
                    guard_decision=decision,
                    reason=decision.reason,
                    via=getattr(audit, "via", ""),
                    user_id=getattr(audit, "user_id", None),
                )
            return ExecutionResult(
                approved=False,
                decision=decision,
                request=request,
                reason=decision.reason,
                policy_version=current_policy_version,
                quote_time=decision.quote_time,
            )

        difference = request.quantity - decision.amount
        if difference > request.quantity_tolerance:
            decision.reason_codes = ["material_quantity_change"]
            decision.revised_terms = {
                **_revised_terms(
                    request=request,
                    price=price,
                    mark=traded_mark,
                    tolerance=tolerance,
                    reason_code="material_quantity_change",
                    policy_version=current_policy_version,
                ),
                "approved_quantity": str(request.quantity),
                "permitted_quantity": str(decision.amount),
            }
            raise MaterialChangeError(
                f"The guard now permits {decision.amount} of the approved "
                f"{request.quantity} {request.ticker}. A different decision requires a "
                "new approval.",
                decision=decision,
                code="material_quantity_change",
                revised_terms=decision.revised_terms,
            )

        notional = decision.notional
        if decision.action == "BUY":
            if notional > portfolio.balance_usd:
                raise ExecutionRejected(
                    f"Cash changed under lock: need ${notional}, have ${portfolio.balance_usd}.",
                    decision=decision,
                    code="cash_changed",
                )
            new_amount = position.amount + decision.amount
            avg_price = asset.avg_purchase_price if asset is not None else ZERO
            if new_amount > 0:
                avg_price = ((position.amount * avg_price) + (decision.amount * price)) / new_amount
            if asset is None:
                asset = Asset(portfolio=portfolio, ticker=request.ticker)
            asset.amount = new_amount
            asset.avg_purchase_price = avg_price.quantize(CENT)
            portfolio.balance_usd = (portfolio.balance_usd - notional).quantize(CENT)
        else:
            if decision.amount > position.amount:
                raise ExecutionRejected(
                    f"Position changed under lock: selling {decision.amount}, "
                    f"holding {position.amount}.",
                    decision=decision,
                    code="position_changed",
                )
            if asset is None:
                raise ExecutionRejected(
                    f"No open position in {request.ticker} to sell.",
                    decision=decision,
                    code="position_missing",
                )
            asset.amount = asset.amount - decision.amount
            if asset.amount == ZERO:
                asset.avg_purchase_price = ZERO.quantize(CENT)
            portfolio.balance_usd = (portfolio.balance_usd + notional).quantize(CENT)

        portfolio.save(update_fields=["balance_usd"])
        asset.save()
        row = Transaction.objects.create(
            portfolio=portfolio,
            ticker=request.ticker,
            tx_type=decision.action,
            amount=decision.amount,
            price=price,
            executed_by=ExecutedBy.AI,
            execution_key=request.execution_key or None,
            execution_request_hash=fingerprint if had_key else "",
            price_observed_at=traded_mark.as_of,
            price_retrieved_at=traded_mark.retrieved_at,
            price_source=traded_mark.source,
            price_currency=traded_mark.currency,
        )

        attempt = None
        if audit is not None and getattr(audit, "run", None) is not None:
            from services.execution_audit import record_fill_outcome

            attempt = record_fill_outcome(
                run=audit.run,
                transaction_row=row,
                decision=decision,
                request=request,
                recommendation=getattr(audit, "recommendation", None),
                reason=decision.reason,
                via=getattr(audit, "via", ""),
                user_id=getattr(audit, "user_id", None),
            )
        else:
            from services.execution_audit import ensure_fill_attempt

            attempt = ensure_fill_attempt(
                transaction_row=row,
                decision=decision,
                request=request,
                audit=audit,
            )

    run = getattr(attempt, "decision_run", None) if attempt is not None else None
    return ExecutionResult(
        approved=True,
        decision=decision,
        request=request,
        transaction=row,
        reason=decision.reason,
        policy_version=current_policy_version,
        quote_time=decision.quote_time,
        execution_attempt=attempt,
        run_id=str(run.id) if run is not None else None,
        terminal_event_id=getattr(attempt, "terminal_event_id", None),
        already_completed=attempt is not None and attempt.terminal_event_id is not None,
    )


def apply_trade(
    portfolio_id: int,
    ticker: str,
    decision: GuardDecision,
    *,
    request: ExecutionRequest | None = None,
    quotes: Mapping[str, Any] | None = None,
    audit: Any = None,
) -> ExecutionResult:
    """Execute an approved decision through the authoritative path.

    Kept as the single ledger-writing entrypoint. A caller that constructs a
    ``GuardDecision`` by hand cannot use it to skip validation: without an
    explicit request the quote and quantity from the decision are treated as the
    approval, and the guard is rerun under the lock. If the rerun disagrees, the
    call raises instead of executing the different decision.

    Returns the :class:`ExecutionResult` (including the under-lock decision, so a
    caller can persist exactly what was executed) on success. A rejection raises
    :class:`ExecutionRejected`; a caller that wants the typed rejection record
    reads ``exc.decision``.
    """
    if request is None:
        if decision.price is None or decision.price <= ZERO:
            raise GuardError("Refusing to execute without a validated price.")
        if decision.action not in {"BUY", "SELL"}:
            raise ExecutionRejected(f"{decision.action} does not reach the ledger.")
        quote_map = dict(quotes or {})
        quote_map.setdefault(ticker, Mark(price=decision.price, as_of=decision.quote_time))
        request = ExecutionRequest(
            portfolio_id=portfolio_id,
            ticker=ticker,
            side=decision.action,
            quantity=decision.amount,
            quotes=quote_map,
            approved_price=decision.price,
            approved_quantity=decision.amount,
            requested_quantity=decision.requested_amount or decision.amount,
            source="direct",
            approved_policy_version=decision.policy_version or None,
            require_full_valuation=decision.action == "BUY",
            quote_time=decision.quote_time,
            quote_source=getattr(decision, "quote_source", ""),
            quote_retrieved_at=getattr(decision, "quote_retrieved_at", None),
            price_tolerance_pct=getattr(decision, "price_tolerance_pct", None),
        )
    result = execute_trade(request, audit=audit)
    if not result.approved:
        raise ExecutionRejected(result.reason, decision=result.decision)
    return result
