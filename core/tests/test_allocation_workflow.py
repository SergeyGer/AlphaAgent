"""FIX-08 acceptance: idempotent submission, advisory review, approval and execution.

The gate has four parts:

1. ``POST /api/decision-runs/`` is authenticated, idempotent and conflict-aware;
2. a stub end-to-end path validates the mechanics from submission through the
   shared execution service;
3. a provider-backed path is exercised through the real budgeted adapter with a
   scripted ``StageClient`` (no network);
4. approval is bound to an exact immutable plan version and an expiry, so a
   revalidated plan cannot execute under the old approval.

Nothing in this module calls a model or the network. The recorded live example
lives in ``docs/spec/fixtures/fix08-live-allocation.v1.json`` and is labelled
there as a real provider run; it is not used to stand in for these tests.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.allocation_models import (
    AllocationSubmission,
    AllocationSubmissionStatus,
    PlanApproval,
    PlanApprovalDecision,
    PlanStatus,
    PortfolioPlan,
)
from core.event_models import EventOutbox
from core.models import Asset, InstrumentClassification, Portfolio, Transaction
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio, make_user
from services.allocation_provider import (
    AllocationProviderUnavailable,
    BudgetedAllocationClient,
    build_allocation_client,
)
from services.allocation_submission import (
    SubmissionRequest,
    collect_quotes,
    submit_analysis,
)
from services.debate_protocol import (
    BudgetExhausted,
    DeadlineExceeded,
    ModelUsage,
    ProtocolBudget,
    ProviderTransientError,
)
from services.market_data import PriceQuote
from services.portfolio_allocation import (
    PROTOCOL_VERSION,
    PortfolioCIO,
    revalidate_plan,
)
from services.portfolio_approval import (
    PlanApprovalError,
    approve_plan,
    reject_plan,
    require_valid_approval,
)
from services.portfolio_review import build_plan_review

D = Decimal
QUOTES = {"AAPL": D("100"), "MSFT": D("320"), "JPM": D("160")}


def make_allocation_portfolio(
    *, cash: str = "10000.00", holdings: dict[str, tuple[str, str]] | None = None
) -> Portfolio:
    portfolio = Portfolio.objects.create(
        user=make_user(),
        balance_usd=D(cash),
        risk_profile="medium",
        max_trade_allocation_pct=D("99.99"),
        max_portfolio_exposure_pct=D("90.00"),
        max_ticker_concentration_pct=D("30.00"),
        max_sector_concentration_pct=D("40.00"),
        max_turnover_pct=D("100.00"),
        min_cash_buffer_pct=D("5.00"),
        daily_loss_limit_usd=D("500.00"),
    )
    for ticker, (amount, avg_cost) in (holdings or {}).items():
        Asset.objects.create(
            portfolio=portfolio,
            ticker=ticker,
            amount=D(amount),
            avg_purchase_price=D(avg_cost),
        )
    return portfolio


def classify(portfolio: Portfolio, mapping: dict[str, str]) -> None:
    for ticker, sector in mapping.items():
        InstrumentClassification.objects.update_or_create(
            portfolio=portfolio,
            ticker=ticker,
            defaults={
                "sector": sector,
                "asset_class": "equity",
                "source": "fixture",
                "source_detail": "test_allocation_workflow",
            },
        )


def fake_quote(ticker: str, *, fallback_price=None, use_cache=True):
    _ = fallback_price, use_cache
    price = QUOTES.get(ticker.upper())
    if price is None:
        return None
    return PriceQuote(
        ticker=ticker.upper(),
        symbol=ticker.upper(),
        price=price,
        source="test_market_data",
        as_of=timezone.now(),
    )


def stub_payload() -> dict:
    """A valid stub proposal: reduce AAPL to 20% and hold 80% cash.

    It uses only a reduction, so it needs no claim id: there is no completed
    candidate DecisionRun in this fixture and the contract forbids inventing one.
    """
    return {
        "schema_version": "portfolio_targets.v1",
        "portfolio_version": "$BRIEF_PORTFOLIO_VERSION",
        "objective": "Reduce the single-name weight and keep a large cash buffer.",
        "target_weights": {"AAPL": "0.20", "CASH": "0.80"},
        "actions": [
            {
                "ticker": "AAPL",
                "action": "reduce",
                "rationale": "Reduce concentration.",
                "claim_ids": [],
            },
            {
                "ticker": "CASH",
                "action": "maintain_cash",
                "rationale": "Cash is a deliberate allocation.",
                "claim_ids": [],
            },
        ],
        "funding_source": "none",
        "rationale": "A smaller single-name position with cash retained.",
        "rejected_alternatives": [
            {"ticker": None, "reason": "Leverage is outside the long-only release."}
        ],
        "unresolved_uncertainties": ["No expected return was supplied."],
        "data_sufficiency": "sufficient",
    }


def request_body(portfolio: Portfolio, key: str, **overrides) -> dict:
    body = {
        "portfolio_id": portfolio.id,
        "tickers": ["MSFT"],
        "mode": "stub",
        "protocol_version": PROTOCOL_VERSION,
        "idempotency_key": key,
        "stub_response": stub_payload(),
    }
    body.update(overrides)
    return body


@override_settings(CACHES=TEST_CACHES)
class AllocationWorkflowFixture(APITestCase):
    def setUp(self):
        self.user = make_user("fix08-owner")
        self.portfolio = make_portfolio(
            self.user,
            balance="10000.00",
            allocation_pct="99.99",
        )
        self.portfolio.max_portfolio_exposure_pct = D("90.00")
        self.portfolio.max_ticker_concentration_pct = D("30.00")
        self.portfolio.max_sector_concentration_pct = D("40.00")
        self.portfolio.max_turnover_pct = D("100.00")
        self.portfolio.min_cash_buffer_pct = D("5.00")
        self.portfolio.save()
        Asset.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            amount=D("100"),
            avg_purchase_price=D("90"),
        )
        classify(
            self.portfolio,
            {
                "AAPL": "technology",
                "MSFT": "technology",
                "JPM": "financials",
            },
        )
        self.client = auth_client(self.user)
        self.quote_patch = patch(
            "services.allocation_submission.get_latest_quote", side_effect=fake_quote
        )
        self.quote_patch.start()
        self.addCleanup(self.quote_patch.stop)

    def submit(self, key: str = "fix08-key-1", **overrides):
        return self.client.post(
            reverse("core:decision-run-list"),
            request_body(self.portfolio, key, **overrides),
            format="json",
        )

    def submit_and_get_plan(self, key: str = "fix08-key-1", **overrides) -> PortfolioPlan:
        response = self.submit(key, **overrides)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(response.data["status"], AllocationSubmissionStatus.COMPLETED)
        return PortfolioPlan.objects.get(pk=response.data["plan_id"])


class SubmissionApiTests(AllocationWorkflowFixture):
    def test_submission_requires_authentication(self):
        response = APIClient().post(
            reverse("core:decision-run-list"), request_body(self.portfolio, "anon"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_another_users_portfolio_is_a_404_not_a_403(self):
        other = make_allocation_portfolio(holdings={"AAPL": ("10", "90")})
        response = self.client.post(
            reverse("core:decision-run-list"),
            {
                "portfolio_id": other.id,
                "tickers": ["AAPL"],
                "mode": "stub",
                "protocol_version": PROTOCOL_VERSION,
                "idempotency_key": "other-key",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_submission_returns_202_a_run_id_and_a_status_url_without_a_trade(self):
        response = self.submit()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn("run_id", response.data)
        self.assertIn("/status/", response.data["status_url"])
        self.assertEqual(response.data["status"], AllocationSubmissionStatus.COMPLETED)
        self.assertIsNotNone(response.data["plan_id"])
        self.assertTrue(response.data["status_path"].startswith("/api/decision-runs/"))
        self.assertEqual(response.data["synthetic_response"], True)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)
        submission = AllocationSubmission.objects.get(pk=response.data["run_id"])
        self.assertEqual(submission.request_hash, response.data["request_hash"])
        self.assertFalse(submission.data_quality["input"]["prices"]["missing_tickers"])

    def test_status_url_reports_completed_and_links_the_plan(self):
        response = self.submit()
        status_response = self.client.get(response.data["status_path"])
        self.assertEqual(status_response.status_code, status.HTTP_200_OK)
        self.assertEqual(status_response.data["run_id"], response.data["run_id"])
        self.assertEqual(status_response.data["status"], AllocationSubmissionStatus.COMPLETED)
        self.assertTrue(status_response.data["terminal"])
        self.assertEqual(status_response.data["plan_id"], response.data["plan_id"])
        self.assertIn("/api/portfolio-plans/", status_response.data["plan_url"])

    def test_repeating_the_same_key_and_body_returns_the_same_run_without_a_second_plan(self):
        first = self.submit("same-key")
        second = self.submit("same-key")
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second.data["run_id"], first.data["run_id"])
        self.assertEqual(second.data["plan_id"], first.data["plan_id"])
        self.assertTrue(second.data["idempotent_replay"])
        self.assertEqual(AllocationSubmission.objects.count(), 1)
        self.assertEqual(PortfolioPlan.objects.count(), 1)

    def test_reusing_a_key_with_a_different_payload_is_a_409(self):
        first = self.submit("conflict-key")
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)
        changed = stub_payload()
        changed["target_weights"] = {"AAPL": "0.10", "CASH": "0.90"}
        changed["actions"][0]["action"] = "reduce"
        second = self.submit("conflict-key", stub_response=changed)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(AllocationSubmission.objects.count(), 1)

    def test_payload_validation_is_typed(self):
        cases = [
            ({"protocol_version": "debate.v1"}, "protocol"),
            ({"mode": "paper"}, "mode"),
            ({"idempotency_key": ""}, "idempotency"),
            ({"tickers": []}, "ticker"),
            ({"tickers": ["not a ticker!"]}, "ticker"),
            ({"horizon_days": 999}, "horizon"),
        ]
        for index, (overrides, label) in enumerate(cases):
            response = self.submit(f"validation-{index}", **overrides)
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, f"{label}: {response.data}"
            )

    def test_a_requested_ticker_without_a_decision_run_is_declared_not_dropped(self):
        plan = self.submit_and_get_plan()
        candidate_info = plan.submissions.get().data_quality["input"]["candidates"]
        self.assertEqual(candidate_info["declared_without_case"], ["MSFT"])
        self.assertIn("MSFT", candidate_info["requested"])
        declared = [
            row
            for row in plan.brief["candidate_cases"]
            if row["ticker"] == "MSFT" and row["mode"] == "no_recorded_case"
        ]
        self.assertEqual(len(declared), 1)
        self.assertEqual(declared[0]["claims"], [])
        self.assertIs(declared[0]["evidence_sufficient"], False)
        self.assertTrue(plan.brief["data_quality"]["insufficient_evidence"])

    def test_missing_classification_is_declared_and_never_inferred(self):
        InstrumentClassification.objects.filter(portfolio=self.portfolio, ticker="MSFT").delete()
        plan = self.submit_and_get_plan()
        review = build_plan_review(plan)
        unclassified = review["data_quality"]["input"]["classifications"]["unclassified"]
        self.assertIn("MSFT", unclassified)
        self.assertEqual(review["data_quality"]["input"]["classifications"]["status"], "incomplete")
        sectors = {row["sector"] for row in review["sector_effects"]["rows"]}
        self.assertNotIn("MSFT", sectors)
        self.assertTrue(
            any(
                "Sector classification is missing" in text
                for text in review["data_quality"]["limitations"]
            )
        )

    def test_missing_candidate_price_is_declared_not_fabricated(self):
        original = fake_quote

        def missing_msft(ticker, *, fallback_price=None, use_cache=True):
            if ticker.upper() == "MSFT":
                return None
            return original(ticker, fallback_price=fallback_price, use_cache=use_cache)

        with patch("services.allocation_submission.get_latest_quote", side_effect=missing_msft):
            response = self.submit("missing-price-key")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        submission = AllocationSubmission.objects.get(pk=response.data["run_id"])
        self.assertIn("MSFT", submission.data_quality["input"]["prices"]["missing_tickers"])
        plan = PortfolioPlan.objects.get(pk=response.data["plan_id"])
        self.assertTrue(
            any(
                "No price was available for: MSFT" in text
                for text in build_plan_review(plan)["data_quality"]["limitations"]
            )
        )

    def test_a_live_request_may_not_supply_a_stub_response(self):
        response = self.submit("live-stub", mode="live")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(AllocationSubmission.objects.count(), 0)

    def test_explicit_stub_mode_without_a_scripted_response_is_a_deterministic_hold(self):
        response = self.client.post(
            reverse("core:decision-run-list"),
            {
                "portfolio_id": self.portfolio.id,
                "tickers": ["AAPL"],
                "mode": "stub",
                "protocol_version": PROTOCOL_VERSION,
                "idempotency_key": "hold-key",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(response.data["status"], AllocationSubmissionStatus.COMPLETED)
        plan = PortfolioPlan.objects.get(pk=response.data["plan_id"])
        self.assertEqual(plan.status, PlanStatus.PROPOSED)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)
        review = build_plan_review(plan)
        self.assertTrue(review["provenance"]["synthetic_response"])
        self.assertEqual(review["provenance"]["provider"], "stub")
        self.assertIn("Stub decision", review["provenance"]["label"])

    def test_a_failed_analysis_is_recorded_and_replayed_not_retried(self):
        with patch(
            "services.allocation_submission.build_allocation_client",
            side_effect=AllocationProviderUnavailable("provider intentionally unavailable"),
        ):
            first = self.submit("failure-key", mode="live", stub_response=None)
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED, first.data)
        self.assertEqual(first.data["status"], AllocationSubmissionStatus.FAILED)
        self.assertEqual(first.data["error"]["code"], "AllocationProviderUnavailable")
        with patch(
            "services.allocation_submission.build_allocation_client",
            side_effect=AssertionError("must not call the provider twice"),
        ):
            second = self.submit("failure-key", mode="live", stub_response=None)
        self.assertEqual(second.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second.data["run_id"], first.data["run_id"])
        self.assertEqual(second.data["status"], AllocationSubmissionStatus.FAILED)
        self.assertEqual(AllocationSubmission.objects.count(), 1)


class ReviewAndApprovalTests(AllocationWorkflowFixture):
    def test_review_shows_holdings_weights_requested_vs_constrained_cash_sector_costs(self):
        plan = self.submit_and_get_plan()
        review = build_plan_review(plan)
        self.assertEqual(review["plan_version"], 1)
        self.assertTrue(review["reviewable"])
        self.assertEqual(review["holdings"][0]["ticker"], "AAPL")
        self.assertEqual(D(review["requested_target_weights"]["AAPL"]), D("0.20"))
        self.assertEqual(D(review["constrained_target_weights"]["AAPL"]), D("0.20"))
        apple = next(row for row in review["weights"] if row["ticker"] == "AAPL")
        self.assertEqual(apple["current_weight"], "0.500000")
        self.assertEqual(apple["requested_weight"], "0.200000")
        self.assertEqual(apple["constrained_weight"], "0.200000")
        self.assertEqual(apple["change_from_current"], "-0.300000")
        self.assertEqual(D(review["cash_effect"]["constrained_cash_weight"]), D("0.80"))
        self.assertEqual(review["cash_effect"]["buffer_status"], "within_policy")
        technology = next(
            row for row in review["sector_effects"]["rows"] if row["sector"] == "technology"
        )
        self.assertEqual(technology["current_weight"], "0.500000")
        self.assertEqual(technology["target_weight"], "0.200000")
        self.assertEqual(review["sector_effects"]["target_coverage_status"], "complete")
        self.assertIn("starting_cash_usd", review["funding"])
        self.assertIn("recorded_fee_note", review["costs"])
        self.assertTrue(review["rejected_alternatives"])
        self.assertTrue(review["data_quality"]["limitations"])
        self.assertIn("risk_statistics", review["data_quality"])
        self.assertEqual(review["approval"]["state"], "pending")
        self.assertIsInstance(review["approval"]["reason"], str)
        self.assertEqual(review["provenance"]["provider"], "stub")
        self.assertTrue(review["provenance"]["synthetic_response"])

    def test_plan_list_and_detail_are_owner_scoped(self):
        plan = self.submit_and_get_plan()
        listing = self.client.get(reverse("core:portfolio-plan-list"))
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(listing.data["results"][0]["plan_id"], str(plan.id))
        detail = self.client.get(reverse("core:portfolio-plan-detail", args=[plan.id]))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["plan_id"], str(plan.id))

        other_user = make_user("fix08-other")
        other_client = auth_client(other_user)
        self.assertEqual(
            other_client.get(reverse("core:portfolio-plan-detail", args=[plan.id])).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(other_client.get(reverse("core:portfolio-plan-list")).data["count"], 0)

    def test_approval_binds_the_exact_version_hash_and_expiry(self):
        plan = self.submit_and_get_plan()
        body = {
            "plan_version": plan.plan_version + 1,
            "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        }
        stale = self.client.post(
            reverse("core:portfolio-plan-approve", args=[plan.id]), body, format="json"
        )
        self.assertEqual(stale.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(stale.data["code"], "plan_version_changed")

        body["plan_version"] = plan.plan_version
        body["approval_hash"] = plan.approval_hash
        response = self.client.post(
            reverse("core:portfolio-plan-approve", args=[plan.id]), body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["approval"]["plan_version"], plan.plan_version)
        self.assertEqual(response.data["approval"]["approval_hash"], plan.approval_hash)
        self.assertEqual(response.data["review"]["approval"]["state"], "approved")
        self.assertTrue(response.data["review"]["provenance"]["synthetic_response"])
        plan.refresh_from_db()
        self.assertEqual(plan.approval_version, plan.plan_version)
        self.assertEqual(plan.approved_by, self.user)
        self.assertIsNotNone(plan.approval_expires_at)
        self.assertEqual(
            PlanApproval.objects.filter(plan=plan, decision=PlanApprovalDecision.APPROVED).count(),
            1,
        )
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

    def test_approval_demands_an_expiry_and_a_version(self):
        plan = self.submit_and_get_plan()
        for body, code in [
            (
                {"expires_at": (timezone.now() + timedelta(hours=1)).isoformat()},
                "missing_plan_version",
            ),
            ({"plan_version": 1}, "missing_expiry"),
            (
                {
                    "plan_version": 1,
                    "expires_at": (timezone.now() - timedelta(hours=1)).isoformat(),
                },
                "expiry_not_future",
            ),
            ({"plan_version": 1, "expires_in_seconds": 999_999}, "expiry_too_far"),
        ]:
            response = self.client.post(
                reverse("core:portfolio-plan-approve", args=[plan.id]), body, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
            self.assertEqual(response.data["code"], code)

    def test_apply_without_approval_is_refused_and_writes_no_ledger_row(self):
        plan = self.submit_and_get_plan()
        response = self.client.post(
            reverse("core:portfolio-plan-apply", args=[plan.id]), {"mode": "paper"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "approval_required")
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

    def test_approved_plan_applies_through_the_shared_execution_service(self):
        plan = self.submit_and_get_plan()
        approve = self.client.post(
            reverse("core:portfolio-plan-approve", args=[plan.id]),
            {
                "plan_version": plan.plan_version,
                "expires_in_seconds": 3600,
                "note": "Reviewed in the acceptance test.",
            },
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK, approve.data)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

        applied = self.client.post(
            reverse("core:portfolio-plan-apply", args=[plan.id]), {"mode": "paper"}, format="json"
        )
        self.assertEqual(applied.status_code, status.HTTP_200_OK, applied.data)
        self.assertEqual(applied.data["application"]["status"], PlanStatus.APPLIED)
        self.assertFalse(applied.data["application"]["already_applied"])
        self.assertEqual(len(applied.data["application"]["transaction_ids"]), 1)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 1)
        transaction = Transaction.objects.get(portfolio=self.portfolio)
        self.assertEqual(transaction.tx_type, "SELL")
        self.assertEqual(transaction.ticker, "AAPL")
        self.assertEqual(transaction.amount, D("60"))
        self.assertEqual(transaction.price, D("100"))
        review = applied.data["review"]
        self.assertEqual(review["execution"]["transaction_ids"], [transaction.id])
        self.assertEqual(review["approval"]["state"], "consumed")
        self.assertEqual(review["execution"]["applied_approval_version"], plan.plan_version)
        self.assertEqual(review["execution"]["applied_by"], self.user.username)

        repeated = self.client.post(
            reverse("core:portfolio-plan-apply", args=[plan.id]), {"mode": "paper"}, format="json"
        )
        self.assertEqual(repeated.status_code, status.HTTP_200_OK)
        self.assertTrue(repeated.data["application"]["already_applied"])
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 1)

    def test_an_expired_approval_cannot_execute(self):
        plan = self.submit_and_get_plan()
        approve = self.client.post(
            reverse("core:portfolio-plan-approve", args=[plan.id]),
            {
                "plan_version": plan.plan_version,
                "expires_in_seconds": 3600,
            },
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK, approve.data)
        PortfolioPlan.objects.filter(pk=plan.id).update(
            approval_expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.client.post(
            reverse("core:portfolio-plan-apply", args=[plan.id]), {"mode": "paper"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "approval_expired")
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

    def test_a_material_change_bumps_the_version_and_requires_new_approval(self):
        plan = self.submit_and_get_plan()
        approve = self.client.post(
            reverse("core:portfolio-plan-approve", args=[plan.id]),
            {"plan_version": plan.plan_version, "expires_in_seconds": 3600},
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK, approve.data)
        original_approval = PlanApproval.objects.get(plan=plan)
        revalidated = revalidate_plan(
            plan.id,
            quotes={
                ticker: PriceQuote(
                    ticker=ticker,
                    symbol=ticker,
                    price=price,
                    source="test_market_data",
                    as_of=timezone.now(),
                )
                for ticker, price in QUOTES.items()
            },
        )
        revalidated.refresh_from_db()
        self.assertEqual(revalidated.plan_version, plan.plan_version + 1)
        self.assertEqual(revalidated.approval_version, 0)
        self.assertIsNone(revalidated.approval_expires_at)
        self.assertEqual(revalidated.status, PlanStatus.PROPOSED)
        self.assertTrue(
            any(row["code"] == "approval_invalidated" for row in revalidated.deviations)
        )
        # The audit row is not deleted; it simply no longer matches the plan.
        original_approval.refresh_from_db()
        self.assertEqual(original_approval.plan_version, plan.plan_version)

        review = build_plan_review(revalidated)
        self.assertEqual(review["approval"]["state"], "invalidated")
        self.assertFalse(review["review_actions"]["can_apply"])
        refused = self.client.post(
            reverse("core:portfolio-plan-apply", args=[plan.id]), {"mode": "paper"}, format="json"
        )
        self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(refused.data["code"], "approval_invalidated")
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

    def test_rejection_and_cancellation_write_audit_rows_and_events(self):
        rejected_plan = self.submit_and_get_plan("reject-key")
        rejected = self.client.post(
            reverse("core:portfolio-plan-reject", args=[rejected_plan.id]),
            {"reason": "The sector exposure is not acceptable.", "plan_version": 1},
            format="json",
        )
        self.assertEqual(rejected.status_code, status.HTTP_200_OK, rejected.data)
        self.assertEqual(rejected.data["approval"]["decision"], PlanApprovalDecision.REJECTED)
        rejected_plan.refresh_from_db()
        self.assertEqual(rejected_plan.status, PlanStatus.REJECTED)
        self.assertEqual(
            PlanApproval.objects.filter(
                plan=rejected_plan, decision=PlanApprovalDecision.REJECTED
            ).count(),
            1,
        )
        self.assertTrue(EventOutbox.objects.filter(event_type="portfolio.plan.rejected").exists())

        cancelled_plan = self.submit_and_get_plan("cancel-key")
        cancelled = self.client.post(
            reverse("core:portfolio-plan-cancel", args=[cancelled_plan.id]),
            {"reason": "No longer needed."},
            format="json",
        )
        self.assertEqual(cancelled.status_code, status.HTTP_200_OK, cancelled.data)
        self.assertEqual(cancelled.data["approval"]["decision"], PlanApprovalDecision.CANCELLED)
        cancelled_plan.refresh_from_db()
        self.assertEqual(cancelled_plan.status, PlanStatus.CANCELLED)
        self.assertTrue(EventOutbox.objects.filter(event_type="portfolio.plan.cancelled").exists())
        # A repeated cancellation is idempotent and creates no second audit row.
        again = self.client.post(
            reverse("core:portfolio-plan-cancel", args=[cancelled_plan.id]), {}, format="json"
        )
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertTrue(again.data["already_cancelled"])
        self.assertEqual(
            PlanApproval.objects.filter(
                plan=cancelled_plan, decision=PlanApprovalDecision.CANCELLED
            ).count(),
            1,
        )

    def test_rejection_requires_a_reason(self):
        plan = self.submit_and_get_plan()
        response = self.client.post(
            reverse("core:portfolio-plan-reject", args=[plan.id]), {"reason": ""}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "reason_required")


class ProviderAdapterTests(TestCase):
    class FakeStageClient:
        provider = "anthropic"
        model = "test-model"
        parameters = {"max_output_tokens": 1024, "temperature": "not_sent"}

        def __init__(self, payload_factory):
            self.payload_factory = payload_factory
            self.calls = []
            self.last_usage = ModelUsage(
                input_tokens=123,
                output_tokens=45,
                cost_usd=None,
                reported=True,
                tokens_measured=True,
                price_table_cost_usd=D("0.000035"),
                cost_unknown_reason="provider_reported_cost_unavailable",
            )

        def complete(self, stage, prompt):
            self.calls.append((stage, prompt))
            return json.dumps(self.payload_factory(prompt))

    def _brief(self):
        from core.tests.test_portfolio_allocation import build_brief

        portfolio = make_allocation_portfolio(holdings={"AAPL": ("100", "90")})
        return portfolio, build_brief(portfolio, quotes=QUOTES)

    def test_a_budgeted_provider_client_records_usage_and_budget_without_network(self):
        _, brief = self._brief()

        def payload_for_prompt(prompt):
            payload = stub_payload()
            marker = "BRIEF (the only permitted source of facts):\n"
            parsed = json.loads(prompt.split(marker, 1)[1].strip())
            payload["portfolio_version"] = parsed["portfolio_version"]
            return payload

        stage = self.FakeStageClient(payload_for_prompt)
        client = BudgetedAllocationClient(
            stage,
            budget=ProtocolBudget(
                stage_timeout_seconds=10,
                run_deadline_seconds=30,
                max_input_tokens=100_000,
                max_output_tokens=10_000,
                provider_retries=0,
                monetary_budget_usd=D("1.00"),
            ),
        )
        proposal = PortfolioCIO(client=client, max_repairs=0).decide(brief)
        metadata = proposal.model_metadata
        self.assertEqual(metadata["provider"], "anthropic")
        self.assertEqual(metadata["model"], "test-model")
        self.assertEqual(metadata["input_tokens"], 123)
        self.assertEqual(metadata["output_tokens"], 45)
        self.assertFalse(metadata["synthetic_response"])
        self.assertEqual(metadata["token_accounting"], "provider_reported")
        self.assertEqual(metadata["adapter"]["adapter"], "budgeted_provider_client")
        self.assertEqual(metadata["adapter"]["usage"]["calls"], 1)
        self.assertEqual(metadata["adapter"]["usage"]["measured_tokens"]["input"], 123)
        self.assertEqual(metadata["adapter"]["budget"]["monetary_budget_usd"], "1.00")
        self.assertFalse(metadata["adapter"]["usage"]["strict_billed_ceiling"])
        self.assertIsNone(metadata["adapter"]["usage"]["billed_cost_usd"])
        self.assertEqual(len(stage.calls), 1)

    def test_the_adapter_enforces_the_input_token_budget_before_calling(self):
        stage = self.FakeStageClient(lambda prompt: stub_payload())
        client = BudgetedAllocationClient(
            stage,
            budget=ProtocolBudget(
                stage_timeout_seconds=10,
                run_deadline_seconds=30,
                max_input_tokens=1,
                max_output_tokens=10_000,
                provider_retries=0,
                monetary_budget_usd=D("1.00"),
            ),
        )
        with self.assertRaises(BudgetExhausted):
            client.complete("portfolio_allocation", "a prompt that is too long")
        self.assertEqual(stage.calls, [])

    def test_the_adapter_stops_a_retry_that_could_not_fit_the_deadline(self):
        class TransientClient:
            provider = "anthropic"
            model = "test-model"
            parameters = {}
            last_usage = None

            def complete(self, stage, prompt):
                raise ProviderTransientError("temporary", stage=stage)

        client = BudgetedAllocationClient(
            TransientClient(),
            budget=ProtocolBudget(
                stage_timeout_seconds=10,
                run_deadline_seconds=0.2,
                max_input_tokens=100_000,
                max_output_tokens=10_000,
                provider_retries=2,
                provider_retry_backoff_seconds=1.0,
                monetary_budget_usd=D("1.00"),
            ),
        )
        with self.assertRaises(DeadlineExceeded):
            client.complete("portfolio_allocation", "prompt")

    def test_a_live_factory_requires_a_configured_provider_and_stub_is_explicit(self):
        with (
            override_settings(AI_CONFIG={"PROVIDER": "none", "API_KEY": "", "MODEL": "none"}),
            self.assertRaises(AllocationProviderUnavailable),
        ):
            build_allocation_client(mode="live")
        with self.assertRaises(AllocationProviderUnavailable):
            build_allocation_client(mode="stub", stub_response=None)
        stub = build_allocation_client(mode="stub", stub_response={"ok": True})
        self.assertEqual(stub.provider, "stub")
        with self.assertRaises(AllocationProviderUnavailable):
            build_allocation_client(mode="something_else")


class LiveSubmissionIntegrationTests(AllocationWorkflowFixture):
    """A provider-shaped run through the real adapter, with a scripted stage client.

    This is deliberately **not** presented as a live model decision: the stage
    client is a test double. The provenance says ``provider: anthropic`` because
    that is what the adapter reports, but the test never claims a network call.
    The separately labelled real recording is ``fix08-live-allocation.v1.json``.
    """

    def _scripted_stage(self):
        class Stage:
            provider = "anthropic"
            model = "test-model"
            parameters = {"max_output_tokens": 2048}

            def __init__(self):
                self.calls = []
                self.last_usage = ModelUsage(
                    input_tokens=321,
                    output_tokens=87,
                    cost_usd=None,
                    reported=True,
                    tokens_measured=True,
                    price_table_cost_usd=D("0.000075"),
                    cost_unknown_reason="provider_reported_cost_unavailable",
                )

            def complete(self, stage, prompt):
                self.calls.append(stage)
                payload = stub_payload()
                marker = "BRIEF (the only permitted source of facts):\n"
                parsed = json.loads(prompt.split(marker, 1)[1].strip())
                payload["portfolio_version"] = parsed["portfolio_version"]
                return json.dumps(payload)

        return Stage()

    def test_a_live_submission_flows_through_the_provider_adapter_and_records_usage(self):
        stage = self._scripted_stage()
        client = BudgetedAllocationClient(
            stage,
            budget=ProtocolBudget(
                stage_timeout_seconds=10,
                run_deadline_seconds=30,
                max_input_tokens=100_000,
                max_output_tokens=10_000,
                provider_retries=0,
                monetary_budget_usd=D("1.00"),
            ),
        )
        with patch("services.allocation_submission.build_allocation_client", return_value=client):
            response = self.submit("live-key", mode="live", stub_response=None)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(response.data["status"], AllocationSubmissionStatus.COMPLETED)
        self.assertEqual(response.data["provider"], "anthropic")
        self.assertEqual(response.data["model"], "test-model")
        self.assertFalse(response.data["synthetic_response"])
        self.assertEqual(len(stage.calls), 1)
        plan = PortfolioPlan.objects.get(pk=response.data["plan_id"])
        review = build_plan_review(plan)
        provenance = review["provenance"]
        self.assertEqual(provenance["provider"], "anthropic")
        self.assertFalse(provenance["synthetic_response"])
        self.assertEqual(provenance["input_tokens"], 321)
        self.assertEqual(provenance["output_tokens"], 87)
        self.assertEqual(provenance["adapter"]["usage"]["calls"], 1)
        self.assertIn("Live model decision", provenance["label"])
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)


class SubmissionServiceUnitTests(TestCase):
    def test_request_hash_is_stable_across_ticker_order(self):
        portfolio = make_allocation_portfolio(holdings={"AAPL": ("10", "90")})
        base = {
            "portfolio_id": portfolio.id,
            "tickers": ["MSFT", "JPM"],
            "mode": "stub",
            "protocol_version": PROTOCOL_VERSION,
            "idempotency_key": "stable-key",
        }
        first = SubmissionRequest.build(portfolio, base)
        second = SubmissionRequest.build(portfolio, {**base, "tickers": ["JPM", "MSFT", "MSFT"]})
        self.assertEqual(first.request_hash, second.request_hash)
        self.assertNotEqual(
            first.request_hash,
            SubmissionRequest.build(portfolio, {**base, "tickers": ["MSFT"]}).request_hash,
        )

    def test_collect_quotes_uses_cost_fallback_only_when_allowed_and_labels_it(self):
        portfolio = make_allocation_portfolio(holdings={"AAPL": ("10", "90")})
        with patch("services.allocation_submission.get_latest_quote", return_value=None):
            fallback = collect_quotes(portfolio, ["AAPL", "MSFT"])
            strict = collect_quotes(portfolio, ["AAPL", "MSFT"], allow_cost_fallback=False)
        self.assertEqual(fallback.quotes["AAPL"], D("90"))
        self.assertEqual(fallback.sources["AAPL"], "average_cost_fallback")
        self.assertIn("MSFT", fallback.missing)
        self.assertNotIn("AAPL", strict.quotes)
        self.assertIn("AAPL", strict.missing)


class ApplyServiceApprovalBindingTests(TestCase):
    """Service-level checks that keep the API from being the only guard."""

    def setUp(self):
        self.user = make_user("fix08-binding")
        self.portfolio = make_allocation_portfolio(holdings={"AAPL": ("100", "90")})
        classify(self.portfolio, {"AAPL": "technology"})
        with patch("services.allocation_submission.get_latest_quote", side_effect=fake_quote):
            submission, _ = submit_analysis(
                self.portfolio, data=request_body(self.portfolio, "binding-key")
            )
        self.assertEqual(submission.status, AllocationSubmissionStatus.COMPLETED)
        self.plan = submission.plan

    def test_require_valid_approval_uses_the_plan_version_and_expiry(self):
        approval = approve_plan(
            self.plan.id,
            user=self.user,
            plan_version=self.plan.plan_version,
            expires_at=timezone.now() + timedelta(hours=1),
            note="reviewed",
        )
        self.assertEqual(approval.decision, PlanApprovalDecision.APPROVED)
        self.plan.refresh_from_db()
        reference = require_valid_approval(self.plan)
        self.assertEqual(reference.plan_version, self.plan.plan_version)
        self.assertEqual(reference.approval_hash, self.plan.approval_hash)
        with self.assertRaises(PlanApprovalError):
            approve_plan(
                self.plan.id,
                user=self.user,
                plan_version=self.plan.plan_version,
                expires_at=timezone.now() + timedelta(hours=1),
            )
        with self.assertRaises(PlanApprovalError):
            reject_plan(self.plan.id, user=self.user, reason="no", plan_version=99)
        PortfolioPlan.objects.filter(pk=self.plan.id).update(
            approval_expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.plan.refresh_from_db()
        with self.assertRaises(PlanApprovalError):
            require_valid_approval(self.plan)

    def test_submission_is_recorded_before_a_slow_or_failing_model_call(self):
        portfolio = make_allocation_portfolio(holdings={"AAPL": ("100", "90")})

        def boom(*args, **kwargs):
            raise RuntimeError("provider exploded")

        with (
            patch("services.allocation_submission.get_latest_quote", side_effect=fake_quote),
            patch("services.allocation_submission.build_allocation_client", side_effect=boom),
        ):
            submission, created = submit_analysis(
                portfolio,
                data=request_body(portfolio, "boom-key", mode="live", stub_response=None),
            )
        self.assertTrue(created)
        self.assertEqual(submission.status, AllocationSubmissionStatus.FAILED)
        self.assertEqual(submission.error_code, "RuntimeError")
        self.assertIn("provider exploded", submission.error_detail)


class RecordedLiveArtifactTests(TestCase):
    """The committed live artifact must never be mistaken for a stub sample."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.conf import settings

        cls.path = (
            Path(settings.BASE_DIR) / "docs" / "spec" / "fixtures" / "fix08-live-allocation.v1.json"
        )
        cls.record = json.loads(cls.path.read_text(encoding="utf-8"))

    def test_the_artifact_is_labelled_as_a_real_provider_run(self):
        record = self.record
        self.assertEqual(record["mode"], "live")
        self.assertEqual(record["source"], "anthropic")
        self.assertEqual(record["provider"], "anthropic")
        self.assertEqual(record["model"], "claude-haiku-5-5")
        self.assertEqual(record["run_kind"], "real_recorded_model_run")
        self.assertEqual(record["model_response_kind"], "provider_generated")
        self.assertFalse(record["synthetic_response"])
        self.assertTrue(record["input_data_is_synthetic"])
        self.assertEqual(
            record["input"]["input_data_classification"], "operator_supplied_demonstration"
        )
        self.assertFalse(record["input"]["realised_history"])
        self.assertTrue(record["recorded_at"])
        self.assertEqual(record["prompt_template_version"], "portfolio_targets.v1")
        self.assertEqual(record["protocol_version"], "portfolio_allocation.v1")
        self.assertEqual(record["budget"]["provider_retries"], 0)
        self.assertEqual(record["budget"]["schema_repair_attempts"], 0)
        self.assertIn("max_output_tokens", record["parameters"])
        self.assertIn("prompt_sha256", record["request"])
        self.assertIn("prompt", record["request"])
        self.assertTrue(
            record["request"]["prompt"].startswith("You are the chief investment officer")
        )
        self.assertEqual(record["request"]["prompt_template_version"], "portfolio_targets.v1")

    def test_the_artifact_records_measured_tokens_and_an_estimated_not_billed_cost(self):
        usage = self.record["usage"]["usage"]
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["token_accounting"], "provider_reported")
        self.assertEqual(usage["measured_tokens"]["input"], 5005)
        self.assertEqual(usage["measured_tokens"]["output"], 3020)
        self.assertEqual(usage["price_table_cost_usd"], "0.002010")
        self.assertIsNone(usage["billed_cost_usd"])
        self.assertEqual(usage["billed_cost_unknown_calls"], 1)
        cost = self.record["cost"]
        self.assertIsNone(cost["billed_cost_usd"])
        self.assertEqual(cost["price_table_estimated_cost_usd"], "0.002010")
        self.assertEqual(cost["input_tokens"], 5005)
        self.assertEqual(cost["output_tokens"], 3020)

    def test_the_recorded_response_is_real_and_no_trade_was_placed(self):
        response = self.record["response"]["parsed_response"]
        self.assertEqual(response["schema_version"], "portfolio_targets.v1")
        weights = {key: D(value) for key, value in response["target_weights"].items()}
        self.assertEqual(sum(weights.values(), D("0")), D("1"))
        self.assertIn("CASH", weights)
        self.assertEqual(self.record["plan"]["status"], "proposed")
        self.assertTrue(self.record["plan"]["created"])
        self.assertEqual(self.record["approval"]["state"], "not_decided")
        self.assertFalse(self.record["traded"])
        self.assertEqual(len(self.record["plan"]["plan"]["legs"]), 2)
        self.assertEqual(len(self.record["plan"]["review"]["legs"]), 2)
        review = self.record["plan"]["review"]
        self.assertFalse(review["provenance"]["synthetic_response"])
        self.assertIn("Live model decision", review["provenance"]["label"])
        self.assertEqual(review["approval"]["state"], "pending")
        self.assertIsInstance(review["approval"]["reason"], str)
        self.assertTrue(review["data_quality"]["limitations"])
        self.assertEqual(review["data_quality"]["risk_statistics"]["status"], "not_available")

    def test_the_input_fixture_declares_itself_as_authored_input(self):
        from django.conf import settings

        path = Path(settings.BASE_DIR) / "docs" / "spec" / "fixtures" / "fix08-live-input.v1.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(fixture["synthetic_input_data"])
        self.assertFalse(fixture["realised_history"])
        self.assertEqual(fixture["input_data_classification"], "operator_supplied_demonstration")
        self.assertIsNone(fixture["price_history"])
        self.assertTrue(fixture["quotes"])
        self.assertTrue(fixture["classifications"])
