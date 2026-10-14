"""Trade execution: the guard and the only database-mutating path.

This module is deliberately free of Celery imports so the REST API, the
Telegram bot and the tests can all reach the *same* guard implementation.
There is exactly one way for an AI proposal to become a ledger row, and it goes
through :func:`evaluate_proposal`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from django.db import DatabaseError, transaction

from core.models import Asset, ExecutedBy, Portfolio, Transaction

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing CrewAI
    from ai_agent import TradeProposal
    from services.ledger import PositionState

# Deliberately no module-level logger: the guard is a pure function and
# performs no I/O, so it has nothing to log. Callers log the reasons it returns.
# (An unused logger here was flagged as py/unused-global-variable.)

__all__ = [
    "CENT",
    "SATOSHI",
    "ZERO",
    "Exposure",
    "GuardDecision",
    "GuardError",
    "apply_trade",
    "evaluate_proposal",
]

ZERO = Decimal("0")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")
SATOSHI = Decimal("0.00000001")


class GuardError(Exception):
    """An execution was attempted with a decision the guard never approved.

    Raised instead of using ``assert``: assertions are silently stripped when
    Python runs with ``-O``/``PYTHONOPTIMIZE``, which would let an unvalidated
    decision reach the ledger in exactly the environment where the check matters
    most. `apply_trade` is the only path that mutates money, so its preconditions
    fail loudly and unconditionally.
    """


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

    @property
    def usable(self) -> bool:
        return self.equity_usd > ZERO


@dataclass
class GuardDecision:
    """Outcome of validating an AI proposal against the portfolio guardrails."""

    approved: bool
    reason: str
    action: str
    amount: Decimal = ZERO
    price: Decimal | None = None
    notional: Decimal = ZERO
    clamped: bool = False
    notes: list[str] = field(default_factory=list)


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
# Trade execution (the only DB-mutating path)
# ---------------------------------------------------------------------------
@transaction.atomic
def apply_trade(
    portfolio_id: int,
    ticker: str,
    decision: GuardDecision,
) -> Transaction:
    """Persist an approved trade. Caller must have validated ``decision``."""
    portfolio = Portfolio.objects.select_for_update().get(pk=portfolio_id)
    asset, _created = Asset.objects.select_for_update().get_or_create(
        portfolio=portfolio, ticker=ticker
    )
    price = decision.price
    if price is None:
        # Not an ``assert``: assertions are stripped under ``python -O``, and a
        # None price reaching the arithmetic below would size a trade from a
        # missing market value. A guard that disappears in production is not a
        # guard. evaluate_proposal already rejects this case, so reaching here
        # means a caller bypassed it.
        raise GuardError("Refusing to execute without a validated price.")
    amount = decision.amount
    notional = decision.notional

    if decision.action == "BUY":
        if notional > portfolio.balance_usd:
            raise DatabaseError(
                f"Balance changed under lock: need ${notional}, have ${portfolio.balance_usd}"
            )
        new_amount = asset.amount + amount
        if new_amount > 0:
            asset.avg_purchase_price = (
                ((asset.amount * asset.avg_purchase_price) + (amount * price)) / new_amount
            ).quantize(CENT)
        asset.amount = new_amount
        portfolio.balance_usd = (portfolio.balance_usd - notional).quantize(CENT)
    else:  # SELL
        if amount > asset.amount:
            raise DatabaseError(
                f"Position changed under lock: selling {amount}, holding {asset.amount}"
            )
        asset.amount = asset.amount - amount
        if asset.amount == 0:
            asset.avg_purchase_price = ZERO.quantize(CENT)
        portfolio.balance_usd = (portfolio.balance_usd + notional).quantize(CENT)

    portfolio.save(update_fields=["balance_usd"])
    asset.save(update_fields=["amount", "avg_purchase_price"])

    return Transaction.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        tx_type=decision.action,
        amount=amount,
        price=price,
        executed_by=ExecutedBy.AI,
    )
