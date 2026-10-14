"""Tests for the persistent position thesis.

The crew runs with ``memory=False`` (ADR-0007), so before this every sweep built a
fresh crew with no knowledge of what it decided last time. The system could sell a
position at 10:00 and buy it back at 10:15 without noticing - not because it
changed its mind, but because it had no record of having one.

What matters here is the lifecycle, not the storage: exactly one live thesis per
instrument, superseded rather than overwritten so the history survives, and closed
when the position is. A thesis left open against a position that no longer exists
is worse than no thesis, because it is fed to the next run as current belief.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from core.models import AgentDecisionLog, PositionThesis, ThesisStatus
from core.tests.helpers import TEST_CACHES, make_portfolio, make_user
from services.thesis import close_thesis, current_thesis, record_thesis, sync_after_trade

D = Decimal


@override_settings(CACHES=TEST_CACHES)
class RecordThesisTests(TestCase):
    def setUp(self):
        self.portfolio = make_portfolio(make_user("thesis-user"))

    def test_it_opens_a_thesis_for_a_position(self):
        thesis = record_thesis(
            portfolio=self.portfolio,
            ticker="AAPL",
            thesis="Services margin expanding; upgrade cycle intact.",
            invalidation="Gross margin below 40% for two quarters.",
        )

        self.assertIsNotNone(thesis)
        self.assertEqual(thesis.status, ThesisStatus.OPEN)
        self.assertEqual(thesis.ticker, "AAPL")

    def test_a_blank_thesis_is_not_recorded(self):
        """Writing a placeholder would put words in the agent's mouth and then
        feed them back to it as its own prior belief."""
        for blank in ("", "   ", "\n", None):
            with self.subTest(blank=blank):
                self.assertIsNone(
                    record_thesis(portfolio=self.portfolio, ticker="AAPL", thesis=blank)
                )

    def test_tickers_are_normalised(self):
        thesis = record_thesis(portfolio=self.portfolio, ticker="aapl", thesis="x")

        self.assertEqual(thesis.ticker, "AAPL")

    def test_a_new_thesis_supersedes_the_old_one_without_deleting_it(self):
        first = record_thesis(portfolio=self.portfolio, ticker="AAPL", thesis="first view")
        second = record_thesis(portfolio=self.portfolio, ticker="AAPL", thesis="second view")

        first.refresh_from_db()
        self.assertEqual(first.status, ThesisStatus.CLOSED)
        self.assertIsNotNone(first.closed_at)
        self.assertEqual(second.status, ThesisStatus.OPEN)
        # The history survives: the table records what was believed at each point,
        # not only what is believed now.
        self.assertEqual(PositionThesis.objects.filter(ticker="AAPL").count(), 2)

    def test_only_one_thesis_is_live_at_a_time(self):
        record_thesis(portfolio=self.portfolio, ticker="AAPL", thesis="first")
        record_thesis(portfolio=self.portfolio, ticker="AAPL", thesis="second")

        self.assertEqual(
            PositionThesis.objects.filter(ticker="AAPL", status=ThesisStatus.OPEN).count(), 1
        )

    def test_the_database_refuses_two_live_theses_for_one_instrument(self):
        """The invariant is enforced in the schema, not only in the helper."""
        record_thesis(portfolio=self.portfolio, ticker="AAPL", thesis="first")

        with self.assertRaises(IntegrityError), transaction.atomic():
            PositionThesis.objects.create(
                portfolio=self.portfolio,
                ticker="AAPL",
                thesis="bypassing the helper",
                status=ThesisStatus.OPEN,
            )

    def test_two_portfolios_may_hold_the_same_instrument(self):
        other = make_portfolio(make_user("thesis-user-2"))

        record_thesis(portfolio=self.portfolio, ticker="AAPL", thesis="mine")
        record_thesis(portfolio=other, ticker="AAPL", thesis="theirs")

        self.assertEqual(PositionThesis.objects.filter(ticker="AAPL").count(), 2)


@override_settings(CACHES=TEST_CACHES)
class CloseThesisTests(TestCase):
    def setUp(self):
        self.portfolio = make_portfolio(make_user("close-user"))
        record_thesis(portfolio=self.portfolio, ticker="AAPL", thesis="held for X")

    def test_closing_marks_the_thesis_and_stamps_a_reason(self):
        closed = close_thesis(portfolio=self.portfolio, ticker="AAPL", reason="Position closed.")

        self.assertEqual(closed, 1)
        thesis = PositionThesis.objects.get(ticker="AAPL")
        self.assertEqual(thesis.status, ThesisStatus.CLOSED)
        self.assertEqual(thesis.close_reason, "Position closed.")
        self.assertIsNotNone(thesis.closed_at)

    def test_current_thesis_stops_returning_it_once_closed(self):
        close_thesis(portfolio=self.portfolio, ticker="AAPL")

        self.assertIsNone(current_thesis(self.portfolio, "AAPL"))

    def test_closing_an_instrument_with_no_thesis_is_harmless(self):
        self.assertEqual(close_thesis(portfolio=self.portfolio, ticker="TSLA"), 0)

    def test_closing_with_an_empty_ticker_is_harmless(self):
        self.assertEqual(close_thesis(portfolio=self.portfolio, ticker=""), 0)


@override_settings(CACHES=TEST_CACHES)
class SyncAfterTradeTests(TestCase):
    def setUp(self):
        self.portfolio = make_portfolio(make_user("sync-user"))

    def test_a_remaining_position_opens_a_thesis(self):
        sync_after_trade(
            portfolio=self.portfolio, ticker="AAPL", position_amount=D("5"), thesis="still hold"
        )

        self.assertIsNotNone(current_thesis(self.portfolio, "AAPL"))

    def test_a_fully_sold_position_closes_the_thesis(self):
        sync_after_trade(
            portfolio=self.portfolio, ticker="AAPL", position_amount=D("5"), thesis="first view"
        )
        sync_after_trade(portfolio=self.portfolio, ticker="AAPL", position_amount=D("0"))

        self.assertIsNone(current_thesis(self.portfolio, "AAPL"))
        self.assertEqual(
            PositionThesis.objects.filter(ticker="AAPL", status=ThesisStatus.CLOSED).count(), 1
        )

    def test_selling_part_of_a_position_leaves_the_thesis_open(self):
        sync_after_trade(
            portfolio=self.portfolio, ticker="AAPL", position_amount=D("5"), thesis="first view"
        )
        sync_after_trade(
            portfolio=self.portfolio, ticker="AAPL", position_amount=D("2"), thesis="still hold"
        )

        self.assertIsNotNone(current_thesis(self.portfolio, "AAPL"))

    def test_a_negative_position_is_treated_as_closed(self):
        """Should be impossible - SELL is clamped to the held amount - but a
        thesis left open against a negative position would be worse than a
        defensive check."""
        sync_after_trade(
            portfolio=self.portfolio, ticker="AAPL", position_amount=D("-1"), thesis="x"
        )

        self.assertIsNone(current_thesis(self.portfolio, "AAPL"))


@override_settings(CACHES=TEST_CACHES)
class ThesisInThePromptTests(TestCase):
    """The thesis is only useful if the agents actually receive it."""

    def _context(self, thesis=None):
        from ai_agent import DecisionContext

        return DecisionContext(
            ticker="AAPL",
            risk_profile="high",
            cash_balance=D("1000"),
            position_amount=D("3"),
            avg_purchase_price=D("100"),
            current_price=D("110"),
            max_trade_budget_usd=D("500"),
            current_thesis=thesis,
        )

    def test_the_absence_of_a_thesis_is_stated_explicitly(self):
        """A blank section reads as "no opinion was ever formed", which invites a
        model to invent one."""
        brief = self._context().as_brief()

        self.assertIn("PRIOR THESIS", brief)
        self.assertIn("None on record", brief)

    def test_a_thesis_and_its_invalidation_reach_the_brief(self):
        thesis = PositionThesis(
            ticker="AAPL",
            thesis="Services margin expanding.",
            invalidation="Gross margin below 40%.",
        )

        brief = self._context(thesis).as_brief()

        self.assertIn("Services margin expanding.", brief)
        self.assertIn("Gross margin below 40%.", brief)

    def test_a_thesis_without_an_invalidation_says_so(self):
        thesis = PositionThesis(ticker="AAPL", thesis="A view with no falsifier.", invalidation="")

        brief = self._context(thesis).as_brief()

        self.assertIn("No invalidation condition was recorded", brief)

    def test_the_cio_is_told_to_engage_with_a_prior_thesis(self):
        import inspect

        from ai_agent import AlphaAgentOrchestrator

        source = inspect.getsource(AlphaAgentOrchestrator._build_crew)
        self.assertIn("PRIOR THESIS", source)
        self.assertIn("what has stopped being true", source)


@override_settings(CACHES=TEST_CACHES)
class ThesisWiringTests(TestCase):
    def test_the_task_feeds_the_thesis_into_the_context(self):
        import inspect

        import tasks

        source = inspect.getsource(tasks.run_alpha_agent_task)
        self.assertIn("current_thesis=current_thesis(", source)

    def test_the_task_writes_the_thesis_only_after_an_executed_trade(self):
        """An advisory trade does not move the position, so it must not move the
        thesis - the recommendation may yet be rejected."""
        import inspect

        import tasks

        source = inspect.getsource(tasks.run_alpha_agent_task)
        marker = "if transaction_row is not None and decision_log is not None:"
        self.assertIn(marker, source)
        self.assertIn("sync_after_trade(", source)


@override_settings(CACHES=TEST_CACHES)
class ThesisApiTests(TestCase):
    def setUp(self):
        self.user = make_user("thesis-api-user")
        self.portfolio = make_portfolio(self.user)
        from core.tests.helpers import auth_client

        self.client = auth_client(self.user)

    def test_a_thesis_is_recorded_against_the_decision_that_opened_it(self):
        log = AgentDecisionLog.objects.create(
            portfolio=self.portfolio, ticker="AAPL", action_taken="BUY 3 AAPL", reasoning="r"
        )

        thesis = record_thesis(
            portfolio=self.portfolio, ticker="AAPL", thesis="the view", decision=log
        )

        self.assertEqual(thesis.opened_by_id, log.id)

    def test_a_closed_thesis_records_which_decision_closed_it(self):
        log = AgentDecisionLog.objects.create(
            portfolio=self.portfolio, ticker="AAPL", action_taken="SELL 3 AAPL", reasoning="r"
        )
        record_thesis(portfolio=self.portfolio, ticker="AAPL", thesis="the view")

        close_thesis(portfolio=self.portfolio, ticker="AAPL", reason="sold", decision=log)

        thesis = PositionThesis.objects.get(ticker="AAPL")
        self.assertEqual(thesis.closed_by_id, log.id)
