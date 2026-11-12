"""P1 remediation step 9: account for provider cost and enforce the daily limiter.

Reproduced before the repair:

* ``budget_state().exhausted`` was computed from ``AgentDecisionLog.api_cost_usd``
  only. A run whose provider reported 800 tokens and no dollars stored
  ``api_cost_usd = NULL`` and ``estimated_cost_usd = 0.0015``; with a $0.001
  ceiling the state said ``spent_usd = 0.00`` and ``exhausted = False``, so the
  next provider call was dispatched even though the price-table spend had
  already crossed the ceiling.
* the estimate was also quantised to cents, so ``$0.0015`` was published as
  ``$0.00`` even on the day it was recorded.
* there was no reservation before the provider call and no durable per-call
  row, so a process restart lost the only remaining evidence of the spend and a
  retry had no key to deduplicate against.

The tests below drive the real task with a provider result that reports tokens
but no dollars, and exercise the ledger's reservation/reconciliation contract
directly. The main provider adapter is unchanged: it is allowed to report no
dollars, but the limiter must never turn that silence into a measured zero.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings

from ai_agent import AgentRunResult, TradeProposal
from core.models import AgentDecisionLog, AIUsageLedger
from core.tests.helpers import TEST_CACHES, make_portfolio, make_user
from services.budget import (
    MICRO_CENT,
    budget_state,
    check_and_reserve,
    cost_state,
    logical_call_key,
    reconcile_decision_run,
    reconcile_report,
    settle_call,
)
from services.market_data import PriceQuote
from services.news import NewsReport

D = Decimal


def quote(price: str) -> PriceQuote:
    return PriceQuote(
        ticker="AAPL",
        symbol="AAPL",
        price=D(price),
        previous_close=None,
        source="test_market_data",
        as_of=None,
    )


def tokens_without_dollars(*, estimated: str = "0.00150") -> AgentRunResult:
    """The real provider adapter's shape: measured tokens, billed cost null."""
    return AgentRunResult(
        proposal=TradeProposal(
            action="HOLD",
            amount=0.0,
            sentiment="NEUTRAL",
            reasoning="The evidence is balanced.",
        ),
        reasoning="The evidence is balanced.",
        tokens_used=800,
        input_tokens=500,
        output_tokens=300,
        api_cost_usd=None,
        estimated_cost_usd=D(estimated),
        cost_basis="price_table_estimate",
        cost_unknown_reason="provider_reported_cost_unavailable",
        source="crewai",
    )


class _TaskFixture(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(
            self.user, balance="10000.00", autonomous=True, allocation_pct="5.00"
        )
        from config.celery import app as celery_app
        from tasks import run_alpha_agent_task

        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", False)
        self.task = run_alpha_agent_task

    def _run(self, ticker: str = "AAPL"):
        with (
            patch("tasks.get_latest_quote", return_value=quote("100.00")),
            patch("tasks.build_news_report", return_value=NewsReport(ticker=ticker)),
        ):
            return self.task.apply(args=[self.portfolio.id, ticker]).get()


class ProviderTokensWithoutDollarsTests(_TaskFixture):
    """9A: reserve first, then reconcile to a labelled estimate, billed stays null."""

    @override_settings(
        CACHES=TEST_CACHES,
        AI_DAILY_SPEND_LIMIT_USD=D("0.0100"),
        AI_RUN_COST_UPPER_BOUND_USD=D("0.0050"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_tokens_without_dollars_reconcile_to_an_estimate_not_zero(self):
        with patch("tasks.run_alpha_agent", return_value=tokens_without_dollars()):
            result = self._run()

        self.assertEqual(result["status"], "hold")
        row = AIUsageLedger.objects.get(call_key__startswith="decision:")
        self.assertEqual(row.state, "estimated")
        self.assertIsNone(row.billed_usd)
        self.assertEqual(row.estimated_usd, D("0.001500"))
        self.assertEqual(row.reserved_usd, D("0.000000"))
        self.assertEqual(row.reserved_upper_bound_usd, D("0.005000"))

        state = budget_state()
        self.assertEqual(state.spent_usd, D("0.000000"))
        self.assertEqual(state.estimated_spend_usd, D("0.001500"))
        self.assertEqual(state.reserved_usd, D("0.000000"))
        self.assertEqual(state.committed_usd, D("0.001500"))
        self.assertEqual(state.unknown_cost_runs, 0)
        # The day is not "zero spend": a real price-table estimate is present,
        # while the billed field remains honestly null.
        self.assertNotEqual(state.estimated_spend_usd, D("0"))
        payload = state.as_dict()
        self.assertEqual(payload["spent_usd"], "0.000000")
        self.assertEqual(payload["estimated_spend_usd"], "0.001500")
        self.assertEqual(payload["committed_usd"], "0.001500")
        self.assertIsNone(payload["unknown_amount_usd"])
        self.assertEqual(payload["basis"], "lower_bound")

        log = AgentDecisionLog.objects.get(portfolio=self.portfolio)
        self.assertIsNone(log.api_cost_usd)
        self.assertEqual(log.estimated_cost_usd, D("0.00150"))
        self.assertEqual(log.cost_ledger_key, row.call_key)

    @override_settings(
        CACHES=TEST_CACHES,
        AI_DAILY_SPEND_LIMIT_USD=D("0.0100"),
        AI_RUN_COST_UPPER_BOUND_USD=D("0.0050"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_a_provider_that_reports_nothing_stays_unknown_not_zero(self):
        silent = AgentRunResult(
            proposal=TradeProposal(
                action="HOLD",
                amount=0.0,
                sentiment="NEUTRAL",
                reasoning="No usage was reported.",
            ),
            reasoning="No usage was reported.",
            tokens_used=0,
            api_cost_usd=None,
            estimated_cost_usd=None,
            cost_basis="unknown",
            cost_unknown_reason="provider_token_usage_unavailable",
            source="crewai",
        )
        with patch("tasks.run_alpha_agent", return_value=silent):
            result = self._run()

        self.assertEqual(result["status"], "hold")
        row = AIUsageLedger.objects.get(call_key__startswith="decision:")
        self.assertEqual(row.state, "unknown")
        self.assertIsNone(row.billed_usd)
        self.assertIsNone(row.estimated_usd)
        self.assertEqual(row.unknown_reason, "provider_token_usage_unavailable")
        # The conservative bound is still held; a missing measurement was not
        # turned into a measured zero.
        self.assertEqual(row.reserved_usd, D("0.005000"))
        state = budget_state()
        self.assertEqual(state.spent_usd, D("0.000000"))
        self.assertEqual(state.estimated_spend_usd, D("0.000000"))
        self.assertEqual(state.unknown_cost_runs, 1)
        self.assertEqual(state.unknown_reserved_usd, D("0.005000"))
        self.assertEqual(state.committed_usd, D("0.005000"))
        self.assertIsNone(state.as_dict()["unknown_amount_usd"])

    @override_settings(
        CACHES=TEST_CACHES,
        AI_DAILY_SPEND_LIMIT_USD=D("1.0000"),
        AI_RUN_COST_UPPER_BOUND_USD=D("0.2500"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_the_reserve_is_visible_while_the_call_is_open(self):
        decision = check_and_reserve(
            call_key=logical_call_key("test", "open-call"),
            upper_bound_usd=D("0.2500"),
            provider="test",
            model="test-model",
        )
        self.assertTrue(decision.allowed)
        state = budget_state()
        self.assertEqual(state.reserved_usd, D("0.250000"))
        self.assertEqual(state.spent_usd, D("0.000000"))
        self.assertEqual(state.committed_usd, D("0.250000"))
        self.assertEqual(state.remaining_usd, D("0.750000"))

        # Reconciliation against a provider report with a price-table figure.
        report = {
            "adapter": "budgeted_provider_client",
            "usage": {
                "calls": 1,
                "input_tokens": 500,
                "output_tokens": 300,
                "billed_cost_usd": None,
                "price_table_cost_usd": "0.001500",
                "billed_cost_unknown_reason": "provider_reported_cost_unavailable",
            },
        }
        reconcile_report(decision.call_key, report)
        row = AIUsageLedger.objects.get(call_key=decision.call_key)
        self.assertEqual(row.state, "estimated")
        self.assertIsNone(row.billed_usd)
        state = budget_state()
        self.assertEqual(state.reserved_usd, D("0.000000"))
        self.assertEqual(state.estimated_spend_usd, D("0.001500"))
        self.assertEqual(state.unknown_cost_runs, 0)


class ReserveRefusalTests(_TaskFixture):
    """9B: a request that would breach the ceiling is refused before the call."""

    @override_settings(
        CACHES=TEST_CACHES,
        AI_DAILY_SPEND_LIMIT_USD=D("0.0020"),
        AI_RUN_COST_UPPER_BOUND_USD=D("0.0100"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_the_provider_is_not_called_when_the_reserved_bound_breaches(self):
        with patch("tasks.run_alpha_agent") as provider:
            result = self._run()

        provider.assert_not_called()
        self.assertEqual(result["status"], "halted")
        self.assertEqual(result["reason"], "ai_spend_reserve_exceeded")
        self.assertEqual(result["reserved_upper_bound_usd"], "0.010000")
        self.assertEqual(result["limit_usd"], "0.002000")
        self.assertEqual(result["committed_usd"], "0.000000")
        self.assertEqual(AIUsageLedger.objects.count(), 0)
        halt = AgentDecisionLog.objects.get(portfolio=self.portfolio)
        self.assertEqual(halt.action_taken, "HALT - AI spend reserve exceeded")
        self.assertIn("provider was not called", halt.reasoning)

    @override_settings(
        CACHES=TEST_CACHES,
        AI_DAILY_SPEND_LIMIT_USD=D("1.0000"),
        AI_RUN_COST_UPPER_BOUND_USD=D("0.4000"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_a_reservation_at_or_below_the_remaining_room_is_allowed(self):
        first = check_and_reserve(call_key=logical_call_key("test", "a"), upper_bound_usd=D("0.6"))
        self.assertTrue(first.allowed)
        second = check_and_reserve(call_key=logical_call_key("test", "b"), upper_bound_usd=D("0.4"))
        self.assertTrue(second.allowed)
        third = check_and_reserve(
            call_key=logical_call_key("test", "c"), upper_bound_usd=D("0.000001")
        )
        self.assertFalse(third.allowed)
        self.assertIn(third.reason, {"ai_spend_reserve_exceeded", "ai_spend_ceiling"})
        self.assertEqual(third.committed_usd, D("1.000000"))


class DurableReservationTests(TransactionTestCase):
    """9C: the count survives a restart and a replay cannot double-count."""

    reset_sequences = True

    def setUp(self):
        self.user = make_user()
        self.portfolio = make_portfolio(self.user)

    @override_settings(
        AI_DAILY_SPEND_LIMIT_USD=D("5.0000"),
        AI_RUN_COST_UPPER_BOUND_USD=D("1.0000"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_a_reservation_is_durable_across_a_connection_restart(self):
        key = logical_call_key("test", "restart-call")
        first = check_and_reserve(call_key=key, upper_bound_usd=D("0.25"))
        self.assertTrue(first.allowed)
        self.assertEqual(AIUsageLedger.objects.filter(call_key=key).count(), 1)

        # Simulate a process restart: drop every connection, then read again.
        connections.close_all()
        self.assertEqual(AIUsageLedger.objects.filter(call_key=key).count(), 1)
        second = check_and_reserve(call_key=key, upper_bound_usd=D("0.25"))
        self.assertTrue(second.replay)
        self.assertEqual(AIUsageLedger.objects.filter(call_key=key).count(), 1)
        state = budget_state()
        self.assertEqual(state.reserved_usd, D("0.250000"))
        self.assertEqual(state.committed_usd, D("0.250000"))

    @override_settings(
        AI_DAILY_SPEND_LIMIT_USD=D("5.0000"),
        AI_RUN_COST_UPPER_BOUND_USD=D("1.0000"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_replaying_a_settled_key_changes_nothing(self):
        key = logical_call_key("test", "settled-call")
        check_and_reserve(call_key=key, upper_bound_usd=D("0.25"))
        settle_call(call_key=key, estimated_usd=D("0.001500"), input_tokens=1, output_tokens=1)
        before = budget_state()
        again = check_and_reserve(call_key=key, upper_bound_usd=D("0.25"))
        after = budget_state()
        self.assertTrue(again.replay)
        self.assertEqual(AIUsageLedger.objects.filter(call_key=key).count(), 1)
        self.assertEqual(before.committed_usd, after.committed_usd)
        self.assertEqual(before.estimated_spend_usd, after.estimated_spend_usd)

        # A redelivered provider result must not become a second estimate.
        settle_call(call_key=key, estimated_usd=D("0.001500"))
        third = budget_state()
        self.assertEqual(third.committed_usd, after.committed_usd)

    @override_settings(
        AI_DAILY_SPEND_LIMIT_USD=D("5.0000"),
        AI_RUN_COST_UPPER_BOUND_USD=D("1.0000"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_a_recrunch_from_a_persisted_decision_result_is_idempotent(self):
        key = logical_call_key("test", "reconcile-call")
        check_and_reserve(call_key=key, upper_bound_usd=D("0.25"))
        result = tokens_without_dollars()
        reconcile_decision_run(key, result)
        first = budget_state()
        reconcile_decision_run(key, result)
        second = budget_state()
        self.assertEqual(first.estimated_spend_usd, second.estimated_spend_usd)
        self.assertEqual(first.committed_usd, second.committed_usd)
        row = AIUsageLedger.objects.get(call_key=key)
        self.assertIsNone(row.billed_usd)
        self.assertEqual(row.state, "estimated")


class LimiterStateShapeTests(TestCase):
    """9D: spent, reserved, estimated and unknown are distinct and reconcile."""

    @override_settings(
        AI_DAILY_SPEND_LIMIT_USD=D("5.0000"),
        AI_RUN_COST_UPPER_BOUND_USD=D("1.0000"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_the_state_distinguishes_every_basis_and_reconciles(self):
        user = make_user()
        portfolio = make_portfolio(user)

        billed = check_and_reserve(
            call_key=logical_call_key("test", "billed"), upper_bound_usd=D("0.50")
        )
        settle_call(call_key=billed.call_key, billed_usd=D("0.250000"))

        estimated = check_and_reserve(
            call_key=logical_call_key("test", "estimated"), upper_bound_usd=D("0.50")
        )
        settle_call(call_key=estimated.call_key, estimated_usd=D("0.001500"))

        holder = check_and_reserve(
            call_key=logical_call_key("test", "open"), upper_bound_usd=D("0.4000")
        )
        self.assertTrue(holder.allowed)

        unknown = check_and_reserve(
            call_key=logical_call_key("test", "unknown"), upper_bound_usd=D("0.6000")
        )
        settle_call(
            call_key=unknown.call_key,
            unknown_reason="provider_reported_no_usage_or_cost",
        )

        # A legacy audit row with no ledger reservation is counted as unknown,
        # never given an invented dollar value and never added to committed.
        AgentDecisionLog.objects.create(
            portfolio=portfolio,
            action_taken="HOLD",
            reasoning="legacy unknown-cost row",
            tokens_used=200,
            api_cost_usd=None,
        )

        state = budget_state()
        self.assertEqual(state.spent_usd, D("0.250000"))
        self.assertEqual(state.estimated_spend_usd, D("0.001500"))
        self.assertEqual(state.reserved_usd, D("1.000000"))  # 0.40 open + 0.60 unknown
        self.assertEqual(state.unknown_reserved_usd, D("0.600000"))
        self.assertEqual(state.unknown_cost_runs, 2)
        self.assertEqual(state.unreserved_unknown_runs, 1)
        self.assertEqual(
            state.committed_usd,
            (state.spent_usd + state.estimated_spend_usd + state.reserved_usd),
        )
        self.assertEqual(
            state.remaining_usd,
            (state.limit_usd - state.committed_usd).quantize(MICRO_CENT),
        )

        payload = state.as_dict()
        self.assertEqual(payload["spent_usd"], "0.250000")
        self.assertEqual(payload["estimated_spend_usd"], "0.001500")
        self.assertEqual(payload["reserved_usd"], "1.000000")
        self.assertEqual(payload["unknown_reserved_usd"], "0.600000")
        self.assertEqual(payload["unknown_cost_runs"], 2)
        self.assertIsNone(payload["unknown_amount_usd"])
        self.assertTrue(payload["lower_bound"])
        self.assertEqual(payload["basis"], "lower_bound")
        self.assertEqual(payload["committed_usd"], "1.251500")

        unknown_row = AIUsageLedger.objects.get(call_key=unknown.call_key)
        self.assertEqual(unknown_row.state, "unknown")
        self.assertIsNone(unknown_row.billed_usd)
        self.assertIsNone(unknown_row.estimated_usd)
        self.assertEqual(unknown_row.reserved_usd, D("0.600000"))

        # The legacy unknown row is visible in the count but contributes no
        # dollar figure. That is the difference between unknown and zero.
        costs = cost_state()
        self.assertEqual(costs["billed_spend_usd"], "0.250000")
        self.assertIsNone(costs["unknown_amount_usd"])

        # The same state is on the portfolio payload an operator reads. A
        # dashboard that only saw spent_usd would show a lower bound as a fact.
        from core.serializers import build_portfolio_payload

        payload = build_portfolio_payload(portfolio)["ai_budget"]
        for key in (
            "spent_usd",
            "reserved_usd",
            "estimated_spend_usd",
            "committed_usd",
            "remaining_usd",
            "unknown_reserved_usd",
            "unknown_cost_runs",
            "unknown_amount_usd",
            "lower_bound",
            "basis",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["spent_usd"], "0.250000")
        self.assertEqual(payload["reserved_usd"], "1.000000")
        self.assertIsNone(payload["unknown_amount_usd"])
        self.assertTrue(payload["lower_bound"])
