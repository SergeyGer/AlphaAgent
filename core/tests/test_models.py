"""Model-layer tests: schema contract, defaults and DB constraints."""

from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import AgentDecisionLog, Asset, MarketSentiment, Portfolio, Transaction, TxType
from core.tests.helpers import make_asset, make_portfolio, make_user


class PortfolioModelTests(TestCase):
    def test_defaults_match_specification(self):
        portfolio = Portfolio.objects.create(user=make_user())
        self.assertEqual(portfolio.balance_usd, Decimal("10000.00"))
        self.assertEqual(portfolio.risk_profile, "medium")
        self.assertFalse(portfolio.is_autonomous)
        self.assertEqual(portfolio.max_trade_allocation_pct, Decimal("5.00"))
        self.assertEqual(portfolio.daily_loss_limit_usd, Decimal("500.00"))
        self.assertIsNotNone(portfolio.created_at)

    def test_related_name_portfolio_on_user(self):
        user = make_user()
        portfolio = make_portfolio(user=user)
        self.assertEqual(user.portfolio, portfolio)

    def test_max_trade_budget_is_pct_of_balance(self):
        portfolio = make_portfolio(balance="20000.00", allocation_pct="7.50")
        self.assertEqual(portfolio.max_trade_budget_usd, Decimal("1500.00"))

    def test_one_portfolio_per_user(self):
        user = make_user()
        make_portfolio(user=user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Portfolio.objects.create(user=user)

    def test_negative_balance_rejected_by_database(self):
        portfolio = make_portfolio()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Portfolio.objects.filter(pk=portfolio.pk).update(balance_usd=Decimal("-1.00"))


class AssetModelTests(TestCase):
    def test_asset_related_name_and_constraint(self):
        portfolio = make_portfolio()
        make_asset(portfolio, "AAPL", "1.5", "100.00")
        self.assertEqual(portfolio.assets.count(), 1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Asset.objects.create(
                portfolio=portfolio,
                ticker="AAPL",
                amount=Decimal("1"),
                avg_purchase_price=Decimal("1"),
            )

    def test_cost_basis_and_market_value(self):
        portfolio = make_portfolio()
        asset = make_asset(portfolio, "AAPL", "2.0", "100.00")
        self.assertEqual(asset.cost_basis_usd, Decimal("200.00"))
        self.assertEqual(asset.market_value_usd(Decimal("150.00")), Decimal("300.00"))
        # Falls back to cost basis when no price is given.
        self.assertEqual(asset.market_value_usd(), Decimal("200.00"))


class TransactionModelTests(TestCase):
    def test_transaction_choices_and_derived_values(self):
        portfolio = make_portfolio()
        tx = Transaction.objects.create(
            portfolio=portfolio,
            ticker="TSLA",
            tx_type=TxType.BUY,
            amount=Decimal("3.0"),
            price=Decimal("200.00"),
            executed_by="AI",
        )
        self.assertEqual(tx.gross_value_usd, Decimal("600.00"))
        self.assertEqual(tx.signed_cash_flow_usd, Decimal("-600.00"))

        sell = Transaction.objects.create(
            portfolio=portfolio,
            ticker="TSLA",
            tx_type=TxType.SELL,
            amount=Decimal("1.0"),
            price=Decimal("250.00"),
            executed_by="USER",
        )
        self.assertEqual(sell.signed_cash_flow_usd, Decimal("250.00"))

    def test_non_positive_amount_rejected(self):
        portfolio = make_portfolio()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Transaction.objects.create(
                portfolio=portfolio,
                ticker="TSLA",
                tx_type=TxType.BUY,
                amount=Decimal("0"),
                price=Decimal("10.00"),
                executed_by="AI",
            )


class AgentDecisionLogTests(TestCase):
    def test_log_links_to_transaction_and_defaults(self):
        portfolio = make_portfolio()
        tx = Transaction.objects.create(
            portfolio=portfolio,
            ticker="AAPL",
            tx_type=TxType.BUY,
            amount=Decimal("1"),
            price=Decimal("100.00"),
            executed_by="AI",
        )
        log = AgentDecisionLog.objects.create(
            portfolio=portfolio,
            transaction=tx,
            reasoning="Chain of thought ...",
            action_taken="Purchased 1 AAPL",
            market_sentiment=MarketSentiment.BULLISH,
        )
        self.assertEqual(log.tokens_used, 0)
        self.assertEqual(log.api_cost_usd, Decimal("0.00000"))
        self.assertEqual(tx.decision_log, log)

    def test_transaction_delete_sets_log_transaction_null(self):
        portfolio = make_portfolio()
        tx = Transaction.objects.create(
            portfolio=portfolio,
            ticker="AAPL",
            tx_type=TxType.BUY,
            amount=Decimal("1"),
            price=Decimal("100.00"),
            executed_by="AI",
        )
        AgentDecisionLog.objects.create(
            portfolio=portfolio, transaction=tx, reasoning="x", action_taken="y"
        )
        tx.delete()
        self.assertIsNone(AgentDecisionLog.objects.get().transaction)
