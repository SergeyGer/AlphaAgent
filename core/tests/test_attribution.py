"""Tests for decision attribution - scoring decisions against outcomes.

The scoring rules are the substance here. A hit rate is easy to compute wrongly
in a way that flatters or punishes the system, and the two mistakes that matter
are both tested explicitly:

* A **SELL** must count as right when the price *falls*. Scoring every action
  against "did the price rise" would mark every correct exit as a failure.
* A **HOLD** must not enter the hit rate at all. It makes no directional claim,
  and counting those rows as misses would drag the measured rate toward zero in
  proportion to how cautious the system was.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import AgentDecisionLog, DecisionOutcome
from core.tests.helpers import TEST_CACHES, make_portfolio, make_user
from services.attribution import (
    action_of,
    build_scorecard,
    directional_hit,
    evaluate_pending,
    signed_return,
)

D = Decimal


def fake_history(points: list[tuple[str, str]]):
    """A ``get_price_history`` stand-in returning fixed (date, close) pairs."""
    from services.fundamentals import PriceHistory

    return lambda ticker, period="1y": PriceHistory(
        ticker=ticker,
        period=period,
        points=[(stamp, D(price)) for stamp, price in points],
        latest=D(points[-1][1]) if points else None,
    )


@override_settings(CACHES=TEST_CACHES)
class ActionNormalisationTests(TestCase):
    def test_the_phrasings_the_log_actually_stores(self):
        self.assertEqual(action_of("BUY 3 AAPL"), "BUY")
        self.assertEqual(action_of("Purchased 10 shares"), "BUY")
        self.assertEqual(action_of("SELL 2 BTC"), "SELL")
        self.assertEqual(action_of("Trimmed position"), "SELL")
        self.assertEqual(action_of("HOLD - no execution required."), "HOLD")

    def test_halts_and_failures_are_not_directional_calls(self):
        """A halt is a state, not a call. Scoring it would be noise."""
        for text in (
            "HALT - AI spend ceiling reached",
            "HALT - daily loss limit breached ($-600)",
            "Blocked by daily loss limit",
            "ERROR - agent pipeline failed",
        ):
            with self.subTest(text=text):
                self.assertEqual(action_of(text), "OTHER")

    def test_an_empty_action_is_other_not_hold(self):
        # "Hold" and "we never recorded anything" are different, and defaulting
        # to HOLD would silently inflate the sample the scorecard averages over.
        self.assertEqual(action_of(""), "OTHER")
        self.assertEqual(action_of(None), "OTHER")


@override_settings(CACHES=TEST_CACHES)
class ScoringRuleTests(TestCase):
    def test_a_sell_profits_when_the_price_falls(self):
        self.assertEqual(signed_return("SELL", D("-10")), D("10"))
        self.assertIs(directional_hit("SELL", D("-10")), True)

    def test_a_sell_loses_when_the_price_rises(self):
        self.assertEqual(signed_return("SELL", D("10")), D("-10"))
        self.assertIs(directional_hit("SELL", D("10")), False)

    def test_a_buy_profits_when_the_price_rises(self):
        self.assertEqual(signed_return("BUY", D("10")), D("10"))
        self.assertIs(directional_hit("BUY", D("10")), True)

    def test_a_hold_makes_no_claim_and_earns_nothing(self):
        for move in (D("10"), D("-10"), D("0")):
            with self.subTest(move=move):
                self.assertEqual(signed_return("HOLD", move), D("0"))
                # None, not False: an unscorable row must not count as a miss.
                self.assertIsNone(directional_hit("HOLD", move))

    def test_a_flat_move_is_not_a_hit_for_either_direction(self):
        self.assertIs(directional_hit("BUY", D("0")), False)
        self.assertIs(directional_hit("SELL", D("0")), False)


@override_settings(CACHES=TEST_CACHES)
class ScorecardTests(TestCase):
    def test_an_empty_scorecard_reports_no_hit_rate_rather_than_zero(self):
        """0% would read as total failure; nothing scored is not the same thing."""
        card = build_scorecard(1)

        self.assertEqual(card.evaluated, 0)
        self.assertIsNone(card.hit_rate_pct)
        self.assertIsNone(card.avg_alpha_pct)

    def test_the_hit_rate_excludes_holds_from_the_denominator(self):
        portfolio = make_portfolio(make_user("score-user"))
        # One decision each: (decision, horizon) is unique, so five outcomes for
        # one decision is not a shape the model permits - and it should not be,
        # since two scores for the same decision and horizon would be ambiguous.
        # One hit, two misses, and two holds that must not count either way.
        for index, hit in enumerate((True, False, False, None, None)):
            log = AgentDecisionLog.objects.create(
                portfolio=portfolio,
                ticker="AAPL",
                action_taken="HOLD" if hit is None else "BUY",
                reasoning=f"r{index}",
            )
            DecisionOutcome.objects.create(
                decision=log,
                horizon_days=1,
                ticker="AAPL",
                action="HOLD" if hit is None else "BUY",
                price_at_decision=D("100"),
                price_at_horizon=D("110"),
                forward_return_pct=D("10"),
                decision_return_pct=D("10") if hit else D("-10"),
                directional_hit=hit,
            )

        card = build_scorecard(1)

        self.assertEqual(card.evaluated, 5)
        self.assertEqual(card.directional_calls, 3)
        self.assertEqual(card.hits, 1)
        self.assertEqual(card.hit_rate_pct, 33.3)

    def test_alpha_is_null_when_no_row_has_a_benchmark(self):
        """An unknown comparison is not a loss."""
        portfolio = make_portfolio(make_user("alpha-user"))
        log = AgentDecisionLog.objects.create(
            portfolio=portfolio, ticker="AAPL", action_taken="BUY", reasoning="r"
        )
        DecisionOutcome.objects.create(
            decision=log,
            horizon_days=1,
            ticker="AAPL",
            action="BUY",
            price_at_decision=D("100"),
            price_at_horizon=D("110"),
            forward_return_pct=D("10"),
            decision_return_pct=D("10"),
            benchmark_return_pct=None,
            directional_hit=True,
        )

        card = build_scorecard(1)

        self.assertIsNone(card.avg_benchmark_return_pct)
        self.assertIsNone(card.avg_alpha_pct)

    def test_the_scorecard_can_be_scoped_to_one_portfolio(self):
        mine = make_portfolio(make_user("mine"))
        theirs = make_portfolio(make_user("theirs"))
        for portfolio, ret in ((mine, "10"), (theirs, "-10")):
            log = AgentDecisionLog.objects.create(
                portfolio=portfolio, ticker="AAPL", action_taken="BUY", reasoning="r"
            )
            DecisionOutcome.objects.create(
                decision=log,
                horizon_days=1,
                ticker="AAPL",
                action="BUY",
                price_at_decision=D("100"),
                price_at_horizon=D("110"),
                forward_return_pct=D(ret),
                decision_return_pct=D(ret),
                directional_hit=D(ret) > 0,
            )

        self.assertEqual(build_scorecard(1, portfolio=mine).evaluated, 1)
        self.assertEqual(build_scorecard(1, portfolio=mine).avg_decision_return_pct, D("10.0000"))
        self.assertEqual(build_scorecard(1).evaluated, 2)


@override_settings(CACHES=TEST_CACHES)
class EvaluatePendingTests(TestCase):
    """The evaluator itself, with price history stubbed out."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("eval-user"))

        # Dated relative to now, not fixed. The evaluator looks for the first
        # trading day at or after the decision, so a hardcoded history silently
        # produces no price the moment the wall clock moves past it - which is
        # how these tests first failed.
        today = timezone.now().date()

        # 30 days of coverage at weekly intervals, enough for 1/7/30-day horizons.
        # A `series()` helper was written here first and then not used, because
        # the two series need different lengths and spellings. CodeQL reported the
        # leftover as py/unused-local-variable - see the note in
        # Quality-and-Testing about how that was found.
        self.history = {
            "AAPL": [
                ((today - timezone.timedelta(days=30 - i * 5)).isoformat(), price)
                for i, price in enumerate(("100", "105", "110", "108", "115", "120", "90"))
            ],
            "SPY": [
                ((today - timezone.timedelta(days=30 - i * 5)).isoformat(), price)
                for i, price in enumerate(("400", "402", "404", "406", "408", "410", "420"))
            ],
        }

    def _log(self, *, ticker="AAPL", action="BUY 3 AAPL", days_ago=10):
        log = AgentDecisionLog.objects.create(
            portfolio=self.portfolio, ticker=ticker, action_taken=action, reasoning="r"
        )
        AgentDecisionLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=days_ago)
        )
        log.refresh_from_db()
        return log

    def _run(self, limit=50):
        def loader(ticker, period="1y"):
            from services.fundamentals import PriceHistory

            points = self.history.get(ticker, [])
            return PriceHistory(
                ticker=ticker,
                period=period,
                points=[(s, D(p)) for s, p in points],
                latest=D(points[-1][1]) if points else None,
            )

        with patch("services.fundamentals.get_price_history", side_effect=loader):
            return evaluate_pending(limit=limit)

    def test_it_scores_a_matured_decision(self):
        self._log(days_ago=10)

        counts = self._run()

        self.assertGreaterEqual(counts["scored"], 1)
        self.assertTrue(DecisionOutcome.objects.exists())

    def test_it_records_the_benchmark_alongside(self):
        self._log(days_ago=10)

        self._run()

        outcome = DecisionOutcome.objects.filter(horizon_days=1).first()
        self.assertIsNotNone(outcome.benchmark_return_pct)

    def test_it_is_idempotent(self):
        """Running twice must not duplicate rows - it runs hourly on Beat."""
        self._log(days_ago=10)

        self._run()
        first = DecisionOutcome.objects.count()
        self._run()

        self.assertEqual(DecisionOutcome.objects.count(), first)

    def test_a_decision_younger_than_a_horizon_is_not_scored_for_it(self):
        self._log(days_ago=0)

        self._run()

        self.assertFalse(DecisionOutcome.objects.filter(horizon_days=30).exists())

    def test_a_decision_with_no_ticker_is_skipped(self):
        """Rows written before the ticker column cannot be priced."""
        log = AgentDecisionLog.objects.create(
            portfolio=self.portfolio, ticker="", action_taken="HOLD", reasoning="r"
        )
        AgentDecisionLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=10)
        )

        self._run()

        self.assertEqual(DecisionOutcome.objects.filter(decision=log).count(), 0)

    def test_unpriced_instruments_are_counted_not_guessed(self):
        """No price for the decision day means no denominator, so no return."""
        self._log(ticker="UNKNOWN", days_ago=10)

        counts = self._run()

        self.assertGreaterEqual(counts["unpriced"], 1)
        self.assertEqual(DecisionOutcome.objects.count(), 0)


@override_settings(CACHES=TEST_CACHES)
class AttributionApiTests(TestCase):
    def setUp(self):
        self.user = make_user("api-attr-user")
        self.portfolio = make_portfolio(self.user)
        from core.tests.helpers import auth_client

        self.client = auth_client(self.user)

    def test_the_endpoint_returns_every_horizon(self):
        response = self.client.get("/api/portfolio/attribution/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            [h["horizon_days"] for h in body["horizons"]], list(DecisionOutcome.HORIZONS)
        )
        self.assertEqual(body["benchmark"], "SPY")

    def test_it_requires_authentication(self):
        from rest_framework.test import APIClient

        self.assertEqual(APIClient().get("/api/portfolio/attribution/").status_code, 401)

    def test_a_hostile_recent_parameter_does_not_500(self):
        for value in ("abc", "-5", "99999", "", "1e999"):
            with self.subTest(value=value):
                response = self.client.get(f"/api/portfolio/attribution/?recent={value}")
                self.assertEqual(response.status_code, 200)


@override_settings(CACHES=TEST_CACHES)
class DecisionLogTickerTests(TestCase):
    """The column that made attribution possible for HOLD rows."""

    def setUp(self):
        self.user = make_user("ticker-user")
        self.portfolio = make_portfolio(self.user)
        from core.tests.helpers import auth_client

        self.client = auth_client(self.user)

    def test_a_hold_records_its_ticker(self):
        """It used to be unrecoverable: no transaction, so no ticker anywhere."""
        log = AgentDecisionLog.objects.create(
            portfolio=self.portfolio, ticker="TSLA", action_taken="HOLD", reasoning="r"
        )

        self.assertEqual(log.ticker, "TSLA")

    def test_the_api_exposes_the_ticker_without_a_transaction(self):
        AgentDecisionLog.objects.create(
            portfolio=self.portfolio, ticker="TSLA", action_taken="HOLD", reasoning="r"
        )

        rows = self.client.get("/api/portfolio/logs/").json()
        rows = rows.get("results", rows)
        self.assertEqual(rows[0]["ticker"], "TSLA")

    def test_filtering_by_ticker_finds_holds(self):
        AgentDecisionLog.objects.create(
            portfolio=self.portfolio, ticker="TSLA", action_taken="HOLD", reasoning="r"
        )
        AgentDecisionLog.objects.create(
            portfolio=self.portfolio, ticker="AAPL", action_taken="HOLD", reasoning="r"
        )

        rows = self.client.get("/api/portfolio/logs/?ticker=TSLA").json()
        rows = rows.get("results", rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "TSLA")
