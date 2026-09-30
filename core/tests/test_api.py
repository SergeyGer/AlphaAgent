"""REST API tests: authentication, the three spec endpoints, and safety rules."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import AgentDecisionLog, MarketSentiment, Transaction, TxType
from core.tests.helpers import TEST_CACHES, auth_client, make_asset, make_portfolio, make_user
from services.market_data import PriceQuote


def fake_quote(price: str):
    def _fetch(symbol: str):
        return PriceQuote(ticker=symbol, symbol=symbol, price=Decimal(price))

    return _fetch


@override_settings(CACHES=TEST_CACHES)
class PortfolioEndpointTests(APITestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()  # market-data cache is process-global
        self.user = make_user()
        self.portfolio = make_portfolio(
            self.user, balance="10000.00", risk="high", autonomous=False
        )
        make_asset(self.portfolio, "AAPL", "10", "100.00")
        self.client = auth_client(self.user)

    def test_requires_authentication(self):
        from rest_framework.test import APIClient

        response = APIClient().get(reverse("core:portfolio-detail"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("services.market_data._fetch_from_yfinance", side_effect=fake_quote("150.00"))
    def test_returns_portfolio_with_nested_assets_and_metrics(self, _mock):
        response = self.client.get(reverse("core:portfolio-detail"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        body = response.json()
        self.assertEqual(body["risk_profile"], "high")
        self.assertEqual(body["balance_usd"], "10000.00")
        self.assertFalse(body["is_autonomous"])
        self.assertEqual(body["max_trade_budget_usd"], "500.00")

        self.assertEqual(len(body["assets"]), 1)
        asset = body["assets"][0]
        self.assertEqual(asset["ticker"], "AAPL")
        self.assertEqual(asset["market_price"], "150.00")
        self.assertEqual(asset["market_value_usd"], "1500.00")
        self.assertEqual(asset["unrealised_pnl_usd"], "500.00")
        self.assertEqual(asset["unrealised_pnl_pct"], "50.00")

        metrics = body["metrics"]
        self.assertEqual(metrics["cash_balance_usd"], "10000.00")
        self.assertEqual(metrics["positions_value_usd"], "1500.00")
        self.assertEqual(metrics["total_equity_usd"], "11500.00")
        self.assertFalse(metrics["is_autonomy_blocked"])

    @patch("services.market_data._fetch_from_yfinance", side_effect=fake_quote("150.00"))
    def test_query_count_is_bounded(self, _mock):
        """select_related/prefetch_related must prevent an N+1 explosion."""
        with self.assertNumQueries(5):  # token+user, portfolio, assets, ledger, (metrics)
            baseline = len(self.client.get(reverse("core:portfolio-detail")).json()["assets"])

        for ticker in ("TSLA", "MSFT", "NVDA", "BTC", "ETH"):
            make_asset(self.portfolio, ticker, "1", "50.00")

        # Adding 5 more positions must not add queries to the hot path.
        with self.assertNumQueries(5):
            body = self.client.get(reverse("core:portfolio-detail")).json()
        self.assertEqual(len(body["assets"]), baseline + 5)

    @patch("services.market_data._fetch_from_yfinance", return_value=None)
    def test_degrades_when_market_data_is_down(self, _mock):
        """A market-data outage must degrade to the stored cost basis, not 500."""
        response = self.client.get(reverse("core:portfolio-detail"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        asset = response.json()["assets"][0]
        self.assertEqual(asset["price_source"], "fallback")
        self.assertEqual(asset["market_price"], "100.00")  # avg purchase price
        self.assertEqual(response.json()["metrics"]["positions_value_usd"], "1000.00")


@override_settings(CACHES=TEST_CACHES)
class ToggleAutonomyEndpointTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="10000.00")
        self.client = auth_client(self.user)
        self.url = reverse("core:portfolio-toggle-autonomy")

    def test_requires_authentication(self):
        from rest_framework.test import APIClient

        self.assertEqual(
            APIClient().post(self.url, {}, format="json").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_enabling_requires_explicit_confirmation(self):
        response = self.client.post(self.url, {"is_autonomous": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.portfolio.refresh_from_db()
        self.assertFalse(self.portfolio.is_autonomous)

    def test_enabling_with_confirmation_succeeds(self):
        response = self.client.post(
            self.url, {"is_autonomous": True, "confirm": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["is_autonomous"])
        self.assertTrue(response.json()["changed"])
        self.portfolio.refresh_from_db()
        self.assertTrue(self.portfolio.is_autonomous)

    def test_empty_body_requires_confirmation_to_enable(self):
        """A blind flip OFF->ON is exactly what `confirm` protects against."""
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.portfolio.refresh_from_db()
        self.assertFalse(self.portfolio.is_autonomous)

    def test_flip_from_on_to_off_needs_no_confirmation(self):
        self.portfolio.is_autonomous = True
        self.portfolio.save(update_fields=["is_autonomous"])
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["is_autonomous"])
        self.portfolio.refresh_from_db()
        self.assertFalse(self.portfolio.is_autonomous)

    def test_idempotent_disable(self):
        response = self.client.post(self.url, {"is_autonomous": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["changed"])

    def test_cannot_enable_with_zero_balance(self):
        self.portfolio.balance_usd = Decimal("0.00")
        self.portfolio.save(update_fields=["balance_usd"])
        response = self.client.post(
            self.url, {"is_autonomous": True, "confirm": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(CACHES=TEST_CACHES)
class DecisionLogEndpointTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.portfolio = make_portfolio(self.user)
        self.client = auth_client(self.user)
        self.url = reverse("core:portfolio-logs")

        self.tx = Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type=TxType.BUY,
            amount=Decimal("1"),
            price=Decimal("100.00"),
            executed_by="AI",
        )
        AgentDecisionLog.objects.create(
            portfolio=self.portfolio,
            transaction=self.tx,
            reasoning="Strong earnings beat plus analyst upgrades.",
            action_taken="Purchased 1 AAPL",
            market_sentiment=MarketSentiment.BULLISH,
            tokens_used=1234,
            api_cost_usd=Decimal("0.00456"),
        )
        AgentDecisionLog.objects.create(
            portfolio=self.portfolio,
            reasoning="Mixed signals, staying flat.",
            action_taken="HOLD - Neutral market, no position change",
            market_sentiment=MarketSentiment.NEUTRAL,
        )

    def test_requires_authentication(self):
        from rest_framework.test import APIClient

        self.assertEqual(APIClient().get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_audit_trail_newest_first(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["action_taken"], "HOLD - Neutral market, no position change")
        self.assertEqual(results[1]["tokens_used"], 1234)
        self.assertEqual(results[1]["api_cost_usd"], "0.00456")
        self.assertEqual(results[1]["ticker"], "AAPL")
        self.assertEqual(results[1]["transaction_id"], self.tx.id)

    def test_filters_by_sentiment(self):
        response = self.client.get(self.url, {"sentiment": "BULLISH"})
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_sentiment"], "BULLISH")

    def test_filters_by_ticker_and_executed(self):
        self.assertEqual(len(self.client.get(self.url, {"ticker": "AAPL"}).json()["results"]), 1)
        self.assertEqual(len(self.client.get(self.url, {"ticker": "TSLA"}).json()["results"]), 0)
        self.assertEqual(len(self.client.get(self.url, {"executed": "true"}).json()["results"]), 1)

    def test_logs_are_scoped_to_the_authenticated_user(self):
        other = make_user("mallory")
        other_portfolio = make_portfolio(other)
        AgentDecisionLog.objects.create(
            portfolio=other_portfolio,
            reasoning="secret",
            action_taken="HOLD",
            market_sentiment=MarketSentiment.NEUTRAL,
        )
        results = self.client.get(self.url).json()["results"]
        self.assertEqual(len(results), 2)
        self.assertNotIn("secret", str(results))


@override_settings(CACHES=TEST_CACHES)
class TransactionEndpointTests(APITestCase):
    def test_lists_only_own_transactions(self):
        user = make_user()
        portfolio = make_portfolio(user)
        Transaction.objects.create(
            portfolio=portfolio,
            ticker="TSLA",
            tx_type=TxType.BUY,
            amount=Decimal("2"),
            price=Decimal("250.00"),
            executed_by="USER",
        )
        other = make_portfolio(make_user("bob"))
        Transaction.objects.create(
            portfolio=other,
            ticker="NVDA",
            tx_type=TxType.BUY,
            amount=Decimal("1"),
            price=Decimal("900.00"),
            executed_by="AI",
        )

        client = auth_client(user)
        response = client.get(reverse("core:portfolio-transactions"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ticker"], "TSLA")
        self.assertEqual(results[0]["gross_value_usd"], "500.00")


@override_settings(CACHES=TEST_CACHES)
class HealthAndTokenTests(APITestCase):
    def test_healthz_is_public(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_token_endpoint_issues_a_token(self):
        make_user("carol", "hunter2-pass")
        response = self.client.post(
            reverse("core:obtain-token"),
            {"username": "carol", "password": "hunter2-pass"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("token", response.json())
