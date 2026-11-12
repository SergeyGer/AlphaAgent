"""FIX-07B: the V0-V3 experiment execution path.

These tests execute the real protocol code with an offline scripted client. They
assert the properties the acceptance review asked for: one frozen opportunity set,
one snapshot per opportunity shared by every arm, genuinely distinct protocol ids,
a denominator that keeps missing and failed runs, and V4 declared but excluded.

They do **not** assert that the returns are meaningful. The prices and the model
replies are authored, so the report says ``engineering_validation_only`` and the
tests assert that label rather than a performance claim.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings

from core.decision_models import DecisionRun, RunMode, RunSource
from core.tests.helpers import make_portfolio, make_user
from services.experiment_execution import (
    ExecutionError,
    execute_opportunity_set,
    load_opportunity_set,
    validate_opportunity_set,
)

OPPORTUNITY_SET_PATH = (
    Path(settings.BASE_DIR) / "docs/spec/fixtures/experiments/alphaagent-arms.v1.json"
)

NO_PROVIDER_CONFIG = {
    "PROVIDER": "none",
    "MODEL": "none",
    "API_KEY": "",
    "ALLOW_HEURISTIC_FALLBACK": True,
}


def load_document() -> dict:
    return json.loads(OPPORTUNITY_SET_PATH.read_text(encoding="utf-8"))


class OpportunitySetValidationTests(TestCase):
    def test_the_committed_opportunity_set_is_valid(self):
        opportunity_set = load_opportunity_set(OPPORTUNITY_SET_PATH)
        self.assertEqual(opportunity_set.opportunity_set_id, "alphaagent-arms.v1")
        self.assertEqual(len(opportunity_set.opportunities), 3)
        self.assertEqual(len(opportunity_set.marks), 9)
        self.assertTrue(opportunity_set.content_hash)

    def test_a_missing_pre_registration_is_refused(self):
        document = load_document()
        document.pop("pre_registration")
        with self.assertRaisesRegex(ExecutionError, "pre_registration is required"):
            validate_opportunity_set(document)

    def test_a_registration_after_the_first_opportunity_is_refused(self):
        document = load_document()
        document["declared_at"] = "2026-02-07T00:00:00Z"
        document["pre_registration"]["registered_at"] = "2026-02-06T00:00:00Z"
        with self.assertRaisesRegex(ExecutionError, "did not precede the run"):
            validate_opportunity_set(document)

    def test_a_registration_after_the_document_is_refused(self):
        document = load_document()
        document["declared_at"] = "2026-02-02T09:00:00Z"
        document["pre_registration"]["registered_at"] = "2026-02-02T10:00:00Z"
        with self.assertRaisesRegex(ExecutionError, "after declared_at"):
            validate_opportunity_set(document)

    def test_v4_cannot_be_marked_implemented(self):
        document = load_document()
        document["arms"]["V4"]["implemented"] = True
        with self.assertRaisesRegex(ExecutionError, "must not be marked implemented"):
            validate_opportunity_set(document)

    def test_an_opportunity_without_a_fill_mark_is_refused(self):
        document = load_document()
        document["marks"] = [
            mark
            for mark in document["marks"]
            if not (mark["ticker"] == "AAPL" and mark["at"] >= "2026-02-03T14:30:00Z")
        ]
        with self.assertRaisesRegex(ExecutionError, "no mark at or after"):
            validate_opportunity_set(document)


class ExecutionPathTests(TestCase):
    def setUp(self):
        self.user = make_user("execution-user")
        self.portfolio = make_portfolio(self.user, balance="10000.00")
        self.opportunity_set = load_opportunity_set(OPPORTUNITY_SET_PATH)

    def execute(self, **kwargs):
        return execute_opportunity_set(
            opportunity_set=self.opportunity_set,
            portfolio=self.portfolio,
            provider_mode=kwargs.pop("provider_mode", "stub"),
            **kwargs,
        )

    def test_all_four_arms_run_against_the_same_snapshots(self):
        report = self.execute()
        arms = {arm["key"]: arm for arm in report["arms"]}

        for key in ("V0", "V1", "V2", "V3"):
            self.assertEqual(arms[key]["status"], "executed")
            self.assertEqual(arms[key]["scheduled_runs"], 3)
            self.assertEqual(arms[key]["completed_runs"], 3)
        self.assertEqual(report["denominators"]["scheduled"], 12)
        self.assertEqual(report["denominators"]["completed"], 12)
        self.assertTrue(report["verification"]["identical_snapshots_across_arms"])

        # Every arm names the same snapshot per opportunity, by content hash.
        by_opportunity: dict[str, set[str]] = {}
        for key in ("V0", "V1", "V2", "V3"):
            for snapshot in arms[key]["snapshots"]:
                by_opportunity.setdefault(snapshot["opportunity"], set()).add(
                    snapshot["content_hash"]
                )
        for opportunity, hashes in by_opportunity.items():
            self.assertEqual(len(hashes), 1, f"{opportunity} has more than one snapshot")

    def test_each_arm_records_its_own_distinct_protocol_id(self):
        report = self.execute()
        arms = {arm["key"]: arm for arm in report["arms"]}

        self.assertTrue(report["verification"]["distinct_protocol_ids"])
        self.assertEqual(arms["V0"]["protocol_version"], "heuristic_fallback.v1")
        self.assertEqual(arms["V1"]["protocol_version"], "single_agent_baseline.v1")
        self.assertEqual(arms["V2"]["protocol_version"], "debate.no_rebuttal.v1")
        self.assertEqual(arms["V3"]["protocol_version"], "debate.v1")
        for key in ("V0", "V1", "V2", "V3"):
            self.assertTrue(arms[key]["protocol_matches_declaration"])

        recorded = {
            run.protocol_version
            for run in DecisionRun.objects.filter(
                fixture_id__startswith=self.opportunity_set.opportunity_set_id
            )
        }
        self.assertEqual(
            recorded,
            {
                "heuristic_fallback.v1",
                "single_agent_baseline.v1",
                "debate.no_rebuttal.v1",
                "debate.v1",
            },
        )

    def test_v2_records_no_rebuttal_and_v3_records_one(self):
        self.execute()
        v2 = DecisionRun.objects.filter(fixture_id__startswith="alphaagent-arms.v1.v2.").first()
        v3 = DecisionRun.objects.filter(fixture_id__startswith="alphaagent-arms.v1.v3.").first()
        self.assertIsNotNone(v2)
        self.assertIsNotNone(v3)
        self.assertEqual(v2.rebuttals.count(), 0)
        self.assertGreater(v3.rebuttals.count(), 0)
        self.assertEqual(v2.protocol_metadata["rebuttal_rounds_run"], 0)
        self.assertEqual(v3.protocol_metadata["rebuttal_rounds_run"], 1)

    def test_a_failed_arm_stays_in_the_denominator(self):
        from services.debate_protocol import ProviderError

        class FailingClient:
            provider = "stub"
            model = "deepseek-reasoner"
            last_usage = None

            def __init__(self):
                self.parameters: dict = {}

            def complete(self, stage, prompt):
                raise ProviderError("the scripted provider failed", stage="analysing")

        report = self.execute(client=FailingClient())
        arms = {arm["key"]: arm for arm in report["arms"]}
        self.assertEqual(arms["V0"]["status"], "executed")
        for key in ("V1", "V2", "V3"):
            self.assertEqual(arms[key]["status"], "failed")
            self.assertEqual(arms[key]["failed_runs"], 3)
            self.assertEqual(arms[key]["runs"][0]["failure_code"], "provider_error")
        self.assertEqual(report["denominators"]["scheduled"], 12)
        self.assertEqual(report["denominators"]["completed"], 3)
        self.assertEqual(report["denominators"]["failed"], 9)
        self.assertEqual(report["denominators"]["not_executed"], 0)

    def test_v4_is_declared_and_excluded_from_the_denominator(self):
        report = self.execute()
        v4 = next(arm for arm in report["arms"] if arm["key"] == "V4")
        self.assertFalse(v4["implemented"])
        self.assertTrue(v4["excluded_from_denominator"])
        self.assertEqual(v4["status"], "not_executed")
        self.assertIn("not executed", v4["reason"])
        self.assertFalse(v4["primary_metric"]["in_denominator"])
        self.assertEqual(report["denominators"]["excluded_unimplemented_arms"], ["V4"])
        self.assertNotIn("V4", report["denominators"]["implemented_arms"])
        self.assertEqual(
            sum(1 for arm in report["arms"] if arm["implemented"] and arm["status"] == "executed"),
            4,
        )

    def test_each_arm_has_its_own_fills_and_trajectory(self):
        report = self.execute()
        arms = {arm["key"]: arm for arm in report["arms"]}
        for key in ("V0", "V1", "V2", "V3"):
            trajectory = arms[key]["trajectory"]
            self.assertIsNotNone(trajectory)
            self.assertEqual(trajectory["provenance"]["simulated_arm"], key)
            run_ids = {row["decision_run_id"] for row in trajectory["orders"]}
            arm_run_ids = {run["decision_run_id"] for run in arms[key]["runs"]}
            self.assertEqual(run_ids, arm_run_ids)

        # V0 (momentum) buys AAPL only; V2 (no-rebuttal script) buys all three.
        self.assertEqual(len(arms["V0"]["trajectory"]["fills"]), 1)
        self.assertEqual(len(arms["V2"]["trajectory"]["fills"]), 3)
        self.assertNotEqual(arms["V0"]["trajectory"]["fills"], arms["V2"]["trajectory"]["fills"])
        # V1 and V3 reply HOLD in the scripted configuration: no fills.
        self.assertEqual(len(arms["V1"]["trajectory"]["fills"]), 0)
        self.assertEqual(len(arms["V3"]["trajectory"]["fills"]), 0)

    def test_costs_keep_measured_estimated_and_unknown_apart(self):
        report = self.execute()
        cost = report["cost_breakdown"]
        # A scripted provider reports no usage summary. The protocol still writes
        # the counts it charged against to the run row, and the report falls back to
        # them rather than showing a measured zero beside a non-zero estimate -
        # which is the defect this assertion used to encode.
        measured = cost["measured_tokens"]
        self.assertGreater(measured["input"], 0)
        self.assertGreater(measured["output"], 0)
        self.assertGreater(cost["estimated_tokens"]["input"], 0)
        self.assertGreater(cost["estimated_tokens"]["output"], 0)
        self.assertIsNotNone(cost["price_table_cost_usd"])
        # No provider was called, so no charge exists. Null, not zero.
        self.assertIsNone(cost["billed_cost_usd"])
        self.assertEqual(cost["billed_cost_unknown_runs"], 9)
        self.assertEqual(
            cost["billed_cost_unknown_reasons"], {"scripted_provider_made_no_charge": 9}
        )
        self.assertFalse(cost["strict_billed_ceiling"])
        for arm in report["arms"]:
            if arm["key"] == "V4":
                continue
            for run in arm["runs"]:
                self.assertTrue(run["cost"]["estimated_tokens"]["input"] > 0 or arm["key"] == "V0")

    def test_a_deterministic_arm_records_a_measured_zero_llm_cost(self):
        report = self.execute()
        v0 = next(arm for arm in report["arms"] if arm["key"] == "V0")
        self.assertEqual(v0["cost"]["billed_cost_usd"], "0.000000")
        self.assertEqual(v0["cost"]["billed_cost_unknown_runs"], 0)
        self.assertEqual(v0["cost"]["measured_tokens"], {"input": 0, "output": 0})
        self.assertEqual(v0["cost"]["estimated_tokens"], {"input": 0, "output": 0})
        run = DecisionRun.objects.filter(fixture_id__startswith="alphaagent-arms.v1.v0.").first()
        self.assertEqual(run.total_cost_usd, Decimal("0"))
        self.assertEqual(run.cost_basis, "deterministic_no_model_call")
        # A known zero cost is not an unknown one: the net series is computable.
        self.assertIsNotNone(v0["trajectory"]["net_of_llm_return_pct"])
        self.assertEqual(
            v0["trajectory"]["net_of_llm_return_pct"],
            v0["trajectory"]["time_weighted_return_pct"],
        )

    def test_the_scripted_provider_is_labelled_simulated(self):
        report = self.execute()
        self.assertTrue(report["honesty"]["simulated_provider"])
        self.assertTrue(report["honesty"]["engineering_validation_only"])
        self.assertTrue(report["honesty"]["no_profitability_claim"])
        self.assertFalse(report["honesty"]["comparative_protocol_claim"])
        for run in DecisionRun.objects.filter(
            fixture_id__startswith="alphaagent-arms.v1.v1."
        ) | DecisionRun.objects.filter(fixture_id__startswith="alphaagent-arms.v1.v2."):
            self.assertEqual(run.mode, RunMode.SIMULATED)
            self.assertEqual(run.source, RunSource.STUB)

    def test_rerunning_reuses_recorded_runs_instead_of_duplicating_them(self):
        first = self.execute()
        before = DecisionRun.objects.filter(
            fixture_id__startswith=self.opportunity_set.opportunity_set_id
        ).count()
        second = self.execute()
        after = DecisionRun.objects.filter(
            fixture_id__startswith=self.opportunity_set.opportunity_set_id
        ).count()
        self.assertEqual(before, after)
        self.assertEqual(first["denominators"]["scheduled"], second["denominators"]["scheduled"])
        self.assertEqual(first["denominators"]["completed"], second["denominators"]["completed"])
        # The report is rebuilt from stored runs; the run identities do not change.
        first_ids = {
            arm["key"]: sorted(run["decision_run_id"] or "" for run in arm["runs"])
            for arm in first["arms"]
        }
        second_ids = {
            arm["key"]: sorted(run["decision_run_id"] or "" for run in arm["runs"])
            for arm in second["arms"]
        }
        self.assertEqual(first_ids, second_ids)
        first_snapshots = {
            snapshot["opportunity"]: snapshot["snapshot_id"]
            for arm in first["arms"]
            for snapshot in arm["snapshots"]
        }
        second_snapshots = {
            snapshot["opportunity"]: snapshot["snapshot_id"]
            for arm in second["arms"]
            for snapshot in arm["snapshots"]
        }
        self.assertEqual(first_snapshots, second_snapshots)

    def test_the_registered_metric_is_computed_when_the_horizon_mark_exists(self):
        report = self.execute()
        result = report["pre_registration"]["primary_metric_result"]
        self.assertEqual(result["id"], "mean_action_score_pct")
        self.assertEqual(result["status"], "computed")
        self.assertIsNotNone(result["value"])
        self.assertGreater(result["denominator"]["usable"], 0)
        self.assertIsNotNone(result["dispersion"]["std_dev_pct"])
        # The TSLA 7-day target falls after the window's last mark, so that
        # observation is excluded with its input named, not scored as a zero.
        self.assertLess(result["denominator"]["usable"], result["denominator"]["scheduled"])
        self.assertIn("no_session_at_or_after_target", result["denominator"]["excluded_by_reason"])
        self.assertTrue(report["pre_registration"]["sample_gate"]["met"])

    @override_settings(AI_CONFIG=NO_PROVIDER_CONFIG)
    def test_an_arm_without_a_provider_stays_not_executed_and_in_the_denominator(self):
        report = self.execute(provider_mode="auto")
        arms = {arm["key"]: arm for arm in report["arms"]}
        self.assertEqual(arms["V0"]["status"], "executed")
        for key in ("V1", "V2", "V3"):
            self.assertEqual(arms[key]["status"], "not_executed")
            self.assertEqual(arms[key]["not_executed_runs"], 3)
            self.assertEqual(arms[key]["runs"][0]["failure_code"], "provider_not_configured")
        self.assertEqual(report["denominators"]["scheduled"], 12)
        self.assertEqual(report["denominators"]["completed"], 3)
        self.assertEqual(report["denominators"]["not_executed"], 9)
        self.assertFalse(report["honesty"]["simulated_provider"])
        # No synthetic case was substituted for the arms that could not run.
        self.assertEqual(
            DecisionRun.objects.filter(fixture_id__startswith="alphaagent-arms.v1.v1.").count(),
            0,
        )

    def test_a_registration_dated_after_the_execution_is_refused(self):
        from django.utils import timezone

        future = replace(
            self.opportunity_set,
            pre_registration={
                **self.opportunity_set.pre_registration,
                "registered_at": (timezone.now() + timedelta(days=1))
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
        with self.assertRaisesRegex(ExecutionError, "in the future"):
            execute_opportunity_set(
                opportunity_set=future,
                portfolio=self.portfolio,
                provider_mode="stub",
            )

    def test_the_report_is_reproducible_from_its_contents_hash(self):
        from core.decision_json import content_hash

        report = self.execute()
        payload = {key: value for key, value in report.items() if key != "contents_hash"}
        self.assertEqual(report["contents_hash"], content_hash(payload))

    def test_risk_rules_and_accounting_method_are_shared(self):
        report = self.execute()
        fairness = report["fairness"]
        self.assertEqual(fairness["same_risk_rules"]["status"], "match")
        self.assertTrue(fairness["same_execution_assumptions"]["applied_by_all_arms"])
        self.assertEqual(fairness["equal_opportunity"]["opportunities_per_implemented_arm"], 3)
        self.assertTrue(fairness["equal_opportunity"]["v4_excluded"])
