"""Sweep-throttle tests.

The behaviour under test exists because of an observed production incident: two
sweeps three seconds apart analysed the same ticker twice and each bought AAPL
inside its own per-trade limit, compounding the portfolio's real exposure.

Two properties matter:

* **Effectiveness** - a second run inside the window is refused.
* **Fail-open** - a cache outage must never stop the risk engine. Losing a
  cooldown costs money; refusing to run costs safety.
"""

from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from core.tests.helpers import TEST_CACHES, make_portfolio, make_user
from services.throttle import (
    claim_sweep_slot,
    claim_ticker_slot,
    release_ticker_slot,
    sweep_debounce_seconds,
    ticker_cooldown_seconds,
    ticker_slot_key,
)


@override_settings(CACHES=TEST_CACHES)
class TickerCooldownTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_first_claim_succeeds_and_the_second_is_refused(self):
        self.assertTrue(claim_ticker_slot(1, "AAPL"))
        self.assertFalse(claim_ticker_slot(1, "AAPL"))

    def test_different_tickers_are_independent(self):
        self.assertTrue(claim_ticker_slot(1, "AAPL"))
        self.assertTrue(claim_ticker_slot(1, "TSLA"))
        self.assertTrue(claim_ticker_slot(1, "BTC"))

    def test_different_portfolios_are_independent(self):
        """Two users holding the same ticker must not block each other."""
        self.assertTrue(claim_ticker_slot(1, "AAPL"))
        self.assertTrue(claim_ticker_slot(2, "AAPL"))

    def test_ticker_is_case_insensitive(self):
        self.assertTrue(claim_ticker_slot(1, "aapl"))
        self.assertFalse(claim_ticker_slot(1, "AAPL"))

    def test_zero_window_disables_throttling(self):
        self.assertTrue(claim_ticker_slot(1, "AAPL", window=0))
        self.assertTrue(claim_ticker_slot(1, "AAPL", window=0))

    def test_release_frees_the_slot_for_a_retry(self):
        self.assertTrue(claim_ticker_slot(1, "AAPL"))
        release_ticker_slot(1, "AAPL")
        self.assertTrue(claim_ticker_slot(1, "AAPL"))

    def test_slot_key_is_namespaced_per_portfolio_and_ticker(self):
        self.assertNotEqual(ticker_slot_key(1, "AAPL"), ticker_slot_key(2, "AAPL"))
        self.assertNotEqual(ticker_slot_key(1, "AAPL"), ticker_slot_key(1, "TSLA"))
        self.assertEqual(ticker_slot_key(1, "aapl"), ticker_slot_key(1, "AAPL"))

    def test_cache_outage_fails_open(self):
        """A Redis outage must not stop the risk engine."""
        with patch("services.throttle.cache.add", side_effect=RuntimeError("redis down")):
            self.assertTrue(claim_ticker_slot(1, "AAPL"))

    def test_configured_window_is_used(self):
        with override_settings(ALPHA_TICKER_COOLDOWN_SECONDS=42):
            self.assertEqual(ticker_cooldown_seconds(), 42)


@override_settings(CACHES=TEST_CACHES)
class SweepDebounceTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_second_fan_out_inside_the_window_is_refused(self):
        self.assertTrue(claim_sweep_slot())
        self.assertFalse(claim_sweep_slot())

    def test_zero_window_disables_the_debounce(self):
        self.assertTrue(claim_sweep_slot(window=0))
        self.assertTrue(claim_sweep_slot(window=0))

    def test_cache_outage_fails_open(self):
        with patch("services.throttle.cache.add", side_effect=RuntimeError("redis down")):
            self.assertTrue(claim_sweep_slot())

    def test_configured_debounce_is_used(self):
        with override_settings(ALPHA_SWEEP_DEBOUNCE_SECONDS=7):
            self.assertEqual(sweep_debounce_seconds(), 7)


@override_settings(CACHES=TEST_CACHES)
class TaskCooldownIntegrationTests(TestCase):
    """The cooldown must actually gate the Celery task, not just exist."""

    def setUp(self):
        from config.celery import app as celery_app

        cache.clear()
        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", False)

        from tasks import run_alpha_agent_task

        self.task = run_alpha_agent_task
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="10000.00", autonomous=True)

    def _run(self):
        from ai_agent import AgentRunResult, TradeProposal
        from services.news import NewsReport

        result = AgentRunResult(
            proposal=TradeProposal(
                action="HOLD", amount=0.0, sentiment="NEUTRAL", reasoning="test"
            ),
            reasoning="test",
            source="crewai",
        )
        with (
            patch("tasks.get_latest_quote", return_value=None),
            patch("tasks.build_news_report", return_value=NewsReport(ticker="AAPL")),
            patch("tasks.get_financial_health", return_value=None),
            patch("tasks.get_price_history", return_value=None),
            patch("tasks.run_alpha_agent", return_value=result),
        ):
            return self.task.apply(args=[self.portfolio.id, "AAPL"]).get()

    def test_second_run_inside_the_window_is_skipped(self):
        first = self._run()
        second = self._run()

        self.assertNotEqual(first["status"], "cooldown")
        self.assertEqual(second["status"], "cooldown")
        self.assertEqual(second["reason"], "ticker_cooldown")

    def test_release_allows_an_immediate_rerun(self):
        self._run()
        release_ticker_slot(self.portfolio.id, "AAPL")
        self.assertNotEqual(self._run()["status"], "cooldown")

    def test_disabling_the_cooldown_allows_back_to_back_runs(self):
        with override_settings(ALPHA_TICKER_COOLDOWN_SECONDS=0):
            self.assertNotEqual(self._run()["status"], "cooldown")
            self.assertNotEqual(self._run()["status"], "cooldown")

    def test_the_cooldown_does_not_block_a_different_ticker(self):
        self._run()

        from ai_agent import AgentRunResult, TradeProposal
        from services.news import NewsReport

        result = AgentRunResult(
            proposal=TradeProposal(
                action="HOLD", amount=0.0, sentiment="NEUTRAL", reasoning="test"
            ),
            reasoning="test",
            source="crewai",
        )
        with (
            patch("tasks.get_latest_quote", return_value=None),
            patch("tasks.build_news_report", return_value=NewsReport(ticker="TSLA")),
            patch("tasks.get_financial_health", return_value=None),
            patch("tasks.get_price_history", return_value=None),
            patch("tasks.run_alpha_agent", return_value=result),
        ):
            other = self.task.apply(args=[self.portfolio.id, "TSLA"]).get()

        self.assertNotEqual(other["status"], "cooldown")


@override_settings(CACHES=TEST_CACHES)
class SweepTaskDebounceIntegrationTests(TestCase):
    def setUp(self):
        from config.celery import app as celery_app

        cache.clear()
        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", False)

    def test_overlapping_sweeps_are_debounced(self):
        """Beat and a manual trigger firing together must not both dispatch."""
        from tasks import autonomous_market_monitoring_task

        portfolio = make_portfolio(balance="1000.00", autonomous=True)

        with patch("tasks.group") as group_mock:
            group_mock.return_value.apply_async.return_value.id = "gid"
            first = autonomous_market_monitoring_task.apply().get()
            second = autonomous_market_monitoring_task.apply().get()

        self.assertEqual(first["status"], "dispatched")
        self.assertEqual(second["status"], "debounced")
        self.assertEqual(second["dispatched"], 0)
        self.assertEqual(group_mock.call_count, 1)

        self.assertTrue(portfolio.is_autonomous)
