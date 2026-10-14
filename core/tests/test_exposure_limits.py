"""Tests for the portfolio-level limits: cumulative exposure and concentration.

The per-trade ceiling answers "is this trade too big on its own?". It cannot
answer "is the portfolio already full?", and a sequence of individually permitted
trades is unbounded - which ADR-0001 lists among its own negative consequences.
Beat sweeps every fifteen minutes, so twenty 5% buys reach fully invested without
any single decision looking wrong.

Two limits, because they are different risks. Exposure caps how much is invested
at all. Concentration caps how much sits in one name: a portfolio 90% invested in
a single stock passes every exposure check and is not diversified in any sense a
risk reviewer would accept.
"""

import inspect
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.models import Portfolio
from core.tests.helpers import TEST_CACHES, make_asset, make_portfolio, make_user
from services.execution import Exposure, evaluate_proposal
from services.ledger import PositionState

D = Decimal


class FakeProposal:
    """Minimal stand-in: the guard reads only ``action`` and ``amount``."""

    def __init__(self, action: str, amount: str):
        self.action = action
        self.amount = D(amount)


def portfolio_at(cash: str, *, allocation="5.00", exposure="90.00", concentration="30.00"):
    return Portfolio(
        balance_usd=D(cash),
        max_trade_allocation_pct=D(allocation),
        daily_loss_limit_usd=D("500"),
        max_portfolio_exposure_pct=D(exposure),
        max_ticker_concentration_pct=D(concentration),
    )


def guard(*, cash="10000", amount="10", exposure_in=None, price="100", **kwargs):
    return evaluate_proposal(
        portfolio_at(cash, **kwargs),
        FakeProposal("BUY", amount),
        D(price),
        PositionState(ticker="AAPL"),
        D("0"),
        exposure=exposure_in,
    )


@override_settings(CACHES=TEST_CACHES)
class CumulativeExposureTests(TestCase):
    def test_a_buy_that_would_breach_the_exposure_ceiling_is_sized_down(self):
        # 88% invested of a 90% ceiling leaves $200 of headroom at $100/share.
        decision = guard(exposure_in=Exposure(D("8800"), D("0"), D("10000")))

        self.assertTrue(decision.approved)
        self.assertTrue(decision.clamped)
        self.assertEqual(decision.amount, D("2.00000000"))

    def test_the_clamp_names_the_ceiling_that_bound_it(self):
        """A note that says only "clamped" sends the reader to the wrong limit."""
        decision = guard(exposure_in=Exposure(D("8800"), D("0"), D("10000")))

        self.assertIn("portfolio exposure ceiling", decision.notes[0])
        self.assertIn("90", decision.notes[0])

    def test_a_fully_invested_portfolio_rejects_new_risk(self):
        decision = guard(exposure_in=Exposure(D("9100"), D("0"), D("10000")))

        self.assertFalse(decision.approved)
        self.assertEqual(decision.amount, D("0"))

    def test_a_portfolio_exactly_at_the_ceiling_cannot_buy_more(self):
        decision = guard(exposure_in=Exposure(D("9000"), D("0"), D("10000")))

        self.assertFalse(decision.approved)

    def test_exposure_is_a_percentage_of_equity_not_of_cash(self):
        """Equity is cash plus positions. Using cash would let a portfolio with a
        large position and little cash look empty."""
        decision = guard(cash="1000", exposure_in=Exposure(D("9000"), D("0"), D("10000")))

        self.assertFalse(decision.approved)

    def test_the_per_trade_ceiling_still_wins_when_it_is_tighter(self):
        # $500 per-trade budget binds well before the exposure headroom does.
        decision = guard(exposure_in=Exposure(D("0"), D("0"), D("10000")))

        self.assertEqual(decision.amount, D("5.00000000"))
        self.assertIn("max_trade_allocation_pct", decision.notes[0])


@override_settings(CACHES=TEST_CACHES)
class ConcentrationTests(TestCase):
    def test_a_buy_that_would_concentrate_too_much_is_sized_down(self):
        # $2900 of a 30% ceiling on $10000 equity leaves $100 of headroom.
        decision = guard(exposure_in=Exposure(D("1000"), D("2900"), D("10000")))

        self.assertTrue(decision.approved)
        self.assertEqual(decision.amount, D("1.00000000"))
        self.assertIn("concentration ceiling", decision.notes[0])

    def test_a_position_at_the_concentration_ceiling_blocks_further_buys(self):
        decision = guard(exposure_in=Exposure(D("1000"), D("3000"), D("10000")))

        self.assertFalse(decision.approved)

    def test_the_tighter_of_the_two_limits_binds(self):
        """Both would bind; the smaller headroom must be the one applied."""
        # Exposure leaves $100 of room, concentration leaves $50.
        decision = guard(exposure_in=Exposure(D("8900"), D("2950"), D("10000")))

        self.assertEqual(decision.amount, D("0.50000000"))
        self.assertIn("concentration", decision.notes[0])

    def test_selling_is_never_blocked_by_either_ceiling(self):
        """Both limits govern new risk. Refusing to de-risk would be perverse."""
        pf = portfolio_at("1000")
        decision = evaluate_proposal(
            pf,
            FakeProposal("SELL", "5"),
            D("100"),
            PositionState(ticker="AAPL", amount=D("5")),
            D("0"),
            exposure=Exposure(D("9000"), D("9000"), D("10000")),
        )

        self.assertTrue(decision.approved)


@override_settings(CACHES=TEST_CACHES)
class ExposureOmittedTests(TestCase):
    def test_the_portfolio_checks_are_skipped_without_exposure(self):
        """Degrades to the per-trade limits, not to no limits."""
        decision = guard(exposure_in=None)

        self.assertTrue(decision.approved)
        self.assertEqual(decision.amount, D("5.00000000"))

    def test_zero_equity_disables_the_checks_rather_than_dividing_by_it(self):
        decision = guard(exposure_in=Exposure(D("0"), D("0"), D("0")))

        self.assertTrue(decision.approved)

    def test_a_zero_ceiling_means_disabled_not_blocking(self):
        """0% would otherwise reject every buy, which is not what an unset field
        should do."""
        decision = guard(exposure_in=Exposure(D("0"), D("0"), D("10000")), exposure="0")

        self.assertTrue(decision.approved)


@override_settings(CACHES=TEST_CACHES)
class ExposureWiringTests(TestCase):
    """The guard skips these checks when it has no exposure.

    That is a genuine gap rather than a graceful default, so the production
    caller is asserted to supply it. A new caller that forgot would silently lose
    both cumulative limits while every per-trade check still passed - the exact
    failure shape this whole change exists to remove.
    """

    def test_the_scheduled_task_supplies_exposure(self):
        import tasks

        source = inspect.getsource(tasks.run_alpha_agent_task)
        self.assertIn("build_exposure(", source)
        self.assertIn("exposure=exposure", source)

    def test_the_approval_path_supplies_exposure(self):
        from services import recommendations

        source = inspect.getsource(recommendations.approve_recommendation)
        self.assertIn("build_exposure(", source)
        self.assertIn("exposure=", source)


@override_settings(CACHES=TEST_CACHES)
class BuildExposureTests(TestCase):
    def setUp(self):
        self.portfolio = make_portfolio(make_user("exposure-user"), balance="1000.00")

    def _quote(self, price):
        from services.market_data import PriceQuote

        return lambda ticker, fallback_price=None, **kw: PriceQuote(
            symbol=ticker, price=D(price), source="test"
        )

    def test_it_values_positions_at_market(self):
        from services.portfolio_metrics import build_exposure

        make_asset(self.portfolio, "AAPL", "10", "100.00")

        with patch("services.portfolio_metrics.build_price_map", return_value={"AAPL": None}):
            exposure = build_exposure(self.portfolio, "AAPL")

        # No quote available, so the position is marked at cost - conservative for
        # a ceiling, and matching how the dashboard treats unpriced holdings.
        self.assertEqual(exposure.ticker_value_usd, D("1000.00"))
        self.assertEqual(exposure.equity_usd, D("2000.00"))

    def test_equity_is_cash_plus_positions(self):
        from services.portfolio_metrics import build_exposure

        make_asset(self.portfolio, "AAPL", "1", "100.00")
        with patch("services.portfolio_metrics.build_price_map", return_value={"AAPL": None}):
            exposure = build_exposure(self.portfolio, "AAPL")

        self.assertEqual(exposure.positions_value_usd, D("100.00"))
        self.assertEqual(exposure.equity_usd, D("1100.00"))

    def test_an_untraded_ticker_has_zero_concentration(self):
        from services.portfolio_metrics import build_exposure

        make_asset(self.portfolio, "AAPL", "1", "100.00")
        with patch("services.portfolio_metrics.build_price_map", return_value={"AAPL": None}):
            exposure = build_exposure(self.portfolio, "TSLA")

        self.assertEqual(exposure.ticker_value_usd, D("0.00"))

    def test_an_empty_portfolio_is_all_cash(self):
        from services.portfolio_metrics import build_exposure

        exposure = build_exposure(self.portfolio, "AAPL")

        self.assertEqual(exposure.positions_value_usd, D("0.00"))
        self.assertEqual(exposure.equity_usd, D("1000.00"))


@override_settings(CACHES=TEST_CACHES)
class LimitDefaultsTests(TestCase):
    def test_both_limits_default_to_something_that_actually_binds(self):
        """A default of 0 would be indistinguishable from disabled."""
        portfolio = make_portfolio(make_user("defaults-user"))

        self.assertGreater(portfolio.max_portfolio_exposure_pct, D("0"))
        self.assertGreater(portfolio.max_ticker_concentration_pct, D("0"))
        self.assertLessEqual(portfolio.max_portfolio_exposure_pct, D("100"))
        self.assertLessEqual(portfolio.max_ticker_concentration_pct, D("100"))

    def test_concentration_may_not_exceed_the_exposure_ceiling_by_default(self):
        """Concentration above exposure would be unreachable, and read as a bug."""
        portfolio = make_portfolio(make_user("defaults2-user"))

        self.assertLessEqual(
            portfolio.max_ticker_concentration_pct, portfolio.max_portfolio_exposure_pct
        )
