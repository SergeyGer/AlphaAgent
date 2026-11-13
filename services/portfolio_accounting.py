"""Ledger-based portfolio accounting (MET-01 / MET-02 / MET-03 / MET-05).

This module is deliberately separate from :mod:`services.attribution`. Signal
diagnostics answer *"was the call right?"* over an instrument's forward return.
Portfolio performance answers *"what did the account earn?"* from cash, holdings,
fills, fees and external flows. The two are different metric families and the
report layer must never add one to the other: overlapping decision windows and
differing position sizes make per-decision returns non-additive (MET-05).

What is computed, and how
-------------------------
``E(t) = cash(t) + sum(quantity(i,t) * price(i,t))``

* **Cash** is reconstructed from the current portfolio balance and the immutable
  ledger: the balance is anchored at "now", and later fills/flows are reversed to
  value an earlier instant. This is exact relative to the stored balance, not
  relative to a bank statement. The basis is reported instead of hidden.
* **Positions** are replayed from every fill up to ``t``. A SELL that would take a
  position below zero is refused: closing or reducing exposure is not the same
  thing as opening a short, and a ledger that oversells is corrupt rather than
  short.
* **Marks** are explicit ``PriceMark`` rows with a validity time and a knowledge
  time. A valuation at ``t`` may only use a mark that was both valid and known at
  ``t``; this is the structural guard against look-ahead. A mark that is missing
  makes ``equity`` null - never zero.
* **Time-weighted return** chains sub-period returns across external-flow
  boundaries. When a valuation at a boundary is unavailable, the module falls
  back to Modified Dietz and marks the result ``approximate`` with a reason. A
  deposit cannot be mistaken for a return.
* **Max drawdown** is the largest decline from a prior equity peak.
* **Turnover** is ``sum(abs(quantity * fill_price)) / average equity``; the
  formula travels with the number.
* **LLM costs** are external API spend, not portfolio cash. They are reported
  separately and deducted in an explicit net-of-LLM equity series. They are never
  also booked as a flow, so they cannot be double counted.
* **Fees** come from ``Transaction.fee``. ``NULL`` means *unrecorded*, and the
  report says ``fees_complete=false`` rather than treating it as zero.

Prices are required as input; this module performs no network I/O. That is what
makes its output replayable from stored observations.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from core.decision_json import utc_iso
from core.measurement_models import CashFlow, CashFlowType
from core.models import PortfolioSnapshot, Transaction, TxType

__all__ = [
    "FILL_RULE",
    "MARK_POLICY",
    "METHOD_VERSION",
    "MODIFIED_DIETZ_METHOD",
    "TWR_METHOD",
    "AccountingError",
    "CounterfactualResult",
    "CounterfactualRule",
    "EquityPoint",
    "ExecutionPriceUnavailable",
    "ExternalFlow",
    "Fill",
    "FillPriceSelection",
    "InvalidFillError",
    "LlmCost",
    "NegativePositionError",
    "OpeningState",
    "PortfolioPerformance",
    "PriceBook",
    "PriceLeakageError",
    "PriceMark",
    "SnapshotAnchor",
    "assert_price_not_before",
    "build_performance",
    "derive_opening_state",
    "evaluate_counterfactual",
    "load_fills",
    "load_flows",
    "positions_at",
    "require_fill_price",
    "select_fill_price",
]

#: Version of every definition below. A result is only comparable to another one
#: that declares the same method version.
METHOD_VERSION = "portfolio_accounting.v1"

#: Historical simulated fills use the first executable price at or after the
#: decision. The rule is named, and stored in the result, because "entry price"
#: without a fill rule is not a measurement.
FILL_RULE = "first_executable_mark_at_or_after_decision.v1"

#: Marks are the latest observation that was valid and known at the valuation
#: instant. Stale marks are reported, not silently refreshed.
MARK_POLICY = "latest_known_mark_at_or_before_valuation.v1"
TWR_METHOD = "flow_boundary_chain.v1"
MODIFIED_DIETZ_METHOD = "modified_dietz.v1"

EXPOSURE_FORMULA = "sum(quantity * mark_price) / equity"
TURNOVER_FORMULA = "sum(abs(quantity * fill_price)) / ((opening_equity + closing_equity) / 2)"

#: A mark older than this is flagged as stale. Seven days is a declaration, not a
#: claim that a week-old price is fine for every instrument.
MARK_STALENESS_DAYS = 7

#: Reconciliation tolerance. Snapshot valuations are stored to cents.
RECONCILIATION_TOLERANCE = Decimal("0.01")

ZERO = Decimal("0")
CENT = Decimal("0.01")
PCT4 = Decimal("0.0001")
PCT1 = Decimal("0.1")
HUNDRED = Decimal("100")
SECONDS_PER_DAY = Decimal("86400")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class AccountingError(Exception):
    """Base error for an accounting input the module refuses."""


class InvalidFillError(AccountingError):
    """A fill has an impossible shape (non-positive quantity, unknown side...)."""


class NegativePositionError(InvalidFillError):
    """A SELL would take a position below zero, i.e. create a short."""


class PriceLeakageError(AccountingError):
    """A price timestamp would introduce look-ahead into a measurement."""


class ExecutionPriceUnavailable(AccountingError):
    """No executable price exists under the declared fill rule."""


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------
def _dec(value, *, field_name: str) -> Decimal:
    """Coerce an exact decimal. Floats are refused (DATA-02)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise AccountingError(
            f"{field_name} must be an exact Decimal or a string, not a float; "
            "a float silently rounds money."
        )
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AccountingError(f"{field_name} is not a decimal: {value!r}") from exc


def _aware(value, *, field_name: str):
    if value is None:
        raise AccountingError(f"{field_name} is required.")
    if timezone.is_naive(value):
        raise AccountingError(f"{field_name} must be timezone-aware (UTC).")
    return value


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(CENT)


def _pct(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(PCT4)


# ---------------------------------------------------------------------------
# Price marks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PriceMark:
    """One observed price, with when it was valid and when it became known.

    ``at`` is the instant the price describes; ``observed_at`` (default: ``at``)
    is when the system learned it. A valuation at ``t`` may only use a mark whose
    ``at`` *and* ``observed_at`` are at or before ``t``. Separating the two is what
    lets a backfill be detected: a close for Monday that was only retrieved on
    Wednesday must not value a Tuesday decision.
    """

    ticker: str
    at: datetime
    price: Decimal
    source: str = "unspecified"
    observed_at: datetime | None = None
    provenance: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", (self.ticker or "").strip().upper())
        if not self.ticker:
            raise AccountingError("A price mark needs a ticker.")
        object.__setattr__(self, "price", _dec(self.price, field_name="price"))
        if self.price <= ZERO:
            raise AccountingError(f"Price for {self.ticker} must be positive.")
        object.__setattr__(self, "at", _aware(self.at, field_name="at"))
        observed = self.observed_at if self.observed_at is not None else self.at
        object.__setattr__(self, "observed_at", _aware(observed, field_name="observed_at"))
        if self.observed_at < self.at:
            raise AccountingError(
                f"Mark for {self.ticker} claims to be known before it is valid "
                f"(observed_at {self.observed_at} < at {self.at})."
            )

    @property
    def knowledge_at(self) -> datetime:
        """The instant the mark may first be used."""
        return self.observed_at or self.at

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "at": utc_iso(self.at),
            "observed_at": utc_iso(self.observed_at),
            "price_usd": str(self.price),
            "source": self.source,
            "provenance": self.provenance or None,
        }


class PriceBook:
    """Indexed marks with explicit no-look-ahead lookup.

    There is intentionally no ``price()`` method that ignores time: every caller
    has to say whether it wants the latest *known-at-or-before* mark (valuation)
    or the first *at-or-after* mark (a simulated fill).
    """

    def __init__(self, marks: Iterable[PriceMark] = ()) -> None:
        grouped: dict[str, list[PriceMark]] = {}
        for mark in marks:
            if not isinstance(mark, PriceMark):
                raise AccountingError("PriceBook accepts PriceMark instances only.")
            grouped.setdefault(mark.ticker, []).append(mark)
        self._marks = {
            ticker: tuple(sorted(values, key=lambda item: (item.at, item.observed_at, item.price)))
            for ticker, values in grouped.items()
        }

    def __bool__(self) -> bool:
        return bool(self._marks)

    def tickers(self) -> list[str]:
        return sorted(self._marks)

    def marks_for(self, ticker: str) -> tuple[PriceMark, ...]:
        return self._marks.get((ticker or "").upper(), ())

    def all_marks(self) -> list[PriceMark]:
        return [mark for values in self._marks.values() for mark in values]

    def mark_at_or_before(self, ticker: str, at: datetime) -> PriceMark | None:
        """Latest mark valid and known at or before ``at`` (no look-ahead)."""
        _aware(at, field_name="at")
        usable = [
            mark for mark in self.marks_for(ticker) if mark.at <= at and mark.knowledge_at <= at
        ]
        return max(usable, key=lambda item: (item.at, item.knowledge_at)) if usable else None

    def mark_at_or_after(
        self, ticker: str, at: datetime, *, as_of: datetime | None = None
    ) -> PriceMark | None:
        """First mark valid at or after ``at``, optionally known by ``as_of``."""
        _aware(at, field_name="at")
        if as_of is not None:
            _aware(as_of, field_name="as_of")
        candidates = [
            mark
            for mark in self.marks_for(ticker)
            if mark.at >= at and (as_of is None or mark.knowledge_at <= as_of)
        ]
        return (
            min(candidates, key=lambda item: (item.at, item.knowledge_at)) if candidates else None
        )

    def source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for mark in self.all_marks():
            counts[mark.source] = counts.get(mark.source, 0) + 1
        return dict(sorted(counts.items()))


def _book(marks: PriceBook | Iterable[PriceMark]) -> PriceBook:
    return marks if isinstance(marks, PriceBook) else PriceBook(marks)


@dataclass(frozen=True)
class FillPriceSelection:
    """The outcome of applying the declared fill rule to one decision."""

    ticker: str
    decision_at: datetime
    fill_rule: str
    price: Decimal | None = None
    at: datetime | None = None
    #: When the selected mark became known. A simulated fill is executed at
    #: this instant, never at the earlier validity instant, so a backfilled mark
    #: cannot be used before the simulation could have learned it.
    knowledge_at: datetime | None = None
    source: str | None = None
    rejected_earlier_marks: int = 0
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.price is not None

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "decision_at": utc_iso(self.decision_at),
            "fill_rule": self.fill_rule,
            "price_usd": None if self.price is None else str(self.price),
            "at": utc_iso(self.at),
            "knowledge_at": utc_iso(self.knowledge_at),
            "source": self.source,
            "rejected_earlier_marks": self.rejected_earlier_marks,
            "reason": self.reason,
        }


def select_fill_price(
    *,
    ticker: str,
    decision_at: datetime,
    marks: PriceBook | Iterable[PriceMark],
    fill_rule: str = FILL_RULE,
    as_of: datetime | None = None,
) -> FillPriceSelection:
    """First executable price at or after the decision under ``fill_rule``.

    Marks strictly before ``decision_at`` are counted and **excluded**. If no
    forward mark exists, the result carries ``price=None`` and a reason; the
    earlier price is never substituted. That substitution is the single most
    common way a backtest manufactures an entry it could not have got.
    """
    symbol = (ticker or "").strip().upper()
    _aware(decision_at, field_name="decision_at")
    book = _book(marks)
    earlier = [mark for mark in book.marks_for(symbol) if mark.at < decision_at]
    mark = book.mark_at_or_after(symbol, decision_at, as_of=as_of)
    if mark is None:
        return FillPriceSelection(
            ticker=symbol,
            decision_at=decision_at,
            fill_rule=fill_rule,
            rejected_earlier_marks=len(earlier),
            reason="no_price_at_or_after_decision",
        )
    return FillPriceSelection(
        ticker=symbol,
        decision_at=decision_at,
        fill_rule=fill_rule,
        price=mark.price,
        at=mark.at,
        knowledge_at=mark.knowledge_at,
        source=mark.source,
        rejected_earlier_marks=len(earlier),
    )


def require_fill_price(
    *,
    ticker: str,
    decision_at: datetime,
    marks: PriceBook | Iterable[PriceMark],
    fill_rule: str = FILL_RULE,
    as_of: datetime | None = None,
) -> FillPriceSelection:
    """As :func:`select_fill_price`, but raises when no executable price exists.

    Batch evaluation should use :func:`select_fill_price` and record the
    instrument as unavailable; a single simulated execution that claims to have
    filled without a price is a bug, so this wrapper refuses to return one.
    """
    selection = select_fill_price(
        ticker=ticker,
        decision_at=decision_at,
        marks=marks,
        fill_rule=fill_rule,
        as_of=as_of,
    )
    if selection.price is None:
        raise ExecutionPriceUnavailable(
            f"No executable price for {selection.ticker} at or after "
            f"{utc_iso(decision_at)} under {fill_rule}; "
            f"{selection.rejected_earlier_marks} earlier mark(s) were rejected."
        )
    return selection


def assert_price_not_before(*, price_at: datetime, decision_at: datetime) -> None:
    """Refuse a price stamp that precedes the decision it would fill."""
    _aware(price_at, field_name="price_at")
    _aware(decision_at, field_name="decision_at")
    if price_at < decision_at:
        raise PriceLeakageError(
            f"A price observed at {utc_iso(price_at)} cannot fill a decision made at "
            f"{utc_iso(decision_at)}: it exists before the decision."
        )


# ---------------------------------------------------------------------------
# Ledger primitives
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Fill:
    """One executed trade, as accounting sees it.

    ``fee=None`` means no fee was recorded. The cash effect uses a zero known fee
    so an equity series can still be built, but ``fee_recorded`` stays False and
    the caller must not present the result as fee-complete.
    """

    at: datetime
    ticker: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal | None = None
    executed_by: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _aware(self.at, field_name="fill.at"))
        symbol = (self.ticker or "").strip().upper()
        if not symbol:
            raise InvalidFillError("A fill needs a ticker.")
        object.__setattr__(self, "ticker", symbol)
        side = (self.side or "").strip().upper()
        if side not in {TxType.BUY, TxType.SELL}:
            raise InvalidFillError(f"Unknown fill side {self.side!r}; expected BUY or SELL.")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", _dec(self.quantity, field_name="quantity"))
        object.__setattr__(self, "price", _dec(self.price, field_name="price"))
        if self.quantity <= ZERO:
            raise InvalidFillError("A fill quantity must be positive.")
        if self.price <= ZERO:
            raise InvalidFillError("A fill price must be positive.")
        if self.fee is not None:
            object.__setattr__(self, "fee", _dec(self.fee, field_name="fee"))
            if self.fee < ZERO:
                raise InvalidFillError("A fee cannot be negative.")

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price

    @property
    def fee_recorded(self) -> bool:
        return self.fee is not None

    @property
    def known_fee(self) -> Decimal:
        return self.fee if self.fee is not None else ZERO

    @property
    def cash_effect(self) -> Decimal:
        """Signed change to cash, using the recorded fee when one exists."""
        if self.side == TxType.BUY:
            return -(self.notional + self.known_fee)
        return self.notional - self.known_fee

    def as_dict(self) -> dict:
        return {
            "at": utc_iso(self.at),
            "ticker": self.ticker,
            "side": self.side,
            "quantity": str(self.quantity),
            "price_usd": str(self.price),
            "notional_usd": str(_money(self.notional)),
            "fee_usd": str(self.fee) if self.fee is not None else None,
            "fee_recorded": self.fee_recorded,
            "executed_by": self.executed_by or None,
            "source": self.source or None,
        }


@dataclass(frozen=True)
class ExternalFlow:
    """A deposit or withdrawal - money, not performance.

    The account is valued in USD and no FX series is stored, so a non-USD flow is
    refused rather than silently added to a USD balance.
    """

    at: datetime
    flow_type: str
    amount: Decimal
    currency: str = "USD"
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _aware(self.at, field_name="flow.at"))
        flow = (self.flow_type or "").strip().upper()
        if flow not in CashFlowType.values:
            raise AccountingError(f"Unknown flow type {self.flow_type!r}.")
        object.__setattr__(self, "flow_type", flow)
        object.__setattr__(self, "amount", _dec(self.amount, field_name="flow.amount"))
        if self.amount <= ZERO:
            raise AccountingError("A cash flow amount must be positive.")
        object.__setattr__(self, "currency", (self.currency or "USD").strip().upper())
        if self.currency != "USD":
            raise AccountingError(
                f"Cash flow in {self.currency} cannot be added to a USD account: no FX "
                "series is stored. Convert it or record a USD flow."
            )

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.flow_type == CashFlowType.DEPOSIT else -self.amount

    def as_dict(self) -> dict:
        return {
            "at": utc_iso(self.at),
            "flow_type": self.flow_type,
            "amount_usd": str(_money(self.amount)),
            "currency": self.currency,
            "signed_usd": str(_money(self.signed_amount)),
            "source": self.source or None,
        }


@dataclass(frozen=True)
class LlmCost:
    """One model/API cost, attributed to the instant it was incurred.

    ``amount`` is spend on the provider account, not a portfolio cash movement.
    The accounting layer deducts it only in the net-of-LLM series and never also
    records it as a flow.
    """

    at: datetime
    amount: Decimal
    source: str = ""
    run_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _aware(self.at, field_name="llm_cost.at"))
        object.__setattr__(self, "amount", _dec(self.amount, field_name="llm_cost.amount"))
        if self.amount < ZERO:
            raise AccountingError("An LLM cost cannot be negative.")

    def as_dict(self) -> dict:
        return {
            "at": utc_iso(self.at),
            "amount_usd": str(self.amount.quantize(Decimal("0.000001"))),
            "source": self.source or None,
            "run_key": self.run_key or None,
        }


@dataclass(frozen=True)
class SnapshotAnchor:
    """A stored aggregate valuation used only to reconcile the ledger replay.

    ``PortfolioSnapshot`` stores cash, positions value and equity, but not
    per-instrument quantities or prices. It therefore cannot value a historical
    position on its own; it can only check that a replay agrees with a recorded
    total.
    """

    at: datetime
    equity_usd: Decimal
    cash_usd: Decimal | None = None
    positions_value_usd: Decimal | None = None
    source: str = "portfolio_snapshot"

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _aware(self.at, field_name="snapshot.at"))
        object.__setattr__(self, "equity_usd", _dec(self.equity_usd, field_name="equity_usd"))
        for name in ("cash_usd", "positions_value_usd"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _dec(value, field_name=name))

    def as_dict(self) -> dict:
        return {
            "at": utc_iso(self.at),
            "equity_usd": str(_money(self.equity_usd)),
            "cash_usd": None if self.cash_usd is None else str(_money(self.cash_usd)),
            "positions_value_usd": (
                None if self.positions_value_usd is None else str(_money(self.positions_value_usd))
            ),
            "source": self.source,
            "detail": "aggregate_only",
        }


@dataclass
class OpeningState:
    """Cash and quantities at the start of an accounting window."""

    at: datetime
    cash: Decimal
    positions: dict[str, Decimal] = field(default_factory=dict)
    basis: str = "declared"
    flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.at = _aware(self.at, field_name="opening.at")
        self.cash = _dec(self.cash, field_name="opening.cash")
        cleaned: dict[str, Decimal] = {}
        for ticker, quantity in (self.positions or {}).items():
            symbol = (ticker or "").strip().upper()
            if not symbol:
                raise AccountingError("An opening position needs a ticker.")
            amount = _dec(quantity, field_name="opening.quantity")
            if amount < ZERO:
                raise NegativePositionError(
                    f"Opening position {symbol} is negative ({amount}); a short must be "
                    "explicit, and this accounting does not model one."
                )
            if amount > ZERO:
                cleaned[symbol] = amount
        self.positions = dict(sorted(cleaned.items()))
        self.flags = tuple(dict.fromkeys(self.flags))


# ---------------------------------------------------------------------------
# Loading stored ledger rows
# ---------------------------------------------------------------------------
def load_fills(
    portfolio,
    *,
    after: datetime | None = None,
    on_or_before: datetime | None = None,
) -> list[Fill]:
    """Load fills from the immutable ledger, ordered oldest first."""
    queryset = Transaction.objects.filter(portfolio=portfolio)
    if after is not None:
        queryset = queryset.filter(timestamp__gt=after)
    if on_or_before is not None:
        queryset = queryset.filter(timestamp__lte=on_or_before)
    fills = [
        Fill(
            at=row.timestamp,
            ticker=row.ticker,
            side=row.tx_type,
            quantity=row.amount,
            price=row.price,
            fee=row.fee,
            executed_by=row.executed_by,
            source=f"transaction:{row.id}",
        )
        for row in queryset.order_by("timestamp", "id")
    ]
    return fills


def load_flows(
    portfolio,
    *,
    after: datetime | None = None,
    on_or_before: datetime | None = None,
) -> list[ExternalFlow]:
    """Load external cash flows, ordered oldest first."""
    queryset = CashFlow.objects.filter(portfolio=portfolio)
    if after is not None:
        queryset = queryset.filter(occurred_at__gt=after)
    if on_or_before is not None:
        queryset = queryset.filter(occurred_at__lte=on_or_before)
    return [
        ExternalFlow(
            at=row.occurred_at,
            flow_type=row.flow_type,
            amount=row.amount,
            currency=row.currency or "USD",
            source=row.source or f"cash_flow:{row.id}",
        )
        for row in queryset.order_by("occurred_at", "id")
    ]


def load_snapshot_anchors(portfolio, *, after=None, on_or_before=None) -> list[SnapshotAnchor]:
    """Load stored aggregate valuations for reconciliation, never for pricing."""
    queryset = PortfolioSnapshot.objects.filter(portfolio=portfolio)
    if after is not None:
        queryset = queryset.filter(captured_at__gt=after)
    if on_or_before is not None:
        queryset = queryset.filter(captured_at__lte=on_or_before)
    return [
        SnapshotAnchor(
            at=row.captured_at,
            equity_usd=row.total_equity_usd,
            cash_usd=row.cash_balance_usd,
            positions_value_usd=row.positions_value_usd,
            source=f"portfolio_snapshot:{row.id}",
        )
        for row in queryset.order_by("captured_at", "id")
    ]


def positions_at(fills: Iterable[Fill], at: datetime, *, strict: bool = True) -> dict[str, Decimal]:
    """Replay fills up to ``at`` into per-ticker quantities.

    ``strict`` refuses a sale of more than is held. The Phase 0 ledger replay
    clamps silently; that is appropriate for defensive reporting but not for
    accounting, where an oversell is a corrupt input rather than a short.
    """
    _aware(at, field_name="positions_at")
    positions: dict[str, Decimal] = {}
    for fill in sorted(fills, key=lambda item: (item.at, item.ticker, item.side, item.source)):
        if fill.at > at:
            continue
        held = positions.get(fill.ticker, ZERO)
        if fill.side == TxType.BUY:
            positions[fill.ticker] = held + fill.quantity
        else:
            if strict and fill.quantity > held:
                raise NegativePositionError(
                    f"SELL {fill.quantity} {fill.ticker} at {utc_iso(fill.at)} exceeds the "
                    f"{held} held; this would create a short, which MET-01 does not permit "
                    "without an explicit strategy."
                )
            remaining = held - fill.quantity
            if remaining > ZERO:
                positions[fill.ticker] = remaining
            else:
                positions.pop(fill.ticker, None)
    return dict(sorted(positions.items()))


def derive_opening_state(
    portfolio,
    *,
    start: datetime,
    fills: Iterable[Fill] | None = None,
    flows: Iterable[ExternalFlow] | None = None,
    cash: Decimal | None = None,
    positions: dict[str, Decimal] | None = None,
) -> OpeningState:
    """Reconstruct the state at ``start`` from the ledger and current balance.

    ``cash(t) = balance_now - sum(cash effect of every event after t)``. This is
    exact relative to the stored current balance, which is the anchor the system
    has. When the balance was edited outside the ledger the two disagree, and
    the reconciliation block reports the difference rather than smoothing it.
    """
    _aware(start, field_name="start")
    if (cash is None) != (positions is None):
        raise AccountingError("Pass both opening cash and positions, or neither.")
    if fills is None:
        fills = load_fills(portfolio)
    else:
        fills = list(fills)
    if flows is None:
        flows = load_flows(portfolio)
    else:
        flows = list(flows)

    derived_positions = positions_at(fills, start)
    if positions is not None:
        declared = positions_at(
            [
                Fill(
                    at=start,
                    ticker=ticker,
                    side=TxType.BUY,
                    quantity=quantity,
                    price=Decimal("1"),
                    source="declared_opening",
                )
                for ticker, quantity in positions.items()
            ],
            start,
        )
        return OpeningState(
            at=start,
            cash=cash,
            positions=declared,
            basis="declared",
            flags=("opening_state_declared_not_ledger_verified",),
        )

    effects_after = sum(
        (fill.cash_effect for fill in fills if fill.at > start),
        ZERO,
    ) + sum((flow.signed_amount for flow in flows if flow.at > start), ZERO)
    derived_cash = portfolio.balance_usd - effects_after

    flags: list[str] = ["cash_anchored_to_current_balance"]
    if any(fill.at <= start and not fill.fee_recorded for fill in fills):
        flags.append("fees_unknown_before_window")
    if derived_cash < ZERO:
        flags.append("derived_opening_cash_negative")

    return OpeningState(
        at=start,
        cash=derived_cash,
        positions=derived_positions,
        basis="reverse_ledger_from_current_balance",
        flags=tuple(flags),
    )


# ---------------------------------------------------------------------------
# Equity points
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EquityPoint:
    """One valuation of the portfolio after all events at ``at``."""

    at: datetime
    cash: Decimal
    positions_value: Decimal | None
    equity: Decimal | None
    net_equity: Decimal | None
    gross_exposure_pct: Decimal | None
    fees_paid_usd: Decimal
    llm_cost_usd: Decimal
    position_values: dict[str, Decimal]
    unpriced_tickers: tuple[str, ...]
    mark_sources: dict[str, str]
    oldest_mark_age_days: Decimal | None
    events: tuple[str, ...]

    @property
    def priced(self) -> bool:
        return self.equity is not None

    def as_dict(self) -> dict:
        return {
            "at": utc_iso(self.at),
            "cash_usd": str(_money(self.cash)),
            "positions_value_usd": (
                None if self.positions_value is None else str(_money(self.positions_value))
            ),
            "equity_usd": None if self.equity is None else str(_money(self.equity)),
            "equity_net_of_llm_usd": (
                None if self.net_equity is None else str(_money(self.net_equity))
            ),
            "gross_exposure_pct": (
                None if self.gross_exposure_pct is None else str(_pct(self.gross_exposure_pct))
            ),
            "position_values_usd": {
                ticker: str(_money(value)) for ticker, value in sorted(self.position_values.items())
            },
            "fees_paid_usd": str(_money(self.fees_paid_usd)),
            "llm_cost_cumulative_usd": str(self.llm_cost_usd.quantize(Decimal("0.000001"))),
            "unpriced_tickers": list(self.unpriced_tickers),
            "mark_sources": dict(self.mark_sources),
            "oldest_mark_age_days": (
                None
                if self.oldest_mark_age_days is None
                else str(self.oldest_mark_age_days.quantize(PCT1))
            ),
            "events": list(self.events),
        }


@dataclass
class PortfolioPerformance:
    """The portfolio-performance metric family for one window."""

    start: datetime
    end: datetime
    currency: str
    opening_equity_usd: Decimal | None
    closing_equity_usd: Decimal | None
    closing_cash_usd: Decimal | None
    closing_positions_value_usd: Decimal | None
    points: list[EquityPoint]
    flow_boundaries: list[dict]
    flows: list[ExternalFlow]
    fills: list[Fill]
    twr: dict
    net_twr: dict
    drawdown: dict
    net_drawdown: dict
    exposure: dict
    turnover: dict
    contributions: dict
    cash_reconciliation: dict
    fees: dict
    llm_costs: dict
    reconciliation: dict
    methodology: dict
    flags: tuple[str, ...]
    provenance: dict

    @property
    def approximate(self) -> bool:
        return bool(self.methodology.get("approximate"))

    def as_dict(self) -> dict:
        flags = list(self.flags)
        series = [point.as_dict() for point in self.points]
        net_series = [
            {
                "at": utc_iso(point.at),
                "equity_net_of_llm_usd": (
                    None if point.net_equity is None else str(_money(point.net_equity))
                ),
            }
            for point in self.points
        ]
        net_twr = dict(self.net_twr)
        if not self.llm_costs.get("costs_complete", False):
            net_twr["known_cost_only_return_pct"] = net_twr.get("return_pct")
            net_twr["return_pct"] = None
            net_twr["approximate"] = True
            net_twr["reasons"] = [
                *net_twr.get("reasons", []),
                "llm_costs_incomplete",
            ]
        return {
            "metric_family": "portfolio_performance",
            "method_version": METHOD_VERSION,
            "currency": self.currency,
            "synthetic": bool(self.provenance.get("synthetic")),
            "provenance": self.provenance,
            "window": {
                "start": utc_iso(self.start),
                "end": utc_iso(self.end),
                "timezone": "UTC",
            },
            "opening_equity_usd": (
                None if self.opening_equity_usd is None else str(_money(self.opening_equity_usd))
            ),
            "closing_equity_usd": (
                None if self.closing_equity_usd is None else str(_money(self.closing_equity_usd))
            ),
            "closing_cash_usd": (
                None if self.closing_cash_usd is None else str(_money(self.closing_cash_usd))
            ),
            "closing_positions_value_usd": (
                None
                if self.closing_positions_value_usd is None
                else str(_money(self.closing_positions_value_usd))
            ),
            "cumulative_return_pct": self._cumulative_return_pct(),
            "time_weighted_return_pct": self.twr.get("return_pct"),
            "net_of_llm_return_pct": self.llm_costs.get("net_return_pct"),
            "twr": self.twr,
            "net_twr": net_twr,
            "max_drawdown_pct": self.drawdown.get("max_drawdown_pct"),
            "drawdown": self.drawdown,
            "net_max_drawdown_pct": self.net_drawdown.get("max_drawdown_pct"),
            "exposure": self.exposure,
            "turnover": self.turnover,
            "contribution_analysis": self.contributions,
            "cash_reconciliation": self.cash_reconciliation,
            "trade_count": len(self.fills),
            "risk_adjusted": _risk_adjusted_block(self.points, self.twr),
            "fees": self.fees,
            "llm_costs": self.llm_costs,
            "external_flows": [flow.as_dict() for flow in self.flows],
            "fills": [fill.as_dict() for fill in self.fills],
            "equity_series": series,
            "net_equity_series": net_series,
            "flow_boundaries": self.flow_boundaries,
            "reconciliation": self.reconciliation,
            "methodology": self.methodology,
            "flags": flags,
            "approximate": self.approximate,
        }

    def _cumulative_return_pct(self) -> str | None:
        """Simple end/start change. Not flow-adjusted, and labelled as such."""
        if not self.opening_equity_usd or not self.closing_equity_usd:
            return None
        if self.opening_equity_usd <= ZERO:
            return None
        value = (
            (self.closing_equity_usd - self.opening_equity_usd) / self.opening_equity_usd * HUNDRED
        ).quantize(PCT4)
        return str(value)


# ---------------------------------------------------------------------------
# Building the series
# ---------------------------------------------------------------------------
def _value_positions(state_positions: dict[str, Decimal], book: PriceBook, at: datetime) -> dict:
    """Value held positions at ``at`` using only marks known by ``at``."""
    total = ZERO
    unpriced: list[str] = []
    sources: dict[str, str] = {}
    values: dict[str, Decimal] = {}
    oldest_age: Decimal | None = None

    for ticker, quantity in sorted(state_positions.items()):
        if quantity <= ZERO:
            continue
        mark = book.mark_at_or_before(ticker, at)
        if mark is None:
            unpriced.append(ticker)
            continue
        value = quantity * mark.price
        total += value
        values[ticker] = value.quantize(CENT)
        sources[ticker] = mark.source
        age = Decimal(str((at - mark.at).total_seconds())) / SECONDS_PER_DAY
        oldest_age = age if oldest_age is None else max(oldest_age, age)

    if unpriced:
        return {
            "positions_value": None,
            "unpriced": tuple(unpriced),
            "sources": sources,
            "values": values,
            "oldest_age": oldest_age,
        }

    return {
        "positions_value": total.quantize(CENT),
        "unpriced": (),
        "sources": sources,
        "values": values,
        "oldest_age": oldest_age,
    }


def _cumulative_cost(costs: list[LlmCost], at: datetime) -> Decimal:
    return sum((cost.amount for cost in costs if cost.at <= at), ZERO)


def _make_point(
    *,
    at: datetime,
    state_cash: Decimal,
    state_positions: dict[str, Decimal],
    book: PriceBook,
    fees_paid: Decimal,
    llm_cumulative: Decimal,
    events: tuple[str, ...],
) -> EquityPoint:
    valued = _value_positions(state_positions, book, at)
    positions_value = valued["positions_value"]
    equity = None if positions_value is None else (state_cash + positions_value).quantize(CENT)
    net_equity = None if equity is None else (equity - llm_cumulative).quantize(CENT)
    gross_exposure = None
    if equity is not None and equity > ZERO and positions_value is not None:
        gross_exposure = (positions_value / equity * HUNDRED).quantize(PCT4)
    return EquityPoint(
        at=at,
        cash=state_cash,
        positions_value=positions_value,
        equity=equity,
        net_equity=net_equity,
        gross_exposure_pct=gross_exposure,
        fees_paid_usd=fees_paid,
        llm_cost_usd=llm_cumulative,
        position_values=valued["values"],
        unpriced_tickers=valued["unpriced"],
        mark_sources=valued["sources"],
        oldest_mark_age_days=valued["oldest_age"],
        events=events,
    )


def _twr(
    *,
    start: datetime,
    end: datetime,
    opening_equity: Decimal | None,
    closing_equity: Decimal | None,
    boundaries: list[dict],
    flows: list[ExternalFlow],
    approximate_reasons: list[str],
) -> dict:
    """Exact flow-boundary chain, or Modified Dietz with the approximation named."""
    reasons = list(approximate_reasons)
    base = opening_equity
    exact = opening_equity is not None and closing_equity is not None
    subperiod_returns: list[Decimal] = []

    if exact and any(not boundary.get("valuation_exact", True) for boundary in boundaries):
        exact = False
        reasons.append("flow_boundary_valuation_not_exact")

    if exact:
        for boundary in boundaries:
            before = boundary["equity_before_usd"]
            after = boundary["equity_after_usd"]
            if before is None or after is None or base is None or base <= ZERO:
                exact = False
                break
            before = Decimal(str(before))
            after = Decimal(str(after))
            subperiod_returns.append((before / base - 1) * HUNDRED)
            if after <= ZERO:
                exact = False
                break
            base = after
        if exact and base is not None and base > ZERO:
            subperiod_returns.append((closing_equity / base - 1) * HUNDRED)
        else:
            exact = False

    if exact:
        factor = Decimal("1")
        for value in subperiod_returns:
            factor *= Decimal("1") + (value / HUNDRED)
        return {
            "method": TWR_METHOD,
            "method_version": METHOD_VERSION,
            "return_pct": str(((factor - 1) * HUNDRED).quantize(PCT4)),
            "approximate": bool(reasons),
            "reasons": reasons,
            "subperiod_returns_pct": [str(value.quantize(PCT4)) for value in subperiod_returns],
            "flow_boundary_count": len(boundaries),
            "note": (
                "Each sub-period ends at an external flow boundary; the flow changes the "
                "next base but is not itself a return."
            ),
        }

    if opening_equity is None or closing_equity is None:
        return {
            "method": "unavailable",
            "method_version": METHOD_VERSION,
            "return_pct": None,
            "approximate": True,
            "reasons": [*reasons, "opening_or_closing_equity_unavailable"],
            "subperiod_returns_pct": [],
            "flow_boundary_count": len(boundaries),
            "note": "A TWR needs both endpoints; a missing valuation is not a zero.",
        }

    window_seconds = Decimal(str((end - start).total_seconds()))
    net_flow = sum((flow.signed_amount for flow in flows), ZERO)
    weighted_flow = ZERO
    if window_seconds > ZERO:
        for flow in flows:
            weight = Decimal(str((end - flow.at).total_seconds())) / window_seconds
            weighted_flow += flow.signed_amount * weight
    denominator = opening_equity + weighted_flow
    if any(
        boundary.get("equity_before_usd") is None or boundary.get("equity_after_usd") is None
        for boundary in boundaries
    ):
        reasons.append("flow_boundary_valuation_unavailable")
    if denominator <= ZERO:
        return {
            "method": MODIFIED_DIETZ_METHOD,
            "method_version": METHOD_VERSION,
            "return_pct": None,
            "approximate": True,
            "reasons": [*reasons, "modified_dietz_denominator_non_positive"],
            "subperiod_returns_pct": [],
            "flow_boundary_count": len(boundaries),
            "note": "Modified Dietz is a fallback; a non-positive denominator has no return.",
        }
    value = ((closing_equity - opening_equity - net_flow) / denominator) * HUNDRED
    return {
        "method": MODIFIED_DIETZ_METHOD,
        "method_version": METHOD_VERSION,
        "return_pct": str(value.quantize(PCT4)),
        "approximate": True,
        "reasons": reasons,
        "subperiod_returns_pct": [],
        "flow_boundary_count": len(boundaries),
        "note": (
            "An exact boundary valuation was unavailable, so this is Modified Dietz. "
            "It is an approximation and must not be presented as a chained TWR."
        ),
    }


def _max_drawdown(points: list[EquityPoint], *, net: bool = False) -> dict:
    values = [
        (point.at, point.net_equity if net else point.equity)
        for point in points
        if (point.net_equity if net else point.equity) is not None
    ]
    usable = [(at, value) for at, value in values if value is not None and value > ZERO]
    if not usable:
        return {
            "max_drawdown_pct": None,
            "observations": 0,
            "reason": "no_priced_equity_points",
        }
    peak_value = usable[0][1]
    peak_at = usable[0][0]
    worst = ZERO
    worst_peak_value = peak_value
    worst_trough_value = peak_value
    worst_peak_at = peak_at
    worst_trough_at = peak_at
    for at, value in usable:
        if value > peak_value:
            peak_value = value
            peak_at = at
        drawdown = (value - peak_value) / peak_value * HUNDRED
        if drawdown < worst:
            worst = drawdown
            worst_peak_value = peak_value
            worst_peak_at = peak_at
            worst_trough_value = value
            worst_trough_at = at
    return {
        "max_drawdown_pct": str(worst.quantize(PCT4)),
        "observations": len(usable),
        "peak_equity_usd": str(_money(worst_peak_value)),
        "peak_at": utc_iso(worst_peak_at),
        "trough_equity_usd": str(_money(worst_trough_value)),
        "trough_at": utc_iso(worst_trough_at),
        "definition": "maximum decline from a prior equity peak over priced observations",
        "sample_limited": len(usable) < 2,
    }


def _risk_adjusted_block(points: list[EquityPoint], twr: dict) -> dict:
    """Sharpe is not reported; the disclosure says why rather than omitting it."""
    observations = len(twr.get("subperiod_returns_pct") or [])
    return {
        "sharpe": {
            "included": False,
            "value": None,
            "reason": (
                "The valuation frequency is event-driven, not a fixed daily series. A Sharpe "
                "ratio needs a declared frequency, annualisation and risk-free rate; this "
                "report does not fabricate them."
            ),
            "frequency": "event_driven",
            "annualization": None,
            "risk_free_rate": None,
            "observations": observations,
            "sample_limits": (
                f"{observations} sub-period return(s); fewer than the 30 a stable Sharpe would "
                "need, and the spacing is not uniform."
            ),
        }
    }


def _contributions(
    *,
    opening: OpeningState,
    end: datetime,
    fills: list[Fill],
    book: PriceBook,
    points: list[EquityPoint],
    opening_equity: Decimal | None,
    closing_equity: Decimal | None,
    flows: list[ExternalFlow],
) -> dict:
    """Per-instrument P&L contribution, with quantities and capital weights.

    This is the additive analysis MET-05 asks for. It works from fills and
    marks, not from decision scores: a per-decision return is not a contribution
    to the account.
    """
    tickers = sorted({*opening.positions, *(fill.ticker for fill in fills)})
    rows: list[dict] = []
    point_by_time = {point.at: point for point in points}
    for ticker in tickers:
        ticker_fills = [fill for fill in fills if fill.ticker == ticker]
        start_quantity = opening.positions.get(ticker, ZERO)
        bought_quantity = sum(
            (fill.quantity for fill in ticker_fills if fill.side == TxType.BUY), ZERO
        )
        sold_quantity = sum(
            (fill.quantity for fill in ticker_fills if fill.side == TxType.SELL), ZERO
        )
        end_quantity = start_quantity + bought_quantity - sold_quantity
        start_mark = book.mark_at_or_before(ticker, opening.at)
        end_mark = book.mark_at_or_before(ticker, end)
        start_value = start_quantity * start_mark.price if start_mark is not None else None
        end_value = end_quantity * end_mark.price if end_mark is not None else None
        buys = sum((fill.notional for fill in ticker_fills if fill.side == TxType.BUY), ZERO)
        sells = sum((fill.notional for fill in ticker_fills if fill.side == TxType.SELL), ZERO)
        fees = sum((fill.known_fee for fill in ticker_fills), ZERO)
        unknown_fees = sum(1 for fill in ticker_fills if not fill.fee_recorded)
        pnl = None
        if start_value is not None and end_value is not None:
            pnl = (end_value - start_value - buys + sells - fees).quantize(CENT)

        weights: list[Decimal] = []
        for point in points:
            value = point.position_values.get(ticker)
            if value is not None and point.equity is not None and point.equity > ZERO:
                weights.append(value / point.equity * HUNDRED)
        average_weight = (sum(weights, ZERO) / len(weights)).quantize(PCT4) if weights else None

        def event_payload(fill: Fill) -> dict:
            value = fill.as_dict()
            point = point_by_time.get(fill.at)
            equity = point.equity if point is not None else None
            value["capital_weight_pct"] = (
                str((fill.notional / equity * HUNDRED).quantize(PCT4))
                if equity is not None and equity > ZERO
                else None
            )
            return value

        rows.append(
            {
                "ticker": ticker,
                "opening_quantity": str(start_quantity),
                "closing_quantity": str(end_quantity),
                "net_quantity_change": str(end_quantity - start_quantity),
                "opening_price_usd": None if start_mark is None else str(start_mark.price),
                "closing_price_usd": None if end_mark is None else str(end_mark.price),
                "buy_notional_usd": str(_money(buys)),
                "sell_notional_usd": str(_money(sells)),
                "fees_recorded_usd": str(_money(fees)),
                "unknown_fee_fill_count": unknown_fees,
                "realised_and_unrealised_pnl_usd": None if pnl is None else str(_money(pnl)),
                "pnl_pct_of_opening_equity": (
                    str((pnl / opening_equity * HUNDRED).quantize(PCT4))
                    if pnl is not None and opening_equity is not None and opening_equity > ZERO
                    else None
                ),
                "average_capital_weight_pct": (
                    None if average_weight is None else str(average_weight)
                ),
                "entries": [
                    event_payload(fill) for fill in ticker_fills if fill.side == TxType.BUY
                ],
                "exits": [event_payload(fill) for fill in ticker_fills if fill.side == TxType.SELL],
            }
        )

    contributions = [row["realised_and_unrealised_pnl_usd"] for row in rows]
    total_pnl = (
        sum((Decimal(value) for value in contributions), ZERO)
        if all(value is not None for value in contributions)
        else None
    )
    net_flow = sum((flow.signed_amount for flow in flows), ZERO)
    expected_change = (
        (closing_equity - opening_equity - net_flow)
        if closing_equity is not None and opening_equity is not None
        else None
    )
    reconciles = None
    if total_pnl is not None and expected_change is not None:
        reconciles = abs(total_pnl - expected_change) <= RECONCILIATION_TOLERANCE
    return {
        "rows": rows,
        "total_pnl_usd": None if total_pnl is None else str(_money(total_pnl)),
        "equity_change_excluding_flows_usd": (
            None if expected_change is None else str(_money(expected_change))
        ),
        "reconciles_to_equity_change": reconciles,
        "reconciliation_tolerance_usd": str(RECONCILIATION_TOLERANCE),
        "basis": "fills_and_marks.v1",
        "note": (
            "Contribution is additive within a position over this window: closing value minus "
            "opening value minus net invested cash minus recorded fees. It is computed from "
            "the ledger, not from per-decision scores. Unknown fees make the reconciliation "
            "optimistic and are counted."
        ),
    }


def _exposure(points: list[EquityPoint]) -> dict:
    priced = [point for point in points if point.gross_exposure_pct is not None]
    if not priced:
        return {
            "average_gross_pct": None,
            "end_gross_pct": None,
            "formula": EXPOSURE_FORMULA,
            "observations": 0,
        }
    average = sum((point.gross_exposure_pct for point in priced), ZERO) / len(priced)
    return {
        "average_gross_pct": str(average.quantize(PCT4)),
        "end_gross_pct": str(priced[-1].gross_exposure_pct.quantize(PCT4)),
        "formula": EXPOSURE_FORMULA,
        "observations": len(priced),
    }


def _turnover(
    fills: list[Fill], opening_equity: Decimal | None, closing_equity: Decimal | None
) -> dict:
    notional = sum((fill.notional for fill in fills), ZERO)
    if opening_equity is not None and closing_equity is not None:
        average = (opening_equity + closing_equity) / 2
        basis = "mean_of_opening_and_closing_equity"
    else:
        average = None
        basis = "unavailable"
    ratio = None
    pct = None
    if average is not None and average > ZERO:
        ratio = notional / average
        pct = (ratio * HUNDRED).quantize(PCT4)
    return {
        "traded_notional_usd": str(_money(notional)),
        "average_equity_usd": None if average is None else str(_money(average)),
        "average_equity_basis": basis,
        "ratio": None if ratio is None else str(ratio.quantize(PCT4)),
        "pct": None if pct is None else str(pct),
        "formula": TURNOVER_FORMULA,
        "trade_count": len(fills),
        "fees_included_in_notional": False,
    }


def _fees(fills: list[Fill]) -> dict:
    unknown = [fill for fill in fills if not fill.fee_recorded]
    recorded = sum((fill.known_fee for fill in fills), ZERO)
    return {
        "recorded_fees_usd": str(_money(recorded)),
        "fees_complete": not unknown,
        "fill_count": len(fills),
        "unknown_fee_fill_count": len(unknown),
        "unknown_fee_fill_sources": [fill.source or fill.ticker for fill in unknown],
        "policy": "recorded_fees_only.v1",
        "note": (
            "NULL fee means no fee was recorded for that fill, not that the fee was zero. "
            "The equity series uses recorded fees only and is marked approximate when any "
            "fill in the window is unknown."
        ),
    }


def _llm_cost_block(
    *,
    costs: list[LlmCost],
    missing_count: int,
    net_twr: dict,
    points: list[EquityPoint],
) -> dict:
    recorded = sum((cost.amount for cost in costs), ZERO)
    complete = missing_count == 0
    return {
        "recorded_cost_usd": (
            None if not costs and missing_count else str(recorded.quantize(Decimal("0.000001")))
        ),
        "cost_events": [cost.as_dict() for cost in costs],
        "cost_event_count": len(costs),
        "missing_cost_run_count": missing_count,
        "costs_complete": complete,
        "net_return_pct": net_twr.get("return_pct") if complete else None,
        "known_cost_only_return_pct": net_twr.get("return_pct"),
        "net_return_established": complete and net_twr.get("return_pct") is not None,
        "net_return_reason": (
            None
            if complete
            else (
                f"{missing_count} run(s) reported no cost. A missing cost is not treated "
                "as zero, so the net-of-LLM return cannot be established."
            )
        ),
        "equity_series_net_of_llm": [
            {
                "at": utc_iso(point.at),
                "equity_net_of_llm_usd": (
                    None if point.net_equity is None else str(_money(point.net_equity))
                ),
            }
            for point in points
        ],
        "funding": "external_provider_account",
        "deducted_from_portfolio_cash": False,
        "double_counted": False,
        "method": "explicit_net_of_llm_equity_series.v1",
        "note": (
            "LLM/API costs are not portfolio cash flows. They are deducted only in the "
            "net series, so they are not also subtracted from the gross equity."
        ),
    }


def _cash_reconciliation(
    *,
    opening: OpeningState,
    fills: list[Fill],
    flows: list[ExternalFlow],
    closing_cash: Decimal,
) -> dict:
    """Every recorded cash movement must explain the closing cash balance."""
    deposits = sum((flow.amount for flow in flows if flow.flow_type == CashFlowType.DEPOSIT), ZERO)
    withdrawals = sum(
        (flow.amount for flow in flows if flow.flow_type == CashFlowType.WITHDRAWAL), ZERO
    )
    buy_notional = sum((fill.notional for fill in fills if fill.side == TxType.BUY), ZERO)
    sell_notional = sum((fill.notional for fill in fills if fill.side == TxType.SELL), ZERO)
    fees = sum((fill.known_fee for fill in fills), ZERO)
    expected = opening.cash + deposits - withdrawals - buy_notional + sell_notional - fees
    difference = (closing_cash - expected).quantize(CENT)
    unknown = sum(1 for fill in fills if not fill.fee_recorded)
    status = "matched" if abs(difference) <= RECONCILIATION_TOLERANCE else "mismatched"
    if unknown:
        status = "matched_with_unrecorded_fees" if status == "matched" else status
    return {
        "status": status,
        "opening_cash_usd": str(_money(opening.cash)),
        "deposits_usd": str(_money(deposits)),
        "withdrawals_usd": str(_money(withdrawals)),
        "buy_notional_usd": str(_money(buy_notional)),
        "sell_notional_usd": str(_money(sell_notional)),
        "fees_recorded_usd": str(_money(fees)),
        "expected_closing_cash_usd": str(_money(expected)),
        "computed_closing_cash_usd": str(_money(closing_cash)),
        "difference_usd": str(difference),
        "unknown_fee_fill_count": unknown,
        "tolerance_usd": str(RECONCILIATION_TOLERANCE),
        "formula": (
            "opening_cash + deposits - withdrawals - buy_notional + sell_notional - recorded_fees"
        ),
        "note": (
            "Unknown fees are not guessed; they are counted. A matched result with an "
            "unknown fee means the known movements reconcile, not that the fee was zero."
        ),
    }


def _reconciliation(anchors: list[SnapshotAnchor], points: list[EquityPoint]) -> dict:
    by_time = {point.at: point for point in points}
    rows = []
    for anchor in anchors:
        point = by_time.get(anchor.at)
        computed = point.equity if point is not None else None
        difference = None
        status = "no_computed_valuation"
        if computed is not None:
            difference = (computed - anchor.equity_usd).quantize(CENT)
            status = "matched" if abs(difference) <= RECONCILIATION_TOLERANCE else "mismatched"
        rows.append(
            {
                **anchor.as_dict(),
                "computed_equity_usd": None if computed is None else str(_money(computed)),
                "difference_usd": None if difference is None else str(difference),
                "status": status,
                "tolerance_usd": str(RECONCILIATION_TOLERANCE),
            }
        )
    statuses = {row["status"] for row in rows}
    if not rows:
        overall = "no_snapshot"
    elif "mismatched" in statuses:
        overall = "mismatched"
    elif "no_computed_valuation" in statuses:
        overall = "not_comparable"
    else:
        overall = "matched"
    return {
        "status": overall,
        "anchors": rows,
        "snapshot_detail": "aggregate_only",
        "note": (
            "A PortfolioSnapshot stores aggregate cash and positions value, not "
            "per-instrument quantities. It can reconcile a replay total; it cannot "
            "value a historical position on its own."
        ),
    }


def build_performance(
    *,
    opening: OpeningState,
    end: datetime,
    fills: Iterable[Fill] = (),
    flows: Iterable[ExternalFlow] = (),
    marks: PriceBook | Iterable[PriceMark] = (),
    llm_costs: Iterable[LlmCost] = (),
    llm_cost_missing_count: int = 0,
    snapshot_anchors: Iterable[SnapshotAnchor] = (),
    provenance: dict | None = None,
    currency: str = "USD",
) -> PortfolioPerformance:
    """Replay a window into the portfolio-performance metric family.

    Every event must be inside ``(opening.at, end]``; pre-window events belong in
    the opening state. That refusal is deliberate: silently dropping an earlier
    fill would understate positions, and silently applying it twice would
    overstate them.
    """
    start = opening.at
    _aware(end, field_name="end")
    if end < start:
        raise AccountingError("The accounting window ends before it starts.")

    fills = list(fills)
    if any(not isinstance(fill, Fill) for fill in fills):
        raise AccountingError("Only Fill instances are accepted.")
    fills.sort(key=lambda item: (item.at, item.ticker, item.side, item.source))
    for fill in fills:
        if fill.at <= start:
            raise InvalidFillError(
                f"Fill at {utc_iso(fill.at)} is not after the window start "
                f"{utc_iso(start)}; include it in the opening state instead."
            )
        if fill.at > end:
            raise InvalidFillError(
                f"Fill at {utc_iso(fill.at)} is after the window end {utc_iso(end)}."
            )

    flows = list(flows)
    if any(not isinstance(flow, ExternalFlow) for flow in flows):
        raise AccountingError("Only ExternalFlow instances are accepted.")
    flows.sort(key=lambda item: (item.at, item.flow_type, item.source))
    for flow in flows:
        if flow.at <= start:
            raise AccountingError(
                f"Flow at {utc_iso(flow.at)} is not after the window start "
                f"{utc_iso(start)}; include it in the opening state instead."
            )
        if flow.at > end:
            raise AccountingError(
                f"Flow at {utc_iso(flow.at)} is after the window end {utc_iso(end)}."
            )

    costs = list(llm_costs)
    if any(not isinstance(cost, LlmCost) for cost in costs):
        raise AccountingError("Only LlmCost instances are accepted.")
    costs.sort(key=lambda item: (item.at, item.source, item.run_key))
    if llm_cost_missing_count < 0:
        raise AccountingError("llm_cost_missing_count cannot be negative.")

    book = _book(marks)
    # A fill is itself a known price observation at its execution instant. Adding
    # it lets the position be valued on the day it was opened without inventing a
    # separate close.
    extra_marks = [
        PriceMark(
            ticker=fill.ticker,
            at=fill.at,
            price=fill.price,
            source="fill",
            provenance=fill.source,
        )
        for fill in fills
    ]
    book = PriceBook([*book.all_marks(), *extra_marks])

    anchors = sorted(
        (anchor for anchor in snapshot_anchors if isinstance(anchor, SnapshotAnchor)),
        key=lambda item: item.at,
    )

    times = {start, end}
    times.update(fill.at for fill in fills)
    times.update(flow.at for flow in flows)
    times.update(cost.at for cost in costs if start <= cost.at <= end)
    times.update(anchor.at for anchor in anchors if start <= anchor.at <= end)
    times.update(mark.at for mark in book.all_marks() if start <= mark.at <= end)
    ordered_times = sorted(times)

    state_positions = dict(opening.positions)
    state_cash = opening.cash
    fees_paid = ZERO

    points: list[EquityPoint] = []
    # Pre-flow valuations live beside the public series so drawdown can see an
    # intra-instant dip that the post-flow point would hide. They are not emitted
    # twice in the equity series.
    pre_flow_points: list[EquityPoint] = []
    boundaries: list[dict] = []

    opening_point = _make_point(
        at=start,
        state_cash=state_cash,
        state_positions=state_positions,
        book=book,
        fees_paid=fees_paid,
        llm_cumulative=_cumulative_cost(costs, start),
        events=(),
    )
    points.append(opening_point)

    for at in ordered_times:
        if at == start:
            continue
        flows_at = [flow for flow in flows if flow.at == at]
        fills_at = [fill for fill in fills if fill.at == at]

        if flows_at:
            before = _make_point(
                at=at,
                state_cash=state_cash,
                state_positions=state_positions,
                book=book,
                fees_paid=fees_paid,
                llm_cumulative=_cumulative_cost(costs, at),
                events=tuple(f"flow_before:{flow.flow_type}" for flow in flows_at),
            )
            flow_total = sum((flow.signed_amount for flow in flows_at), ZERO)
            after_equity = (
                None if before.equity is None else (before.equity + flow_total).quantize(CENT)
            )
            after_net = (
                None
                if before.net_equity is None
                else (before.net_equity + flow_total).quantize(CENT)
            )
            pre_flow_points.append(before)
            boundaries.append(
                {
                    "at": utc_iso(at),
                    "flow_usd": str(_money(flow_total)),
                    "equity_before_usd": (
                        None if before.equity is None else str(_money(before.equity))
                    ),
                    "equity_after_usd": (
                        None if after_equity is None else str(_money(after_equity))
                    ),
                    "net_equity_before_usd": (
                        None if before.net_equity is None else str(_money(before.net_equity))
                    ),
                    "net_equity_after_usd": (None if after_net is None else str(_money(after_net))),
                    "valuation_exact": before.oldest_mark_age_days in (None, ZERO),
                    "barrier": "external_flow",
                }
            )

        events: list[str] = []
        for flow in flows_at:
            state_cash += flow.signed_amount
            events.append(f"FLOW {flow.flow_type} {flow.amount}")
        for fill in fills_at:
            held = state_positions.get(fill.ticker, ZERO)
            if fill.side == TxType.BUY:
                state_positions[fill.ticker] = held + fill.quantity
            else:
                if fill.quantity > held:
                    raise NegativePositionError(
                        f"SELL {fill.quantity} {fill.ticker} at {utc_iso(at)} exceeds the "
                        f"{held} held at that instant; this is not a short sale and the "
                        "accounting refuses it."
                    )
                remaining = held - fill.quantity
                if remaining > ZERO:
                    state_positions[fill.ticker] = remaining
                else:
                    state_positions.pop(fill.ticker, None)
            state_cash += fill.cash_effect
            fees_paid += fill.known_fee
            events.append(
                f"{fill.side} {fill.quantity} {fill.ticker} @ {fill.price}"
                + ("" if fill.fee_recorded else " (fee unrecorded)")
            )

        points.append(
            _make_point(
                at=at,
                state_cash=state_cash,
                state_positions=state_positions,
                book=book,
                fees_paid=fees_paid,
                llm_cumulative=_cumulative_cost(costs, at),
                events=tuple(events),
            )
        )

    points.sort(key=lambda point: point.at)
    closing = points[-1]

    fees_block = _fees(fills)
    unknown_fees = not fees_block["fees_complete"]
    stale_points = [
        point
        for point in points
        if point.oldest_mark_age_days is not None
        and point.oldest_mark_age_days > Decimal(MARK_STALENESS_DAYS)
    ]

    approximate_reasons: list[str] = []
    if unknown_fees:
        approximate_reasons.append("fees_unrecorded_for_some_fills")
    if stale_points:
        approximate_reasons.append("marks_older_than_staleness_threshold")

    twr = _twr(
        start=start,
        end=end,
        opening_equity=opening_point.equity,
        closing_equity=closing.equity,
        boundaries=boundaries,
        flows=flows,
        approximate_reasons=approximate_reasons,
    )
    net_boundaries = [
        {
            **boundary,
            "equity_before_usd": boundary["net_equity_before_usd"],
            "equity_after_usd": boundary["net_equity_after_usd"],
        }
        for boundary in boundaries
    ]
    net_twr = _twr(
        start=start,
        end=end,
        opening_equity=opening_point.net_equity,
        closing_equity=closing.net_equity,
        boundaries=net_boundaries,
        flows=flows,
        approximate_reasons=list(approximate_reasons),
    )

    # Order matters when a mark updates at the same instant as a flow: the
    # pre-flow valuation is the one that shows the dip, so it must come first.
    drawdown_observations = [
        item[2]
        for item in sorted(
            [(point.at, 1, point) for point in points]
            + [(point.at, 0, point) for point in pre_flow_points],
            key=lambda item: (item[0], item[1]),
        )
    ]
    drawdown = _max_drawdown(drawdown_observations)
    net_drawdown = _max_drawdown(drawdown_observations, net=True)
    exposure = _exposure(points)
    turnover = _turnover(fills, opening_point.equity, closing.equity)
    contributions = _contributions(
        opening=opening,
        end=end,
        fills=fills,
        book=book,
        points=points,
        opening_equity=opening_point.equity,
        closing_equity=closing.equity,
        flows=flows,
    )
    cash_reconciliation = _cash_reconciliation(
        opening=opening,
        fills=fills,
        flows=flows,
        closing_cash=closing.cash,
    )
    llm_block = _llm_cost_block(
        costs=costs,
        missing_count=llm_cost_missing_count,
        net_twr=net_twr,
        points=points,
    )
    reconciliation = _reconciliation(anchors, points)

    data_flags: list[str] = []
    if unknown_fees:
        data_flags.append("fees_incomplete")
    if llm_cost_missing_count:
        data_flags.append("llm_costs_incomplete")
    if any(not point.priced for point in points):
        data_flags.append("unpriced_equity_points")
    if not costs and llm_cost_missing_count:
        data_flags.append("llm_cost_not_reported")
    if reconciliation["status"] == "mismatched":
        data_flags.append("snapshot_reconciliation_mismatch")
    if stale_points:
        data_flags.append("stale_marks")

    methodology = {
        "version": METHOD_VERSION,
        "equity_definition": "E(t) = cash(t) + sum(quantity(i,t) * price(i,t))",
        "cash_basis": opening.basis,
        "cash_basis_flags": list(opening.flags),
        "opening_cash_usd": str(_money(opening.cash)),
        "opening_positions": {
            ticker: str(quantity) for ticker, quantity in opening.positions.items()
        },
        "position_basis": "full_ledger_replay_from_portfolio_inception",
        "short_policy": (
            "A SELL may only close or reduce a long. An oversell is refused rather than "
            "silently treated as a short."
        ),
        "mark_policy": MARK_POLICY,
        "mark_sources": book.source_counts(),
        "mark_staleness_days": MARK_STALENESS_DAYS,
        "valuation_timing": (
            "End of timestamp: external flows are applied before fills at the same instant."
        ),
        "fill_rule": FILL_RULE,
        "twr_method": twr["method"],
        "twr_note": twr["note"],
        "fee_policy": fees_block["policy"],
        "llm_cost_policy": llm_block["method"],
        "turnover_formula": TURNOVER_FORMULA,
        "drawdown_definition": (
            "maximum decline from a prior equity peak over priced observations"
        ),
        "approximate": twr["approximate"] or bool(approximate_reasons),
        "approximate_reasons": list(dict.fromkeys(twr["reasons"])),
        "observations": len(points),
        "priced_observations": sum(1 for point in points if point.priced),
        "unpriced_observations": sum(1 for point in points if not point.priced),
        "flow_count": len(flows),
        "fill_count": len(fills),
    }

    return PortfolioPerformance(
        start=start,
        end=end,
        currency=currency,
        opening_equity_usd=opening_point.equity,
        closing_equity_usd=closing.equity,
        closing_cash_usd=closing.cash,
        closing_positions_value_usd=closing.positions_value,
        points=points,
        flow_boundaries=boundaries,
        flows=flows,
        fills=fills,
        twr=twr,
        net_twr=net_twr,
        drawdown=drawdown,
        net_drawdown=net_drawdown,
        exposure=exposure,
        turnover=turnover,
        contributions=contributions,
        cash_reconciliation=cash_reconciliation,
        fees=fees_block,
        llm_costs=llm_block,
        reconciliation=reconciliation,
        methodology=methodology,
        flags=tuple(data_flags),
        provenance=provenance or {"source": "stored_ledger", "synthetic": False},
    )


# ---------------------------------------------------------------------------
# Counterfactuals (MET-05)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CounterfactualRule:
    """A declared rule for measuring what a rejected proposal *would* have done.

    Without one, "the guard prevented a loss" is an assertion, not a
    measurement. The horizon range is a sensitivity, not a confidence interval.
    """

    holding_period_days: int = 7
    horizons_days: tuple[int, ...] = (1, 7, 30)
    exit_rule: str = "first_known_mark_at_or_after_horizon.v1"
    quantity_basis: str = "allowed_quantity"
    include_fees: bool = False

    def __post_init__(self) -> None:
        if self.holding_period_days <= 0:
            raise AccountingError("holding_period_days must be positive.")
        horizons = tuple(sorted({int(value) for value in self.horizons_days}))
        if not horizons or any(value <= 0 for value in horizons):
            raise AccountingError("horizons_days must be non-empty positive integers.")
        object.__setattr__(self, "horizons_days", horizons)
        if self.quantity_basis not in {"requested_quantity", "allowed_quantity"}:
            raise AccountingError(
                "quantity_basis must be 'requested_quantity' or 'allowed_quantity'."
            )

    def as_dict(self) -> dict:
        return {
            "holding_period_days": self.holding_period_days,
            "horizons_days": list(self.horizons_days),
            "exit_rule": self.exit_rule,
            "quantity_basis": self.quantity_basis,
            "include_fees": self.include_fees,
        }


@dataclass
class CounterfactualResult:
    """One hypothetical outcome, with its uncertainty and its non-claim."""

    established: bool
    ticker: str
    action: str
    quantity: Decimal
    rule: CounterfactualRule | None
    entry: FillPriceSelection | None
    exit: dict | None
    pnl_usd: Decimal | None
    return_pct: Decimal | None
    horizons: list[dict]
    uncertainty: dict
    assumptions: list[str]
    reason: str | None
    established_by_rejection_alone: bool = False

    def as_dict(self) -> dict:
        return {
            "established": self.established,
            "established_by_rejection_alone": self.established_by_rejection_alone,
            "ticker": self.ticker,
            "action": self.action,
            "quantity": str(self.quantity),
            "rule": self.rule.as_dict() if self.rule else None,
            "entry": self.entry.as_dict() if self.entry else None,
            "exit": self.exit,
            "hypothetical_pnl_usd": None if self.pnl_usd is None else str(_money(self.pnl_usd)),
            "hypothetical_return_pct": (
                None if self.return_pct is None else str(self.return_pct.quantize(PCT4))
            ),
            "horizon_sensitivity": self.horizons,
            "uncertainty": self.uncertainty,
            "assumptions": self.assumptions,
            "reason": self.reason,
            "note": (
                "A counterfactual is not executed performance. It is one hypothetical path "
                "under a declared rule and must never enter the portfolio equity series."
            ),
        }


def evaluate_counterfactual(
    *,
    ticker: str,
    decision_at: datetime,
    action: str,
    quantity,
    marks: PriceBook | Iterable[PriceMark],
    rule: CounterfactualRule | None = None,
    as_of: datetime | None = None,
) -> CounterfactualResult:
    """Measure a rejected proposal under a declared holding/exit rule.

    With no rule, the result is explicitly *not established*. A rejected trade
    on its own says nothing about what was avoided; the counterfactual has to say
    how long it would have been held and at what price it would have exited.
    """
    symbol = (ticker or "").strip().upper()
    action = (action or "").strip().upper()
    _aware(decision_at, field_name="decision_at")
    amount = _dec(quantity if quantity is not None else ZERO, field_name="quantity")

    if rule is None:
        return CounterfactualResult(
            established=False,
            ticker=symbol,
            action=action,
            quantity=amount,
            rule=None,
            entry=None,
            exit=None,
            pnl_usd=None,
            return_pct=None,
            horizons=[],
            uncertainty={
                "method": "none",
                "confidence_interval": None,
                "reason": (
                    "No counterfactual holding/exit rule was declared, so loss prevention "
                    "is not established."
                ),
            },
            assumptions=[],
            reason="no_declared_holding_exit_rule",
        )

    assumptions = [
        f"entry fill rule: {FILL_RULE}",
        f"exit rule: {rule.exit_rule}",
        "quantity basis: " + rule.quantity_basis,
        "no slippage and no market impact",
        "no fees"
        if not rule.include_fees
        else "recorded fees are not available for a simulated fill",
    ]
    if action not in {TxType.BUY, TxType.SELL} or amount <= ZERO:
        return CounterfactualResult(
            established=False,
            ticker=symbol,
            action=action,
            quantity=amount,
            rule=rule,
            entry=None,
            exit=None,
            pnl_usd=None,
            return_pct=None,
            horizons=[],
            uncertainty={
                "method": "none",
                "confidence_interval": None,
                "reason": "Only a positive BUY or SELL proposal has a counterfactual.",
            },
            assumptions=assumptions,
            reason="not_a_directional_proposal",
        )

    book = _book(marks)
    entry = select_fill_price(ticker=symbol, decision_at=decision_at, marks=book, as_of=as_of)
    if entry.price is None:
        return CounterfactualResult(
            established=False,
            ticker=symbol,
            action=action,
            quantity=amount,
            rule=rule,
            entry=entry,
            exit=None,
            pnl_usd=None,
            return_pct=None,
            horizons=[],
            uncertainty={
                "method": "none",
                "confidence_interval": None,
                "reason": "No executable entry price exists under the declared fill rule.",
            },
            assumptions=assumptions,
            reason="entry_price_unavailable",
        )

    horizons: list[dict] = []
    for days in rule.horizons_days:
        target = decision_at + timedelta(days=days)
        exit_mark = book.mark_at_or_after(symbol, target, as_of=as_of)
        if exit_mark is None:
            horizons.append(
                {
                    "holding_days": days,
                    "exit_at": None,
                    "exit_price_usd": None,
                    "pnl_usd": None,
                    "return_pct": None,
                    "reason": "no_exit_mark_at_or_after_horizon",
                }
            )
            continue
        move = (exit_mark.price - entry.price) / entry.price * HUNDRED
        signed_move = move if action == TxType.BUY else -move
        pnl = amount * (exit_mark.price - entry.price)
        if action == TxType.SELL:
            pnl = -pnl
        horizons.append(
            {
                "holding_days": days,
                "exit_at": utc_iso(exit_mark.at),
                "exit_price_usd": str(exit_mark.price),
                "pnl_usd": str(_money(pnl)),
                "return_pct": str(signed_move.quantize(PCT4)),
                "reason": None,
            }
        )

    primary = next(
        (row for row in horizons if row["holding_days"] == rule.holding_period_days), None
    )
    available = [row for row in horizons if row["return_pct"] is not None]
    if primary is None or primary["return_pct"] is None:
        return CounterfactualResult(
            established=False,
            ticker=symbol,
            action=action,
            quantity=amount,
            rule=rule,
            entry=entry,
            exit=primary,
            pnl_usd=None,
            return_pct=None,
            horizons=horizons,
            uncertainty={
                "method": "horizon_range",
                "confidence_interval": None,
                "reason": "The declared holding horizon has no exit price.",
            },
            assumptions=assumptions,
            reason="primary_horizon_unavailable",
        )

    pnl_values = [Decimal(row["pnl_usd"]) for row in available]
    return_values = [Decimal(row["return_pct"]) for row in available]
    uncertainty = {
        "method": "declared_horizon_sensitivity.v1",
        "horizon_count": len(available),
        "pnl_low_usd": str(_money(min(pnl_values))) if pnl_values else None,
        "pnl_high_usd": str(_money(max(pnl_values))) if pnl_values else None,
        "return_low_pct": str(min(return_values).quantize(PCT4)) if return_values else None,
        "return_high_pct": str(max(return_values).quantize(PCT4)) if return_values else None,
        "confidence_interval": None,
        "confidence_interval_reason": (
            "One observed price path cannot support a distributional interval. The range "
            "across declared horizons is a sensitivity, not a confidence interval."
        ),
    }
    return CounterfactualResult(
        established=True,
        ticker=symbol,
        action=action,
        quantity=amount,
        rule=rule,
        entry=entry,
        exit=primary,
        pnl_usd=Decimal(primary["pnl_usd"]),
        return_pct=Decimal(primary["return_pct"]),
        horizons=horizons,
        uncertainty=uncertainty,
        assumptions=assumptions,
        reason=None,
    )
