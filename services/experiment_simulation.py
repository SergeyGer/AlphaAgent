"""Pure simulated time, and the registered-metric calculation (steps 7-8).

Why this module is separate
---------------------------
The comparison path in :mod:`services.experiment_execution` runs protocols and
writes database rows. The *simulation* of an arm's decisions and the
calculation of its pre-registered metric are pure functions over recorded
inputs. Keeping them here means an archived report can be rebuilt without a
database and without importing a model, and it makes the replay deterministic:
the same decisions, snapshots and marks produce the same timeline.

Two rules are load-bearing.

**Simulated time is monotonic.** A decision scheduled at ``T`` may only use
data known at or before ``T``. Its fill under
``first_executable_mark_at_or_after_decision.v1`` may be priced from a later
mark, but the fill is *applied* when the simulated clock reaches that mark, not
at the decision. A second decision at ``T + 1`` therefore cannot see a fill
whose price was observed at ``T + 5``. If the first executable mark is at
``T`` itself, the fill applies immediately.

**Every step is recorded.** The timeline carries the clock, the data known at
that instant, the decision, and the fill (or the stable reason it was deferred,
rejected or expired). The timeline is hashed, so two replays can be compared
byte for byte.

The registered metric is read from the pre-registration record - its id, family,
horizon and direction - never from a module constant. Constants here only map a
registered id to a calculator and name the method version.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from core.decision_json import content_hash, utc_iso
from services.attribution import directional_hit, signed_return
from services.execution import SATOSHI, Exposure, evaluate_proposal
from services.ledger import PositionState, utc_day_start
from services.portfolio_accounting import (
    FILL_RULE,
    Fill,
    PriceBook,
    PriceMark,
    select_fill_price,
)
from services.signal_diagnostics import evaluate_horizon

__all__ = [
    "BENCHMARK_MATCH_POLICY",
    "COST_BASIS_POLICY",
    "FEE_POLICY",
    "FILL_TIMING_RULE",
    "FILL_VALIDATION_POLICY",
    "METRIC_METHOD_VERSION",
    "REASON_DECISION_INPUT_AFTER_CLOCK",
    "REASON_FILL_CLIPPED_CASH",
    "REASON_FILL_CLIPPED_HOLDING",
    "REASON_FILL_DEFERRED",
    "REASON_FILL_EXPIRED",
    "REASON_FILL_FEE_EXCEEDS_PROCEEDS",
    "REASON_FILL_INSUFFICIENT_CASH",
    "REASON_FILL_NO_ELIGIBLE_HOLDING",
    "REASON_QUOTE_AFTER_CLOCK",
    "SIMULATION_METHOD_VERSION",
    "SUPPORTED_REGISTERED_METRICS",
    "TRADING_DAY_POLICY",
    "DecisionInput",
    "SimulationError",
    "SnapshotInput",
    "compute_registered_metric",
    "simulate_arm_timeline",
]

ZERO = Decimal("0")
CENT = Decimal("0.01")
PCT4 = Decimal("0.0001")

#: The timeline method. A rebuilt report names the version it was built with.
SIMULATION_METHOD_VERSION = "monotonic_fill_validation.v2"

#: The fill applies at the mark's own instant, never earlier.
FILL_TIMING_RULE = "fill_applied_at_first_available_mark.v1"

#: Every fill, immediate or deferred, is revalidated at its executable instant
#: against cash (including fees), eligible holdings, the day's realised P&L and
#: the applicable portfolio limits. The decision-time quantity is a ceiling only.
FILL_VALIDATION_POLICY = "fill_time_revalidation.v1"

#: The declared day boundary for the daily loss check. UTC, matching every other
#: timestamp in this contract.
TRADING_DAY_POLICY = "utc_calendar_day.v1"

#: Realised P&L uses the same weighted-average cost basis as ``services.ledger``.
COST_BASIS_POLICY = "weighted_average_cost.v1"

#: A fee is a realised cost: it reduces cash and the day's realised P&L on either
#: side. It is never folded into the average cost of the remaining position.
FEE_POLICY = "fee_reduces_realised_pnl.v1"

#: The metric's definition (id, family, horizon, direction) comes from the
#: registration; this names the calculator's method.
METRIC_METHOD_VERSION = "registered_signal_metric.v1"

#: Stable reasons. They are part of the report contract, not prose.
REASON_FILL_DEFERRED = "fill_priced_after_simulated_clock"
REASON_FILL_EXPIRED = "fill_after_observation_window"
REASON_DECISION_INPUT_AFTER_CLOCK = "decision_input_after_simulated_clock"
REASON_QUOTE_AFTER_CLOCK = "quote_after_simulated_clock"
REASON_NO_DECISION_PRICE = "no_decision_price_known_at_clock"
REASON_FILL_INSUFFICIENT_CASH = "insufficient_cash_including_fees_at_fill"
REASON_FILL_CLIPPED_CASH = "clipped_to_available_cash_including_fees"
REASON_FILL_NO_ELIGIBLE_HOLDING = "no_eligible_holding_at_fill"
REASON_FILL_CLIPPED_HOLDING = "clipped_to_holding_at_fill"
REASON_FILL_CLIPPED_BY_GUARD = "clipped_by_guard_at_fill"
REASON_FILL_FEE_EXCEEDS_PROCEEDS = "fee_exceeds_sale_proceeds_at_fill"

#: Benchmark pairing is declared, not assumed. The benchmark's entry observation
#: must fall on the instrument's entry session and its horizon observation on the
#: instrument's horizon session; the target session is the first benchmark mark
#: at or after the instrument session, never the prior close.
BENCHMARK_MATCH_POLICY = "benchmark_same_session_pair.v1"
BENCHMARK_TARGET_POLICY = "benchmark_first_mark_at_or_after_target_session.v1"

#: The calculator registry. The *definition* is the registration's; this only
#: answers "which arithmetic implements this registered id".
SUPPORTED_REGISTERED_METRICS = {
    "mean_action_score_pct": "signal_diagnostics",
    "mean_forward_return_pct": "signal_diagnostics",
    "hit_rate_pct": "signal_diagnostics",
    "mean_relative_forward_return_pct": "signal_diagnostics",
    "twr_pct": "portfolio_performance",
    "net_twr_pct": "portfolio_performance",
}


class SimulationError(Exception):
    """A recorded input cannot be simulated without inventing data."""


def _decimal(value: Any, *, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SimulationError(f"{field_name} must be a decimal.") from exc


def _parse_at(value: Any, *, field_name: str) -> datetime:
    from django.utils.dateparse import parse_datetime

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = parse_datetime(value.strip())
    else:
        parsed = None
    if parsed is None:
        raise SimulationError(f"{field_name} must be an ISO-8601 timestamp.")
    if parsed.tzinfo is None:
        from datetime import UTC

        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# ---------------------------------------------------------------------------
# Recorded inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SnapshotInput:
    """The immutable snapshot an arm's decision saw, as recorded."""

    id: str
    content_hash: str
    portfolio_version: str
    analysis_cutoff: datetime
    captured_at: datetime
    holdings: tuple[dict, ...] = ()
    cash: dict = field(default_factory=dict)
    quotes: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict) -> SnapshotInput:
        return cls(
            id=str(payload.get("snapshot_id") or payload.get("id") or ""),
            content_hash=str(payload.get("content_hash") or ""),
            portfolio_version=str(payload.get("portfolio_version") or ""),
            analysis_cutoff=_parse_at(
                payload.get("analysis_cutoff"), field_name="snapshot.analysis_cutoff"
            ),
            captured_at=_parse_at(payload.get("captured_at"), field_name="snapshot.captured_at"),
            holdings=tuple(dict(item) for item in payload.get("holdings") or ()),
            cash=dict(payload.get("cash") or {}),
            quotes=dict(payload.get("quotes") or {}),
        )

    def as_dict(self) -> dict:
        return {
            "snapshot_id": self.id,
            "content_hash": self.content_hash,
            "portfolio_version": self.portfolio_version,
            "analysis_cutoff": utc_iso(self.analysis_cutoff),
            "captured_at": utc_iso(self.captured_at),
            "holdings": [dict(item) for item in self.holdings],
            "cash": dict(self.cash),
            "quotes": dict(self.quotes),
        }

    def quote_for(self, ticker: str) -> dict:
        quote = self.quotes.get(ticker.upper()) or {}
        return dict(quote) if isinstance(quote, dict) else {}


@dataclass(frozen=True)
class DecisionInput:
    """One recorded decision, with the snapshot and the economic instant."""

    run_id: str
    opportunity_key: str
    ticker: str
    decision_at: datetime
    action: str
    requested_quantity: Decimal
    rationale: str
    snapshot: SnapshotInput
    recorded_at: datetime | None = None

    @property
    def normalised_action(self) -> str:
        return (self.action or "").strip().upper()


# ---------------------------------------------------------------------------
# The simulator
# ---------------------------------------------------------------------------
@dataclass
class _PendingOrder:
    """A decision that has been approved but not yet filled.

    ``approved_quantity`` is the decision-time guard's ceiling, not a fill
    authorisation. At the executable mark the same guard is re-evaluated against
    the state and the price that actually obtain, and the resulting amount is
    capped by this ceiling. A quantity the arm cannot support at the executable
    price is clipped or rejected there; it is never applied from this record.
    """

    order_index: int
    opportunity_key: str
    ticker: str
    side: str
    proposal: Any
    approved_quantity: Decimal
    decision_at: datetime
    scheduled_at: datetime
    price: Decimal
    sequence: int


def _fee_for(notional: Decimal, assumptions: dict) -> Decimal:
    bps = _decimal(assumptions.get("fee_bps") or ZERO, field_name="execution_assumptions.fee_bps")
    return (notional * bps / Decimal("10000")).quantize(CENT)


def _affordable_quantity(cash: Decimal, price: Decimal, fee_bps: Decimal) -> Decimal:
    """The largest quantity whose notional **plus its fee** fits ``cash``.

    The fee is a function of the notional, so this solves
    ``q * price * (1 + rate) <= cash`` and then walks down by one satoshi while
    the cent-rounded fee still overflows. The walk is bounded and small: the fee
    is rounded to a cent, so at most a few steps are needed.
    """
    if price <= ZERO or cash <= ZERO:
        return ZERO
    ceiling = cash / (Decimal("1") + fee_bps / Decimal("10000"))
    quantity = (ceiling / price).quantize(SATOSHI, rounding=ROUND_DOWN)
    for _ in range(1000):
        if quantity <= ZERO:
            return ZERO
        notional = (quantity * price).quantize(CENT)
        if notional + _fee_for(notional, {"fee_bps": fee_bps}) <= cash:
            return quantity
        quantity -= SATOSHI
    return ZERO


def _known_marks(book: PriceBook, at: datetime) -> list[dict]:
    rows = [
        mark.as_dict() for mark in book.all_marks() if mark.knowledge_at <= at and mark.at <= at
    ]
    return sorted(rows, key=lambda row: (row["ticker"], row["at"], row["price_usd"]))


def _quote_as_of(quote: dict) -> datetime | None:
    value = quote.get("as_of")
    return _parse_at(value, field_name="quote.as_of") if value else None


def _equity(cash: Decimal, positions: dict[str, Decimal], book: PriceBook, at: datetime) -> Decimal:
    value = cash
    for ticker, quantity in positions.items():
        if quantity == ZERO:
            continue
        mark = book.mark_at_or_before(ticker, at)
        if mark is not None:
            value += quantity * mark.price
    return value


def _unpriced(positions: dict[str, Decimal], book: PriceBook, at: datetime) -> list[str]:
    return sorted(
        ticker
        for ticker, quantity in positions.items()
        if quantity != ZERO and book.mark_at_or_before(ticker, at) is None
    )


def _positions_dict(positions: dict[str, Decimal]) -> dict[str, str]:
    return {
        ticker: str(quantity) for ticker, quantity in sorted(positions.items()) if quantity != ZERO
    }


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT))


def _apply_fill(
    fill: Fill,
    *,
    cash: Decimal,
    positions: dict[str, Decimal],
    average_cost: dict[str, Decimal],
) -> Decimal:
    cash += fill.cash_effect
    current = positions.get(fill.ticker, ZERO)
    if fill.side == "BUY":
        notional = fill.notional
        new_quantity = current + fill.quantity
        if new_quantity > ZERO:
            average_cost[fill.ticker] = (
                current * average_cost.get(fill.ticker, ZERO) + notional
            ) / new_quantity
        positions[fill.ticker] = new_quantity
    else:
        remaining = current - fill.quantity
        positions[fill.ticker] = ZERO if remaining == ZERO else remaining
        if positions[fill.ticker] == ZERO:
            average_cost[fill.ticker] = ZERO
    return cash


def _realised_delta(
    *, side: str, quantity: Decimal, price: Decimal, fee: Decimal, average_cost: Decimal
) -> Decimal:
    """Realised P&L of one completed fill under the registered cost basis.

    A SELL realises ``quantity * (price - average_cost)``. The fee is a realised
    cost on either side, so it reduces the day's realised P&L even when the
    trade itself is flat. That is the declared ``fee_reduces_realised_pnl``
    policy, not an accounting accident.
    """
    if side == "SELL":
        return (quantity * (price - average_cost)).quantize(CENT) - fee
    return -fee


def simulate_arm_timeline(
    *,
    arm: str,
    opportunities: Sequence[Any],
    decisions: dict[str, DecisionInput],
    marks: Sequence[PriceMark],
    window_start: datetime,
    window_end: datetime,
    starting_state: dict,
    execution_assumptions: dict,
    risk_rules: dict,
    guard_portfolio_factory,
    as_of: datetime | None = None,
) -> dict:
    """Replay one arm's decisions on a monotonic simulated clock.

    Proposals and fills are separate. A decision produces a proposal and a
    decision-time guard result; the fill is created only when the simulated
    clock reaches its executable mark, and only after the guard, the cash
    (including fees), the eligible holding and the day's realised P&L have been
    rechecked against the state at that instant. Immediate and deferred orders
    use this one validation path.

    Returns orders, fills, the timeline, the daily realised P&L and the hash of
    the steps. The caller is responsible for persisting any state; this function
    performs no I/O.
    """
    book = PriceBook(marks)
    fee_bps = _decimal(
        execution_assumptions.get("fee_bps") or ZERO,
        field_name="execution_assumptions.fee_bps",
    )
    opening_at = window_start - timedelta(seconds=1)
    clock = opening_at
    cash = _decimal(starting_state.get("cash"), field_name="starting_state.cash")
    positions: dict[str, Decimal] = {
        str(ticker).upper(): _decimal(quantity, field_name="starting_state.positions")
        for ticker, quantity in (starting_state.get("positions") or {}).items()
    }
    declared_cost = starting_state.get("average_cost") or {}
    average_cost: dict[str, Decimal] = {
        ticker: _decimal(declared_cost.get(ticker, ZERO), field_name="starting_state.average_cost")
        for ticker in positions
    }
    pending: list[_PendingOrder] = []
    orders: list[dict] = []
    fills: list[Fill] = []
    guards: dict[str, Any] = {}
    realised_by_day: dict[str, Decimal] = {}
    realised_day = utc_day_start(opening_at)
    realised_pnl_today = ZERO
    steps: list[dict] = [
        {
            "sequence": 0,
            "event": "simulation_start",
            "clock": utc_iso(opening_at),
            "synthetic": True,
            "starting_state": {
                "cash_usd": str(cash),
                "positions": _positions_dict(positions),
            },
            "available_data": {
                "known_marks": _known_marks(book, opening_at),
                "realised_pnl_today_usd": _money(realised_pnl_today),
            },
        }
    ]

    def next_sequence() -> int:
        return len(steps)

    def sync_trading_day(at: datetime) -> None:
        """Reset the day's realised P&L when the clock crosses a UTC day.

        The declared trading day is the UTC calendar day (``utc_calendar_day.v1``).
        The reset happens at the boundary; a decision before it sees yesterday's
        accumulated loss and a decision after it sees zero. The per-day totals
        stay in ``realised_by_day`` so nothing is lost by the reset.
        """
        nonlocal realised_day, realised_pnl_today
        day = utc_day_start(at)
        if day != realised_day:
            realised_by_day[str(realised_day.date())] = realised_pnl_today.quantize(CENT)
            realised_day = day
            realised_pnl_today = ZERO

    def record_fill_step(
        *,
        fill: Fill,
        order: dict,
        opportunity_key: str,
        decision_at: datetime,
        realised_delta: Decimal,
        validation: dict,
    ) -> None:
        steps.append(
            {
                "sequence": next_sequence(),
                "event": "fill",
                "clock": utc_iso(fill.at),
                "opportunity": opportunity_key,
                "decision_clock": utc_iso(decision_at),
                "fill": fill.as_dict(),
                "fill_validation": validation,
                "realised_pnl_delta_usd": _money(realised_delta),
                "realised_pnl_today_usd": _money(realised_pnl_today),
                "cash_after_usd": _money(cash),
                "positions_after": _positions_dict(positions),
                "available_data": {
                    "known_marks": _known_marks(book, fill.at),
                    "realised_pnl_today_usd": _money(realised_pnl_today),
                },
                "reason": (
                    "priced_at_the_decision_clock"
                    if fill.at <= decision_at
                    else REASON_FILL_DEFERRED
                ),
            }
        )
        order["fill"] = fill.as_dict()
        order["applied_at"] = utc_iso(fill.at)
        order["fill_rule_status"] = "filled" if fill.at <= decision_at else "deferred"
        order["fill_reason"] = (
            validation["clip_reason"]
            if validation.get("clipped")
            else (None if fill.at <= decision_at else REASON_FILL_DEFERRED)
        )

    def validate_and_apply(
        *,
        order: dict,
        opportunity_key: str,
        ticker: str,
        side: str,
        proposal,
        approved_quantity: Decimal,
        decision_at: datetime,
        price: Decimal,
        fill_at: datetime,
    ) -> bool:
        """The one fill-time validation path, used for immediate and deferred orders."""
        nonlocal cash, realised_pnl_today
        sync_trading_day(fill_at)
        held = positions.get(ticker, ZERO)
        positions_value = ZERO
        for held_ticker, quantity in positions.items():
            if quantity == ZERO:
                continue
            mark = book.mark_at_or_before(held_ticker, fill_at)
            if mark is not None:
                positions_value += quantity * mark.price
        ticker_value = held * price
        state = PositionState(
            ticker=ticker,
            amount=held,
            avg_cost=average_cost.get(ticker, ZERO),
        )
        view = guard_portfolio_factory(cash, positions_value)
        exposure = Exposure(
            positions_value_usd=positions_value,
            ticker_value_usd=ticker_value,
            equity_usd=cash + positions_value,
            sector_value_usd=None,
        )
        validated_proposal = proposal.model_copy(update={"amount": float(approved_quantity)})
        guard = evaluate_proposal(
            view,
            validated_proposal,
            price,
            state,
            realised_pnl_today,
            exposure=exposure,
        )
        guard.policy_version = str(
            execution_assumptions.get("guard_policy_version")
            or "services.execution.evaluate_proposal"
        )
        guard.evaluated_portfolio_version = (
            f"{arm}:{opportunity_key}:fill_at={utc_iso(fill_at)}:cash={_money(cash)}"
        )
        guard.quote_time = fill_at
        validation = {
            "policy": FILL_VALIDATION_POLICY,
            "validated_at": utc_iso(fill_at),
            "executable_price_usd": str(price),
            "approved_quantity_at_decision": str(approved_quantity),
            "cash_before_usd": _money(cash),
            "holdings_before": str(held),
            "realised_pnl_today_before_usd": _money(realised_pnl_today),
            "cash_includes_fees": True,
            "fee_rate_bps": str(execution_assumptions.get("fee_bps") or "0"),
            "holdings_rechecked": True,
            "daily_loss_rechecked": True,
            "portfolio_limits_rechecked": True,
            "guard_status": "approved" if guard.approved else "rejected",
            "guard_reason": guard.reason,
            "binding_rule": guard.binding_rule or None,
            "clipped": False,
            "clip_reason": None,
            "requested_quantity_at_fill": None,
            "filled_quantity": None,
            "status": None,
            "reason": None,
        }

        def reject(reason: str, order_reason: str | None = None) -> bool:
            """Record a fill-time refusal as its own auditable timeline step."""
            validation["status"] = "rejected"
            validation["reason"] = reason
            order["fill_validation"] = validation
            order["fill_rule_status"] = "rejected"
            order["fill_reason"] = reason
            order["reason"] = order_reason or reason
            steps.append(
                {
                    "sequence": next_sequence(),
                    "event": "fill_rejected",
                    "clock": utc_iso(fill_at),
                    "opportunity": opportunity_key,
                    "decision_clock": utc_iso(decision_at),
                    "fill_validation": validation,
                    "reason": reason,
                    "available_data": {
                        "known_marks": _known_marks(book, fill_at),
                        "realised_pnl_today_usd": _money(realised_pnl_today),
                    },
                }
            )
            return False

        if not guard.approved:
            return reject(guard.binding_rule or "rejected_at_fill", guard.reason)

        quantity = min(guard.amount, approved_quantity)
        if quantity < approved_quantity and not validation["clipped"]:
            validation["clipped"] = True
            validation["clip_reason"] = REASON_FILL_CLIPPED_BY_GUARD
        validation["requested_quantity_at_fill"] = str(quantity)
        if side == "BUY":
            affordable = _affordable_quantity(cash, price, fee_bps)
            if quantity > affordable:
                validation["clipped"] = True
                validation["clip_reason"] = REASON_FILL_CLIPPED_CASH
                quantity = affordable
            notional = (quantity * price).quantize(CENT)
            fee = _fee_for(notional, execution_assumptions)
            if quantity <= ZERO or notional + fee > cash:
                return reject(REASON_FILL_INSUFFICIENT_CASH)
        else:
            if held <= ZERO:
                return reject(REASON_FILL_NO_ELIGIBLE_HOLDING)
            if quantity > held:
                validation["clipped"] = True
                validation["clip_reason"] = REASON_FILL_CLIPPED_HOLDING
                quantity = held
            notional = (quantity * price).quantize(CENT)
            fee = _fee_for(notional, execution_assumptions)
            if quantity <= ZERO or notional - fee < ZERO:
                return reject(REASON_FILL_FEE_EXCEEDS_PROCEEDS)
            if cash + (notional - fee) < ZERO:
                return reject(REASON_FILL_INSUFFICIENT_CASH)

        fill = Fill(
            at=fill_at,
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            executed_by="simulated",
            source=f"simulated_arm:{arm}",
        )
        if cash + fill.cash_effect < ZERO:  # defensive: the checks above must hold
            return reject(REASON_FILL_INSUFFICIENT_CASH)

        realised_delta = _realised_delta(
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            average_cost=average_cost.get(ticker, ZERO),
        )
        cash = _apply_fill(fill, cash=cash, positions=positions, average_cost=average_cost)
        realised_pnl_today += realised_delta
        realised_by_day[str(realised_day.date())] = realised_pnl_today.quantize(CENT)
        fills.append(fill)
        validation["status"] = "clipped" if validation["clipped"] else "filled"
        validation["reason"] = validation["clip_reason"]
        validation["filled_quantity"] = str(quantity)
        validation["realised_pnl_delta_usd"] = _money(realised_delta)
        validation["cash_after_usd"] = _money(cash)
        order["fill_validation"] = validation
        record_fill_step(
            fill=fill,
            order=order,
            opportunity_key=opportunity_key,
            decision_at=decision_at,
            realised_delta=realised_delta,
            validation=validation,
        )
        return True

    def advance_to(target: datetime) -> None:
        nonlocal clock
        if target < clock:
            raise SimulationError(
                f"Simulated clock cannot move backwards from {utc_iso(clock)} to {utc_iso(target)}."
            )
        while pending and pending[0].scheduled_at <= target:
            item = pending.pop(0)
            clock = item.scheduled_at
            order = orders[item.order_index]
            validate_and_apply(
                order=order,
                opportunity_key=item.opportunity_key,
                ticker=item.ticker,
                side=item.side,
                proposal=item.proposal,
                approved_quantity=item.approved_quantity,
                decision_at=item.decision_at,
                price=item.price,
                fill_at=item.scheduled_at,
            )
        clock = target
        sync_trading_day(target)

    for opportunity in opportunities:
        advance_to(opportunity.scheduled_at)
        decision = decisions.get(opportunity.key)
        order_index = len(orders)
        equity_before = _equity(cash, positions, book, clock)
        order = {
            "opportunity": opportunity.key,
            "decision_run_id": None if decision is None else decision.run_id,
            "decision_at": utc_iso(opportunity.scheduled_at),
            "decision_recorded_at": None if decision is None else utc_iso(decision.recorded_at),
            "simulated_clock_at_decision": utc_iso(clock),
            "proposal_action": None,
            "requested_quantity": None,
            "price_at_decision_usd": None,
            "guard_status": None,
            "guard_reason": None,
            "binding_rule": None,
            "allowed_quantity": "0",
            "fill": None,
            "fill_rule_status": "not_executed",
            "fill_reason": None,
            "scheduled_fill_at": None,
            "applied_at": None,
            "fill_validation": None,
            "equity_before_usd": _money(equity_before),
            "equity_after_usd": None,
            "cash_before_usd": _money(cash),
            "positions_before": _positions_dict(positions),
            "cash_after_decision_usd": _money(cash),
            "positions_after_decision": _positions_dict(positions),
            "realised_pnl_today_usd": _money(realised_pnl_today),
            "trading_day": utc_iso(realised_day),
            "unpriced_positions": _unpriced(positions, book, clock),
            "reason": None,
        }
        orders.append(order)
        step = {
            "sequence": next_sequence(),
            "event": "decision",
            "clock": utc_iso(clock),
            "opportunity": opportunity.key,
            "ticker": opportunity.ticker,
            "synthetic": True,
            "available_data": {
                "snapshot_id": None if decision is None else decision.snapshot.id,
                "snapshot_hash": None if decision is None else decision.snapshot.content_hash,
                "analysis_cutoff": (
                    None if decision is None else utc_iso(decision.snapshot.analysis_cutoff)
                ),
                "quote": (
                    None
                    if decision is None
                    else decision.snapshot.quote_for(opportunity.ticker) or None
                ),
                "known_marks": _known_marks(book, clock),
                "realised_pnl_today_usd": _money(realised_pnl_today),
            },
            "decision": None,
            "order": {
                "fill_rule_status": "not_executed",
                "fill_reason": None,
            },
            "reason": None,
        }
        if decision is None:
            order["reason"] = "no_decision_recorded_for_this_arm"
            order["fill_reason"] = order["reason"]
            step["reason"] = order["reason"]
            step["order"] = {
                "fill_rule_status": "not_executed",
                "fill_reason": order["reason"],
            }
            steps.append(step)
            continue

        # A decision may not see data from its own future. The snapshot's
        # declared cutoff and the quote's observation time are both checked
        # against the simulated clock, not against wall-clock time.
        if decision.snapshot.analysis_cutoff > clock:
            order["fill_rule_status"] = "rejected"
            order["fill_reason"] = REASON_DECISION_INPUT_AFTER_CLOCK
            order["reason"] = REASON_DECISION_INPUT_AFTER_CLOCK
            order["proposal_action"] = decision.normalised_action
            order["requested_quantity"] = (
                str(decision.requested_quantity) if decision.requested_quantity else None
            )
            order["equity_after_usd"] = _money(equity_before)
            step["decision"] = {
                "run_id": decision.run_id,
                "action": decision.normalised_action,
                "requested_quantity": str(decision.requested_quantity),
                "recorded_at": utc_iso(decision.recorded_at),
            }
            step["order"] = {
                "fill_rule_status": "rejected",
                "fill_reason": REASON_DECISION_INPUT_AFTER_CLOCK,
            }
            step["reason"] = REASON_DECISION_INPUT_AFTER_CLOCK
            steps.append(step)
            continue

        action = decision.normalised_action
        requested = decision.requested_quantity
        quote = decision.snapshot.quote_for(opportunity.ticker)
        quote_as_of = _quote_as_of(quote)
        if quote_as_of is not None and quote_as_of > clock:
            order["fill_rule_status"] = "rejected"
            order["fill_reason"] = REASON_QUOTE_AFTER_CLOCK
            order["reason"] = REASON_QUOTE_AFTER_CLOCK
            order["proposal_action"] = decision.normalised_action
            order["requested_quantity"] = (
                str(decision.requested_quantity) if decision.requested_quantity else None
            )
            order["equity_after_usd"] = _money(equity_before)
            step["decision"] = {
                "run_id": decision.run_id,
                "action": action,
                "requested_quantity": str(requested),
                "recorded_at": utc_iso(decision.recorded_at),
            }
            step["order"] = {
                "fill_rule_status": "rejected",
                "fill_reason": REASON_QUOTE_AFTER_CLOCK,
            }
            step["reason"] = REASON_QUOTE_AFTER_CLOCK
            steps.append(step)
            continue

        quote_price = quote.get("price_usd") or quote.get("price")
        position_mark = book.mark_at_or_before(opportunity.ticker, clock)
        if quote_price not in (None, ""):
            price = _decimal(quote_price, field_name="quote.price")
        elif position_mark is not None:
            price = position_mark.price
        else:
            price = None

        positions_value = ZERO
        for ticker, quantity in positions.items():
            if quantity == ZERO:
                continue
            mark = book.mark_at_or_before(ticker, clock)
            if mark is not None:
                positions_value += quantity * mark.price

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
            reasoning=(decision.rationale or "")[:2000],
        )
        view = guard_portfolio_factory(cash, positions_value)
        exposure = Exposure(
            positions_value_usd=positions_value,
            ticker_value_usd=state.amount * price if price is not None else ZERO,
            equity_usd=cash + positions_value,
            sector_value_usd=None,
        )
        guard = evaluate_proposal(
            view,
            proposal,
            price,
            state,
            realised_pnl_today,
            exposure=exposure,
        )
        guard.policy_version = str(
            execution_assumptions.get("guard_policy_version")
            or "services.execution.evaluate_proposal"
        )
        guard.evaluated_portfolio_version = f"{arm}:{opportunity.key}:cash={_money(cash)}"
        guard.quote_time = clock
        guards[opportunity.key] = guard
        guard_status = (
            "hold_no_trade" if action == "HOLD" else ("approved" if guard.approved else "rejected")
        )
        order.update(
            {
                "proposal_action": action,
                "requested_quantity": str(requested) if requested else None,
                "price_at_decision_usd": None if price is None else str(price),
                "guard_status": guard_status,
                "guard_reason": guard.reason,
                "binding_rule": guard.binding_rule or None,
                "allowed_quantity": str(guard.amount) if guard.approved else "0",
            }
        )
        if action == "HOLD":
            order["fill_rule_status"] = "hold_no_trade"
        elif not guard.approved:
            order["fill_rule_status"] = "rejected"
            order["fill_reason"] = guard.binding_rule or "rejected_by_guard"
            order["reason"] = guard.reason
        elif price is None:
            order["fill_rule_status"] = "unpriced"
            order["fill_reason"] = REASON_NO_DECISION_PRICE
            order["reason"] = REASON_NO_DECISION_PRICE
        elif guard.amount > ZERO:
            selection = select_fill_price(
                ticker=opportunity.ticker,
                decision_at=clock,
                marks=book,
                as_of=as_of,
            )
            if selection.price is None or selection.at is None:
                order["fill_rule_status"] = "unpriced"
                order["fill_reason"] = selection.reason or "no_executable_price"
                order["reason"] = order["fill_reason"]
            elif (selection.knowledge_at or selection.at) <= clock:
                # Immediate execution goes through the same fill-time path as a
                # deferred one; only the instant differs.
                validate_and_apply(
                    order=order,
                    opportunity_key=opportunity.key,
                    ticker=opportunity.ticker,
                    side=action,
                    proposal=proposal,
                    approved_quantity=guard.amount,
                    decision_at=clock,
                    price=selection.price,
                    fill_at=selection.knowledge_at or selection.at,
                )
            else:
                execution_at = selection.knowledge_at or selection.at
                order["fill_rule_status"] = "deferred"
                order["fill_reason"] = REASON_FILL_DEFERRED
                order["scheduled_fill_at"] = utc_iso(execution_at)
                order["executable_mark"] = {
                    "valid_at": utc_iso(selection.at),
                    "known_at": utc_iso(execution_at),
                    "source": selection.source,
                }
                pending.append(
                    _PendingOrder(
                        order_index=order_index,
                        opportunity_key=opportunity.key,
                        ticker=opportunity.ticker,
                        side=action,
                        proposal=proposal,
                        approved_quantity=guard.amount,
                        decision_at=clock,
                        scheduled_at=execution_at,
                        price=selection.price,
                        sequence=next_sequence(),
                    )
                )
                pending.sort(key=lambda item: (item.scheduled_at, item.sequence))
        else:
            order["fill_rule_status"] = "hold_no_trade"

        # Equity after the decision uses only the state at this clock. A
        # deferred fill is not in cash or positions yet.
        equity_after = _equity(cash, positions, book, clock)
        order["equity_after_usd"] = _money(equity_after)
        order["cash_after_decision_usd"] = _money(cash)
        order["positions_after_decision"] = _positions_dict(positions)
        order["realised_pnl_today_usd"] = _money(realised_pnl_today)
        step["decision"] = {
            "run_id": decision.run_id,
            "action": action,
            "requested_quantity": str(requested),
            "recorded_at": utc_iso(decision.recorded_at),
        }
        step["order"] = {
            "fill_rule_status": order["fill_rule_status"],
            "fill_reason": order["fill_reason"],
        }
        steps.append(step)

    # Close the window: apply every pending fill priced at or before it, and
    # name the ones the window cannot reach instead of silently dropping them.
    advance_to(window_end)
    expired = []
    while pending:
        item = pending.pop(0)
        order = orders[item.order_index]
        order["fill_rule_status"] = "expired"
        order["fill_reason"] = REASON_FILL_EXPIRED
        order["reason"] = REASON_FILL_EXPIRED
        expired.append(
            {
                "opportunity": item.opportunity_key,
                "scheduled_fill_at": utc_iso(item.scheduled_at),
                "reason": REASON_FILL_EXPIRED,
            }
        )
    steps.append(
        {
            "sequence": next_sequence(),
            "event": "window_close",
            "clock": utc_iso(window_end),
            "synthetic": True,
            "ending_state": {
                "cash_usd": _money(cash),
                "positions": _positions_dict(positions),
            },
            "expired_fills": expired,
            "realised_pnl_by_day_usd": {
                day: _money(value) for day, value in sorted(realised_by_day.items())
            },
            "available_data": {
                "known_marks": _known_marks(book, window_end),
                "realised_pnl_today_usd": _money(realised_pnl_today),
            },
        }
    )

    steps_hash = content_hash(steps)
    return {
        "orders": orders,
        "fills": fills,
        "guards": guards,
        "steps": steps,
        "steps_hash": steps_hash,
        "clock_started_at": utc_iso(opening_at),
        "clock_ended_at": utc_iso(window_end),
        "ending_cash": cash,
        "ending_positions": dict(positions),
        "expired_fills": expired,
        "realised_pnl": {
            "cost_basis": COST_BASIS_POLICY,
            "fee_policy": FEE_POLICY,
            "trading_day": TRADING_DAY_POLICY,
            "by_day_usd": {day: _money(value) for day, value in sorted(realised_by_day.items())},
            "today_usd": _money(realised_pnl_today),
        },
        "method_versions": {
            "simulation": SIMULATION_METHOD_VERSION,
            "fill_timing": FILL_TIMING_RULE,
            "fill_rule": FILL_RULE,
            "fill_validation": FILL_VALIDATION_POLICY,
            "cost_basis": COST_BASIS_POLICY,
            "fee_policy": FEE_POLICY,
            "trading_day": TRADING_DAY_POLICY,
        },
    }


# ---------------------------------------------------------------------------
# The registered metric
# ---------------------------------------------------------------------------
def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, ZERO) / len(values) if values else None


def _quantize(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(PCT4))


def _dispersion(values: Sequence[Decimal]) -> dict:
    if not values:
        return {
            "metric": "sample_standard_deviation_pct",
            "std_dev_pct": None,
            "min_pct": None,
            "max_pct": None,
            "range_pct": None,
            "observations": 0,
            "reason": "no_observations",
        }
    minimum = min(values)
    maximum = max(values)
    if len(values) < 2:
        return {
            "metric": "sample_standard_deviation_pct",
            "std_dev_pct": None,
            "min_pct": _quantize(minimum),
            "max_pct": _quantize(maximum),
            "range_pct": _quantize(maximum - minimum),
            "observations": 1,
            "reason": "fewer_than_two_observations",
        }
    mean = sum(values, ZERO) / len(values)
    variance = sum(((value - mean) ** 2 for value in values), ZERO) / (len(values) - 1)
    return {
        "metric": "sample_standard_deviation_pct",
        "std_dev_pct": _quantize(variance.sqrt()),
        "min_pct": _quantize(minimum),
        "max_pct": _quantize(maximum),
        "range_pct": _quantize(maximum - minimum),
        "observations": len(values),
        "reason": None,
    }


def _interval(values: Sequence[Decimal], uncertainty: dict) -> dict:
    from services.experiments import time_block_bootstrap

    if len(values) < 2:
        return {
            "method": "time_block_bootstrap",
            "statistic": "mean",
            "lower": None,
            "upper": None,
            "confidence": str(uncertainty.get("confidence", "0.95")),
            "block_size": int(uncertainty.get("block_size", 1) or 1),
            "resamples": 0,
            "seed": int(uncertainty.get("seed", 0) or 0),
            "observations": len(values),
            "reason": "fewer_than_two_observations",
        }
    block = max(1, int(uncertainty.get("block_size", 1) or 1))
    result = time_block_bootstrap(
        values,
        block_size=min(block, len(values)),
        resamples=int(uncertainty.get("resamples", 1000) or 1000),
        confidence=uncertainty.get("confidence", "0.95"),
        seed=int(uncertainty.get("seed", 0) or 0),
    )
    return {
        "method": result["method"],
        "statistic": result["statistic"],
        "lower": result["lower"],
        "upper": result["upper"],
        "confidence": result["confidence"],
        "block_size": result["block_size"],
        "resamples": result["resamples"],
        "seed": result["seed"],
        "observations": result["n"],
        "reason": result["reason"],
    }


#: Stable benchmark exclusion reasons. A relative result is computed only from
#: a pair where both observations fall on the instrument's own sessions.
BENCHMARK_ENTRY_MISSING = "benchmark_entry_observation_missing"
BENCHMARK_ENTRY_SESSION_MISMATCH = "benchmark_entry_session_mismatch"
BENCHMARK_HORIZON_MISSING = "benchmark_horizon_observation_missing"
BENCHMARK_HORIZON_SESSION_MISMATCH = "benchmark_horizon_session_mismatch"
BENCHMARK_HORIZON_BEFORE_TARGET = "benchmark_horizon_before_target_session"
BENCHMARK_NOT_RECORDED = "benchmark_marks_not_recorded"
BENCHMARK_INSTRUMENT_UNAVAILABLE = "instrument_horizon_unavailable"
BENCHMARK_MATCHED = "matched"


def _session_start(observed: datetime) -> datetime:
    return observed.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _mark_observation(book: PriceBook, ticker: str, at: datetime) -> dict | None:
    """The mark that produced a price, with its own observation instant."""
    for mark in book.marks_for(ticker):
        if mark.at == at:
            return {
                "price": mark.price,
                "at": mark.at,
                "observed_at": mark.observed_at or mark.at,
                "source": mark.source or "mark",
            }
    return None


def _decision_observation(decision: DecisionInput, book: PriceBook) -> dict | None:
    """The instrument's entry observation: the quote or the mark it used."""
    quote = decision.snapshot.quote_for(decision.ticker)
    price = quote.get("price_usd") or quote.get("price")
    if price not in (None, ""):
        observed = _quote_as_of(quote) or decision.decision_at
        return {
            "price": _decimal(price, field_name="quote.price"),
            "at": observed,
            "observed_at": observed,
            "source": str(quote.get("source") or "snapshot_quote"),
            "kind": "snapshot_quote",
        }
    mark = book.mark_at_or_before(decision.ticker, decision.decision_at)
    if mark is None:
        return None
    return {
        "price": mark.price,
        "at": mark.at,
        "observed_at": mark.observed_at or mark.at,
        "source": mark.source or "mark_at_or_before_decision",
        "kind": "mark_at_or_before_decision",
    }


def _benchmark_pair(
    *,
    book: PriceBook,
    ticker: str,
    entry_observed: datetime | None,
    horizon_observed: datetime | None,
    target_at: datetime | None,
    as_of: datetime | None,
) -> dict:
    """Match a benchmark entry and horizon observation to the instrument's sessions.

    The declared policy is ``benchmark_same_session_pair.v1``: the benchmark's
    entry observation must be on the instrument's entry session, its horizon
    observation on the instrument's horizon session, and the horizon observation
    must not precede the target session. It never substitutes a prior close.
    """
    pair: dict = {
        "ticker": ticker,
        "matching_policy": BENCHMARK_MATCH_POLICY,
        "target_policy": BENCHMARK_TARGET_POLICY,
        "entry": None,
        "horizon": None,
        "entry_session": None,
        "horizon_session": None,
        "return_pct": None,
        "status": "excluded",
        "reason": None,
    }
    if not book.all_marks():
        pair["reason"] = BENCHMARK_NOT_RECORDED
        return pair
    if entry_observed is None or horizon_observed is None:
        pair["reason"] = BENCHMARK_INSTRUMENT_UNAVAILABLE
        return pair
    if as_of is not None and horizon_observed > as_of:
        pair["reason"] = BENCHMARK_INSTRUMENT_UNAVAILABLE
        return pair
    entry_session = entry_observed.astimezone(UTC).date()
    horizon_session = horizon_observed.astimezone(UTC).date()
    pair["entry_session"] = entry_session.isoformat()
    pair["horizon_session"] = horizon_session.isoformat()

    entry_mark = book.mark_at_or_after(ticker, _session_start(entry_observed), as_of=as_of)
    if entry_mark is None:
        pair["reason"] = BENCHMARK_ENTRY_MISSING
        return pair
    pair["entry"] = {
        "price_usd": str(entry_mark.price),
        "at": utc_iso(entry_mark.at),
        "observed_at": utc_iso(entry_mark.observed_at or entry_mark.at),
        "session": entry_mark.at.astimezone(UTC).date().isoformat(),
        "source": entry_mark.source or "mark",
    }
    if entry_mark.at.astimezone(UTC).date() != entry_session:
        pair["reason"] = BENCHMARK_ENTRY_SESSION_MISMATCH
        return pair

    horizon_mark = book.mark_at_or_after(ticker, _session_start(horizon_observed), as_of=as_of)
    if horizon_mark is None:
        pair["reason"] = BENCHMARK_HORIZON_MISSING
        return pair
    pair["horizon"] = {
        "price_usd": str(horizon_mark.price),
        "at": utc_iso(horizon_mark.at),
        "observed_at": utc_iso(horizon_mark.observed_at or horizon_mark.at),
        "session": horizon_mark.at.astimezone(UTC).date().isoformat(),
        "source": horizon_mark.source or "mark",
    }
    if horizon_mark.at.astimezone(UTC).date() != horizon_session:
        pair["reason"] = BENCHMARK_HORIZON_SESSION_MISMATCH
        return pair
    if target_at is not None and horizon_mark.at.astimezone(UTC).date() < target_at.date():
        pair["reason"] = BENCHMARK_HORIZON_BEFORE_TARGET
        return pair
    if entry_mark.price <= ZERO:
        pair["reason"] = BENCHMARK_ENTRY_MISSING
        return pair
    pair["return_pct"] = _quantize(
        (horizon_mark.price - entry_mark.price) / entry_mark.price * Decimal("100")
    )
    pair["status"] = BENCHMARK_MATCHED
    return pair


def _metric_observation(
    *,
    decision: DecisionInput,
    horizon_days: int,
    marks: PriceBook,
    as_of: datetime | None,
    benchmark: dict | None = None,
    benchmark_book: PriceBook | None = None,
) -> dict:
    entry_observation = _decision_observation(decision, marks)
    if entry_observation is None:
        return {
            "run_id": decision.run_id,
            "opportunity": decision.opportunity_key,
            "ticker": decision.ticker,
            "action": decision.normalised_action,
            "maturity": "unavailable",
            "reason": REASON_NO_DECISION_PRICE,
            "price_source": "unavailable",
            "benchmark": {
                "ticker": (benchmark or {}).get("ticker"),
                "matching_policy": BENCHMARK_MATCH_POLICY,
                "target_policy": BENCHMARK_TARGET_POLICY,
                "entry": None,
                "horizon": None,
                "return_pct": None,
                "status": "excluded",
                "reason": BENCHMARK_INSTRUMENT_UNAVAILABLE,
            },
        }
    entry = entry_observation["price"]
    evaluation = evaluate_horizon(
        ticker=decision.ticker,
        decision_at=decision.decision_at,
        horizon_days=horizon_days,
        marks=marks,
        start_price=entry,
        as_of=as_of,
    )
    benchmark_ticker = str((benchmark or {}).get("ticker") or "").upper()
    horizon_mark = (
        _mark_observation(marks, decision.ticker, evaluation.price_observed_at)
        if evaluation.price_observed_at is not None
        else None
    )
    pair = _benchmark_pair(
        book=benchmark_book or PriceBook([]),
        ticker=benchmark_ticker,
        entry_observed=entry_observation["observed_at"],
        horizon_observed=evaluation.price_observed_at,
        target_at=evaluation.target_at,
        as_of=as_of,
    )
    base = {
        "run_id": decision.run_id,
        "opportunity": decision.opportunity_key,
        "ticker": decision.ticker,
        "action": decision.normalised_action,
        "price_at_decision": _quantize(entry),
        "price_observed_at_decision": utc_iso(entry_observation["observed_at"]),
        "price_observation_at_decision": utc_iso(entry_observation["at"]),
        "session_at_decision": entry_observation["observed_at"].astimezone(UTC).date().isoformat(),
        "price_source": entry_observation["source"],
        "price_source_kind": entry_observation["kind"],
        "benchmark": pair,
    }
    if evaluation.maturity != "evaluated" or evaluation.forward_return_pct is None:
        return {
            **base,
            "maturity": evaluation.maturity,
            "reason": evaluation.reason or "horizon_not_evaluated",
            "target_at": utc_iso(evaluation.target_at),
        }
    action = decision.normalised_action
    forward = evaluation.forward_return_pct
    relative = None
    if pair["status"] == BENCHMARK_MATCHED and pair["return_pct"] is not None:
        relative = _quantize(forward - Decimal(pair["return_pct"]))
    if action not in {"BUY", "SELL", "HOLD"}:
        return {
            **base,
            "maturity": "unavailable",
            "reason": "action_not_scored",
            "target_at": utc_iso(evaluation.target_at),
        }
    score = signed_return(action, forward)
    hit = directional_hit(action, forward)
    return {
        **base,
        "maturity": "evaluated",
        "reason": None,
        "target_at": utc_iso(evaluation.target_at),
        "price_at_horizon": _quantize(evaluation.price_at_horizon),
        "price_observed_at": utc_iso(evaluation.price_observed_at),
        "price_source_at_horizon": (horizon_mark["source"] if horizon_mark is not None else "mark"),
        "session_at_horizon": (
            evaluation.price_observed_at.astimezone(UTC).date().isoformat()
            if evaluation.price_observed_at is not None
            else None
        ),
        "session_shift_days": evaluation.session_shift_days,
        "forward_return_pct": _quantize(forward),
        "relative_forward_return_pct": relative,
        "action_score_pct": _quantize(score),
        "directional_hit": hit,
        "hold_zero_convention": action == "HOLD",
    }


def _metric_usable(row: dict, metric_id: str) -> bool:
    """Whether a row contributes a value to the registered metric."""
    if row["maturity"] != "evaluated":
        return False
    if metric_id == "mean_relative_forward_return_pct":
        return row.get("relative_forward_return_pct") is not None
    return True


def _row_exclusion_reason(row: dict, metric_id: str) -> str:
    if metric_id == "mean_relative_forward_return_pct" and row["maturity"] == "evaluated":
        return (row.get("benchmark") or {}).get("reason") or "benchmark_pair_unmatched"
    return row.get("reason") or "not_evaluated"


def _benchmark_summary(observations: Sequence[dict], benchmark: dict | None) -> dict:
    evaluated = [row for row in observations if row["maturity"] == "evaluated"]
    reasons: dict[str, int] = {}
    matched = 0
    for row in evaluated:
        if (row.get("benchmark") or {}).get("status") == BENCHMARK_MATCHED:
            matched += 1
            continue
        reason = (row.get("benchmark") or {}).get("reason") or "benchmark_pair_unmatched"
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "ticker": (benchmark or {}).get("ticker"),
        "asset_class": (benchmark or {}).get("asset_class"),
        "matching_policy": BENCHMARK_MATCH_POLICY,
        "target_policy": BENCHMARK_TARGET_POLICY,
        "scheduled_pairs": len(evaluated),
        "matched_pairs": matched,
        "excluded_pairs": len(evaluated) - matched,
        "excluded_by_reason": dict(sorted(reasons.items())),
        "note": (
            "A relative result uses matched pairs only. An excluded pair is published "
            "with the reason it was excluded; it is never treated as a zero."
        ),
    }


def _observation_state(row: dict, metric_id: str, as_of: datetime | None) -> tuple[str, str | None]:
    """Classify one scheduled observation into its published state."""
    if _metric_usable(row, metric_id):
        return "evaluated", None
    if row["maturity"] == "not_recorded":
        return (
            row.get("observation_state") or "not_recorded",
            row.get("state_reason") or "no_decision_recorded_for_this_arm",
        )
    if row["maturity"] == "unavailable" and row.get("reason") == "no_session_at_or_after_target":
        target_value = row.get("target_at")
        if target_value is not None and as_of is not None:
            try:
                target = _parse_at(target_value, field_name="target_at")
            except SimulationError:
                target = None
            if target is not None and target > as_of:
                return "pending", "target_session_after_as_of"
    return "missing", _row_exclusion_reason(row, metric_id)


def _metric_value(
    metric_id: str, observations: Sequence[dict]
) -> tuple[Decimal | None, str | None]:
    if metric_id == "mean_action_score_pct":
        values = [
            Decimal(row["action_score_pct"])
            for row in observations
            if row["maturity"] == "evaluated" and row.get("action_score_pct") is not None
        ]
        return (_mean(values), None)
    if metric_id == "mean_forward_return_pct":
        values = [
            Decimal(row["forward_return_pct"])
            for row in observations
            if row["maturity"] == "evaluated" and row.get("forward_return_pct") is not None
        ]
        return (_mean(values), None)
    if metric_id == "hit_rate_pct":
        calls = [
            row
            for row in observations
            if row["maturity"] == "evaluated"
            and row["action"] in {"BUY", "SELL"}
            and row.get("directional_hit") is not None
        ]
        if not calls:
            return None, "no_directional_calls"
        hits = sum(1 for row in calls if row["directional_hit"] is True)
        return (Decimal(hits) / Decimal(len(calls)) * Decimal("100"), None)
    if metric_id == "mean_relative_forward_return_pct":
        values = [
            Decimal(row["relative_forward_return_pct"])
            for row in observations
            if row["maturity"] == "evaluated" and row.get("relative_forward_return_pct") is not None
        ]
        if not values:
            return None, "no_matched_benchmark_pairs"
        return (_mean(values), None)
    return None, "registered_metric_has_no_calculator"


def _metric_values(metric_id: str, observations: Sequence[dict]) -> list[Decimal]:
    """The values the registered statistic is computed over, per metric id."""
    if metric_id in {"mean_action_score_pct", "mean_forward_return_pct"}:
        key = "action_score_pct" if metric_id == "mean_action_score_pct" else "forward_return_pct"
        return [
            Decimal(row[key])
            for row in observations
            if row["maturity"] == "evaluated" and row.get(key) is not None
        ]
    if metric_id == "mean_relative_forward_return_pct":
        return [
            Decimal(row["relative_forward_return_pct"])
            for row in observations
            if row["maturity"] == "evaluated" and row.get("relative_forward_return_pct") is not None
        ]
    if metric_id == "hit_rate_pct":
        return [
            Decimal("100") if row.get("directional_hit") is True else ZERO
            for row in observations
            if row["maturity"] == "evaluated"
            and row["action"] in {"BUY", "SELL"}
            and row.get("directional_hit") is not None
        ]
    return []


def _arm_slice(
    *,
    arm: str,
    metric_id: str,
    opportunities: Sequence[Any],
    decisions: Sequence[DecisionInput],
    marks: PriceBook,
    horizon_days: int,
    as_of: datetime | None,
    uncertainty: dict,
    benchmark: dict | None = None,
    benchmark_marks: Sequence[PriceMark] = (),
    run_states: dict[str, dict] | None = None,
) -> dict:
    by_opportunity = {decision.opportunity_key: decision for decision in decisions}
    run_states = run_states or {}
    benchmark_book = PriceBook(benchmark_marks)
    observations: list[dict] = []
    for opportunity in opportunities:
        decision = by_opportunity.get(opportunity.key)
        if decision is None:
            state = run_states.get(opportunity.key) or {}
            status = str(state.get("status") or "")
            failure_code = str(state.get("failure_code") or "")
            if status == "failed" and failure_code.startswith("budget"):
                observation_state = "budget_blocked"
            elif status == "failed":
                observation_state = "failed"
            else:
                observation_state = "not_recorded"
            observations.append(
                {
                    "run_id": None,
                    "opportunity": opportunity.key,
                    "ticker": opportunity.ticker,
                    "action": None,
                    "maturity": "not_recorded",
                    "observation_state": observation_state,
                    "state_reason": failure_code or "no_decision_recorded_for_this_arm",
                    "reason": failure_code or "no_decision_recorded_for_this_arm",
                }
            )
            continue
        observation = _metric_observation(
            decision=decision,
            horizon_days=horizon_days,
            marks=marks,
            as_of=as_of,
            benchmark=benchmark,
            benchmark_book=benchmark_book,
        )
        state, state_reason = _observation_state(observation, metric_id, as_of)
        failure = run_states.get(opportunity.key) or {}
        if (
            state not in {"evaluated", "pending"}
            and str(failure.get("status") or "") == "failed"
            and str(failure.get("failure_code") or "").startswith("budget")
        ):
            state = "budget_blocked"
            state_reason = str(failure.get("failure_code") or "budget_exhausted")
        observation["observation_state"] = state
        observation["state_reason"] = state_reason
        observations.append(observation)
    value, value_reason = _metric_value(metric_id, observations)
    usable = sum(1 for row in observations if _metric_usable(row, metric_id))
    scores = _metric_values(metric_id, observations)
    reasons: dict[str, int] = {}
    for row in observations:
        if _metric_usable(row, metric_id):
            continue
        reason = _row_exclusion_reason(row, metric_id)
        reasons[reason] = reasons.get(reason, 0) + 1
    state_counts = {
        "evaluated": 0,
        "pending": 0,
        "failed": 0,
        "budget_blocked": 0,
        "missing": 0,
        "not_recorded": 0,
    }
    for row in observations:
        state = str(row.get("observation_state") or "missing")
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "arm": arm,
        "status": ("computed" if value is not None else "not_computable"),
        "value": _quantize(value),
        "unit": "percent",
        "observations": usable,
        "scheduled_observations": len(observations),
        "excluded_observations": len(observations) - usable,
        "excluded_by_reason": dict(sorted(reasons.items())),
        "observation_states": state_counts,
        "benchmark_pairs": _benchmark_summary(observations, benchmark),
        "contributing_runs": [
            row["run_id"]
            for row in observations
            if _metric_usable(row, metric_id) and row.get("run_id")
        ],
        "observations_detail": observations,
        "dispersion": _dispersion(scores),
        "interval": _interval(scores, uncertainty),
        "reason": value_reason,
    }


def _empty_metric_result(
    base: dict, *, reason: str, missing_input: str | None, uncertainty: dict
) -> dict:
    return {
        **base,
        "status": "not_computable",
        "value": None,
        "reason": reason,
        "missing_input": missing_input,
        "denominator": {
            "definition": "decisions_with_a_matured_registered_horizon",
            "scheduled": 0,
            "usable": 0,
            "excluded": 0,
            "excluded_by_reason": {},
            "by_state": {
                "evaluated": 0,
                "pending": 0,
                "failed": 0,
                "budget_blocked": 0,
                "missing": 0,
                "not_recorded": 0,
            },
            "by_arm": {},
            "v4_excluded": True,
        },
        "observations": 0,
        "contributing_runs": 0,
        "contributing_run_ids": [],
        "benchmark_pairs": _benchmark_summary([], None),
        "dispersion": _dispersion([]),
        "interval": _interval([], uncertainty),
        "by_arm": {},
    }


def compute_registered_metric(
    *,
    metric: dict,
    opportunities: Sequence[Any],
    marks: Sequence[PriceMark],
    decisions_by_arm: dict[str, Sequence[DecisionInput]],
    trajectories: dict[str, dict] | None = None,
    as_of: datetime | None = None,
    uncertainty: dict | None = None,
    benchmark: dict | None = None,
    benchmark_marks: Sequence[PriceMark] = (),
    run_states_by_arm: dict[str, dict[str, dict]] | None = None,
) -> dict:
    """Compute the pre-registered metric from recorded decisions and marks.

    The registration supplies the id, family and horizon. This function does not
    substitute a constant for any of them, and a missing input is named rather
    than reported as a zero or a generic ``not implemented``.

    A relative metric is computed only from pairs where the benchmark's entry and
    horizon observations fall on the instrument's own sessions under the declared
    matching policy. Excluded pairs are published with their reason and never
    enter the mean. Pending, failed, budget-blocked, missing and not-recorded
    observations are counted separately so the scheduled-run denominator can be
    read without guessing why a row is absent.
    """
    metric = dict(metric or {})
    metric_id = str(metric.get("id") or "")
    family = str(metric.get("family") or "")
    horizon_days = metric.get("horizon_days")
    uncertainty = dict(uncertainty or {})
    run_states_by_arm = run_states_by_arm or {}
    base = {
        "id": metric_id or None,
        "family": family or None,
        "horizon_days": horizon_days,
        "direction": metric.get("direction"),
        "description": metric.get("description"),
        "unit": "percent",
        "method_version": METRIC_METHOD_VERSION,
        "pre_registered": True,
        "profitability_threshold": None,
    }
    if not metric_id or not family:
        return _empty_metric_result(
            base,
            reason="registered_metric_definition_incomplete",
            missing_input="pre_registration.primary_metric.id and .family",
            uncertainty=uncertainty,
        )
    if metric_id not in SUPPORTED_REGISTERED_METRICS:
        return _empty_metric_result(
            base,
            reason="registered_metric_not_supported",
            missing_input=f"a calculator for registered metric id {metric_id!r}",
            uncertainty=uncertainty,
        )

    implemented_arms = ["V0", "V1", "V2", "V3"]
    by_arm: dict[str, dict] = {}
    if family == "signal_diagnostics":
        if not isinstance(horizon_days, int) or horizon_days <= 0:
            return _empty_metric_result(
                base,
                reason="registered_horizon_missing_or_invalid",
                missing_input="pre_registration.primary_metric.horizon_days",
                uncertainty=uncertainty,
            )
        book = PriceBook(marks)
        for arm in implemented_arms:
            by_arm[arm] = _arm_slice(
                arm=arm,
                metric_id=metric_id,
                opportunities=opportunities,
                decisions=decisions_by_arm.get(arm, []),
                marks=book,
                horizon_days=horizon_days,
                as_of=as_of,
                uncertainty=uncertainty,
                benchmark=benchmark,
                benchmark_marks=benchmark_marks,
                run_states=run_states_by_arm.get(arm, {}),
            )
    elif family == "portfolio_performance":
        for arm in implemented_arms:
            trajectory = (trajectories or {}).get(arm) or {}
            value = trajectory.get(
                "net_of_llm_return_pct"
                if metric_id == "net_twr_pct"
                else "time_weighted_return_pct"
            )
            reason = None
            if value is None:
                reason = (
                    "trajectory_return_unavailable"
                    if not trajectory
                    else "registered_portfolio_metric_unavailable_in_trajectory"
                )
            by_arm[arm] = {
                "arm": arm,
                "status": "computed" if value is not None else "not_computable",
                "value": None if value is None else str(Decimal(value)),
                "unit": "percent",
                "observations": 1 if value is not None else 0,
                "scheduled_observations": 1,
                "excluded_observations": 0 if value is not None else 1,
                "excluded_by_reason": {} if value is not None else {reason: 1},
                "observation_states": {
                    "evaluated": 1 if value is not None else 0,
                    "pending": 0,
                    "failed": 0,
                    "budget_blocked": 0,
                    "missing": 0 if value is not None else 1,
                    "not_recorded": 0,
                },
                "benchmark_pairs": _benchmark_summary([], benchmark),
                "contributing_runs": [],
                "observations_detail": [],
                "dispersion": _dispersion([] if value is None else [Decimal(value)]),
                "interval": _interval([] if value is None else [Decimal(value)], uncertainty),
                "reason": reason,
            }
        # A portfolio TWR belongs to one arm's trajectory; averaging distinct
        # arms would be the metric-family blend the contract forbids.
        return {
            **base,
            "status": "computed_per_arm",
            "value": None,
            "reason": "portfolio_metric_is_per_arm_not_pooled",
            "missing_input": None,
            "denominator": {
                "definition": "one_trajectory_per_implemented_arm",
                "scheduled": len(implemented_arms),
                "usable": sum(1 for result in by_arm.values() if result["status"] == "computed"),
                "excluded": sum(1 for result in by_arm.values() if result["status"] != "computed"),
                "excluded_by_reason": {},
                "by_state": {
                    "evaluated": sum(
                        1 for result in by_arm.values() if result["status"] == "computed"
                    ),
                    "pending": 0,
                    "failed": 0,
                    "budget_blocked": 0,
                    "missing": sum(
                        1 for result in by_arm.values() if result["status"] != "computed"
                    ),
                    "not_recorded": 0,
                },
                "by_arm": {
                    arm: {
                        "scheduled": result["scheduled_observations"],
                        "usable": result["observations"],
                        "excluded": result["excluded_observations"],
                    }
                    for arm, result in by_arm.items()
                },
                "v4_excluded": True,
            },
            "observations": sum(result["observations"] for result in by_arm.values()),
            "contributing_runs": 0,
            "contributing_run_ids": [],
            "benchmark_pairs": _benchmark_summary([], benchmark),
            "dispersion": _dispersion([]),
            "interval": _interval([], uncertainty),
            "by_arm": by_arm,
        }
    else:
        return _empty_metric_result(
            base,
            reason="registered_metric_family_unrecognised",
            missing_input="pre_registration.primary_metric.family",
            uncertainty=uncertainty,
        )

    # The headline pools the usable observations of the implemented arms; the
    # per-arm values are the comparison and are published beside it. V4 is not
    # in the denominator: it did not execute.
    usable_rows = [
        row
        for arm in implemented_arms
        for row in by_arm[arm]["observations_detail"]
        if _metric_usable(row, metric_id)
    ]
    pooled_value, pooled_reason = _metric_value(metric_id, usable_rows)
    contributing_run_ids = [row["run_id"] for row in usable_rows if row.get("run_id")]

    excluded_by_reason: dict[str, int] = {}
    state_totals = {
        "evaluated": 0,
        "pending": 0,
        "failed": 0,
        "budget_blocked": 0,
        "missing": 0,
        "not_recorded": 0,
    }
    for arm in implemented_arms:
        for reason, count in by_arm[arm]["excluded_by_reason"].items():
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + count
        for state, count in by_arm[arm]["observation_states"].items():
            state_totals[state] = state_totals.get(state, 0) + count
    usable = len(usable_rows)
    scheduled = sum(by_arm[arm]["scheduled_observations"] for arm in implemented_arms)
    if pooled_value is None:
        status = "not_computable"
        reason = pooled_reason or "no_usable_observations"
        if metric_id == "mean_relative_forward_return_pct":
            missing_input = (
                "a matching benchmark entry and horizon observation for at least one "
                "decision; the declared policy is " + BENCHMARK_MATCH_POLICY
            )
        elif excluded_by_reason.get("no_session_at_or_after_target"):
            missing_input = (
                f"a mark at or after the registered {horizon_days}-day horizon for at least "
                "one decision"
            )
        else:
            missing_input = "at least one decision whose registered horizon can be measured"
    else:
        status = "computed"
        reason = None
        missing_input = None
    pooled_benchmark = _benchmark_summary(
        [row for arm in implemented_arms for row in by_arm[arm]["observations_detail"]],
        benchmark,
    )
    return {
        **base,
        "status": status,
        "value": _quantize(pooled_value),
        "reason": reason,
        "missing_input": missing_input,
        "denominator": {
            "definition": "decisions_with_a_matured_registered_horizon",
            "scheduled": scheduled,
            "usable": usable,
            "excluded": scheduled - usable,
            "excluded_by_reason": dict(sorted(excluded_by_reason.items())),
            "by_state": state_totals,
            "by_arm": {
                arm: {
                    "scheduled": by_arm[arm]["scheduled_observations"],
                    "usable": by_arm[arm]["observations"],
                    "excluded": by_arm[arm]["excluded_observations"],
                    "value": by_arm[arm]["value"],
                }
                for arm in implemented_arms
            },
            "v4_excluded": True,
        },
        "observations": usable,
        "contributing_runs": len(contributing_run_ids),
        "contributing_run_ids": contributing_run_ids,
        "benchmark_pairs": pooled_benchmark,
        "dispersion": _dispersion(_metric_values(metric_id, usable_rows)),
        "interval": _interval(_metric_values(metric_id, usable_rows), uncertainty),
        "by_arm": by_arm,
    }
