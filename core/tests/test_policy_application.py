"""FIX-03 acceptance: policy is enforced when a plan is applied, not only planned.

Each test changes exactly one input that decides permission after the plan was
approved and asserts that application stops before the first ledger row. The
plan remains revalidatable; nothing is silently replanned and no fill is written.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import InstrumentClassification, Transaction
from core.tests.helpers import TEST_CACHES
from core.tests.test_portfolio_allocation import (
    QUOTES,
    build_brief,
    make_allocation_portfolio,
    payload_for,
)
from services.market_data import PriceQuote
from services.portfolio_allocation import (
    PlanLegFailed,
    PlanReapprovalRequired,
    SubmissionConflictError,
    apply_plan,
    create_plan,
)
from services.portfolio_policy import AllocationPolicy
from services.portfolio_targets import validate_target_proposal

D = Decimal
CLAIM_IDS = {"MSFT": ["claim-msft"], "JPM": ["claim-jpm"], "XYZ": ["claim-xyz"]}
TIMED_QUOTES = {
    ticker: PriceQuote(
        ticker=ticker,
        symbol=ticker,
        price=price,
        source="test_market_data",
        as_of=timezone.now(),
    )
    for ticker, price in QUOTES.items()
}


def timed(prices=None):
    as_of = timezone.now()
    return {
        ticker: PriceQuote(
            ticker=ticker,
            symbol=ticker,
            price=price,
            source="test_market_data",
            as_of=as_of,
        )
        for ticker, price in (prices or QUOTES).items()
    }


def plan_for(portfolio, brief, weights, *, key, **kwargs):
    proposal = validate_target_proposal(payload_for(brief, weights, claim_ids=CLAIM_IDS), brief)
    plan, _created = create_plan(
        portfolio, brief=brief, proposal=proposal, submission_key=key, **kwargs
    )
    return plan


@override_settings(CACHES=TEST_CACHES)
class PolicyAtApplicationTests(TestCase):
    def setUp(self):
        self.portfolio = make_allocation_portfolio(cash="10000.00", holdings={})
        self.brief = build_brief(self.portfolio)
        self.weights = {"MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.60")}

    def _assert_no_ledger(self):
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

    def test_a_sector_ceiling_tightened_after_planning_requires_reapproval(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="sector-tightened")
        self.portfolio.max_sector_concentration_pct = D("20.00")
        self.portfolio.save(update_fields=["max_sector_concentration_pct"])
        with self.assertRaises(PlanReapprovalRequired):
            apply_plan(plan.id, quotes=timed())
        self._assert_no_ledger()
        plan.refresh_from_db()
        self.assertTrue(plan.revalidation_required)
        self.assertEqual(plan.status, "proposed")

    def test_doubling_one_sectors_prices_before_application_requires_replanning(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="price-double")
        doubled = {**QUOTES, "MSFT": D("200")}
        with self.assertRaises(PlanReapprovalRequired) as caught:
            apply_plan(plan.id, quotes=timed(doubled))
        self.assertIn("no longer matches", str(caught.exception))
        self._assert_no_ledger()

    def test_a_policy_override_at_application_requires_reapproval(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="policy-override")
        tightened = AllocationPolicy.from_portfolio(
            self.portfolio, overrides={"max_sector_weight": D("0.20")}
        )
        with self.assertRaises(PlanReapprovalRequired):
            apply_plan(plan.id, quotes=timed(), policy=tightened)
        self._assert_no_ledger()

    def test_tightening_the_cash_buffer_after_planning_requires_reapproval(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="cash-tightened")
        self.portfolio.min_cash_buffer_pct = D("80.00")
        self.portfolio.save(update_fields=["min_cash_buffer_pct"])
        with self.assertRaises(PlanReapprovalRequired):
            apply_plan(plan.id, quotes=timed())
        self._assert_no_ledger()

    def test_a_changed_classification_after_planning_requires_reapproval(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="classification-changed")
        InstrumentClassification.objects.filter(portfolio=self.portfolio, ticker="MSFT").update(
            sector="consumer"
        )
        with self.assertRaises(PlanReapprovalRequired):
            apply_plan(plan.id, quotes=timed())
        self._assert_no_ledger()

    def test_removing_a_recorded_classification_after_planning_requires_reapproval(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="classification-removed")
        InstrumentClassification.objects.filter(portfolio=self.portfolio, ticker="MSFT").delete()
        with self.assertRaises(PlanReapprovalRequired):
            apply_plan(plan.id, quotes=timed())
        self._assert_no_ledger()

    def test_removing_the_quote_of_an_untouched_holding_fails_without_a_ledger_row(self):
        portfolio = make_allocation_portfolio(cash="8000.00", holdings={"AAPL": ("20", "100")})
        brief = build_brief(portfolio)
        weights = {
            "AAPL": D("0.20"),
            "MSFT": D("0.20"),
            "JPM": D("0.10"),
            "CASH": D("0.50"),
        }
        plan = plan_for(portfolio, brief, weights, key="untouched-missing")
        with self.assertRaises(PlanLegFailed) as caught:
            apply_plan(plan.id, quotes=timed({"MSFT": QUOTES["MSFT"], "JPM": QUOTES["JPM"]}))
        self.assertEqual(caught.exception.code, "price_unavailable")
        self.assertIn("AAPL", str(caught.exception))
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 0)

    def test_a_stale_quote_fails_before_any_ledger_row(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="stale-mark")
        stale = {
            ticker: PriceQuote(
                ticker=ticker,
                symbol=ticker,
                price=price,
                as_of=timezone.now() - timedelta(hours=2),
            )
            for ticker, price in QUOTES.items()
        }
        with self.assertRaises(PlanLegFailed) as caught:
            apply_plan(plan.id, quotes=stale)
        self.assertEqual(caught.exception.code, "stale_quote")
        self._assert_no_ledger()

    def test_tightening_turnover_after_planning_requires_reapproval(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="turnover-tightened")
        self.portfolio.max_turnover_pct = D("1.00")
        self.portfolio.save(update_fields=["max_turnover_pct"])
        with self.assertRaises(PlanReapprovalRequired):
            apply_plan(plan.id, quotes=timed())
        self._assert_no_ledger()

    def test_a_non_finite_quote_fails_before_any_ledger_row(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="nan-mark")
        broken = {**QUOTES, "MSFT": D("NaN")}
        with self.assertRaises(PlanLegFailed):
            apply_plan(plan.id, quotes=broken)
        self._assert_no_ledger()

    def test_live_application_refuses_to_fall_back_to_planned_prices(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="no-marks")
        with self.assertRaises(Exception) as caught:
            apply_plan(plan.id)
        self.assertIn("current quotes", str(caught.exception))
        self._assert_no_ledger()

    def test_paper_mode_is_explicit_and_labelled(self):
        plan = plan_for(
            self.portfolio,
            self.brief,
            self.weights,
            key="paper-explicit",
            execution_mode="paper",
        )
        application = apply_plan(plan.id)
        self.assertEqual(application.plan.status, "applied")
        self.assertEqual(application.plan.execution_mode, "paper")
        self.assertEqual(
            Transaction.objects.filter(portfolio=self.portfolio).count(), len(application.legs)
        )

    def test_reusing_a_submission_key_with_different_targets_is_rejected(self):
        first = plan_for(self.portfolio, self.brief, self.weights, key="reused-key")
        different = {"MSFT": D("0.10"), "JPM": D("0.30"), "CASH": D("0.60")}
        with self.assertRaises(SubmissionConflictError):
            plan_for(self.portfolio, self.brief, different, key="reused-key")
        self.assertEqual(
            type(first)
            .objects.filter(portfolio=self.portfolio, submission_key="reused-key")
            .count(),
            1,
        )

    def test_repeating_a_successful_plan_adds_no_duplicate_fill(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="repeat-apply")
        first = apply_plan(plan.id, quotes=timed())
        count = Transaction.objects.filter(portfolio=self.portfolio).count()
        self.assertEqual(count, len(first.legs))
        again = apply_plan(plan.id, quotes=timed())
        self.assertTrue(again.already_applied)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), count)

    def test_requested_projected_and_executed_weights_are_persisted_with_reasons(self):
        plan = plan_for(self.portfolio, self.brief, self.weights, key="weights-recorded")
        application = apply_plan(plan.id, quotes=timed())
        plan.refresh_from_db()
        self.assertTrue(plan.requested_target_weights)
        self.assertTrue(plan.target_weights)
        self.assertTrue(plan.executed_target_weights)
        self.assertEqual(len(plan.execution_reasons), len(application.legs))
        self.assertTrue(all(row["reason"] for row in plan.execution_reasons))
        self.assertAlmostEqual(
            sum(Decimal(value) for value in plan.executed_target_weights.values()),
            D("1"),
            places=5,
        )
