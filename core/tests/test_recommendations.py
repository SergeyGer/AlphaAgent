"""Human-in-the-loop approval tests.

The critical property under test: **approving a recommendation is not a bypass.**
A proposal that was valid when the AI made it must be re-validated against
current prices and limits before it can reach the ledger.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from ai_agent import TradeProposal
from core.models import (
    AgentDecisionLog,
    DecidedVia,
    RecommendationStatus,
    TradeRecommendation,
    Transaction,
    TxType,
)
from core.tests.helpers import TEST_CACHES, make_asset, make_portfolio, make_user
from services.market_data import PriceQuote
from services.recommendations import (
    approve_recommendation,
    create_recommendation,
    expire_stale_recommendations,
    reject_recommendation,
)


def quote(price: str) -> PriceQuote:
    return PriceQuote(ticker="AAPL", symbol="AAPL", price=Decimal(price))


def proposal(action: str = "BUY", amount: float = 2.0, sentiment: str = "BULLISH"):
    return TradeProposal(
        action=action, amount=amount, sentiment=sentiment, reasoning="Test reasoning."
    )


@override_settings(CACHES=TEST_CACHES)
class CreateRecommendationTests(TestCase):
    def setUp(self):
        self.portfolio = make_portfolio(balance="10000.00", autonomous=False)

    def test_creates_pending_recommendation(self):
        reco = create_recommendation(self.portfolio, proposal(), Decimal("100.00"), ticker="AAPL")
        self.assertIsNotNone(reco)
        self.assertEqual(reco.status, RecommendationStatus.PENDING)
        self.assertEqual(reco.ticker, "AAPL")
        self.assertEqual(reco.notional_usd, Decimal("200.00"))
        self.assertTrue(reco.is_actionable)
        self.assertIsNotNone(reco.expires_at)

    def test_hold_produces_no_recommendation(self):
        self.assertIsNone(
            create_recommendation(
                self.portfolio, proposal("HOLD", 0.0, "NEUTRAL"), Decimal("100.00"), ticker="AAPL"
            )
        )

    def test_duplicate_pending_is_suppressed(self):
        first = create_recommendation(self.portfolio, proposal(), Decimal("100.00"), ticker="AAPL")
        second = create_recommendation(self.portfolio, proposal(), Decimal("100.00"), ticker="AAPL")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(TradeRecommendation.objects.count(), 1)

    def test_different_tickers_are_not_deduped(self):
        create_recommendation(self.portfolio, proposal(), Decimal("100.00"), ticker="AAPL")
        create_recommendation(self.portfolio, proposal(), Decimal("100.00"), ticker="TSLA")
        self.assertEqual(TradeRecommendation.objects.count(), 2)

    def test_zero_amount_is_ignored(self):
        self.assertIsNone(
            create_recommendation(
                self.portfolio, proposal("BUY", 0.0), Decimal("100.00"), ticker="AAPL"
            )
        )

    def test_expiry_uses_configured_ttl(self):
        with override_settings(RECOMMENDATION_TTL_MINUTES=5):
            reco = create_recommendation(
                self.portfolio, proposal(), Decimal("100.00"), ticker="AAPL"
            )
        delta = reco.expires_at - timezone.now()
        self.assertLess(delta, timedelta(minutes=6))
        self.assertGreater(delta, timedelta(minutes=3))


@override_settings(CACHES=TEST_CACHES)
class ApproveRecommendationTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()  # the quote cache is process-global
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="10000.00", autonomous=False)
        self.reco = create_recommendation(
            self.portfolio, proposal("BUY", 2.0), Decimal("100.00"), ticker="AAPL"
        )

    def test_approval_executes_within_limits(self):
        with patch("services.recommendations.get_latest_quote", return_value=quote("100.00")):
            outcome = approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        self.assertTrue(outcome.executed)
        self.assertEqual(outcome.recommendation.status, RecommendationStatus.EXECUTED)
        self.assertIsNotNone(outcome.transaction_id)

        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.balance_usd, Decimal("9800.00"))

        tx = Transaction.objects.get(pk=outcome.transaction_id)
        self.assertEqual(tx.tx_type, TxType.BUY)
        self.assertEqual(tx.amount, Decimal("2.00000000"))
        self.assertEqual(tx.executed_by, "AI")

    def test_approval_records_how_it_was_decided(self):
        with patch("services.recommendations.get_latest_quote", return_value=quote("100.00")):
            approve_recommendation(self.reco.id, via=DecidedVia.TELEGRAM)
        self.reco.refresh_from_db()
        self.assertEqual(self.reco.decided_via, DecidedVia.TELEGRAM)
        self.assertIsNotNone(self.reco.decided_at)

    def test_approval_writes_a_human_decision_to_the_audit_trail(self):
        with patch("services.recommendations.get_latest_quote", return_value=quote("100.00")):
            approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        log = AgentDecisionLog.objects.filter(portfolio=self.portfolio).latest("created_at")
        self.assertIn("Human decision", log.reasoning)
        self.assertIn("APPROVED", log.reasoning)
        self.assertIn("APPROVED by operator", log.action_taken)

    # -- the important ones: approval must NOT bypass the guard ------------
    def test_price_spike_is_clamped_at_approval_time(self):
        """A stale proposal must not slip past the allocation ceiling.

        $2 x $100 = $200 fitted the 5% ($500) budget when the AI proposed it.
        After a spike to $400 the same order is worth $800, so the guard clamps
        it back down to the ceiling instead of executing the original size.
        """
        with patch("services.recommendations.get_latest_quote", return_value=quote("400.00")):
            outcome = approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        self.assertTrue(outcome.executed)
        tx = Transaction.objects.get()
        self.assertLessEqual(tx.gross_value_usd, Decimal("500.00"))
        self.assertLess(tx.amount, Decimal("2"))
        # The stored recommendation reflects what actually executed.
        self.reco.refresh_from_db()
        self.assertEqual(self.reco.notional_usd, tx.gross_value_usd)

    def test_a_tiny_budget_clamps_down_rather_than_rejecting(self):
        """The guard prefers a smaller valid trade over no trade at all."""
        self.portfolio.balance_usd = Decimal("10.00")
        self.portfolio.save(update_fields=["balance_usd"])

        with patch("services.recommendations.get_latest_quote", return_value=quote("400.00")):
            outcome = approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        # 5% of $10 = a $0.50 ceiling, so the $800 order is clamped to fit it.
        self.assertTrue(outcome.executed)
        tx = Transaction.objects.get()
        self.assertLessEqual(tx.gross_value_usd, Decimal("0.50"))

    def test_zero_allocation_blocks_the_buy_outright(self):
        """With a $0 ceiling nothing can fit, so the guard rejects."""
        self.portfolio.max_trade_allocation_pct = Decimal("0.00")
        self.portfolio.save(update_fields=["max_trade_allocation_pct"])

        with patch("services.recommendations.get_latest_quote", return_value=quote("100.00")):
            outcome = approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.recommendation.status, RecommendationStatus.BLOCKED)
        self.assertIn("max_trade_allocation_pct", outcome.guard_reason)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_sell_approval_without_position_is_blocked(self):
        reco = create_recommendation(
            self.portfolio, proposal("SELL", 5.0, "BEARISH"), Decimal("100.00"), ticker="TSLA"
        )
        with patch("services.recommendations.get_latest_quote", return_value=quote("100.00")):
            outcome = approve_recommendation(reco.id, via=DecidedVia.WEB)

        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.recommendation.status, RecommendationStatus.BLOCKED)
        self.assertIn("no open position", outcome.guard_reason)

    def test_buy_blocked_when_daily_loss_limit_is_breached(self):
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

        with patch("services.recommendations.get_latest_quote", return_value=quote("100.00")):
            outcome = approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        self.assertFalse(outcome.executed)
        self.assertIn("daily loss limit", outcome.guard_reason.lower())

    def test_expired_recommendation_cannot_be_approved(self):
        TradeRecommendation.objects.filter(pk=self.reco.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        with patch("services.recommendations.get_latest_quote", return_value=quote("100.00")):
            outcome = approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.recommendation.status, RecommendationStatus.EXPIRED)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_double_approval_is_rejected(self):
        with patch("services.recommendations.get_latest_quote", return_value=quote("100.00")):
            approve_recommendation(self.reco.id, via=DecidedVia.WEB)
            second = approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        self.assertFalse(second.executed)
        self.assertIn("already decided", second.message)
        self.assertEqual(Transaction.objects.count(), 1)

    def test_no_price_at_all_blocks_execution(self):
        """No quote and no fallback => the guard refuses to trade blind."""
        with patch("services.recommendations.get_latest_quote", return_value=None):
            outcome = approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.recommendation.status, RecommendationStatus.BLOCKED)
        self.assertIn("no reliable market price", outcome.guard_reason)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_provider_outage_falls_back_to_the_proposed_price(self):
        """A market-data outage degrades to the proposal's own price, not a crash.

        This exercises the real fallback chain in ``services.market_data`` rather
        than stubbing the public accessor.
        """
        with patch("services.market_data._fetch_from_yfinance", return_value=None):
            outcome = approve_recommendation(self.reco.id, via=DecidedVia.WEB)

        self.assertTrue(outcome.executed)
        tx = Transaction.objects.get()
        self.assertEqual(tx.price, Decimal("100.00"))


@override_settings(CACHES=TEST_CACHES)
class RejectRecommendationTests(TestCase):
    def setUp(self):
        self.portfolio = make_portfolio(balance="10000.00", autonomous=False)
        self.reco = create_recommendation(
            self.portfolio, proposal(), Decimal("100.00"), ticker="AAPL"
        )

    def test_reject_marks_status_and_writes_audit_row(self):
        outcome = reject_recommendation(self.reco.id, via=DecidedVia.WEB)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.recommendation.status, RecommendationStatus.REJECTED)
        self.assertEqual(Transaction.objects.count(), 0)

        log = AgentDecisionLog.objects.latest("created_at")
        self.assertIn("REJECTED", log.reasoning)

    def test_reject_does_not_move_money(self):
        self.portfolio.refresh_from_db()
        before = self.portfolio.balance_usd
        reject_recommendation(self.reco.id, via=DecidedVia.WEB)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.balance_usd, before)

    def test_double_reject_is_idempotent(self):
        reject_recommendation(self.reco.id, via=DecidedVia.WEB)
        second = reject_recommendation(self.reco.id, via=DecidedVia.WEB)
        self.assertIn("already decided", second.message)


@override_settings(CACHES=TEST_CACHES)
class ExpiryTests(TestCase):
    def test_stale_recommendations_expire(self):
        portfolio = make_portfolio(balance="10000.00")
        reco = create_recommendation(portfolio, proposal(), Decimal("100.00"), ticker="AAPL")
        TradeRecommendation.objects.filter(pk=reco.pk).update(
            expires_at=timezone.now() - timedelta(minutes=5)
        )

        self.assertEqual(expire_stale_recommendations(), 1)
        reco.refresh_from_db()
        self.assertEqual(reco.status, RecommendationStatus.EXPIRED)
        self.assertEqual(reco.decided_via, DecidedVia.AUTO)

    def test_fresh_recommendations_are_untouched(self):
        portfolio = make_portfolio(balance="10000.00")
        create_recommendation(portfolio, proposal(), Decimal("100.00"), ticker="AAPL")
        self.assertEqual(expire_stale_recommendations(), 0)
        self.assertEqual(TradeRecommendation.objects.get().status, RecommendationStatus.PENDING)


@override_settings(CACHES=TEST_CACHES)
class AdvisoryModeTaskTests(TestCase):
    """The Celery task must queue proposals instead of trading them."""

    def setUp(self):
        from config.celery import app as celery_app

        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", False)

        from tasks import run_alpha_agent_task

        self.task = run_alpha_agent_task
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="10000.00", autonomous=False)

    def _run(self, require_approval: bool, action: str = "BUY", amount: float = 2.0):
        from ai_agent import AgentRunResult
        from services.news import NewsReport

        result = AgentRunResult(
            proposal=proposal(action, amount, "BULLISH" if action == "BUY" else "BEARISH"),
            reasoning="Test reasoning.",
            tokens_used=100,
            api_cost_usd=Decimal("0.001"),
            source="crewai",
        )
        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=NewsReport(ticker="AAPL")),
            patch("tasks.run_alpha_agent", return_value=result),
            patch("tasks.notify_recommendation.delay"),
        ):
            return self.task.apply(args=[self.portfolio.id, "AAPL", require_approval]).get()

    def test_advisory_mode_queues_instead_of_executing(self):
        result = self._run(require_approval=True)

        self.assertEqual(result["status"], "awaiting_approval")
        self.assertIsNotNone(result["recommendation_id"])
        self.assertEqual(Transaction.objects.count(), 0)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.balance_usd, Decimal("10000.00"))

        log = AgentDecisionLog.objects.latest("created_at")
        self.assertIn("AWAITING APPROVAL", log.action_taken)

    def test_non_autonomous_portfolio_is_skipped_without_advisory_flag(self):
        result = self._run(require_approval=False)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "autonomy_disabled")
        self.assertEqual(TradeRecommendation.objects.count(), 0)

    def test_autonomous_portfolio_ignores_the_advisory_flag(self):
        """Autonomy always wins: an autonomous portfolio never queues for approval."""
        self.portfolio.is_autonomous = True
        self.portfolio.save(update_fields=["is_autonomous"])

        result = self._run(require_approval=True)

        self.assertEqual(result["status"], "executed")
        self.assertIsNone(result["recommendation_id"])
        self.assertEqual(TradeRecommendation.objects.count(), 0)
        self.assertEqual(Transaction.objects.count(), 1)

    def test_hold_in_advisory_mode_creates_nothing(self):
        result = self._run(require_approval=True, action="HOLD", amount=0.0)
        self.assertEqual(result["status"], "hold")
        self.assertIsNone(result["recommendation_id"])
        self.assertEqual(TradeRecommendation.objects.count(), 0)


@override_settings(CACHES=TEST_CACHES)
class SnapshotTests(TestCase):
    def test_snapshot_records_valuation(self):
        from services.snapshots import capture_snapshot

        portfolio = make_portfolio(balance="5000.00")
        make_asset(portfolio, "AAPL", "10", "100.00")

        with patch("services.portfolio_metrics.get_latest_quote", return_value=quote("150.00")):
            snapshot = capture_snapshot(portfolio)

        self.assertEqual(snapshot.cash_balance_usd, Decimal("5000.00"))
        self.assertEqual(snapshot.positions_value_usd, Decimal("1500.00"))
        self.assertEqual(snapshot.total_equity_usd, Decimal("6500.00"))

    def test_equity_series_is_chronological(self):
        from services.snapshots import capture_snapshot, equity_series

        portfolio = make_portfolio(balance="1000.00")
        for _ in range(3):
            capture_snapshot(portfolio, price_map={})

        rows = equity_series(portfolio)
        self.assertEqual(len(rows), 3)
        self.assertLessEqual(rows[0].captured_at, rows[-1].captured_at)

    def test_manual_asset_prices_do_not_break_capture(self):
        from services.snapshots import capture_snapshot

        portfolio = make_portfolio(balance="1000.00")
        make_asset(portfolio, "AAPL", "2", "50.00")
        snapshot = capture_snapshot(portfolio, price_map={})
        self.assertEqual(snapshot.positions_value_usd, Decimal("100.00"))


@override_settings(CACHES=TEST_CACHES)
class DebatePersistenceTests(TestCase):
    """The bull and bear arguments must survive into the audit trail."""

    def setUp(self):
        from config.celery import app as celery_app

        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", False)

        from tasks import run_alpha_agent_task

        self.task = run_alpha_agent_task
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="10000.00", autonomous=True)

    def _run(self):
        from ai_agent import AgentRunResult
        from services.news import NewsReport

        result = AgentRunResult(
            proposal=proposal("BUY", 2.0),
            reasoning="The bull case wins: three upgrades outweighed margin concerns.",
            bull_case="BULL CASE\n+ analysts upgraded\n+ record demand",
            bear_case="BEAR CASE\n- debt/equity of 210\n- below the 200-day average",
            tokens_used=1000,
            api_cost_usd=Decimal("0.01"),
            source="crewai",
        )
        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=NewsReport(ticker="AAPL")),
            patch("tasks.get_financial_health", return_value=None),
            patch("tasks.get_price_history", return_value=None),
            patch("tasks.run_alpha_agent", return_value=result),
            patch("tasks.notify_recommendation.delay"),
        ):
            return self.task.apply(args=[self.portfolio.id, "AAPL"]).get()

    def test_both_cases_are_persisted_on_the_decision_log(self):
        self._run()
        log = AgentDecisionLog.objects.latest("created_at")
        self.assertIn("BULL CASE", log.bull_case)
        self.assertIn("record demand", log.bull_case)
        self.assertIn("BEAR CASE", log.bear_case)
        self.assertIn("debt/equity", log.bear_case)
        self.assertIn("bull case wins", log.reasoning)

    def test_task_result_reports_debate_sizes(self):
        result = self._run()
        self.assertGreater(result["bull_case_chars"], 0)
        self.assertGreater(result["bear_case_chars"], 0)

    def test_debate_is_exposed_over_the_api(self):
        from rest_framework.authtoken.models import Token
        from rest_framework.test import APIClient

        self._run()
        token, _ = Token.objects.get_or_create(user=self.user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        row = client.get("/api/portfolio/logs/").json()["results"][0]
        self.assertIn("BULL CASE", row["bull_case"])
        self.assertIn("BEAR CASE", row["bear_case"])


@override_settings(CACHES=TEST_CACHES)
class DeterministicDebateTests(TestCase):
    """The offline engine must still produce both sides for the UI."""

    def _context(self, with_evidence: bool = False):
        from ai_agent import DecisionContext
        from services.fundamentals import FinancialHealth, PriceHistory
        from services.news import NewsArticle, NewsReport

        report = NewsReport(ticker="AAPL")
        report.articles = [
            NewsArticle(
                title="Apple beats expectations", source="Wire", polarity=3.0, sentiment="BULLISH"
            ),
            NewsArticle(
                title="Apple margins compress", source="Wire", polarity=-2.5, sentiment="BEARISH"
            ),
        ]
        return DecisionContext(
            ticker="AAPL",
            risk_profile="medium",
            cash_balance=Decimal("10000"),
            position_amount=Decimal("0"),
            avg_purchase_price=Decimal("0"),
            current_price=Decimal("100"),
            max_trade_budget_usd=Decimal("500"),
            news_report=report,
            financial_health=(
                FinancialHealth(ticker="AAPL", red_flags=["High leverage: D/E 210"])
                if with_evidence
                else None
            ),
            price_history=(
                PriceHistory(ticker="AAPL", degradation_flags=["Below the 200-day average"])
                if with_evidence
                else None
            ),
        )

    def test_both_cases_are_generated(self):
        from ai_agent import HeuristicDecisionEngine

        bull, bear = HeuristicDecisionEngine().debate(self._context(with_evidence=True))
        self.assertIn("BULL CASE", bull)
        self.assertIn("Apple beats expectations", bull)
        self.assertIn("BEAR CASE", bear)
        self.assertIn("Apple margins compress", bear)
        self.assertIn("High leverage", bear)
        self.assertIn("200-day", bear)

    def test_engine_admits_when_a_side_has_no_evidence(self):
        from ai_agent import HeuristicDecisionEngine
        from services.news import NewsReport

        context = self._context()
        context.news_report = NewsReport(ticker="AAPL")
        bull, bear = HeuristicDecisionEngine().debate(context)
        self.assertIn("No positive headlines", bull)
        self.assertIn("No negative headlines", bear)

    def test_debate_is_not_an_endorsement(self):
        from ai_agent import HeuristicDecisionEngine

        bull, _bear = HeuristicDecisionEngine().debate(self._context(with_evidence=True))
        self.assertIn("not an endorsement", bull)
