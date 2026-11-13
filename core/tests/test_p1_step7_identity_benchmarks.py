"""P1 steps 7: attempt identity, retained reruns and matched benchmarks.

The defects these tests exercise were reproduced against the pre-fix tree:

* with two resolved models (``OfflineStageClient(model="model-a")`` and
  ``model="model-b"``) the second execution reused all 12 recorded runs and the
  report carried no resolved provider/model/parameter/budget record at all;
* ``execute_opportunity_set(force=True)`` deleted the earlier ``DecisionRun``
  and its decision, guard and event rows (the old run id no longer resolved);
* the registered relative metric returned ``benchmark_marks_not_recorded`` even
  when the opportunity set supplied benchmark marks, and no row carried the
  benchmark's entry/horizon observations, timestamps or matching policy.

The tests hold the repair: a resolved attempt identity, a forced rerun that
appends an attempt instead of deleting, and a relative metric computed only from
matched benchmark windows with every exclusion published by reason.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from core.decision_models import DecisionRecord, DecisionRun, GuardRecord, RunEvent
from core.tests.helpers import make_portfolio, make_user
from services.debate_protocol import ProtocolBudget
from services.experiment_execution import (
    Opportunity,
    execute_opportunity_set,
    validate_opportunity_set,
)
from services.experiment_simulation import (
    BENCHMARK_MATCH_POLICY,
    DecisionInput,
    SnapshotInput,
    compute_registered_metric,
)
from services.portfolio_accounting import PriceMark
from services.stub_stage_client import OfflineStageClient

OPPORTUNITY_SET_PATH = (
    Path(settings.BASE_DIR) / "docs/spec/fixtures/experiments/alphaagent-arms.v1.json"
)


def load_document() -> dict:
    return json.loads(OPPORTUNITY_SET_PATH.read_text(encoding="utf-8"))


def parse_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def run(document: dict, portfolio, **kwargs) -> dict:
    return execute_opportunity_set(
        opportunity_set=validate_opportunity_set(copy.deepcopy(document)),
        portfolio=portfolio,
        **kwargs,
    )


def run_ids(report: dict) -> set[str]:
    return {
        run["decision_run_id"]
        for arm in report["arms"]
        for run in arm["runs"]
        if run.get("decision_run_id")
    }


class ResolvedAttemptIdentityTests(TestCase):
    """7A - changed resolved configuration is a distinct attempt identity."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("step7a"), balance="10000.00")
        self.document = load_document()

    def test_two_resolved_models_and_a_changed_budget_get_distinct_identities(self):
        model_a = OfflineStageClient(model="model-a")
        model_b = OfflineStageClient(model="model-b")
        first = run(self.document, self.portfolio, client=model_a, provider_mode="auto")
        second = run(self.document, self.portfolio, client=model_b, provider_mode="auto")

        first_ids, second_ids = run_ids(first), run_ids(second)
        # V0 is deterministic: its identity does not depend on a model, and its
        # three runs are correctly reused. The model-driven arms are disjoint.
        self.assertEqual(len(first_ids), 12)
        self.assertEqual(len(second_ids), 12)
        self.assertEqual(len(first_ids & second_ids), 3)
        first_v1 = next(arm for arm in first["arms"] if arm["key"] == "V1")
        second_v1 = next(arm for arm in second["arms"] if arm["key"] == "V1")
        self.assertTrue(
            {run["decision_run_id"] for run in first_v1["runs"]}.isdisjoint(
                {run["decision_run_id"] for run in second_v1["runs"]}
            )
        )
        identity_a = first_v1["runs"][0]["attempt_identity"]
        identity_b = second_v1["runs"][0]["attempt_identity"]
        self.assertEqual(identity_a["model"], "model-a")
        self.assertEqual(identity_b["model"], "model-b")
        self.assertNotEqual(identity_a["identity_hash"], identity_b["identity_hash"])
        self.assertEqual(identity_a["provider"], "stub")
        self.assertEqual(identity_a["protocol_version"], "single_agent_baseline.v1")
        self.assertIn("monetary_budget_usd", identity_a["budget"])
        self.assertNotEqual(
            first["execution"]["configuration_id"],
            second["execution"]["configuration_id"],
        )
        self.assertEqual(
            first["verification"]["configuration_id"], first["execution"]["configuration_id"]
        )

        # An exact repeat of model-b reuses its own attempt instead of spending
        # again or duplicating rows.
        repeat = run(self.document, self.portfolio, client=model_b, provider_mode="auto")
        self.assertEqual(run_ids(repeat), second_ids)
        self.assertTrue(
            all(
                row["reused"] for arm in repeat["arms"] if arm["key"] != "V4" for row in arm["runs"]
            )
        )

        # A different declared budget is a different configuration, even for the
        # same provider and model.
        budgeted = run(
            self.document,
            self.portfolio,
            client=model_a,
            provider_mode="auto",
            budget=ProtocolBudget(monetary_budget_usd=Decimal("0.25")),
        )
        budgeted_v1 = next(arm for arm in budgeted["arms"] if arm["key"] == "V1")
        budget_identity = budgeted_v1["runs"][0]["attempt_identity"]
        self.assertEqual(budget_identity["model"], "model-a")
        self.assertEqual(budget_identity["budget"]["monetary_budget_usd"], "0.25")
        self.assertNotEqual(budget_identity["identity_hash"], identity_a["identity_hash"])
        self.assertNotEqual(
            budgeted["execution"]["configuration_id"],
            first["execution"]["configuration_id"],
        )
        self.assertEqual(len(run_ids(first) & run_ids(budgeted)), 3)  # V0 only
        self.assertEqual(DecisionRun.objects.filter(portfolio=self.portfolio).count(), 30)

        # Every identity names the run key and the attempt ordinal it belongs to.
        for arm in budgeted["arms"]:
            if arm["key"] == "V4":
                continue
            for row in arm["runs"]:
                self.assertEqual(row["attempt"], 1)
                self.assertEqual(row["fixture_id"].split(".")[-1], f"a{row['attempt']}")


class ForcedRerunRetainsEarlierWorkTests(TestCase):
    """7B - a forced rerun appends an attempt; earlier references still resolve."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("step7b"), balance="10000.00")
        self.document = load_document()

    def test_forced_rerun_creates_a_new_attempt_and_keeps_the_old_one_resolvable(self):
        first_report = run(self.document, self.portfolio, provider_mode="stub")
        v2 = next(arm for arm in first_report["arms"] if arm["key"] == "V2")
        first_run_row = v2["runs"][0]
        run_id = first_run_row["decision_run_id"]
        first_run = DecisionRun.objects.get(pk=run_id)
        decisions_before = DecisionRecord.objects.filter(run=first_run).count()
        guards_before = GuardRecord.objects.filter(proposal__run=first_run).count()
        events_before = RunEvent.objects.filter(run=first_run).count()
        self.assertGreaterEqual(decisions_before, 1)
        self.assertGreaterEqual(guards_before, 1)
        self.assertGreater(events_before, 0)

        second_report = run(self.document, self.portfolio, provider_mode="stub", force=True)
        second_v2 = next(arm for arm in second_report["arms"] if arm["key"] == "V2")
        second_run_row = second_v2["runs"][0]

        # The earlier attempt is still there, with its decision, guard evaluations
        # and events. Only the report's reference moved to the new attempt.
        self.assertTrue(DecisionRun.objects.filter(pk=run_id).exists())
        self.assertEqual(DecisionRecord.objects.filter(run=first_run).count(), decisions_before)
        self.assertEqual(GuardRecord.objects.filter(proposal__run=first_run).count(), guards_before)
        self.assertEqual(RunEvent.objects.filter(run=first_run).count(), events_before)

        # The first report's references all still resolve after the rerun.
        self.assertTrue(DecisionRun.objects.filter(pk=first_run_row["decision_run_id"]).exists())

        self.assertNotEqual(second_run_row["decision_run_id"], run_id)
        self.assertEqual(second_run_row["attempt"], 2)
        self.assertEqual(
            second_run_row["attempt_identity"]["identity_hash"],
            first_run_row["attempt_identity"]["identity_hash"],
        )
        self.assertEqual(DecisionRun.objects.filter(portfolio=self.portfolio).count(), 24)

        # An unforced repeat reuses attempt 2, not attempt 1 and not a new row.
        third_report = run(self.document, self.portfolio, provider_mode="stub")
        third_v2 = next(arm for arm in third_report["arms"] if arm["key"] == "V2")
        self.assertEqual(third_v2["runs"][0]["decision_run_id"], second_run_row["decision_run_id"])
        self.assertTrue(third_v2["runs"][0]["reused"])
        self.assertEqual(DecisionRun.objects.filter(portfolio=self.portfolio).count(), 24)


class BenchmarkWindowMatchingTests(TestCase):
    """7C - only a matching benchmark window enters the relative result."""

    def setUp(self):
        self.benchmark_marks = [
            {"ticker": "SPY", "at": "2026-02-03T20:00:00Z", "price": "400.00"},
            {"ticker": "SPY", "at": "2026-02-04T20:00:00Z", "price": "402.00"},
            {"ticker": "SPY", "at": "2026-02-10T20:00:00Z", "price": "410.00"},
            {"ticker": "SPY", "at": "2026-02-12T20:00:00Z", "price": "420.00"},
        ]

    def _decision(self, key: str, when: str) -> DecisionInput:
        snapshot = SnapshotInput(
            id=f"snapshot-{key}",
            content_hash=f"hash-{key}",
            portfolio_version="v1",
            analysis_cutoff=parse_at(when),
            captured_at=parse_at(when),
            quotes={
                "AAPL": {
                    "price_usd": "100.00",
                    "as_of": when,
                    "source": "declared_fixture_quote",
                }
            },
        )
        return DecisionInput(
            run_id=f"run-{key}",
            opportunity_key=key,
            ticker="AAPL",
            decision_at=parse_at(when),
            action="BUY",
            requested_quantity=Decimal("1"),
            rationale="step 7C",
            snapshot=snapshot,
        )

    def test_matching_mismatched_and_missing_benchmark_windows_are_separated(self):
        matched = self._decision("matched", "2026-02-03T14:30:00Z")
        mismatched = self._decision("mismatched", "2026-02-04T14:30:00Z")
        missing = self._decision("missing", "2026-02-12T14:30:00Z")
        opportunities = [
            Opportunity(
                key=decision.opportunity_key,
                ticker="AAPL",
                scheduled_at=decision.decision_at,
                snapshot={},
            )
            for decision in (matched, mismatched, missing)
        ]
        marks = [
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-10T20:00:00Z"),
                price=Decimal("110"),
                source="declared_instrument_mark",
            ),
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-11T20:00:00Z"),
                price=Decimal("100"),
                source="declared_instrument_mark",
            ),
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-19T20:00:00Z"),
                price=Decimal("100"),
                source="declared_instrument_mark",
            ),
        ]
        benchmark_marks = [
            PriceMark(
                ticker="SPY",
                at=parse_at(payload["at"]),
                price=Decimal(payload["price"]),
                source="declared_benchmark_mark",
            )
            for payload in self.benchmark_marks
        ]
        result = compute_registered_metric(
            metric={
                "id": "mean_relative_forward_return_pct",
                "family": "signal_diagnostics",
                "horizon_days": 7,
                "direction": "higher_is_better",
            },
            opportunities=opportunities,
            marks=marks,
            decisions_by_arm={"V0": [matched, mismatched, missing]},
            benchmark={"ticker": "SPY", "asset_class": "equity"},
            benchmark_marks=benchmark_marks,
        )

        v0_rows = {row["opportunity"]: row for row in result["by_arm"]["V0"]["observations_detail"]}
        self.assertEqual(v0_rows["matched"]["benchmark"]["status"], "matched")
        self.assertEqual(v0_rows["matched"]["benchmark"]["matching_policy"], BENCHMARK_MATCH_POLICY)
        self.assertEqual(
            v0_rows["matched"]["benchmark"]["entry"]["observed_at"],
            "2026-02-03T20:00:00Z",
        )
        self.assertEqual(
            v0_rows["matched"]["benchmark"]["horizon"]["observed_at"],
            "2026-02-10T20:00:00Z",
        )
        self.assertEqual(
            v0_rows["matched"]["benchmark"]["entry"]["source"], "declared_benchmark_mark"
        )
        self.assertEqual(v0_rows["mismatched"]["benchmark"]["status"], "excluded")
        self.assertEqual(
            v0_rows["mismatched"]["benchmark"]["reason"],
            "benchmark_horizon_session_mismatch",
        )
        self.assertEqual(v0_rows["missing"]["benchmark"]["status"], "excluded")
        self.assertEqual(
            v0_rows["missing"]["benchmark"]["reason"],
            "benchmark_horizon_observation_missing",
        )

        # Only the matched pair contributes to the relative value.
        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["value"], "7.5000")
        self.assertEqual(result["denominator"]["usable"], 1)
        self.assertEqual(result["denominator"]["scheduled"], 12)
        self.assertEqual(result["benchmark_pairs"]["matched_pairs"], 1)
        self.assertEqual(result["denominator"]["by_state"]["evaluated"], 1)
        self.assertEqual(result["denominator"]["by_state"]["missing"], 2)
        self.assertIn(
            "benchmark_horizon_session_mismatch",
            result["denominator"]["excluded_by_reason"],
        )
        self.assertIn(
            "benchmark_horizon_observation_missing",
            result["denominator"]["excluded_by_reason"],
        )

    def test_a_document_with_no_benchmark_marks_names_the_missing_input(self):
        result = compute_registered_metric(
            metric={
                "id": "mean_relative_forward_return_pct",
                "family": "signal_diagnostics",
                "horizon_days": 7,
                "direction": "higher_is_better",
            },
            opportunities=[],
            marks=[],
            decisions_by_arm={},
            benchmark={"ticker": "SPY"},
            benchmark_marks=[],
        )
        self.assertEqual(result["status"], "not_computable")
        self.assertIsNone(result["value"])
        self.assertIn("benchmark", result["missing_input"])


class ObservationStateTests(TestCase):
    """7D - pending, failed, budget-blocked and missing are separate states."""

    def test_observation_states_are_counted_separately(self):
        def decision(key: str, when: str) -> DecisionInput:
            snapshot = SnapshotInput(
                id=f"snapshot-{key}",
                content_hash=f"hash-{key}",
                portfolio_version="v1",
                analysis_cutoff=parse_at(when),
                captured_at=parse_at(when),
                quotes={
                    "AAPL": {
                        "price_usd": "100.00",
                        "as_of": when,
                        "source": "declared_fixture_quote",
                    }
                },
            )
            return DecisionInput(
                run_id=f"run-{key}",
                opportunity_key=key,
                ticker="AAPL",
                decision_at=parse_at(when),
                action="BUY",
                requested_quantity=Decimal("1"),
                rationale="7D",
                snapshot=snapshot,
            )

        opportunities = [
            Opportunity(
                key="pending",
                ticker="AAPL",
                scheduled_at=parse_at("2026-01-20T14:30:00Z"),
                snapshot={},
            ),
            Opportunity(
                key="failed",
                ticker="AAPL",
                scheduled_at=parse_at("2026-01-21T14:30:00Z"),
                snapshot={},
            ),
            Opportunity(
                key="budget",
                ticker="AAPL",
                scheduled_at=parse_at("2026-01-22T14:30:00Z"),
                snapshot={},
            ),
            Opportunity(
                key="missing",
                ticker="AAPL",
                scheduled_at=parse_at("2026-01-10T14:30:00Z"),
                snapshot={},
            ),
        ]
        result = compute_registered_metric(
            metric={
                "id": "mean_action_score_pct",
                "family": "signal_diagnostics",
                "horizon_days": 7,
                "direction": "higher_is_better",
            },
            opportunities=opportunities,
            marks=[],
            decisions_by_arm={
                "V0": [
                    decision("pending", "2026-01-20T14:30:00Z"),
                    decision("missing", "2026-01-10T14:30:00Z"),
                ]
            },
            as_of=parse_at("2026-01-25T00:00:00Z"),
            run_states_by_arm={
                "V0": {
                    "failed": {"status": "failed", "failure_code": "provider_error"},
                    "budget": {"status": "failed", "failure_code": "budget_exhausted"},
                }
            },
        )
        states = result["denominator"]["by_state"]
        self.assertEqual(states["pending"], 1)
        self.assertEqual(states["failed"], 1)
        self.assertEqual(states["budget_blocked"], 1)
        self.assertEqual(states["missing"], 1)
        self.assertEqual(states["evaluated"], 0)
        # The other three implemented arms recorded no decision at all.
        self.assertEqual(states["not_recorded"], 12)
        self.assertEqual(sum(states.values()), result["denominator"]["scheduled"])
        rows = {row["opportunity"]: row for row in result["by_arm"]["V0"]["observations_detail"]}
        self.assertEqual(rows["pending"]["observation_state"], "pending")
        self.assertEqual(rows["failed"]["observation_state"], "failed")
        self.assertEqual(rows["budget"]["observation_state"], "budget_blocked")
        self.assertEqual(rows["missing"]["observation_state"], "missing")
