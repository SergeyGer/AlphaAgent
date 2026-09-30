"""Execution-guard tests - the safety-critical layer.

``evaluate_proposal`` is a pure function, so these tests need no I/O and pin
down exactly when the AI is allowed to touch the database.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from ai_agent import TradeProposal
from core.tests.helpers import make_portfolio
from services.ledger import PositionState
from tasks import evaluate_proposal


def proposal(action: str, amount: float, sentiment: str = "BULLISH") -> TradeProposal:
    return TradeProposal(action=action, amount=amount, sentiment=sentiment, reasoning="because")


class AllocationGuardTests(TestCase):
    """max_trade_allocation_pct enforcement."""

    def setUp(self):
        # 5% of $10,000 => $500 per-trade budget.
        self.portfolio = make_portfolio(balance="10000.00", allocation_pct="5.00")
        self.flat = PositionState(ticker="AAPL")

    def test_buy_within_budget_is_approved(self):
        decision = evaluate_proposal(
            self.portfolio, proposal("BUY", 2.0), Decimal("100.00"), self.flat, Decimal("0")
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, Decimal("200.00"))
        self.assertFalse(decision.clamped)

    def test_buy_exactly_at_budget_is_approved(self):
        decision = evaluate_proposal(
            self.portfolio, proposal("BUY", 5.0), Decimal("100.00"), self.flat, Decimal("0")
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, Decimal("500.00"))

    def test_buy_over_budget_is_clamped_to_ceiling(self):
        decision = evaluate_proposal(
            self.portfolio, proposal("BUY", 50.0), Decimal("100.00"), self.flat, Decimal("0")
        )
        self.assertTrue(decision.approved)
        self.assertTrue(decision.clamped)
        self.assertEqual(decision.amount, Decimal("5.00000000"))
        self.assertEqual(decision.notional, Decimal("500.00"))
        self.assertTrue(any("Clamped" in note for note in decision.notes))

    def test_buy_blocked_when_allocation_pct_is_zero(self):
        portfolio = make_portfolio(allocation_pct="0.00")
        decision = evaluate_proposal(
            portfolio,
            proposal("BUY", 1.0),
            Decimal("100.00"),
            PositionState(ticker="AAPL"),
            Decimal("0"),
        )
        self.assertFalse(decision.approved)
        self.assertIn("max_trade_allocation_pct", decision.reason)

    def test_buy_blocked_when_cash_is_zero(self):
        portfolio = make_portfolio(balance="0.00", allocation_pct="50.00")
        decision = evaluate_proposal(
            portfolio,
            proposal("BUY", 1.0),
            Decimal("100.00"),
            PositionState(ticker="AAPL"),
            Decimal("0"),
        )
        self.assertFalse(decision.approved)
        self.assertIn("$0 budget", decision.reason)


class DailyLossLimitTests(TestCase):
    """daily_loss_limit_usd (portfolio stop-loss) enforcement."""

    def setUp(self):
        self.portfolio = make_portfolio(balance="10000.00", loss_limit="500.00")
        self.position = PositionState(ticker="AAPL", amount=Decimal("10"), avg_cost=Decimal("100"))

    def test_buy_allowed_below_loss_limit(self):
        decision = evaluate_proposal(
            self.portfolio,
            proposal("BUY", 1.0),
            Decimal("100.00"),
            self.position,
            Decimal("-400.00"),
        )
        self.assertTrue(decision.approved)

    def test_buy_blocked_when_loss_limit_breached(self):
        decision = evaluate_proposal(
            self.portfolio,
            proposal("BUY", 1.0),
            Decimal("100.00"),
            self.position,
            Decimal("-500.00"),
        )
        self.assertFalse(decision.approved)
        self.assertIn("daily loss limit", decision.reason.lower())

    def test_buy_blocked_when_loss_limit_exceeded(self):
        decision = evaluate_proposal(
            self.portfolio,
            proposal("BUY", 1.0),
            Decimal("100.00"),
            self.position,
            Decimal("-900.00"),
        )
        self.assertFalse(decision.approved)

    def test_sell_still_allowed_when_loss_limit_breached(self):
        """De-risking must remain possible after the stop-loss trips."""
        decision = evaluate_proposal(
            self.portfolio,
            proposal("SELL", 5.0, "BEARISH"),
            Decimal("100.00"),
            self.position,
            Decimal("-900.00"),
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.amount, Decimal("5.00000000"))


class PositionAndPriceGuardTests(TestCase):
    def setUp(self):
        self.portfolio = make_portfolio(balance="10000.00")
        self.position = PositionState(ticker="AAPL", amount=Decimal("3"), avg_cost=Decimal("100"))

    def test_hold_is_never_executed(self):
        decision = evaluate_proposal(
            self.portfolio,
            proposal("HOLD", 0, "NEUTRAL"),
            Decimal("100.00"),
            self.position,
            Decimal("0"),
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.action, "HOLD")

    def test_missing_price_blocks_execution(self):
        decision = evaluate_proposal(
            self.portfolio, proposal("BUY", 1.0), None, self.position, Decimal("0")
        )
        self.assertFalse(decision.approved)
        self.assertIn("no reliable market price", decision.reason)

    def test_zero_price_blocks_execution(self):
        decision = evaluate_proposal(
            self.portfolio, proposal("BUY", 1.0), Decimal("0"), self.position, Decimal("0")
        )
        self.assertFalse(decision.approved)

    def test_zero_amount_rejected(self):
        decision = evaluate_proposal(
            self.portfolio, proposal("BUY", 0.0), Decimal("100.00"), self.position, Decimal("0")
        )
        self.assertFalse(decision.approved)
        self.assertIn("positive amount", decision.reason)

    def test_sell_without_position_rejected(self):
        decision = evaluate_proposal(
            self.portfolio,
            proposal("SELL", 1.0, "BEARISH"),
            Decimal("100.00"),
            PositionState(ticker="AAPL"),
            Decimal("0"),
        )
        self.assertFalse(decision.approved)
        self.assertIn("no open position", decision.reason)

    def test_sell_larger_than_position_is_clamped(self):
        decision = evaluate_proposal(
            self.portfolio,
            proposal("SELL", 99.0, "BEARISH"),
            Decimal("100.00"),
            self.position,
            Decimal("0"),
        )
        self.assertTrue(decision.approved)
        self.assertTrue(decision.clamped)
        self.assertEqual(decision.amount, Decimal("3.00000000"))
        self.assertEqual(decision.notional, Decimal("300.00"))

    def test_negative_amount_is_rejected_at_the_schema_boundary(self):
        """Pydantic blocks negatives before the guard is even consulted."""
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            proposal("BUY", -5.0)

    def test_fractional_crypto_sizing(self):
        decision = evaluate_proposal(
            self.portfolio,
            proposal("BUY", 0.005),
            Decimal("83000.00"),
            PositionState(ticker="BTC"),
            Decimal("0"),
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, Decimal("415.00"))
