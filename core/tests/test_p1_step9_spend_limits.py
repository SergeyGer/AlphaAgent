"""P1 remediation steps 9 and 10: spend ceilings for every paid attempt.

The first round of Step 9 taught the daily limiter to reserve before a call and
reconcile afterwards. The acceptance review then reproduced two gaps in that
ledger:

* **A reservation was keyed by the user's request key.** Two portfolios may
  legitimately use the same idempotency key - the durable uniqueness constraint
  is ``(portfolio, idempotency_key)`` - so the second submission found the
  first portfolio's ledger row, treated it as a replay, and was allowed to call
  the provider against an already-committed day.
* **The experiment CLI never reserved at all.** ``run_experiment`` built the
  default protocol budget and called the provider; the pre-registered
  ``cost_ceiling_usd`` in the opportunity set was published in the report but
  never enforced.

The settlement path had two further gaps. ``AgentRunResult`` carried only the
billed figure out of the protocol, so a token-only provider reply (the real
adapter's shape) arrived at the ledger as ``unknown`` even when a price-table
estimate existed. And ``settle_call`` refused to change a terminal row, so a
confirmed charge reported after an estimate was silently dropped.

These tests drive the production allocation worker, the production experiment
runner and the production ``CrewAIStageClient`` with a stubbed provider. They
assert money stays ``Decimal`` and every unknown stays ``None`` with a reason.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings

from ai_agent import AgentRunResult, AlphaAgentOrchestrator, CrewAIStageClient
from core.models import AIUsageLedger
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio, make_user
from core.tests.test_allocation_workflow import (
    AllocationWorkflowFixture,
    classify,
    make_allocation_portfolio,
    request_body,
    stub_payload,
)
from services.allocation_provider import BudgetedAllocationClient
from services.allocation_submission import process_submission
from services.budget import (
    budget_state,
    check_and_reserve,
    logical_call_key,
    mark_unknown,
    reconcile_decision_run,
    settle_call,
    settle_confirmed_charge,
)
from services.debate_protocol import ModelUsage, ProtocolBudget, ProtocolUsage
from services.experiment_execution import execute_opportunity_set, load_opportunity_set
from services.stub_stage_client import OfflineStageClient

D = Decimal
OPPORTUNITY_SET_PATH = settings.BASE_DIR / "docs/spec/fixtures/experiments/alphaagent-arms.v1.json"


def costing_budget() -> ProtocolBudget:
    """A run budget wider than any scripted call, so the ceiling is the binding limit."""
    return ProtocolBudget(
        stage_timeout_seconds=10,
        run_deadline_seconds=30,
        max_input_tokens=100_000,
        max_output_tokens=10_000,
        provider_retries=0,
        monetary_budget_usd=D("1.00"),
    )


class CountingStage:
    """A scripted provider that counts calls; it never touches the network."""

    provider = "anthropic"
    model = "test-model"
    parameters = {"max_output_tokens": 2048}

    def __init__(self) -> None:
        self.calls = 0
        self.last_usage = ModelUsage(
            input_tokens=100,
            output_tokens=50,
            cost_usd=None,
            reported=True,
            tokens_measured=True,
            price_table_cost_usd=D("0.0001"),
            cost_unknown_reason="provider_reported_cost_unavailable",
        )

    def complete(self, stage, prompt):
        self.calls += 1
        payload = stub_payload()
        marker = "BRIEF (the only permitted source of facts):\n"
        parsed = json.loads(prompt.split(marker, 1)[1].strip())
        payload["portfolio_version"] = parsed["portfolio_version"]
        return json.dumps(payload)


def budgeted(stage: CountingStage) -> BudgetedAllocationClient:
    return BudgetedAllocationClient(stage, budget=costing_budget())


class BillableOfflineStageClient(OfflineStageClient):
    """The scripted client, declared billable for the allowance tests.

    ``OfflineStageClient`` makes no provider call and is not billable in
    production. This test double keeps its replies and token estimation but opts
    in to the reservation path, so the daily/run/experiment ceilings can be
    exercised without a network or a model key.
    """

    billable = True


class SameRequestKeyAllocationTests(AllocationWorkflowFixture):
    """9A: two portfolios sharing a request key reserve independently."""

    def _second_portfolio(self):
        portfolio = make_allocation_portfolio(holdings={"AAPL": ("100", "90")})
        classify(portfolio, {"AAPL": "technology", "MSFT": "technology"})
        return portfolio, auth_client(portfolio.user)

    def _submit(self, client, portfolio, key: str):
        response = client.post(
            "/api/decision-runs/",
            request_body(portfolio, key, mode="live", stub_response=None),
            format="json",
        )
        self.assertEqual(response.status_code, 202, response.data)
        return response.data["run_id"]

    @override_settings(
        CACHES=TEST_CACHES,
        AI_DAILY_SPEND_LIMIT_USD=D("0.40"),
        AI_SPEND_LIMIT_ENFORCED=True,
        ALLOCATION_CONFIG={"SUBMISSION_MONETARY_BUDGET_USD": "0.20"},
    )
    def test_two_portfolios_with_the_same_key_each_hold_their_own_reservation(self):
        other_portfolio, other_client = self._second_portfolio()
        first_id = self._submit(self.client, self.portfolio, "shared-request-key")
        second_id = self._submit(other_client, other_portfolio, "shared-request-key")
        self.assertNotEqual(first_id, second_id)

        stage = CountingStage()
        with patch(
            "services.allocation_submission.build_allocation_client",
            return_value=budgeted(stage),
        ):
            first = process_submission(first_id, owner="worker-first")
            second = process_submission(second_id, owner="worker-second")

        self.assertEqual(first.status, "succeeded", first.error_code)
        self.assertEqual(second.status, "succeeded", second.error_code)
        self.assertEqual(stage.calls, 2)

        rows = AIUsageLedger.objects.filter(call_key__startswith="allocation:")
        self.assertEqual(rows.count(), 2)
        keys = {row.call_key for row in rows}
        self.assertEqual(len(keys), 2, keys)
        metadata = sorted((row.metadata for row in rows), key=lambda item: item["submission_id"])
        self.assertEqual(len({item["portfolio_id"] for item in metadata}), 2)
        self.assertEqual(len({item["submission_id"] for item in metadata}), 2)
        # The two submissions used the same *user* key; the reservation identity
        # is the durable submission, not that key.
        self.assertEqual({item["idempotency_key"] for item in metadata}, {"shared-request-key"})
        self.assertEqual({item["paid_attempt"] for item in metadata}, {1})
        self.assertEqual(len({item["request_hash"] for item in metadata}), 2)

    @override_settings(
        CACHES=TEST_CACHES,
        AI_DAILY_SPEND_LIMIT_USD=D("0.20"),
        AI_SPEND_LIMIT_ENFORCED=True,
        ALLOCATION_CONFIG={"SUBMISSION_MONETARY_BUDGET_USD": "0.20"},
    )
    def test_with_headroom_for_one_the_other_never_calls_the_provider(self):
        other_portfolio, other_client = self._second_portfolio()
        first_id = self._submit(self.client, self.portfolio, "one-attempt-key")
        second_id = self._submit(other_client, other_portfolio, "one-attempt-key")

        stage = CountingStage()
        with patch(
            "services.allocation_submission.build_allocation_client",
            return_value=budgeted(stage),
        ):
            first = process_submission(first_id, owner="worker-first")
            second = process_submission(second_id, owner="worker-second")

        self.assertEqual(first.status, "succeeded", first.error_code)
        self.assertEqual(second.status, "failed")
        self.assertEqual(second.error_code, "ai_spend_reserve_exceeded")
        self.assertEqual(stage.calls, 1)
        self.assertEqual(
            AIUsageLedger.objects.filter(call_key__startswith="allocation:").count(), 1
        )
        self.assertIn("provider was not called", second.error_detail)

    @override_settings(
        CACHES=TEST_CACHES,
        AI_DAILY_SPEND_LIMIT_USD=D("0.40"),
        AI_SPEND_LIMIT_ENFORCED=True,
        ALLOCATION_CONFIG={"SUBMISSION_MONETARY_BUDGET_USD": "0.20"},
    )
    def test_reusing_a_key_for_another_owner_or_request_is_refused_not_replayed(self):
        key = logical_call_key("allocation", "identity-check")
        first = check_and_reserve(
            call_key=key,
            upper_bound_usd=D("0.20"),
            owner="portfolio:1",
            request_key="request-hash-one",
        )
        self.assertTrue(first.allowed)

        same = check_and_reserve(
            call_key=key,
            upper_bound_usd=D("0.20"),
            owner="portfolio:1",
            request_key="request-hash-one",
        )
        self.assertTrue(same.allowed)
        self.assertTrue(same.replay)

        other_owner = check_and_reserve(
            call_key=key,
            upper_bound_usd=D("0.20"),
            owner="portfolio:2",
            request_key="request-hash-one",
        )
        self.assertFalse(other_owner.allowed)
        self.assertEqual(other_owner.reason, "ai_spend_reservation_conflict")

        other_request = check_and_reserve(
            call_key=key,
            upper_bound_usd=D("0.20"),
            owner="portfolio:1",
            request_key="request-hash-two",
        )
        self.assertFalse(other_request.allowed)
        self.assertEqual(other_request.reason, "ai_spend_reservation_conflict")

        self.assertEqual(AIUsageLedger.objects.filter(call_key=key).count(), 1)
        self.assertEqual(budget_state().reserved_usd, D("0.200000"))


class ExperimentAllowanceTests(TestCase):
    """9B: the registered ceiling reaches the runner and blocks before dispatch."""

    def setUp(self):
        self.user = make_user("experiment-allowance-user")
        self.portfolio = make_portfolio(self.user, balance="10000.00")
        self.opportunity_set = load_opportunity_set(OPPORTUNITY_SET_PATH)

    @override_settings(
        AI_DAILY_SPEND_LIMIT_USD=D("5.00"),
        AI_RUN_COST_UPPER_BOUND_USD=D("1.00"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_only_the_first_paid_attempt_dispatches_and_the_rest_stay_visible(self):
        client = BillableOfflineStageClient()
        report = execute_opportunity_set(
            opportunity_set=self.opportunity_set,
            portfolio=self.portfolio,
            provider_mode="stub",
            client=client,
        )

        self.assertEqual(len(client.calls), 1)
        arms = {arm["key"]: arm for arm in report["arms"]}
        self.assertEqual(arms["V1"]["runs"][0]["status"], "completed")
        blocked = [
            run
            for arm in ("V1", "V2", "V3")
            for run in arms[arm]["runs"]
            if run["status"] == "failed"
        ]
        self.assertEqual(len(blocked), 8)
        self.assertEqual({run["failure_code"] for run in blocked}, {"ai_spend_experiment_exceeded"})
        self.assertTrue(
            any("provider was not called" in (run["failure_detail"] or "") for run in blocked)
        )

        self.assertEqual(report["denominators"]["scheduled"], 12)
        self.assertEqual(report["denominators"]["completed"], 4)
        self.assertEqual(report["denominators"]["failed"], 8)
        allowances = report["execution"]["allowances"]
        self.assertEqual(allowances["experiment_ceiling_usd"], "1.00")
        self.assertEqual(allowances["run_upper_bound_usd"], "1.00")
        self.assertEqual(allowances["daily_limit_usd"], "5.000000")
        self.assertEqual(allowances["ledger"]["rows"], 1)
        self.assertEqual(allowances["ledger"]["estimated_usd"], "0.000630")

        # Re-running the same execution reuses the recorded run and cannot spend
        # again: the same key replays its reservation, and a blocked attempt has
        # no new headroom.
        again = execute_opportunity_set(
            opportunity_set=self.opportunity_set,
            portfolio=self.portfolio,
            provider_mode="stub",
            client=client,
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(again["denominators"]["completed"], 4)
        self.assertEqual(again["denominators"]["failed"], 8)
        self.assertEqual(
            AIUsageLedger.objects.filter(call_key__startswith="experiment:").count(), 1
        )


class _FakeLLM:
    """A provider whose counters move but which never reports dollars."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def call(self, prompt: str) -> str:
        self.prompt_tokens += 500
        self.completion_tokens += 300
        return '{"ok": true}'

    def get_token_usage_summary(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class _FakeOrchestrator:
    provider = "anthropic"
    model = "deepseek-reasoner"
    config = {
        "MODEL": "deepseek-reasoner",
        "TEMPERATURE": 0.0,
        "MAX_TOKENS": 4096,
        "REQUEST_TIMEOUT": 30,
    }

    def __init__(self) -> None:
        self.llm = _FakeLLM()

    def build_llm(self):
        return self.llm


class TokenOnlyAdapterSettlementTests(TestCase):
    """9C: the real adapter's token-only result settles as an estimate, not unknown."""

    def setUp(self):
        self.user = make_user("adapter-settlement-user")
        self.portfolio = make_portfolio(self.user)

    def _adapter_usage(self) -> ModelUsage:
        client = CrewAIStageClient(_FakeOrchestrator())
        client.complete("adjudication", "A prompt with no provider dollar figure.")
        usage = client.last_usage
        self.assertIsNotNone(usage)
        self.assertIsNone(usage.cost_usd)
        self.assertTrue(usage.measured_tokens)
        self.assertEqual(usage.input_tokens, 500)
        self.assertEqual(usage.output_tokens, 300)
        self.assertIsNotNone(usage.price_table_cost_usd)
        self.assertTrue(usage.pricing_version)
        return usage

    @staticmethod
    def _result_from(usage: ModelUsage) -> AgentRunResult:
        protocol_usage = ProtocolUsage()
        protocol_usage.calls = 1
        protocol_usage.measured_input_tokens = usage.input_tokens
        protocol_usage.measured_output_tokens = usage.output_tokens
        protocol_usage.price_table_priced_calls = 1
        protocol_usage.price_table_cost_usd = usage.price_table_cost_usd
        protocol_usage.billed_cost_unknown_calls = 1
        protocol_usage.billed_cost_unknown_reason = usage.cost_unknown_reason
        protocol_usage.pricing_version = usage.pricing_version
        outcome = SimpleNamespace(
            proposal=SimpleNamespace(
                action="HOLD",
                amount=0,
                sentiment="NEUTRAL",
                reasoning="The provider reported tokens, not dollars.",
                thesis="",
                invalidation_condition="",
            ),
            usage=protocol_usage,
            run=None,
            recorded=False,
            limits={},
            metadata={},
            cases={},
        )
        orchestrator = AlphaAgentOrchestrator.__new__(AlphaAgentOrchestrator)
        return orchestrator._result_from_outcome(outcome, time.monotonic())

    @override_settings(
        AI_DAILY_SPEND_LIMIT_USD=D("5.00"),
        AI_RUN_COST_UPPER_BOUND_USD=D("1.00"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_a_price_table_estimate_is_recorded_and_the_billed_field_stays_null(self):
        adapter_usage = self._adapter_usage()
        result = self._result_from(adapter_usage)

        expected = adapter_usage.price_table_cost_usd
        self.assertEqual(result.estimated_cost_usd, expected)
        self.assertIsNone(result.api_cost_usd)
        self.assertEqual(result.cost_basis, "price_table_estimate")
        self.assertTrue(result.tokens_measured)
        self.assertEqual(result.measured_input_tokens, 500)
        self.assertEqual(result.measured_output_tokens, 300)
        self.assertTrue(result.pricing_version)
        self.assertEqual(result.cost_unknown_reason, "provider_reported_cost_unavailable")

        key = logical_call_key("decision", "token-only-adapter")
        check_and_reserve(call_key=key, upper_bound_usd=D("1.00"))
        row = reconcile_decision_run(key, result)

        self.assertEqual(row.state, "estimated")
        self.assertIsNone(row.billed_usd)
        self.assertEqual(row.estimated_usd, expected)
        self.assertEqual(row.input_tokens, 500)
        self.assertEqual(row.output_tokens, 300)
        self.assertTrue(row.tokens_measured)
        self.assertEqual(row.pricing_version, result.pricing_version)
        self.assertEqual(row.billed_cost_unknown_reason, "provider_reported_cost_unavailable")
        self.assertEqual(row.reserved_usd, D("0.000000"))

        state = budget_state()
        self.assertEqual(state.spent_usd, D("0.000000"))
        self.assertEqual(state.estimated_spend_usd, expected.quantize(D("0.000001")))
        self.assertEqual(state.unknown_cost_runs, 0)


class ConfirmedChargeTests(TestCase):
    """Step 9.4: a later confirmed charge replaces the estimate, once."""

    def setUp(self):
        make_user("confirmed-charge-user")

    @override_settings(
        AI_DAILY_SPEND_LIMIT_USD=D("5.00"),
        AI_RUN_COST_UPPER_BOUND_USD=D("1.00"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_a_confirmed_charge_replaces_an_estimate_and_releases_the_reservation(self):
        key = logical_call_key("test", "confirmed-after-estimate")
        check_and_reserve(call_key=key, upper_bound_usd=D("0.50"))
        settle_call(call_key=key, estimated_usd=D("0.001500"), input_tokens=500, output_tokens=300)
        before = budget_state()
        self.assertEqual(before.estimated_spend_usd, D("0.001500"))
        self.assertEqual(before.spent_usd, D("0.000000"))

        row = settle_confirmed_charge(
            call_key=key,
            billed_usd=D("0.002000"),
            pricing_version="ai_model_pricing.v1:test",
            metadata={"invoice": "provider-invoice-1"},
        )
        self.assertEqual(row.state, "billed")
        self.assertEqual(row.billed_usd, D("0.002000"))
        self.assertIsNone(row.estimated_usd)
        self.assertEqual(row.reserved_usd, D("0.000000"))
        self.assertEqual(row.revision, 1)
        self.assertEqual(row.metadata["previous_state"], "estimated")
        self.assertEqual(row.metadata["previous_estimated_usd"], "0.001500")

        after = budget_state()
        self.assertEqual(after.spent_usd, D("0.002000"))
        self.assertEqual(after.estimated_spend_usd, D("0.000000"))
        self.assertEqual(after.reserved_usd, D("0.000000"))
        self.assertEqual(after.committed_usd, D("0.002000"))
        self.assertEqual(AIUsageLedger.objects.filter(call_key=key).count(), 1)

        # A redelivered confirmation is not a second charge.
        again = settle_confirmed_charge(call_key=key, billed_usd=D("0.002000"))
        self.assertEqual(again.billed_usd, D("0.002000"))
        self.assertEqual(again.revision, 1)
        final = budget_state()
        self.assertEqual(final.committed_usd, D("0.002000"))
        self.assertEqual(AIUsageLedger.objects.filter(call_key=key).count(), 1)

    @override_settings(
        AI_DAILY_SPEND_LIMIT_USD=D("5.00"),
        AI_RUN_COST_UPPER_BOUND_USD=D("1.00"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_a_confirmed_charge_also_closes_an_unknown_outcome(self):
        key = logical_call_key("test", "confirmed-after-unknown")
        check_and_reserve(call_key=key, upper_bound_usd=D("0.50"))
        mark_unknown(call_key=key, reason="provider_reported_no_usage")
        self.assertEqual(budget_state().reserved_usd, D("0.500000"))

        row = settle_confirmed_charge(call_key=key, billed_usd=D("0.004250"))
        self.assertEqual(row.state, "billed")
        self.assertEqual(row.billed_usd, D("0.004250"))
        self.assertIsNone(row.estimated_usd)
        self.assertEqual(row.reserved_usd, D("0.000000"))
        self.assertEqual(row.metadata["previous_state"], "unknown")
        self.assertEqual(row.metadata["previous_unknown_reason"], "provider_reported_no_usage")

        state = budget_state()
        self.assertEqual(state.reserved_usd, D("0.000000"))
        self.assertEqual(state.unknown_reserved_usd, D("0.000000"))
        self.assertEqual(state.unknown_cost_runs, 0)
        self.assertEqual(state.committed_usd, D("0.004250"))

    @override_settings(
        AI_DAILY_SPEND_LIMIT_USD=D("5.00"),
        AI_RUN_COST_UPPER_BOUND_USD=D("1.00"),
        AI_SPEND_LIMIT_ENFORCED=True,
    )
    def test_a_confirmed_charge_without_a_reservation_is_refused(self):
        with self.assertRaises(ValueError):
            settle_confirmed_charge(
                call_key=logical_call_key("test", "never-reserved"),
                billed_usd=D("0.002000"),
            )
