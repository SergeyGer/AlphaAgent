"""Service-layer tests: sentiment scoring, ledger replay and market-data fallbacks."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Transaction, TxType
from core.tests.helpers import TEST_CACHES, make_portfolio
from services.ledger import realised_pnl_today, replay_positions
from services.market_data import PriceQuote, get_latest_quote, normalize_ticker
from services.news import NewsArticle, NewsReport, _dedupe, _parse_rss
from services.portfolio_metrics import compute_metrics
from services.sentiment import BEARISH, BULLISH, NEUTRAL, aggregate_sentiment, score_text


@override_settings(CACHES=TEST_CACHES)
class TickerNormalisationTests(TestCase):
    def test_crypto_maps_to_usd_pair(self):
        self.assertEqual(normalize_ticker("btc"), "BTC-USD")
        self.assertEqual(normalize_ticker("ETH"), "ETH-USD")

    def test_equities_pass_through(self):
        self.assertEqual(normalize_ticker("aapl"), "AAPL")
        self.assertEqual(normalize_ticker(" tsla "), "TSLA")

    def test_suffixed_crypto_normalised(self):
        self.assertEqual(normalize_ticker("BTCUSDT"), "BTC-USD")
        self.assertEqual(normalize_ticker("SOLUSD"), "SOL-USD")


@override_settings(CACHES=TEST_CACHES)
class MarketDataTests(TestCase):
    def setUp(self):
        # LocMemCache is process-global; reset it so cases stay independent.
        cache.clear()

    def test_returns_quote_from_provider(self):
        fake = PriceQuote(ticker="AAPL", symbol="AAPL", price=Decimal("123.45"))
        with patch("services.market_data._fetch_from_yfinance", return_value=fake):
            quote = get_latest_quote("AAPL")
        self.assertIsNotNone(quote)
        self.assertEqual(quote.price, Decimal("123.45"))

    def test_second_call_is_served_from_cache(self):
        fake = PriceQuote(ticker="AAPL", symbol="AAPL", price=Decimal("100.00"))
        with patch("services.market_data._fetch_from_yfinance", return_value=fake) as provider:
            get_latest_quote("AAPL")
            quote = get_latest_quote("AAPL")
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(quote.source, "cache")

    def test_provider_outage_falls_back_to_stored_price(self):
        with patch("services.market_data._fetch_from_yfinance", return_value=None):
            quote = get_latest_quote("AAPL", fallback_price=Decimal("99.99"))
        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, "fallback")
        self.assertEqual(quote.price, Decimal("99.99"))

    def test_total_outage_returns_none(self):
        with patch("services.market_data._fetch_from_yfinance", return_value=None):
            self.assertIsNone(get_latest_quote("AAPL"))

    def test_provider_exception_is_contained(self):
        with patch("services.market_data._fetch_from_yfinance", side_effect=RuntimeError("boom")):
            self.assertIsNone(get_latest_quote("AAPL"))

    def test_change_pct_computation(self):
        quote = PriceQuote(
            ticker="AAPL", symbol="AAPL", price=Decimal("110.00"), previous_close=Decimal("100.00")
        )
        self.assertEqual(quote.change_pct, Decimal("10.00"))


class SentimentTests(TestCase):
    def test_bullish_headlines(self):
        result = aggregate_sentiment(
            [
                "Apple shares surge as earnings beat expectations",
                "Analysts upgrade Apple to outperform on record demand",
                "Apple stock rallies to new highs amid strong growth",
            ]
        )
        self.assertEqual(result.label, BULLISH)
        self.assertGreater(result.score, 0)

    def test_bearish_headlines(self):
        result = aggregate_sentiment(
            [
                "Tesla shares plunge after earnings miss",
                "Analysts downgrade Tesla on weak demand and layoffs",
                "Tesla stock tumbles as investigation widens",
            ]
        )
        self.assertEqual(result.label, BEARISH)
        self.assertLess(result.score, 0)

    def test_neutral_headlines(self):
        result = aggregate_sentiment(
            ["Company schedules annual meeting", "Board announces conference date"]
        )
        self.assertEqual(result.label, NEUTRAL)
        self.assertEqual(result.score, 0.0)

    def test_negation_flips_polarity(self):
        positive, _, _ = score_text("earnings beat expectations")
        negated, _, _ = score_text("earnings did not beat expectations")
        self.assertGreater(positive, 0)
        self.assertLess(negated, positive)

    def test_empty_input_is_neutral(self):
        result = aggregate_sentiment([])
        self.assertEqual(result.label, NEUTRAL)
        self.assertEqual(result.articles_scored, 0)

    def test_intensifier_amplifies(self):
        plain, _, _ = score_text("shares rise")
        strong, _, _ = score_text("shares rise sharply")
        self.assertGreater(strong, plain)


class NewsParsingTests(TestCase):
    RSS = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Apple shares surge on record iPhone demand</title>
        <link>https://example.com/a</link>
        <pubDate>Mon, 01 Jan 2026 10:00:00 GMT</pubDate>
        <description>&lt;p&gt;Strong &lt;b&gt;results&lt;/b&gt;&lt;/p&gt;</description>
        <source>Example Wire</source>
      </item>
      <item><title>Apple shares surge on record iPhone demand</title><link>https://example.com/dup</link></item>
      <item><title></title><link>https://example.com/empty</link></item>
    </channel></rss>"""

    def test_parses_rss_items(self):
        articles = _parse_rss(self.RSS, "example")
        self.assertEqual(len(articles), 2)  # empty title dropped, dup kept by parser
        self.assertEqual(articles[0].source, "Example Wire")
        self.assertNotIn("<b>", articles[0].summary)

    def test_dedupe_removes_repeated_titles(self):
        articles = [
            NewsArticle(title="Apple beats", source="a"),
            NewsArticle(title="Apple beats", source="b"),
            NewsArticle(title="Apple beats again", source="c"),
        ]
        self.assertEqual(len(_dedupe(articles)), 2)

    def test_malformed_xml_is_contained(self):
        self.assertEqual(_parse_rss(b"<not-xml", "broken"), [])

    def test_entity_expansion_bomb_is_blocked(self):
        """RSS is untrusted input: billion-laughs must be rejected, not expanded."""
        bomb = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
            b"<rss><channel><item><title>&lol3;</title></item></channel></rss>"
        )
        self.assertEqual(_parse_rss(bomb, "hostile-feed"), [])

    def test_external_entity_is_blocked(self):
        xxe = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b"<rss><channel><item><title>&xxe;</title></item></channel></rss>"
        )
        self.assertEqual(_parse_rss(xxe, "hostile-feed"), [])

    def test_report_prompt_block(self):
        report = NewsReport(
            ticker="AAPL",
            articles=[NewsArticle(title="Apple rallies", source="Wire")],
        )
        self.assertIn("AAPL", report.as_prompt_block())
        self.assertIn("Apple rallies", report.as_prompt_block())

    def test_empty_report_says_so(self):
        self.assertIn("No recent headlines", NewsReport(ticker="AAPL").as_prompt_block())


class LedgerReplayTests(TestCase):
    def setUp(self):
        self.portfolio = make_portfolio(balance="10000.00")

    def _tx(self, ticker, tx_type, amount, price, when=None):
        tx = Transaction.objects.create(
            portfolio=self.portfolio,
            ticker=ticker,
            tx_type=tx_type,
            amount=Decimal(amount),
            price=Decimal(price),
            executed_by="AI",
        )
        if when is not None:
            Transaction.objects.filter(pk=tx.pk).update(timestamp=when)
        return tx

    def test_weighted_average_cost(self):
        self._tx("AAPL", TxType.BUY, "10", "100.00")
        self._tx("AAPL", TxType.BUY, "10", "120.00")
        states = replay_positions(self.portfolio)
        self.assertEqual(states["AAPL"].amount, Decimal("20.00000000"))
        self.assertEqual(states["AAPL"].avg_cost, Decimal("110.00"))

    def test_realised_pnl_on_sell(self):
        self._tx("AAPL", TxType.BUY, "10", "100.00")
        self._tx("AAPL", TxType.SELL, "4", "130.00")
        states = replay_positions(self.portfolio)
        self.assertEqual(states["AAPL"].amount, Decimal("6.00000000"))
        self.assertEqual(states["AAPL"].realised_pnl_today, Decimal("120.00"))
        self.assertEqual(states["AAPL"].avg_cost, Decimal("100.00"))

    def test_realised_loss_is_negative(self):
        self._tx("AAPL", TxType.BUY, "10", "100.00")
        self._tx("AAPL", TxType.SELL, "10", "80.00")
        self.assertEqual(realised_pnl_today(self.portfolio), Decimal("-200.00"))

    def test_full_exit_resets_cost_basis(self):
        self._tx("AAPL", TxType.BUY, "10", "100.00")
        self._tx("AAPL", TxType.SELL, "10", "150.00")
        states = replay_positions(self.portfolio)
        self.assertEqual(states["AAPL"].amount, Decimal("0"))
        self.assertEqual(states["AAPL"].avg_cost, Decimal("0"))

    def test_yesterdays_pnl_excluded_from_today(self):
        yesterday = timezone.now() - timedelta(days=1)
        self._tx("AAPL", TxType.BUY, "10", "100.00", when=yesterday)
        self._tx("AAPL", TxType.SELL, "10", "90.00", when=yesterday)
        states = replay_positions(self.portfolio)
        self.assertEqual(states["AAPL"].realised_pnl_today, Decimal("0"))
        self.assertEqual(states["AAPL"].realised_pnl_total, Decimal("-100.00"))

    def test_positions_are_independent_per_ticker(self):
        self._tx("AAPL", TxType.BUY, "10", "100.00")
        self._tx("BTC", TxType.BUY, "0.5", "80000.00")
        states = replay_positions(self.portfolio)
        self.assertEqual(states["AAPL"].amount, Decimal("10.00000000"))
        self.assertEqual(states["BTC"].amount, Decimal("0.50000000"))


@override_settings(CACHES=TEST_CACHES)
class PortfolioMetricsTests(TestCase):
    def test_metrics_with_live_prices(self):
        portfolio = make_portfolio(balance="5000.00")
        from core.tests.helpers import make_asset

        make_asset(portfolio, "AAPL", "10", "100.00")
        price_map = {"AAPL": PriceQuote(ticker="AAPL", symbol="AAPL", price=Decimal("150.00"))}
        metrics = compute_metrics(portfolio, price_map)
        self.assertEqual(metrics.positions_value_usd, Decimal("1500.00"))
        self.assertEqual(metrics.total_equity_usd, Decimal("6500.00"))
        self.assertEqual(metrics.invested_cost_usd, Decimal("1000.00"))
        self.assertEqual(metrics.unrealised_pnl_usd, Decimal("500.00"))
        self.assertEqual(metrics.unrealised_pnl_pct, Decimal("50.00"))
        self.assertEqual(metrics.max_trade_budget_usd, Decimal("250.00"))

    def test_metrics_flag_loss_limit_breach(self):
        portfolio = make_portfolio(balance="10000.00", loss_limit="200.00")
        Transaction.objects.create(
            portfolio=portfolio,
            ticker="AAPL",
            tx_type=TxType.BUY,
            amount=Decimal("10"),
            price=Decimal("100.00"),
            executed_by="AI",
        )
        Transaction.objects.create(
            portfolio=portfolio,
            ticker="AAPL",
            tx_type=TxType.SELL,
            amount=Decimal("10"),
            price=Decimal("80.00"),
            executed_by="AI",
        )
        metrics = compute_metrics(portfolio, {})
        self.assertEqual(metrics.realised_pnl_today_usd, Decimal("-200.00"))
        self.assertEqual(metrics.daily_loss_used_usd, Decimal("200.00"))
        self.assertEqual(metrics.daily_loss_remaining_usd, Decimal("0.00"))
        self.assertTrue(metrics.is_autonomy_blocked)

    def test_unpriced_asset_falls_back_to_cost_basis(self):
        portfolio = make_portfolio(balance="1000.00")
        from core.tests.helpers import make_asset

        make_asset(portfolio, "AAPL", "2", "50.00")
        metrics = compute_metrics(portfolio, {})
        self.assertEqual(metrics.positions_value_usd, Decimal("100.00"))
        self.assertIn("AAPL", metrics.unpriced_tickers)
