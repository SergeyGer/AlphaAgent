"""P0 remediation acceptance: quote provenance, approval terms and execution audit.

The defects this module exercises against are the ones the review named:

* the plan apply path dropped observation times before the execution check;
* the recommendation path could replace the displayed price with a refreshed
  quote before testing drift;
* a fill could commit while its decision run stayed unresolved;
* a delayed plan failure could overwrite a plan another attempt had applied; and
* a suppressed duplicate recommendation could leave a run waiting forever.

The tests use the authenticated plan apply route for the quote policy, real
PostgreSQL connections for the concurrency case, and Decimal/UTC throughout.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

import services.portfolio_allocation as allocation
from core.allocation_models import PlanApproval, PlanExecutionAttempt, PlanStatus
from core.decision_models import (
    DecisionExecutionState,
    DecisionRun,
    ExecutionAttempt,
    RunEventType,
)
from core.models import RecommendationApproval, RecommendationStatus, Transaction
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio, make_user
from core.tests.test_portfolio_allocation import (
    QUOTES,
    build_brief,
    make_allocation_portfolio,
    payload_for,
)
from services.execution import ExecutionRejected, ExecutionRequest, execute_trade
from services.market_data import PriceQuote
from services.portfolio_targets import validate_target_proposal
from services.recommendations import (
    approve_recommendation,
    create_recommendation,
    find_pending_recommendation,
)

D = Decimal


def make_run(portfolio, ticker: str = "AAPL", **kwargs) -> DecisionRun:
    kwargs.setdefault("status", "validating")
    return DecisionRun.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        protocol_version="test.v1",
        mode="live",
        source="crewai",
        **kwargs,
    )


def quotes_for(tickers, *, as_of=None, untimed=False, source="test_market_data", prices=None):
    prices = prices or {ticker: QUOTES[ticker] for ticker in tickers}
    moment = None if untimed else (as_of or timezone.now())
    return {
        ticker: PriceQuote(
            ticker=ticker,
            symbol=ticker,
            price=price,
            currency="USD",
            source=source,
            as_of=moment,
            retrieved_at=timezone.now(),
        )
        for ticker, price in prices.items()
    }


def proposal_for(brief, weights, claim_ids=None):
    return validate_target_proposal(payload_for(brief, weights, claim_ids=claim_ids or {}), brief)


@override_settings(CACHES=TEST_CACHES)
class PlanQuotePolicyAcceptanceTests(APITestCase):
    """1A: only a policy-authorised mark combination may write a fill."""

    def setUp(self):
        self.user = make_user("quote-policy-owner")
        self.portfolio = make_allocation_portfolio(
            cash="10000.00",
            holdings={"AAPL": ("100", "100"), "MSFT": ("20", "300")},
            sector_pct="100.00",
            name_pct="100.00",
        )
        self.portfolio.user = self.user
        self.portfolio.save(update_fields=["user"])
        self.brief = build_brief(self.portfolio)
        self.client = auth_client(self.user)
        self.tickers = ["AAPL", "MSFT"]
        self.live_quotes = quotes_for(self.tickers)
        self.quote_patch = patch(
            "services.allocation_submission.get_latest_quote",
            side_effect=lambda ticker, **kw: self.live_quotes.get(ticker.upper()),
        )
        self.quote_patch.start()
        self.addCleanup(self.quote_patch.stop)

    def make_plan(self, weights=None, *, key="quote-policy", hold=False, execution_mode="live"):
        if weights is None:
            msft = self.brief.state.weight("MSFT")
            weights = {"AAPL": D("0.20"), "MSFT": msft, "CASH": D("1") - D("0.20") - msft}
        proposal = (
            allocation.build_hold_proposal(self.brief, "Hold current weights.")
            if hold
            else proposal_for(self.brief, weights)
        )
        plan = allocation.create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=proposal,
            submission_key=key,
            execution_mode=execution_mode,
        )[0]
        return plan

    def approve(self, plan):
        response = self.client.post(
            reverse("core:portfolio-plan-approve", args=[plan.id]),
            {"plan_version": plan.plan_version, "expires_in_seconds": 3600},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan.refresh_from_db()
        return plan

    def apply(self, plan, *, mode="live"):
        return self.client.post(
            reverse("core:portfolio-plan-apply", args=[plan.id]),
            {"mode": mode},
            format="json",
        )

    def fills(self):
        return Transaction.objects.filter(portfolio=self.portfolio)

    def test_fresh_marks_authorise_the_plan_and_write_the_leg_fill(self):
        plan = self.approve(self.make_plan(key="fresh"))
        response = self.apply(plan)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["application"]["status"], PlanStatus.APPLIED)
        self.assertEqual(self.fills().count(), 1)
        self.assertEqual(self.fills().get().tx_type, "SELL")
        self.assertIsNotNone(response.data["review"]["approval"]["terms_hash"])
        self.assertTrue(response.data["review"]["approval"]["approved_legs"])

    def test_stale_traded_quote_is_refused_with_a_stable_reason(self):
        self.live_quotes = quotes_for(self.tickers, as_of=timezone.now() - timedelta(hours=2))
        plan = self.approve(self.make_plan(key="stale-traded"))
        response = self.apply(plan)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY, response.data)
        self.assertEqual(response.data["code"], "stale_quote")
        self.assertEqual(self.fills().count(), 0)

    def test_future_dated_quote_is_refused_before_a_fill(self):
        self.live_quotes = quotes_for(self.tickers, as_of=timezone.now() + timedelta(hours=1))
        plan = self.approve(self.make_plan(key="future"))
        response = self.apply(plan)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY, response.data)
        self.assertEqual(response.data["code"], "future_quote")
        self.assertEqual(self.fills().count(), 0)

    def test_untimed_quote_is_missing_data_not_a_current_mark(self):
        self.live_quotes = quotes_for(self.tickers, untimed=True)
        plan = self.approve(self.make_plan(key="untimed"))
        response = self.apply(plan)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY, response.data)
        self.assertEqual(response.data["code"], "untimed_quote")
        self.assertEqual(self.fills().count(), 0)

    def test_fallback_quote_is_refused_outside_paper_or_estimation_mode(self):
        self.live_quotes = quotes_for(self.tickers, untimed=True, source="fallback")
        plan = self.approve(self.make_plan(key="fallback"))
        response = self.apply(plan)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY, response.data)
        self.assertEqual(response.data["code"], "fallback_quote_disallowed")
        self.assertEqual(self.fills().count(), 0)

    def test_stale_material_holding_is_refused_when_valuation_is_required(self):
        plan = self.approve(self.make_plan(hold=True, key="hold-stale"))
        self.live_quotes = {
            **quotes_for(["AAPL"]),
            "MSFT": quotes_for(["MSFT"], as_of=timezone.now() - timedelta(hours=2))["MSFT"],
        }
        response = self.apply(plan)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY, response.data)
        self.assertEqual(response.data["code"], "stale_quote")
        self.assertEqual(self.fills().count(), 0)

    def test_risk_reducing_sale_records_the_exception_and_fills(self):
        plan = self.approve(self.make_plan(key="risk-reducing"))
        self.live_quotes = {
            **quotes_for(["AAPL"]),
            "MSFT": quotes_for(["MSFT"], as_of=timezone.now() - timedelta(hours=2))["MSFT"],
        }
        response = self.apply(plan)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.fills().count(), 1)
        plan.refresh_from_db()
        self.assertTrue(
            any("risk_reducing_exception" in json.dumps(row) for row in plan.execution_reasons)
        )

    def test_paper_mode_is_explicitly_labelled_and_may_use_planned_marks(self):
        plan = self.approve(self.make_plan(key="paper"))
        response = self.apply(plan, mode="paper")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["application"]["mode"], "paper")
        self.assertEqual(self.fills().count(), 1)

    def test_expired_approval_is_refused_with_its_stable_reason(self):
        from core.allocation_models import PortfolioPlan

        plan = self.approve(self.make_plan(key="expired"))
        PortfolioPlan.objects.filter(pk=plan.id).update(
            approval_expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.apply(plan)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["code"], "approval_expired")
        self.assertEqual(self.fills().count(), 0)

    def test_legacy_approval_without_terms_requires_fresh_approval(self):
        plan = self.approve(self.make_plan(key="legacy"))
        PlanApproval.objects.filter(plan=plan, decision="approved").update(
            terms_hash="", approved_legs=[]
        )
        response = self.apply(plan)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["code"], "approval_terms_unknown")
        self.assertEqual(self.fills().count(), 0)

    def test_approval_persists_side_quantity_reference_price_tolerance_and_versions(self):
        plan = self.approve(self.make_plan(key="terms"))
        approval = PlanApproval.objects.get(plan=plan, decision="approved")
        legs = list(plan.legs.order_by("sequence"))
        self.assertTrue(approval.terms_hash)
        self.assertTrue(approval.approved_legs)
        self.assertEqual(len(approval.approved_legs), len(legs))
        first = approval.approved_legs[0]
        self.assertEqual(first["side"], legs[0].side)
        self.assertEqual(Decimal(first["quantity"]), legs[0].quantity)
        self.assertEqual(Decimal(first["reference_price"]), legs[0].price)
        self.assertIsNotNone(approval.price_tolerance_pct)
        self.assertIsNotNone(approval.expires_at)
        self.assertTrue(approval.policy_version)
        self.assertTrue(approval.classification_version)
        self.assertEqual(approval.plan_version, plan.plan_version)


@override_settings(CACHES=TEST_CACHES)
@override_settings(EXECUTION_MAX_PRICE_DRIFT_PCT="10")
class RecommendationApprovalAcceptanceTests(TestCase):
    """1B: the displayed $100 is the reference; $120 requires a new approval."""

    def setUp(self):
        self.portfolio = make_allocation_portfolio(cash="10000.00", holdings={})
        self.run = make_run(self.portfolio, "MSFT")
        self.recommendation = create_recommendation(
            self.portfolio,
            type(
                "P",
                (),
                {
                    "action": "BUY",
                    "amount": D("2"),
                    "sentiment": "BULLISH",
                    "reasoning": "acceptance",
                },
            )(),
            D("100.00"),
            ticker="MSFT",
            decision_run=self.run,
            quote_observed_at=timezone.now(),
            price_source="test_market_data",
        )
        self.run.status = "awaiting_approval"
        self.run.execution_state = DecisionExecutionState.AWAITING_APPROVAL
        self.run.save(update_fields=["status", "execution_state"])

    def quote(self, price, *, as_of=None):
        return PriceQuote(
            ticker="MSFT",
            symbol="MSFT",
            price=D(price),
            currency="USD",
            source="test_market_data",
            as_of=as_of or timezone.now(),
            retrieved_at=timezone.now(),
        )

    def test_price_drift_returns_revised_terms_and_writes_no_fill(self):
        with patch("services.recommendations.get_latest_quote", return_value=self.quote("120")):
            outcome = approve_recommendation(
                self.recommendation.id, terms_version=self.recommendation.terms_version
            )
        self.assertFalse(outcome.executed)
        self.assertTrue(outcome.requires_new_approval)
        self.assertEqual(outcome.reason_code, "price_drift_requires_approval")
        self.assertEqual(Decimal(outcome.revised_terms["reference_price"]), D("120.00"))
        self.assertEqual(Transaction.objects.count(), 0)
        self.recommendation.refresh_from_db()
        self.assertEqual(self.recommendation.status, RecommendationStatus.PENDING)
        self.assertEqual(self.recommendation.price, D("120.00"))
        self.assertEqual(self.recommendation.terms_version, 2)
        approval = RecommendationApproval.objects.get(recommendation=self.recommendation)
        self.assertFalse(approval.accepted)
        self.assertEqual(approval.reason_code, "price_drift_requires_approval")
        self.run.refresh_from_db()
        self.assertEqual(self.run.execution_state, DecisionExecutionState.AWAITING_APPROVAL)

    def test_a_second_approval_at_the_refreshed_terms_fills_once(self):
        with patch("services.recommendations.get_latest_quote", return_value=self.quote("120")):
            approve_recommendation(
                self.recommendation.id, terms_version=self.recommendation.terms_version
            )
        self.recommendation.refresh_from_db()
        with patch("services.recommendations.get_latest_quote", return_value=self.quote("120")):
            outcome = approve_recommendation(
                self.recommendation.id, terms_version=self.recommendation.terms_version
            )
        self.assertTrue(outcome.executed)
        self.assertEqual(Transaction.objects.count(), 1)
        self.recommendation.refresh_from_db()
        self.assertEqual(self.recommendation.status, RecommendationStatus.EXECUTED)
        attempt = ExecutionAttempt.objects.get(
            execution_key=f"recommendation:{self.recommendation.id}"
        )
        self.assertIsNotNone(attempt.transaction_id)
        self.assertIsNotNone(attempt.terminal_event_id)
        self.run.refresh_from_db()
        self.assertEqual(self.run.execution_state, DecisionExecutionState.EXECUTED)
        self.assertEqual(
            self.run.events.filter(event_type=RunEventType.DECISION_COMPLETED).count(), 1
        )


class AutonomyAndLegacyAcceptanceTests(TestCase):
    def test_autonomy_disabled_blocks_an_autonomous_fill(self):
        portfolio = make_portfolio(balance="10000.00", autonomous=False)
        quote = PriceQuote(
            ticker="AAPL",
            symbol="AAPL",
            price=D("100"),
            source="test_market_data",
            as_of=timezone.now(),
            retrieved_at=timezone.now(),
        )
        request = ExecutionRequest(
            portfolio_id=portfolio.id,
            ticker="AAPL",
            side="BUY",
            quantity=D("1"),
            quotes={"AAPL": quote},
            execution_key="autonomous-disabled",
            source="autonomous",
            approved_price=D("100"),
        )
        with self.assertRaises(ExecutionRejected) as caught:
            execute_trade(request)
        self.assertEqual(caught.exception.code, "autonomy_disabled")
        self.assertEqual(Transaction.objects.count(), 0)

    def test_a_legacy_fill_without_an_attempt_record_requires_fresh_approval(self):
        portfolio = make_portfolio(balance="10000.00", autonomous=True)
        existing = Transaction.objects.create(
            portfolio=portfolio,
            ticker="AAPL",
            tx_type="BUY",
            amount=D("1"),
            price=D("100"),
            executed_by="AI",
            execution_key="legacy-fill",
            execution_request_hash="",
        )
        quote = PriceQuote(
            ticker="AAPL",
            symbol="AAPL",
            price=D("100"),
            source="test_market_data",
            as_of=timezone.now(),
            retrieved_at=timezone.now(),
        )
        request = ExecutionRequest(
            portfolio_id=portfolio.id,
            ticker="AAPL",
            side="BUY",
            quantity=D("1"),
            quotes={"AAPL": quote},
            execution_key="legacy-fill",
            source="direct",
            approved_price=D("100"),
        )
        with self.assertRaises(ExecutionRejected) as caught:
            execute_trade(request)
        self.assertEqual(caught.exception.code, "legacy_unknown_provenance")
        self.assertEqual(Transaction.objects.filter(pk=existing.pk).count(), 1)


@override_settings(CACHES=TEST_CACHES)
class FillAuditAtomicityAcceptanceTests(TestCase):
    """2A: a failure between the fill and its audit rolls back; retry writes once."""

    def setUp(self):
        self.portfolio = make_allocation_portfolio(cash="10000.00", holdings={})
        self.run = make_run(self.portfolio, "MSFT")
        self.recommendation = create_recommendation(
            self.portfolio,
            type(
                "P",
                (),
                {
                    "action": "BUY",
                    "amount": D("2"),
                    "sentiment": "BULLISH",
                    "reasoning": "acceptance",
                },
            )(),
            D("100.00"),
            ticker="MSFT",
            decision_run=self.run,
            quote_observed_at=timezone.now(),
            price_source="test_market_data",
        )
        self.run.status = "awaiting_approval"
        self.run.execution_state = DecisionExecutionState.AWAITING_APPROVAL
        self.run.save(update_fields=["status", "execution_state"])
        self.quote = PriceQuote(
            ticker="MSFT",
            symbol="MSFT",
            price=D("100"),
            source="test_market_data",
            as_of=timezone.now(),
            retrieved_at=timezone.now(),
        )

    def test_injected_audit_failure_rolls_back_then_retry_writes_one_fill(self):
        with (
            patch("services.recommendations.get_latest_quote", return_value=self.quote),
            patch(
                "services.execution_audit.ensure_fill_attempt",
                side_effect=RuntimeError("audit write failed"),
            ),
            self.assertRaises(RuntimeError),
        ):
            approve_recommendation(
                self.recommendation.id, terms_version=self.recommendation.terms_version
            )
        self.assertEqual(Transaction.objects.count(), 0)
        self.run.refresh_from_db()
        self.assertEqual(self.run.execution_state, DecisionExecutionState.AWAITING_APPROVAL)

        with patch("services.recommendations.get_latest_quote", return_value=self.quote):
            outcome = approve_recommendation(
                self.recommendation.id, terms_version=self.recommendation.terms_version
            )
        self.assertTrue(outcome.executed)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(
            self.run.events.filter(event_type=RunEventType.DECISION_COMPLETED).count(), 1
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.execution_state, DecisionExecutionState.EXECUTED)
        self.assertEqual(
            ExecutionAttempt.objects.filter(
                decision_run=self.run, state=DecisionExecutionState.EXECUTED
            ).count(),
            1,
        )

    def test_notification_dispatch_failure_does_not_lose_the_fill_or_audit(self):
        with (
            patch("services.recommendations.get_latest_quote", return_value=self.quote),
            patch("config.celery.app.send_task", side_effect=RuntimeError("broker down")),
            self.captureOnCommitCallbacks(execute=True),
        ):
            outcome = approve_recommendation(
                self.recommendation.id, terms_version=self.recommendation.terms_version
            )
        self.assertTrue(outcome.executed)
        self.assertEqual(Transaction.objects.count(), 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.execution_state, DecisionExecutionState.EXECUTED)
        self.assertTrue(
            ExecutionAttempt.objects.filter(
                decision_run=self.run, transaction__isnull=False
            ).exists()
        )


@override_settings(CACHES=TEST_CACHES)
class DuplicateRecommendationAcceptanceTests(TestCase):
    def test_a_suppressed_duplicate_run_is_terminal_and_links_the_existing_recommendation(self):
        portfolio = make_allocation_portfolio(cash="10000.00", holdings={})
        first_run = make_run(portfolio, "MSFT")
        existing = create_recommendation(
            portfolio,
            type(
                "P",
                (),
                {"action": "BUY", "amount": D("2"), "sentiment": "BULLISH", "reasoning": "x"},
            )(),
            D("100.00"),
            ticker="MSFT",
            decision_run=first_run,
            quote_observed_at=timezone.now(),
        )
        second_run = make_run(portfolio, "MSFT")
        self.assertIsNone(
            create_recommendation(
                portfolio,
                type(
                    "P",
                    (),
                    {
                        "action": "BUY",
                        "amount": D("2"),
                        "sentiment": "BULLISH",
                        "reasoning": "x",
                    },
                )(),
                D("100.00"),
                ticker="MSFT",
                decision_run=second_run,
            )
        )
        linked = find_pending_recommendation(portfolio, "MSFT")
        self.assertEqual(linked.id, existing.id)
        from services.execution_audit import record_terminal_outcome

        with self.captureOnCommitCallbacks(execute=True):
            record_terminal_outcome(
                run=second_run,
                outcome=DecisionExecutionState.SUPPRESSED,
                duplicate_recommendation=linked,
                reason="duplicate advisory proposal",
            )
        second_run.refresh_from_db()
        existing.refresh_from_db()
        self.assertEqual(second_run.status, "completed")
        self.assertEqual(second_run.execution_state, DecisionExecutionState.SUPPRESSED)
        self.assertEqual(second_run.duplicate_recommendation_id, existing.id)
        self.assertEqual(existing.status, RecommendationStatus.PENDING)
        self.assertTrue(
            ExecutionAttempt.objects.filter(
                decision_run=second_run, state=DecisionExecutionState.SUPPRESSED
            ).exists()
        )


def _concurrent_apply(plan_id, quotes, barrier, results):
    close_old_connections()
    try:
        if barrier is not None:
            barrier.wait(timeout=15)
        application = allocation.apply_plan(plan_id, quotes=quotes)
        results.append(("applied", application))
    except Exception as exc:
        results.append(("error", exc))
    finally:
        close_old_connections()
        connections.close_all()


@override_settings(CACHES=TEST_CACHES)
class PlanFailureConcurrencyAcceptanceTests(TransactionTestCase):
    """2B: a delayed failure handler cannot overwrite an applied plan."""

    def setUp(self):
        self.portfolio = make_allocation_portfolio(cash="10000.00", holdings={})
        self.brief = build_brief(self.portfolio)
        proposal = proposal_for(
            self.brief,
            {"MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.60")},
            claim_ids={"MSFT": ["claim-msft"], "JPM": ["claim-jpm"]},
        )
        self.plan = allocation.create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=proposal,
            submission_key="concurrent-failure",
        )[0]
        self.fresh = quotes_for(["MSFT", "JPM"], prices={"MSFT": D("320"), "JPM": D("160")})
        self.stale = quotes_for(
            ["MSFT", "JPM"],
            prices={"MSFT": D("320"), "JPM": D("160")},
            as_of=timezone.now() - timedelta(hours=2),
        )

    def test_one_connection_fails_while_another_applies_and_delayed_failure_is_ignored(self):
        barrier = None
        first_failed = threading.Event()
        release_failure = threading.Event()
        original = allocation._record_plan_failure

        def delayed_failure(plan_id, failure, **kwargs):
            if threading.current_thread().name == "first-apply":
                first_failed.set()
                release_failure.wait(timeout=15)
            return original(plan_id, failure, **kwargs)

        results: list[tuple[str, object]] = []

        def first():
            _concurrent_apply(self.plan.id, self.stale, barrier, results)

        threads = [
            threading.Thread(target=first, name="first-apply"),
            threading.Thread(
                target=_concurrent_apply,
                args=(self.plan.id, self.fresh, barrier, results),
                name="second-apply",
            ),
        ]
        with patch.object(allocation, "_record_plan_failure", side_effect=delayed_failure):
            threads[0].start()
            self.assertTrue(first_failed.wait(timeout=15), "first attempt did not fail")
            threads[1].start()
            threads[1].join(timeout=20)
            self.assertFalse(threads[1].is_alive(), "second apply did not finish")
            release_failure.set()
            threads[0].join(timeout=20)
            self.assertFalse(threads[0].is_alive(), "first apply did not finish")

        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, PlanStatus.APPLIED)
        self.assertEqual(
            Transaction.objects.filter(portfolio=self.portfolio).count(),
            self.plan.legs.count(),
        )
        attempts = PlanExecutionAttempt.objects.filter(plan=self.plan)
        self.assertTrue(attempts.exists())
        self.assertTrue(attempts.filter(status__in={"superseded", "failed"}).exists())


@override_settings(CACHES=TEST_CACHES)
class RepairCommandAcceptanceTests(TestCase):
    def test_repair_command_dry_run_reports_without_writing(self):
        portfolio = make_allocation_portfolio(cash="10000.00", holdings={})
        brief = build_brief(portfolio)
        proposal = proposal_for(
            brief,
            {"MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.60")},
            claim_ids={"MSFT": ["claim-msft"], "JPM": ["claim-jpm"]},
        )
        plan = allocation.create_plan(
            portfolio, brief=brief, proposal=proposal, submission_key="repair"
        )[0]
        allocation.apply_plan(
            plan.id,
            quotes=quotes_for(["MSFT", "JPM"], prices={"MSFT": D("320"), "JPM": D("160")}),
        )
        fill = Transaction.objects.filter(portfolio=portfolio).first()
        ExecutionAttempt.objects.filter(transaction=fill).delete()
        run = make_run(portfolio, "MSFT")
        run.execution_state = DecisionExecutionState.AWAITING_APPROVAL
        run.status = "awaiting_approval"
        run.save(update_fields=["execution_state", "status"])

        out = StringIO()
        call_command("repair_execution_audit", "--dry-run", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertGreaterEqual(payload["summary"]["fills_missing_attempt"], 1)
        self.assertGreaterEqual(payload["summary"]["approval_without_recommendation"], 1)
        self.assertFalse(ExecutionAttempt.objects.filter(transaction=fill).exists())
        run.refresh_from_db()
        self.assertEqual(run.execution_state, DecisionExecutionState.AWAITING_APPROVAL)

        out = StringIO()
        call_command("repair_execution_audit", "--apply", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["dry_run"])
        self.assertTrue(ExecutionAttempt.objects.filter(transaction=fill).exists())
        run.refresh_from_db()
        self.assertEqual(run.execution_state, DecisionExecutionState.BLOCKED)
