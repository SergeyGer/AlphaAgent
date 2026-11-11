"""FIX-01 acceptance: the real decision lifecycle from analysis to completion.

A fresh protocol run is produced by the real ``run_alpha_agent`` path (with a
deterministic stub model client), then driven through ``tasks.run_alpha_agent_task``
so the guard, ledger, recommendation and run record are all exercised together.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ai_agent import run_alpha_agent
from core.decision_models import (
    DecisionRun,
    DecisionStatus,
    GuardRecord,
    OutcomeMaturity,
    OutcomeRecord,
    RunEventType,
)
from core.models import (
    AgentDecisionLog,
    RecommendationStatus,
    TradeRecommendation,
    Transaction,
)
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio
from core.tests.test_debate_protocol import (
    StubStageClient,
    make_context,
    standard_handlers,
)
from services import attribution
from services.attribution import evaluate_pending
from services.decision_lifecycle import complete_run
from services.decision_outcomes import OUTCOME_METHOD_VERSION
from services.market_data import PriceQuote
from services.recommendations import (
    approve_recommendation,
    expire_stale_recommendations,
    reject_recommendation,
)

D = Decimal


def quote(price: str = "100.00") -> PriceQuote:
    return PriceQuote(ticker="AAPL", symbol="AAPL", price=D(price), source="test")


def adjudication_handler(action: str, amount: int):
    def handler(client, prompt: str) -> str:
        return json.dumps(
            {
                "action": action,
                "amount": amount,
                "sentiment": "BULLISH" if action == "BUY" else "NEUTRAL",
                "reasoning": (
                    "The services case is decisive enough to act, with the supplier warning "
                    "recorded as the strongest objection."
                ),
                "decisive_claim_ids": [],
                "strongest_objection": "The supplier demand warning could widen.",
                "unresolved_uncertainty": "Whether hardware weakness reaches services.",
                "thesis": "Services growth is durable.",
                "invalidation_condition": "Services growth falls below 10%.",
            }
        )

    return handler


def protocol_client(action: str = "BUY", amount: int = 5) -> StubStageClient:
    return StubStageClient(
        {**standard_handlers(), "adjudication": adjudication_handler(action, amount)}
    )


@override_settings(CACHES=TEST_CACHES)
class DecisionLifecycleTests(TestCase):
    def setUp(self):
        from config.celery import app as celery_app

        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", False)
        from tasks import run_alpha_agent_task

        self.task = run_alpha_agent_task
        self.portfolio = make_portfolio(balance="10000.00", autonomous=True)
        self.context_news = make_context().news_report

    def _run(self, *, action="BUY", amount=5, require_approval=False):
        client = protocol_client(action, amount)

        def real_protocol(context):
            return run_alpha_agent(context, model_client=client)

        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=self.context_news),
            patch("tasks.run_alpha_agent", side_effect=real_protocol),
        ):
            return self.task.apply(args=[self.portfolio.id, "AAPL", require_approval]).get()

    def _run_detail(self, run):
        return auth_client(self.portfolio.user).get(
            reverse("core:decision-run-detail-rich", args=[str(run.id)])
        )

    def test_buy_approved_traces_one_run_through_guard_ledger_and_detail(self):
        result = self._run(action="BUY", amount=5)
        self.assertEqual(result["status"], "executed")

        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.status, DecisionStatus.COMPLETED)
        self.assertEqual(run.execution_state, "executed")
        transaction = Transaction.objects.get(portfolio=self.portfolio)
        self.assertEqual(result["transaction_id"], transaction.id)

        guard = GuardRecord.objects.get(proposal__run=run)
        self.assertEqual(guard.allowed_quantity, transaction.amount)
        self.assertEqual(guard.status, "approved")
        self.assertTrue(guard.policy_version)
        self.assertTrue(guard.evaluated_portfolio_version)
        self.assertIsNotNone(guard.quote_time)
        self.assertTrue(guard.numerical_checks["checks"])

        event_types = list(run.events.values_list("event_type", flat=True))
        self.assertIn(RunEventType.GUARD_EVALUATED, event_types)
        self.assertIn(RunEventType.EXECUTION_RECORDED, event_types)
        self.assertIn(RunEventType.DECISION_COMPLETED, event_types)
        execution_event = run.events.get(event_type=RunEventType.EXECUTION_RECORDED)
        self.assertEqual(execution_event.payload["transaction_id"], transaction.id)
        self.assertEqual(run.legacy_log_id, AgentDecisionLog.objects.get().id)

        detail = self._run_detail(run).json()
        self.assertEqual(detail["run"]["id"], str(run.id))
        self.assertEqual(detail["run"]["execution_state"], "executed")
        self.assertTrue(detail["run"]["execution"]["executed"])
        self.assertEqual(detail["guard"]["allowed_quantity"], str(transaction.amount))
        export = auth_client(self.portfolio.user).get(
            reverse("core:decision-run-export", args=[str(run.id)])
        )
        self.assertEqual(export.json()["run"]["id"], str(run.id))

    def test_buy_clamped_records_requested_and_allowed_quantities(self):
        result = self._run(action="BUY", amount=500)
        self.assertEqual(result["status"], "executed")
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        guard = GuardRecord.objects.get(proposal__run=run)
        self.assertEqual(guard.status, "clamped")
        self.assertEqual(guard.requested_quantity, D("500"))
        self.assertEqual(guard.allowed_quantity, D("5"))
        self.assertEqual(result["notional"], "500.00")

    def test_buy_rejected_completes_with_a_rejection_not_an_execution(self):
        self.portfolio.balance_usd = D("0.00")
        self.portfolio.save(update_fields=["balance_usd"])
        result = self._run(action="BUY", amount=5)
        self.assertEqual(result["status"], "blocked")
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.status, DecisionStatus.COMPLETED)
        self.assertEqual(run.execution_state, "blocked")
        self.assertEqual(Transaction.objects.count(), 0)
        guard = GuardRecord.objects.get(proposal__run=run)
        self.assertEqual(guard.status, "rejected")
        self.assertEqual(guard.allowed_quantity, D("0"))
        detail = self._run_detail(run).json()
        self.assertFalse(detail["run"]["execution"]["executed"])

    def test_hold_has_an_explicit_no_trade_completion_and_no_guard_record(self):
        result = self._run(action="HOLD", amount=0)
        self.assertEqual(result["status"], "hold")
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.status, DecisionStatus.COMPLETED)
        self.assertEqual(run.execution_state, "held")
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(GuardRecord.objects.filter(proposal__run=run).count(), 0)
        completed = run.events.get(event_type=RunEventType.DECISION_COMPLETED)
        self.assertTrue(completed.payload["no_trade"])
        self.assertFalse(completed.payload["executed"])

    def test_advisory_pending_is_not_an_execution_and_approval_completes_the_same_run(self):
        self.portfolio.is_autonomous = False
        self.portfolio.save(update_fields=["is_autonomous"])
        result = self._run(action="BUY", amount=5, require_approval=True)
        self.assertEqual(result["status"], "awaiting_approval")

        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.status, DecisionStatus.AWAITING_APPROVAL)
        self.assertEqual(run.execution_state, "awaiting_approval")
        recommendation = TradeRecommendation.objects.get(portfolio=self.portfolio)
        self.assertEqual(recommendation.decision_run_id, run.id)
        self.assertEqual(Transaction.objects.count(), 0)
        event_types = list(run.events.values_list("event_type", flat=True))
        self.assertIn(RunEventType.APPROVAL_REQUESTED, event_types)
        self.assertNotIn(RunEventType.EXECUTION_RECORDED, event_types)
        detail = self._run_detail(run).json()
        self.assertFalse(detail["run"]["execution"]["executed"])
        self.assertNotEqual(detail["run"]["execution_state"], "executed")

        with patch("services.recommendations.get_latest_quote", return_value=quote("100.00")):
            outcome = approve_recommendation(recommendation.id)
        self.assertTrue(outcome.executed)

        run.refresh_from_db()
        self.assertEqual(run.id, DecisionRun.objects.get(pk=result["decision_run_id"]).id)
        self.assertEqual(run.status, DecisionStatus.COMPLETED)
        self.assertEqual(run.execution_state, "executed")
        self.assertEqual(recommendation.decision_run_id, run.id)
        self.assertEqual(Transaction.objects.count(), 1)
        guards = GuardRecord.objects.filter(proposal__run=run).order_by("-created_at", "-id")
        self.assertEqual(guards.first().allowed_quantity, Transaction.objects.get().amount)

    def test_advisory_under_lock_rejection_blocks_without_a_fill(self):
        from services.execution import ExecutionResult, GuardDecision

        self.portfolio.is_autonomous = False
        self.portfolio.save(update_fields=["is_autonomous"])
        result = self._run(action="BUY", amount=5, require_approval=True)
        recommendation = TradeRecommendation.objects.get(portfolio=self.portfolio)
        blocked = GuardDecision(
            approved=False,
            reason="Rejected under lock: policy changed.",
            action="BUY",
            price=D("100.00"),
            requested_amount=D("5"),
        )
        with patch(
            "services.recommendations.execute_trade",
            return_value=ExecutionResult(
                approved=False, decision=blocked, request=None, reason=blocked.reason
            ),
        ):
            outcome = approve_recommendation(recommendation.id)
        self.assertFalse(outcome.executed)
        recommendation.refresh_from_db()
        self.assertEqual(recommendation.status, RecommendationStatus.BLOCKED)
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.execution_state, "blocked")
        self.assertEqual(Transaction.objects.count(), 0)

    def test_advisory_rejection_completes_the_run_as_rejected(self):
        self.portfolio.is_autonomous = False
        self.portfolio.save(update_fields=["is_autonomous"])
        result = self._run(action="BUY", amount=5, require_approval=True)
        recommendation = TradeRecommendation.objects.get(portfolio=self.portfolio)
        reject_recommendation(recommendation.id)
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.status, DecisionStatus.COMPLETED)
        self.assertEqual(run.execution_state, "rejected")
        self.assertEqual(Transaction.objects.count(), 0)

    def test_expired_recommendation_completes_the_same_run_as_expired(self):
        self.portfolio.is_autonomous = False
        self.portfolio.save(update_fields=["is_autonomous"])
        result = self._run(action="BUY", amount=5, require_approval=True)
        recommendation = TradeRecommendation.objects.get(portfolio=self.portfolio)
        TradeRecommendation.objects.filter(pk=recommendation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=5)
        )
        self.assertEqual(expire_stale_recommendations(), 1)
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.status, DecisionStatus.COMPLETED)
        self.assertEqual(run.execution_state, "expired")
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertIn(
            RunEventType.APPROVAL_DECIDED,
            list(run.events.values_list("event_type", flat=True)),
        )

    def test_under_lock_rejection_is_a_blocked_decision_not_a_null_execution(self):
        from services.execution import ExecutionResult, GuardDecision

        blocked = GuardDecision(
            approved=False,
            reason="Rejected under lock: concentration ceiling.",
            action="BUY",
            price=D("100.00"),
            requested_amount=D("5"),
        )
        with patch(
            "tasks.apply_trade",
            return_value=ExecutionResult(
                approved=False, decision=blocked, request=None, reason=blocked.reason
            ),
        ):
            result = self._run(action="BUY", amount=5)
        self.assertEqual(result["status"], "blocked")
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.status, DecisionStatus.COMPLETED)
        self.assertEqual(run.execution_state, "blocked")
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertNotIn(
            RunEventType.EXECUTION_RECORDED,
            list(run.events.values_list("event_type", flat=True)),
        )

    def test_execution_failure_is_a_failed_run_with_no_ledger_row(self):
        from django.db import DatabaseError

        with patch("tasks.apply_trade", side_effect=DatabaseError("boom")):
            result = self._run(action="BUY", amount=5)
        self.assertEqual(result["status"], "failed")
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.status, DecisionStatus.FAILED)
        self.assertEqual(run.execution_state, "failed")
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertIn(
            RunEventType.EXECUTION_FAILED, list(run.events.values_list("event_type", flat=True))
        )

    def test_delayed_approval_uses_the_later_mark_and_the_same_run(self):
        self.portfolio.is_autonomous = False
        self.portfolio.save(update_fields=["is_autonomous"])
        result = self._run(action="BUY", amount=5, require_approval=True)
        recommendation = TradeRecommendation.objects.get(portfolio=self.portfolio)
        TradeRecommendation.objects.filter(pk=recommendation.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        with patch("services.recommendations.get_latest_quote", return_value=quote("101.00")):
            outcome = approve_recommendation(recommendation.id)
        self.assertTrue(outcome.executed)
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        self.assertEqual(run.execution_state, "executed")
        self.assertEqual(Transaction.objects.get().price, D("101.00"))

    def test_retrying_a_completion_handler_creates_no_duplicates(self):
        result = self._run(action="BUY", amount=5)
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        guard = GuardRecord.objects.get(proposal__run=run)
        event_count = run.events.count()
        transaction = Transaction.objects.get(portfolio=self.portfolio)
        decision_record = run.decision

        # Rebuild the same typed decision the execution used. The evaluation key
        # is derived from the run and the numbers, so this is the same handler.
        rebuilt = type(
            "D",
            (),
            {
                "approved": True,
                "reason": guard.numerical_checks["reason"],
                "action": "BUY",
                "amount": guard.allowed_quantity,
                "price": transaction.price,
                "notional": transaction.gross_value_usd,
                "clamped": False,
                "notes": [],
                "requested_amount": guard.requested_quantity,
                "policy_version": guard.policy_version,
                "evaluated_portfolio_version": guard.evaluated_portfolio_version,
                "quote_time": guard.quote_time,
                "binding_rule": guard.binding_rule,
                "reason_codes": guard.reason_codes,
                "checks": guard.numerical_checks["checks"],
            },
        )()
        for _ in range(2):
            complete_run(
                run,
                outcome="executed",
                guard_decision=rebuilt,
                transaction_row=transaction,
                reason=rebuilt.reason,
            )
        self.assertEqual(GuardRecord.objects.filter(proposal=decision_record).count(), 1)
        self.assertEqual(run.events.count(), event_count)


@override_settings(CACHES=TEST_CACHES)
class DuplicateCeleryDeliveryTests(TestCase):
    def test_a_redelivered_task_writes_exactly_one_ledger_row(self):
        from django.core.cache import cache

        from tasks import run_alpha_agent_task

        portfolio = make_portfolio(balance="10000.00", autonomous=True)
        client = protocol_client("BUY", 5)
        news = make_context().news_report

        def real_protocol(context):
            return run_alpha_agent(context, model_client=client)

        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=news),
            patch("tasks.run_alpha_agent", side_effect=real_protocol),
            patch("tasks.claim_ticker_slot", return_value=True),
        ):
            first = run_alpha_agent_task.apply(
                args=[portfolio.id, "AAPL"], task_id="redelivered-task"
            ).get()
            cache.clear()
            second = run_alpha_agent_task.apply(
                args=[portfolio.id, "AAPL"], task_id="redelivered-task"
            ).get()

        self.assertEqual(first["status"], "executed")
        self.assertEqual(second["status"], "executed")
        self.assertTrue(second["duplicate_delivery"])
        self.assertEqual(first["transaction_id"], second["transaction_id"])
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 1)
        self.assertEqual(AgentDecisionLog.objects.filter(portfolio=portfolio).count(), 1)
        self.assertEqual(DecisionRun.objects.filter(portfolio=portfolio).count(), 1)


@override_settings(CACHES=TEST_CACHES)
class MaturedOutcomeBridgeTests(TestCase):
    def setUp(self):
        from tasks import run_alpha_agent_task

        self.task = run_alpha_agent_task
        self.portfolio = make_portfolio(balance="10000.00", autonomous=True)
        self.context_news = make_context().news_report

    def _run(self):
        client = protocol_client("BUY", 5)

        def real_protocol(context):
            return run_alpha_agent(context, model_client=client)

        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=self.context_news),
            patch("tasks.run_alpha_agent", side_effect=real_protocol),
        ):
            return self.task.apply(args=[self.portfolio.id, "AAPL"]).get()

    def test_matured_outcome_uses_the_same_method_version_as_the_evaluator(self):
        result = self._run()
        run = DecisionRun.objects.get(pk=result["decision_run_id"])
        log = AgentDecisionLog.objects.get(pk=run.legacy_log_id)
        AgentDecisionLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(days=10)
        )

        with (
            patch.object(attribution, "_start_price", return_value=D("100")),
            patch.object(attribution, "_price_at", return_value=D("100")),
            patch.object(
                attribution,
                "_close_on_or_after",
                side_effect=lambda ticker, target, horizon_days=1: (
                    D("105") if ticker == "AAPL" else D("102")
                ),
            ),
        ):
            counts = evaluate_pending(now=timezone.now())
        self.assertGreaterEqual(counts["scored"], 1)

        records = OutcomeRecord.objects.filter(decision__run=run)
        self.assertTrue(records.exists())
        self.assertTrue(all(r.method_version == OUTCOME_METHOD_VERSION for r in records))
        self.assertTrue(any(r.maturity == OutcomeMaturity.EVALUATED for r in records))

        detail = (
            auth_client(self.portfolio.user)
            .get(reverse("core:decision-run-detail-rich", args=[str(run.id)]))
            .json()
        )
        self.assertTrue(detail["outcomes"])
        self.assertTrue(
            all(row["method_version"] == OUTCOME_METHOD_VERSION for row in detail["outcomes"])
        )
        self.assertEqual(
            {row["method_version"] for row in detail["outcomes"]}, {OUTCOME_METHOD_VERSION}
        )

        # Re-running the evaluator or the bridge must not append a second row.
        before = OutcomeRecord.objects.filter(decision__run=run).count()
        with (
            patch.object(attribution, "_start_price", return_value=D("100")),
            patch.object(attribution, "_price_at", return_value=D("100")),
            patch.object(
                attribution,
                "_close_on_or_after",
                side_effect=lambda ticker, target, horizon_days=1: (
                    D("105") if ticker == "AAPL" else D("102")
                ),
            ),
        ):
            evaluate_pending(now=timezone.now())
        from io import StringIO

        from django.core.management import call_command

        from services.decision_outcomes import sync_outcomes_for_run

        sync_outcomes_for_run(run)
        call_command("backfill_outcome_records", stdout=StringIO())
        self.assertEqual(OutcomeRecord.objects.filter(decision__run=run).count(), before)
