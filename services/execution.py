"""Trade execution: the guard and the only database-mutating path.

This module is deliberately free of Celery imports so the REST API, the
Telegram bot and the tests can all reach the *same* guard implementation.
There is exactly one way for an AI proposal to become a ledger row, and it goes
through :func:`evaluate_proposal`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from django.db import DatabaseError, transaction

from core.models import Asset, ExecutedBy, Portfolio, Transaction

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing CrewAI
    from ai_agent import TradeProposal
    from services.ledger import PositionState

logger = logging.getLogger("alphaagent.execution")

__all__ = [
    "CENT",
    "SATOSHI",
    "ZERO",
    "GuardDecision",
    "GuardError",
    "apply_trade",
    "evaluate_proposal",
]

ZERO = Decimal("0")
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


def evaluate_proposal(
    portfolio: Portfolio,
    proposal: TradeProposal,
    price: Decimal | None,
    position: PositionState,
    realised_pnl_today: Decimal,
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

        max_affordable = min(budget, portfolio.balance_usd)
        notional = (amount * price).quantize(CENT)
        if notional > max_affordable:
            clamped_amount = (max_affordable / price).quantize(SATOSHI, rounding=ROUND_DOWN)
            if clamped_amount <= 0:
                decision.reason = (
                    f"Rejected: requested ${notional} exceeds the ${max_affordable} "
                    "per-trade ceiling and cannot be sized down."
                )
                return decision
            decision.clamped = True
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
