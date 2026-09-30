"""Celery task tests: fan-out dispatch, execution guard integration, audit trail."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from ai_agent import AgentRunResult, TradeProposal
from core.models import AgentDecisionLog, Asset, MarketSentiment, Transaction, TxType
from core.tests.helpers import TEST_CACHES, make_asset, make_portfolio, make_user
from services.market_data import PriceQuote
from services.news import NewsReport


def quote(price: str, change: str | None = None) -> PriceQuote:
    return PriceQuote(
        ticker="AAPL",
        symbol="AAPL",
        price=Decimal(price),
        previous_close=Decimal(change) if change else None,
    )


def agent_result(action: str, amount: float, sentiment: str = "BULLISH") -> AgentRunResult:
    proposal = TradeProposal(
        action=action, amount=amount, sentiment=sentiment, reasoning="Test chain-of-thought."
    )
    return AgentRunResult(
        proposal=proposal,
        reasoning=proposal.reasoning,
        tokens_used=1500,
        api_cost_usd=Decimal("0.00200"),
        source="crewai",
    )


@override_settings(CACHES=TEST_CACHES)
class RunAlphaAgentTaskTests(TestCase):
    """End-to-end behaviour of one (portfolio, ticker) agent run."""

    def setUp(self):
        from config.celery import app as celery_app

        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", False)

        from tasks import run_alpha_agent_task

        self.task = run_alpha_agent_task

        self.user = make_user()
        self.portfolio = make_portfolio(
            self.user, balance="10000.00", autonomous=True, allocation_pct="5.00"
        )

    def _run(self, ticker: str = "AAPL"):
        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=NewsReport(ticker=ticker)),
        ):
            return self.task.apply(args=[self.portfolio.id, ticker]).get()

    # -- happy paths -------------------------------------------------------
    def test_approved_buy_mutates_state_and_writes_audit_trail(self):
        with patch("tasks.run_alpha_agent", return_value=agent_result("BUY", 2.0)):
            result = self._run()

        self.assertEqual(result["status"], "executed")
        self.assertTrue(result["approved"])

        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.balance_usd, Decimal("9800.00"))

        asset = Asset.objects.get(portfolio=self.portfolio, ticker="AAPL")
        self.assertEqual(asset.amount, Decimal("2.00000000"))
        self.assertEqual(asset.avg_purchase_price, Decimal("100.00"))

        tx = Transaction.objects.get(portfolio=self.portfolio)
        self.assertEqual(tx.tx_type, TxType.BUY)
        self.assertEqual(tx.executed_by, "AI")
        self.assertEqual(tx.gross_value_usd, Decimal("200.00"))

        log = AgentDecisionLog.objects.get(portfolio=self.portfolio)
        self.assertEqual(log.transaction_id, tx.id)
        self.assertIn("Purchased", log.action_taken)
        self.assertEqual(log.market_sentiment, MarketSentiment.BULLISH)
        self.assertEqual(log.tokens_used, 1500)
        self.assertEqual(log.api_cost_usd, Decimal("0.00200"))
        self.assertIn("Test chain-of-thought", log.reasoning)

    def test_weighted_average_price_updates_across_two_buys(self):
        make_asset(self.portfolio, "AAPL", "2", "80.00")
        Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type=TxType.BUY,
            amount=Decimal("2"),
            price=Decimal("80.00"),
            executed_by="AI",
        )
        with patch("tasks.run_alpha_agent", return_value=agent_result("BUY", 2.0)):
            self._run()

        asset = Asset.objects.get(portfolio=self.portfolio, ticker="AAPL")
        self.assertEqual(asset.amount, Decimal("4.00000000"))
        self.assertEqual(asset.avg_purchase_price, Decimal("90.00"))

    def test_approved_sell_returns_cash_to_balance(self):
        make_asset(self.portfolio, "AAPL", "10", "80.00")
        for _ in range(10):
            Transaction.objects.create(
                portfolio=self.portfolio,
                ticker="AAPL",
                tx_type=TxType.BUY,
                amount=Decimal("1"),
                price=Decimal("80.00"),
                executed_by="AI",
            )
        with patch("tasks.run_alpha_agent", return_value=agent_result("SELL", 4.0, "BEARISH")):
            result = self._run()

        self.assertEqual(result["status"], "executed")
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.balance_usd, Decimal("10400.00"))
        asset = Asset.objects.get(portfolio=self.portfolio, ticker="AAPL")
        self.assertEqual(asset.amount, Decimal("6.00000000"))

    def test_hold_writes_log_without_touching_the_ledger(self):
        with patch("tasks.run_alpha_agent", return_value=agent_result("HOLD", 0.0, "NEUTRAL")):
            result = self._run()

        self.assertEqual(result["status"], "hold")
        self.assertFalse(result["approved"])
        self.assertEqual(Transaction.objects.count(), 0)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.balance_usd, Decimal("10000.00"))

        log = AgentDecisionLog.objects.get(portfolio=self.portfolio)
        self.assertIsNone(log.transaction)
        self.assertTrue(log.action_taken.startswith("HOLD"))

    # -- guardrail integration --------------------------------------------
    def test_oversized_buy_is_clamped_to_allocation_ceiling(self):
        with patch("tasks.run_alpha_agent", return_value=agent_result("BUY", 900.0)):
            result = self._run()

        self.assertTrue(result["approved"])
        self.assertEqual(result["notional"], "500.00")  # 5% of $10,000
        self.assertEqual(Decimal(result["amount"]), Decimal("5"))

        asset = Asset.objects.get(portfolio=self.portfolio, ticker="AAPL")
        self.assertEqual(asset.amount, Decimal("5.00000000"))
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.balance_usd, Decimal("9500.00"))

        log = AgentDecisionLog.objects.get(portfolio=self.portfolio)
        self.assertIn("GUARDRAIL", log.reasoning)
        self.assertIn("Clamped", log.reasoning)

    def test_sell_without_position_is_blocked(self):
        with patch("tasks.run_alpha_agent", return_value=agent_result("SELL", 5.0, "BEARISH")):
            result = self._run()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("no open position", result["guard_reason"])
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertTrue(
            AgentDecisionLog.objects.get(portfolio=self.portfolio).action_taken.startswith(
                "BLOCKED"
            )
        )

    def test_daily_loss_limit_blocks_new_buys(self):
        self.portfolio.daily_loss_limit_usd = Decimal("100.00")
        self.portfolio.save(update_fields=["daily_loss_limit_usd"])
        for price in ("100.00", "50.00"):
            Transaction.objects.create(
                portfolio=self.portfolio,
                ticker="AAPL",
                tx_type=TxType.BUY,
                amount=Decimal("10"),
                price=Decimal(price),
                executed_by="AI",
            )
        Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type=TxType.SELL,
            amount=Decimal("10"),
            price=Decimal("50.00"),
            executed_by="AI",
        )

        with patch("tasks.run_alpha_agent", return_value=agent_result("BUY", 1.0)):
            result = self._run()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("daily loss limit", result["guard_reason"].lower())
        self.assertEqual(Transaction.objects.filter(tx_type=TxType.BUY).count(), 2)

    def test_execution_failure_rolls_back_and_is_logged(self):
        with (
            patch("tasks.run_alpha_agent", return_value=agent_result("BUY", 2.0)),
            patch(
                "tasks.apply_trade",
                side_effect=__import__("django").db.DatabaseError("boom"),
            ),
        ):
            result = self._run()

        self.assertFalse(result["approved"])
        self.assertIn("rolled back", result["guard_reason"])
        self.assertEqual(Transaction.objects.count(), 0)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.balance_usd, Decimal("10000.00"))

    # -- lifecycle guards --------------------------------------------------
    def test_non_autonomous_portfolio_is_skipped(self):
        self.portfolio.is_autonomous = False
        self.portfolio.save(update_fields=["is_autonomous"])
        result = self._run()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "autonomy_disabled")
        self.assertEqual(AgentDecisionLog.objects.count(), 0)

    def test_missing_portfolio_is_skipped(self):
        result = self.task.apply(args=[999999, "AAPL"]).get()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "portfolio_missing")

    def test_agent_crash_is_captured_in_the_audit_trail(self):
        with patch("tasks.run_alpha_agent", side_effect=RuntimeError("provider exploded")):
            result = self._run()

        self.assertEqual(result["status"], "error")
        log = AgentDecisionLog.objects.get(portfolio=self.portfolio)
        self.assertIn("provider exploded", log.reasoning)
        self.assertIn("ERROR", log.action_taken)


@override_settings(CACHES=TEST_CACHES)
class MonitoringFanOutTests(TestCase):
    """The fan-out entrypoint must dispatch one independent subtask per ticker."""

    def setUp(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from tasks import autonomous_market_monitoring_task

        # The sweep debounce lives in the process-global cache.
        cache.clear()
        self.task = autonomous_market_monitoring_task
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, autonomous=True)

        # Replace celery's canvas group with an inline executor: it records the
        # signatures the task built and runs each one exactly as the worker
        # pool would, keeping the test independent of broker availability.
        self.captured: list = []

        def inline_group(signatures):
            self.captured = list(signatures)

            class _InlineGroup:
                def apply_async(self_inner, *args, **kwargs):
                    for signature in self.captured:
                        signature.apply()
                    return SimpleNamespace(id="inline-group-id")

            return _InlineGroup()

        patcher = patch("tasks.group", side_effect=inline_group)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_builds_one_signature_per_portfolio_ticker_pair(self):
        make_asset(self.portfolio, "AAPL", "1", "100.00")
        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=NewsReport(ticker="X")),
            patch("tasks.run_alpha_agent", return_value=agent_result("HOLD", 0.0, "NEUTRAL")),
        ):
            result = self.task.apply().get()

        tickers = [sig.args[1] for sig in self.captured]
        # Held ticker first, then the configured watchlist (deduplicated).
        self.assertEqual(tickers, ["AAPL", "TSLA", "BTC"])
        self.assertTrue(all(sig.args[0] == self.portfolio.id for sig in self.captured))
        self.assertEqual(result["dispatched"], 3)
        self.assertEqual(result["portfolios_scanned"], 1)

    def test_disabled_portfolios_are_not_scanned(self):
        self.portfolio.is_autonomous = False
        self.portfolio.save(update_fields=["is_autonomous"])
        result = self.task.apply().get()
        self.assertEqual(result["status"], "noop")
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(self.captured, [])

    def test_fan_out_runs_every_ticker_independently(self):
        """Each ticker gets its own subtask and its own audit-trail row."""
        make_asset(self.portfolio, "AAPL", "1", "100.00")
        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=NewsReport(ticker="X")),
            patch("tasks.run_alpha_agent", return_value=agent_result("HOLD", 0.0, "NEUTRAL")),
        ):
            result = self.task.apply().get()

        self.assertEqual(result["dispatched"], 3)
        self.assertEqual(AgentDecisionLog.objects.filter(portfolio=self.portfolio).count(), 3)

    def test_one_failing_ticker_does_not_stop_the_others(self):
        def flaky(context):
            if context.ticker == "TSLA":
                raise RuntimeError("TSLA data source down")
            return agent_result("HOLD", 0.0, "NEUTRAL")

        make_asset(self.portfolio, "AAPL", "1", "100.00")
        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=NewsReport(ticker="X")),
            patch("tasks.run_alpha_agent", side_effect=flaky),
        ):
            self.task.apply().get()

        logs = AgentDecisionLog.objects.filter(portfolio=self.portfolio)
        self.assertEqual(logs.count(), 3)
        self.assertEqual(logs.filter(action_taken__startswith="ERROR").count(), 1)


@override_settings(CACHES=TEST_CACHES)
class RiskSweepTaskTests(TestCase):
    def setUp(self):
        from tasks import daily_loss_limit_sweep_task

        self.task = daily_loss_limit_sweep_task
        self.user = make_user()
        self.portfolio = make_portfolio(
            self.user, autonomous=True, loss_limit="100.00", balance="1000.00"
        )

    def _realise_loss(self, loss: str):
        Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type=TxType.BUY,
            amount=Decimal("10"),
            price=Decimal("100.00"),
            executed_by="AI",
        )
        Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type=TxType.SELL,
            amount=Decimal("10"),
            price=Decimal("100.00") - Decimal(loss) / Decimal("10"),
            executed_by="AI",
        )

    def test_breaching_portfolio_is_halted_and_logged(self):
        self._realise_loss("150.00")
        result = self.task.apply().get()

        self.assertEqual(result["halted_count"], 1)
        self.portfolio.refresh_from_db()
        self.assertFalse(self.portfolio.is_autonomous)
        log = AgentDecisionLog.objects.get(portfolio=self.portfolio)
        self.assertIn("HALT", log.action_taken)
        self.assertEqual(log.market_sentiment, MarketSentiment.BEARISH)

    def test_portfolio_within_limit_keeps_trading(self):
        self._realise_loss("50.00")
        result = self.task.apply().get()

        self.assertEqual(result["halted_count"], 0)
        self.portfolio.refresh_from_db()
        self.assertTrue(self.portfolio.is_autonomous)

    def test_profitable_portfolio_is_untouched(self):
        Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type=TxType.BUY,
            amount=Decimal("10"),
            price=Decimal("100.00"),
            executed_by="AI",
        )
        Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type=TxType.SELL,
            amount=Decimal("10"),
            price=Decimal("150.00"),
            executed_by="AI",
        )
        self.assertEqual(self.task.apply().get()["halted_count"], 0)
        self.portfolio.refresh_from_db()
        self.assertTrue(self.portfolio.is_autonomous)


@override_settings(CACHES=TEST_CACHES)
class HousekeepingTaskTests(TestCase):
    def test_purges_old_tokens_only(self):
        from datetime import timedelta

        from django.utils import timezone
        from rest_framework.authtoken.models import Token

        from tasks import purge_expired_auth_tokens_task

        old_user = make_user("old")
        new_user = make_user("new")
        old_token, _ = Token.objects.get_or_create(user=old_user)
        Token.objects.get_or_create(user=new_user)
        Token.objects.filter(pk=old_token.pk).update(created=timezone.now() - timedelta(days=90))

        result = purge_expired_auth_tokens_task.apply(args=[30]).get()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(Token.objects.count(), 1)
