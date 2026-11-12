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
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from core.decision_json import content_hash, utc_iso
from services.attribution import directional_hit, signed_return
from services.execution import Exposure, evaluate_proposal
from services.ledger import PositionState
from services.portfolio_accounting import (
    FILL_RULE,
    Fill,
    PriceBook,
    PriceMark,
    select_fill_price,
)
from services.signal_diagnostics import evaluate_horizon

__all__ = [
    "FILL_TIMING_RULE",
    "METRIC_METHOD_VERSION",
    "REASON_DECISION_INPUT_AFTER_CLOCK",
    "REASON_FILL_DEFERRED",
    "REASON_FILL_EXPIRED",
    "REASON_QUOTE_AFTER_CLOCK",
    "SIMULATION_METHOD_VERSION",
    "SUPPORTED_REGISTERED_METRICS",
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
SIMULATION_METHOD_VERSION = "monotonic_event_clock.v1"

#: The fill applies at the mark's own instant, never earlier.
FILL_TIMING_RULE = "fill_applied_at_first_available_mark.v1"

#: The metric's definition (id, family, horizon, direction) comes from the
#: registration; this names the calculator's method.
METRIC_METHOD_VERSION = "registered_signal_metric.v1"

#: Stable reasons. They are part of the report contract, not prose.
REASON_FILL_DEFERRED = "fill_priced_after_simulated_clock"
REASON_FILL_EXPIRED = "fill_after_observation_window"
REASON_DECISION_INPUT_AFTER_CLOCK = "decision_input_after_simulated_clock"
REASON_QUOTE_AFTER_CLOCK = "quote_after_simulated_clock"
REASON_NO_DECISION_PRICE = "no_decision_price_known_at_clock"

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
class _PendingFill:
    fill: Fill
    opportunity_key: str
    decision_at: datetime
    order_index: int
    sequence: int


def _fee_for(notional: Decimal, assumptions: dict) -> Decimal:
    bps = _decimal(assumptions.get("fee_bps") or ZERO, field_name="execution_assumptions.fee_bps")
    return (notional * bps / Decimal("10000")).quantize(CENT)


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
        positions[fill.ticker] = max(current - fill.quantity, ZERO)
    return cash


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

    Returns orders, fills, timeline steps and the hash of the steps. The caller
    is responsible for persisting any state; this function performs no I/O.
    """
    book = PriceBook(marks)
    opening_at = window_start - timedelta(seconds=1)
    clock = opening_at
    cash = _decimal(starting_state.get("cash"), field_name="starting_state.cash")
    positions: dict[str, Decimal] = {
        str(ticker).upper(): _decimal(quantity, field_name="starting_state.positions")
        for ticker, quantity in (starting_state.get("positions") or {}).items()
    }
    average_cost: dict[str, Decimal] = dict.fromkeys(positions, ZERO)
    pending: list[_PendingFill] = []
    orders: list[dict] = []
    fills: list[Fill] = []
    guards: dict[str, Any] = {}
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
            "available_data": {"known_marks": _known_marks(book, opening_at)},
        }
    ]

    def next_sequence() -> int:
        return len(steps)

    def append_fill_step(pending_fill: _PendingFill) -> None:
        nonlocal clock
        fill = pending_fill.fill
        order = orders[pending_fill.order_index]
        order["fill"] = fill.as_dict()
        order["applied_at"] = utc_iso(fill.at)
        order["fill_rule_status"] = "filled" if fill.at <= pending_fill.decision_at else "deferred"
        if order["fill_rule_status"] == "deferred":
            order["fill_reason"] = REASON_FILL_DEFERRED
        steps.append(
            {
                "sequence": next_sequence(),
                "event": "fill",
                "clock": utc_iso(fill.at),
                "opportunity": pending_fill.opportunity_key,
                "decision_clock": utc_iso(pending_fill.decision_at),
                "fill": fill.as_dict(),
                "cash_after_usd": str(cash.quantize(CENT)),
                "positions_after": _positions_dict(positions),
                "available_data": {"known_marks": _known_marks(book, fill.at)},
                "reason": (
                    "priced_at_the_decision_clock"
                    if fill.at <= pending_fill.decision_at
                    else REASON_FILL_DEFERRED
                ),
            }
        )

    def advance_to(target: datetime) -> None:
        nonlocal cash, clock
        if target < clock:
            raise SimulationError(
                f"Simulated clock cannot move backwards from {utc_iso(clock)} to {utc_iso(target)}."
            )
        while pending and pending[0].fill.at <= target:
            item = pending.pop(0)
            clock = item.fill.at
            cash = _apply_fill(item.fill, cash=cash, positions=positions, average_cost=average_cost)
            fills.append(item.fill)
            append_fill_step(item)
        clock = target

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
            "equity_before_usd": str(equity_before.quantize(CENT)),
            "equity_after_usd": None,
            "cash_before_usd": str(cash.quantize(CENT)),
            "positions_before": _positions_dict(positions),
            "cash_after_decision_usd": str(cash.quantize(CENT)),
            "positions_after_decision": _positions_dict(positions),
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
            order["equity_after_usd"] = str(equity_before.quantize(CENT))
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
            order["equity_after_usd"] = str(equity_before.quantize(CENT))
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
            realised_pnl_today=ZERO,
            exposure=exposure,
        )
        guard.policy_version = str(
            execution_assumptions.get("guard_policy_version")
            or "services.execution.evaluate_proposal"
        )
        guard.evaluated_portfolio_version = f"{arm}:{opportunity.key}:cash={cash.quantize(CENT)}"
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
            else:
                notional = (guard.amount * selection.price).quantize(CENT)
                fee = _fee_for(notional, execution_assumptions)
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
                if fill.at <= clock:
                    cash = _apply_fill(
                        fill, cash=cash, positions=positions, average_cost=average_cost
                    )
                    fills.append(fill)
                    order["fill"] = fill.as_dict()
                    order["applied_at"] = utc_iso(fill.at)
                    order["fill_rule_status"] = "filled"
                    steps.append(
                        {
                            "sequence": next_sequence(),
                            "event": "fill",
                            "clock": utc_iso(fill.at),
                            "opportunity": opportunity.key,
                            "decision_clock": utc_iso(clock),
                            "fill": fill.as_dict(),
                            "cash_after_usd": str(cash.quantize(CENT)),
                            "positions_after": _positions_dict(positions),
                            "available_data": {"known_marks": _known_marks(book, fill.at)},
                            "reason": "priced_at_the_decision_clock",
                        }
                    )
                else:
                    order["fill_rule_status"] = "deferred"
                    order["fill_reason"] = REASON_FILL_DEFERRED
                    order["scheduled_fill_at"] = utc_iso(fill.at)
                    pending.append(
                        _PendingFill(
                            fill=fill,
                            opportunity_key=opportunity.key,
                            decision_at=clock,
                            order_index=order_index,
                            sequence=next_sequence(),
                        )
                    )
                    pending.sort(key=lambda item: (item.fill.at, item.sequence))
        else:
            order["fill_rule_status"] = "hold_no_trade"

        # Equity after the decision uses only the state at this clock. A
        # deferred fill is not in cash or positions yet.
        equity_after = _equity(cash, positions, book, clock)
        order["equity_after_usd"] = str(equity_after.quantize(CENT))
        order["cash_after_decision_usd"] = str(cash.quantize(CENT))
        order["positions_after_decision"] = _positions_dict(positions)
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
                "scheduled_fill_at": utc_iso(item.fill.at),
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
                "cash_usd": str(cash.quantize(CENT)),
                "positions": _positions_dict(positions),
            },
            "expired_fills": expired,
            "available_data": {"known_marks": _known_marks(book, window_end)},
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
        "method_versions": {
            "simulation": SIMULATION_METHOD_VERSION,
            "fill_timing": FILL_TIMING_RULE,
            "fill_rule": FILL_RULE,
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


def _decision_price(decision: DecisionInput, marks: PriceBook) -> tuple[Decimal | None, str]:
    quote = decision.snapshot.quote_for(decision.ticker)
    price = quote.get("price_usd") or quote.get("price")
    if price not in (None, ""):
        return _decimal(price, field_name="quote.price"), "snapshot_quote"
    mark = marks.mark_at_or_before(decision.ticker, decision.decision_at)
    if mark is not None:
        return mark.price, "mark_at_or_before_decision"
    return None, REASON_NO_DECISION_PRICE


def _metric_observation(
    *,
    decision: DecisionInput,
    horizon_days: int,
    marks: PriceBook,
    as_of: datetime | None,
) -> dict:
    entry, price_source = _decision_price(decision, marks)
    if entry is None:
        return {
            "run_id": decision.run_id,
            "opportunity": decision.opportunity_key,
            "ticker": decision.ticker,
            "action": decision.normalised_action,
            "maturity": "unavailable",
            "reason": REASON_NO_DECISION_PRICE,
            "price_source": price_source,
        }
    evaluation = evaluate_horizon(
        ticker=decision.ticker,
        decision_at=decision.decision_at,
        horizon_days=horizon_days,
        marks=marks,
        start_price=entry,
        as_of=as_of,
    )
    if evaluation.maturity != "evaluated" or evaluation.forward_return_pct is None:
        return {
            "run_id": decision.run_id,
            "opportunity": decision.opportunity_key,
            "ticker": decision.ticker,
            "action": decision.normalised_action,
            "maturity": evaluation.maturity,
            "reason": evaluation.reason or "horizon_not_evaluated",
            "price_at_decision": _quantize(entry),
            "price_source": price_source,
        }
    action = decision.normalised_action
    forward = evaluation.forward_return_pct
    if action not in {"BUY", "SELL", "HOLD"}:
        return {
            "run_id": decision.run_id,
            "opportunity": decision.opportunity_key,
            "ticker": decision.ticker,
            "action": action,
            "maturity": "unavailable",
            "reason": "action_not_scored",
            "price_at_decision": _quantize(entry),
            "price_source": price_source,
        }
    score = signed_return(action, forward)
    hit = directional_hit(action, forward)
    return {
        "run_id": decision.run_id,
        "opportunity": decision.opportunity_key,
        "ticker": decision.ticker,
        "action": action,
        "maturity": "evaluated",
        "reason": None,
        "price_at_decision": _quantize(entry),
        "price_source": price_source,
        "target_at": utc_iso(evaluation.target_at),
        "price_at_horizon": _quantize(evaluation.price_at_horizon),
        "price_observed_at": utc_iso(evaluation.price_observed_at),
        "session_shift_days": evaluation.session_shift_days,
        "forward_return_pct": _quantize(forward),
        "action_score_pct": _quantize(score),
        "directional_hit": hit,
        "hold_zero_convention": action == "HOLD",
    }


def _metric_value(
    metric_id: str, observations: Sequence[dict]
) -> tuple[Decimal | None, str | None]:
    evaluated = [row for row in observations if row["maturity"] == "evaluated"]
    if metric_id == "mean_action_score_pct":
        values = [
            Decimal(row["action_score_pct"])
            for row in evaluated
            if row.get("action_score_pct") is not None
        ]
        return (_mean(values), None)
    if metric_id == "mean_forward_return_pct":
        values = [
            Decimal(row["forward_return_pct"])
            for row in evaluated
            if row.get("forward_return_pct") is not None
        ]
        return (_mean(values), None)
    if metric_id == "hit_rate_pct":
        calls = [
            row
            for row in evaluated
            if row["action"] in {"BUY", "SELL"} and row.get("directional_hit") is not None
        ]
        if not calls:
            return None, "no_directional_calls"
        hits = sum(1 for row in calls if row["directional_hit"] is True)
        return (Decimal(hits) / Decimal(len(calls)) * Decimal("100"), None)
    if metric_id == "mean_relative_forward_return_pct":
        return None, "benchmark_marks_not_recorded"
    return None, "registered_metric_has_no_calculator"


def _metric_values(metric_id: str, observations: Sequence[dict]) -> list[Decimal]:
    """The values the registered statistic is computed over, per metric id."""
    evaluated = [row for row in observations if row["maturity"] == "evaluated"]
    if metric_id in {"mean_action_score_pct", "mean_forward_return_pct"}:
        key = "action_score_pct" if metric_id == "mean_action_score_pct" else "forward_return_pct"
        return [Decimal(row[key]) for row in evaluated if row.get(key) is not None]
    if metric_id == "hit_rate_pct":
        return [
            Decimal("100") if row.get("directional_hit") is True else ZERO
            for row in evaluated
            if row["action"] in {"BUY", "SELL"} and row.get("directional_hit") is not None
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
) -> dict:
    by_opportunity = {decision.opportunity_key: decision for decision in decisions}
    observations: list[dict] = []
    for opportunity in opportunities:
        decision = by_opportunity.get(opportunity.key)
        if decision is None:
            observations.append(
                {
                    "run_id": None,
                    "opportunity": opportunity.key,
                    "ticker": opportunity.ticker,
                    "action": None,
                    "maturity": "not_recorded",
                    "reason": "no_decision_recorded_for_this_arm",
                }
            )
            continue
        observations.append(
            _metric_observation(
                decision=decision,
                horizon_days=horizon_days,
                marks=marks,
                as_of=as_of,
            )
        )
    value, value_reason = _metric_value(metric_id, observations)
    evaluated = [row for row in observations if row["maturity"] == "evaluated"]
    scores = _metric_values(metric_id, observations)
    reasons: dict[str, int] = {}
    for row in observations:
        if row["maturity"] == "evaluated":
            continue
        reason = row.get("reason") or "not_evaluated"
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "arm": arm,
        "status": ("computed" if value is not None else "not_computable"),
        "value": _quantize(value),
        "unit": "percent",
        "observations": len(evaluated),
        "scheduled_observations": len(observations),
        "excluded_observations": len(observations) - len(evaluated),
        "excluded_by_reason": dict(sorted(reasons.items())),
        "contributing_runs": [row["run_id"] for row in evaluated if row.get("run_id")],
        "observations_detail": observations,
        "dispersion": _dispersion(scores),
        "interval": _interval(scores, uncertainty),
        "reason": value_reason,
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
) -> dict:
    """Compute the pre-registered metric from recorded decisions and marks.

    The registration supplies the id, family and horizon. This function does not
    substitute a constant for any of them, and a missing input is named rather
    than reported as a zero or a generic ``not implemented``.
    """
    metric = dict(metric or {})
    metric_id = str(metric.get("id") or "")
    family = str(metric.get("family") or "")
    horizon_days = metric.get("horizon_days")
    uncertainty = dict(uncertainty or {})
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
        return {
            **base,
            "status": "not_computable",
            "value": None,
            "reason": "registered_metric_definition_incomplete",
            "missing_input": "pre_registration.primary_metric.id and .family",
            "denominator": {
                "definition": "decisions_with_a_matured_registered_horizon",
                "scheduled": 0,
                "usable": 0,
                "excluded": 0,
                "excluded_by_reason": {},
                "by_arm": {},
                "v4_excluded": True,
            },
            "observations": 0,
            "contributing_runs": 0,
            "contributing_run_ids": [],
            "dispersion": _dispersion([]),
            "interval": _interval([], uncertainty),
            "by_arm": {},
        }
    if metric_id not in SUPPORTED_REGISTERED_METRICS:
        return {
            **base,
            "status": "not_computable",
            "value": None,
            "reason": "registered_metric_not_supported",
            "missing_input": f"a calculator for registered metric id {metric_id!r}",
            "denominator": {
                "definition": "decisions_with_a_matured_registered_horizon",
                "scheduled": 0,
                "usable": 0,
                "excluded": 0,
                "excluded_by_reason": {},
                "by_arm": {},
                "v4_excluded": True,
            },
            "observations": 0,
            "contributing_runs": 0,
            "contributing_run_ids": [],
            "dispersion": _dispersion([]),
            "interval": _interval([], uncertainty),
            "by_arm": {},
        }

    implemented_arms = ["V0", "V1", "V2", "V3"]
    by_arm: dict[str, dict] = {}
    if family == "signal_diagnostics":
        if not isinstance(horizon_days, int) or horizon_days <= 0:
            return {
                **base,
                "status": "not_computable",
                "value": None,
                "reason": "registered_horizon_missing_or_invalid",
                "missing_input": "pre_registration.primary_metric.horizon_days",
                "denominator": {
                    "definition": "decisions_with_a_matured_registered_horizon",
                    "scheduled": 0,
                    "usable": 0,
                    "excluded": 0,
                    "excluded_by_reason": {},
                    "by_arm": {},
                    "v4_excluded": True,
                },
                "observations": 0,
                "contributing_runs": 0,
                "contributing_run_ids": [],
                "dispersion": _dispersion([]),
                "interval": _interval([], uncertainty),
                "by_arm": {},
            }
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
            "dispersion": _dispersion([]),
            "interval": _interval([], uncertainty),
            "by_arm": by_arm,
        }
    else:
        return {
            **base,
            "status": "not_computable",
            "value": None,
            "reason": "registered_metric_family_unrecognised",
            "missing_input": "pre_registration.primary_metric.family",
            "denominator": {
                "definition": "decisions_with_a_matured_registered_horizon",
                "scheduled": 0,
                "usable": 0,
                "excluded": 0,
                "excluded_by_reason": {},
                "by_arm": {},
                "v4_excluded": True,
            },
            "observations": 0,
            "contributing_runs": 0,
            "contributing_run_ids": [],
            "dispersion": _dispersion([]),
            "interval": _interval([], uncertainty),
            "by_arm": {},
        }

    # The headline pools the usable observations of the implemented arms; the
    # per-arm values are the comparison and are published beside it. V4 is not
    # in the denominator: it did not execute.
    observations = [
        row
        for arm in implemented_arms
        for row in by_arm[arm]["observations_detail"]
        if row["maturity"] == "evaluated"
    ]
    scores = [
        Decimal(row["action_score_pct"])
        for row in observations
        if row.get("action_score_pct") is not None
    ]
    if metric_id == "hit_rate_pct":
        calls = [row for row in observations if row["action"] in {"BUY", "SELL"}]
        pooled_value = (
            Decimal(sum(1 for row in calls if row.get("directional_hit") is True))
            / Decimal(len(calls))
            * Decimal("100")
            if calls
            else None
        )
        pooled_reason = None if calls else "no_directional_calls"
    elif metric_id == "mean_forward_return_pct":
        forwards = [
            Decimal(row["forward_return_pct"])
            for row in observations
            if row.get("forward_return_pct") is not None
        ]
        pooled_value = _mean(forwards)
        pooled_reason = None
    elif metric_id == "mean_relative_forward_return_pct":
        pooled_value = None
        pooled_reason = "benchmark_marks_not_recorded"
    else:
        pooled_value = _mean(scores)
        pooled_reason = None

    excluded_by_reason: dict[str, int] = {}
    for arm in implemented_arms:
        for reason, count in by_arm[arm]["excluded_by_reason"].items():
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + count
    usable = len(observations)
    scheduled = sum(by_arm[arm]["scheduled_observations"] for arm in implemented_arms)
    contributing_run_ids = [row["run_id"] for row in observations if row.get("run_id")]
    if pooled_value is None:
        status = "not_computable"
        reason = pooled_reason or "no_usable_observations"
        if metric_id == "mean_relative_forward_return_pct":
            missing_input = (
                "benchmark marks for the declared benchmark; the opportunity set records a "
                "benchmark ticker but no benchmark prices"
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
        "dispersion": _dispersion(_metric_values(metric_id, observations)),
        "interval": _interval(_metric_values(metric_id, observations), uncertainty),
        "by_arm": by_arm,
    }
