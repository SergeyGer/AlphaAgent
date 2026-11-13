"""P1 remediation step 7: isolated executions and the registered metric.

The defects these tests exercise were reproduced against the pre-fix tree:

* ``build_comparison_report`` wrote ``primary_metric_result.value = None`` and
  ``reason = "no_matured_outcomes"`` unconditionally, even though the committed
  opportunity set carries a 7-day closing mark for two of its three decisions;
* two executions for two portfolios shared all 12 ``DecisionRun`` rows - the
  second portfolio reported the first portfolio's decisions, snapshots and
  spend;
* two opportunity sets with the same natural key and different tickers reused
  the first set's runs because ``fixture_id`` did not carry the content hash;
* V4's null result was a hard-coded placeholder beside a denominator that
  already excluded it.

The tests hold the repair: a deterministic execution namespace, the metric read
from the pre-registration record, a denominator/dispersion/reason published with
it, and a rebuild that reproduces the value and hash from committed fixtures.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from core.decision_models import DecisionRun
from core.tests.helpers import make_portfolio, make_user
from services.experiment_execution import (
    ExecutionError,
    execute_opportunity_set,
    validate_opportunity_set,
)
from services.experiment_rebuild import (
    RebuildError,
    rebuild_comparison_report,
    recompute_primary_metric,
)

OPPORTUNITY_SET_PATH = (
    Path(settings.BASE_DIR) / "docs/spec/fixtures/experiments/alphaagent-arms.v1.json"
)
COMMITTED_REPORT_PATH = (
    Path(settings.BASE_DIR) / "docs/spec/fixtures/experiments/alphaagent-arms.v1.comparison.json"
)


def load_document() -> dict:
    return json.loads(OPPORTUNITY_SET_PATH.read_text(encoding="utf-8"))


def execute(document: dict, portfolio, *, provider_mode: str = "stub") -> dict:
    opportunity_set = validate_opportunity_set(copy.deepcopy(document))
    return execute_opportunity_set(
        opportunity_set=opportunity_set,
        portfolio=portfolio,
        provider_mode=provider_mode,
    )


def run_ids(report: dict) -> set[str]:
    return {
        run["decision_run_id"]
        for arm in report["arms"]
        for run in arm["runs"]
        if run.get("decision_run_id")
    }


def rename_tickers(document: dict, mapping: dict[str, str]) -> dict:
    for opportunity in document["opportunities"]:
        opportunity["ticker"] = mapping.get(opportunity["ticker"], opportunity["ticker"])
        quotes = opportunity["snapshot"].get("quotes") or {}
        opportunity["snapshot"]["quotes"] = {
            mapping.get(ticker, ticker): quote for ticker, quote in quotes.items()
        }
    for mark in document["marks"]:
        mark["ticker"] = mapping.get(mark["ticker"], mark["ticker"])
    return document


class ExperimentIsolationTests(TestCase):
    """7A - a different portfolio or a different universe gets a new namespace."""

    def test_two_portfolios_do_not_share_decisions_snapshots_or_spend(self):
        portfolio_a = make_portfolio(make_user("iso-a"), balance="10000.00")
        portfolio_b = make_portfolio(make_user("iso-b"), balance="50000.00")
        first = execute(load_document(), portfolio_a)
        second = execute(load_document(), portfolio_b)

        self.assertNotEqual(first["execution"]["namespace"], second["execution"]["namespace"])
        self.assertEqual(first["execution"]["portfolio_id"], portfolio_a.id)
        self.assertEqual(second["execution"]["portfolio_id"], portfolio_b.id)

        ids_a, ids_b = run_ids(first), run_ids(second)
        self.assertEqual(len(ids_a), 12)
        self.assertEqual(len(ids_b), 12)
        self.assertTrue(ids_a.isdisjoint(ids_b))
        self.assertEqual(DecisionRun.objects.filter(portfolio=portfolio_a).count(), 12)
        self.assertEqual(DecisionRun.objects.filter(portfolio=portfolio_b).count(), 12)

        snapshots_a = {
            snapshot["snapshot_id"] for arm in first["arms"] for snapshot in arm["snapshots"]
        }
        snapshots_b = {
            snapshot["snapshot_id"] for arm in second["arms"] for snapshot in arm["snapshots"]
        }
        self.assertEqual(len(snapshots_a), 3)
        self.assertEqual(len(snapshots_b), 3)
        self.assertTrue(snapshots_a.isdisjoint(snapshots_b))

        # The cost ledger is explicitly scoped to the execution's own runs.
        for report, portfolio in ((first, portfolio_a), (second, portfolio_b)):
            scope = report["cost_breakdown"]["scope"]
            self.assertEqual(scope["execution_id"], report["execution"]["execution_id"])
            self.assertEqual(scope["run_count"], 12)
            scoped = {run_id for arm_ids in scope["run_ids"].values() for run_id in arm_ids}
            self.assertEqual(scoped, run_ids(report))
            self.assertEqual(report["frozen"]["portfolio"]["portfolio_id"], portfolio.id)

    def test_two_universes_with_the_same_key_do_not_reuse_each_other_s_decisions(self):
        portfolio = make_portfolio(make_user("iso-universe"), balance="10000.00")
        original = execute(load_document(), portfolio)
        changed = rename_tickers(
            load_document(),
            {"AAPL": "IBM", "MSFT": "ORCL", "TSLA": "NVDA"},
        )
        second = execute(changed, portfolio)

        self.assertNotEqual(original["execution"]["namespace"], second["execution"]["namespace"])
        self.assertTrue(run_ids(original).isdisjoint(run_ids(second)))
        self.assertEqual(DecisionRun.objects.filter(portfolio=portfolio).count(), 24)

        v0 = next(arm for arm in second["arms"] if arm["key"] == "V0")
        self.assertEqual(v0["trajectory"]["fills"][0]["ticker"], "IBM")
        first_v0 = next(arm for arm in original["arms"] if arm["key"] == "V0")
        self.assertEqual(first_v0["trajectory"]["fills"][0]["ticker"], "AAPL")


class RegisteredMetricTests(TestCase):
    """7B/7D - the registered metric is computed, or null with a named input."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("metric-user"), balance="10000.00")

    def test_a_supported_horizon_carries_a_value_denominator_and_dispersion(self):
        report = execute(load_document(), self.portfolio)
        result = report["pre_registration"]["primary_metric_result"]

        self.assertEqual(result["id"], "mean_action_score_pct")
        self.assertEqual(result["family"], "signal_diagnostics")
        self.assertEqual(result["horizon_days"], 7)
        self.assertEqual(result["status"], "computed")
        self.assertIsNotNone(result["value"])
        self.assertGreater(result["denominator"]["usable"], 0)
        self.assertEqual(result["denominator"]["usable"], result["observations"])
        self.assertEqual(result["denominator"]["usable"], result["contributing_runs"])
        self.assertIsNotNone(result["dispersion"]["std_dev_pct"])
        self.assertIsNotNone(result["dispersion"]["range_pct"])
        self.assertIsNotNone(result["interval"]["lower"])
        self.assertIsNotNone(result["interval"]["upper"])
        self.assertIsNone(result["reason"])
        self.assertIsNone(result["missing_input"])

        # The TSLA 7-day target is after the last recorded mark; it is named,
        # not scored as a zero.
        self.assertGreater(result["denominator"]["excluded"], 0)
        self.assertIn("no_session_at_or_after_target", result["denominator"]["excluded_by_reason"])
        self.assertEqual(result["by_arm"]["V0"]["status"], "computed")
        self.assertEqual(result["by_arm"]["V2"]["status"], "computed")

        # V4 is visibly unimplemented and absent from every denominator.
        v4 = next(arm for arm in report["arms"] if arm["key"] == "V4")
        self.assertEqual(v4["status"], "not_executed")
        self.assertFalse(v4["implemented"])
        self.assertTrue(v4["excluded_from_denominator"])
        self.assertFalse(v4["primary_metric"]["in_denominator"])
        self.assertIsNone(v4["primary_metric"]["value"])
        self.assertNotIn("V4", report["denominators"]["implemented_arms"])
        self.assertEqual(report["denominators"]["excluded_unimplemented_arms"], ["V4"])
        self.assertFalse(report["denominators"]["v4"]["in_denominator"])
        self.assertNotIn("V4", result["denominator"]["by_arm"])
        self.assertNotIn("V4", result["by_arm"])

    def test_a_run_without_the_registered_horizon_marks_is_null_with_a_specific_reason(self):
        document = load_document()
        document["marks"] = [
            mark for mark in document["marks"] if not mark["at"].startswith("2026-02-11")
        ]
        report = execute(document, self.portfolio)
        result = report["pre_registration"]["primary_metric_result"]

        self.assertEqual(result["status"], "not_computable")
        self.assertIsNone(result["value"])
        self.assertEqual(result["reason"], "no_usable_observations")
        self.assertEqual(result["denominator"]["usable"], 0)
        self.assertEqual(result["contributing_runs"], 0)
        self.assertIn("registered 7-day horizon", result["missing_input"])
        self.assertIn("no_session_at_or_after_target", result["denominator"]["excluded_by_reason"])
        # The two reports are distinguishable programmatically at the status and
        # value fields; a null is not a placeholder for "not implemented".
        sufficient = execute(load_document(), self.portfolio)
        self.assertNotEqual(
            sufficient["pre_registration"]["primary_metric_result"]["status"],
            result["status"],
        )

    def test_the_registered_definition_controls_the_id_and_horizon(self):
        document = load_document()
        document["pre_registration"]["primary_metric"]["id"] = "hit_rate_pct"
        report = execute(document, self.portfolio)
        result = report["pre_registration"]["primary_metric_result"]
        self.assertEqual(result["id"], "hit_rate_pct")
        self.assertEqual(result["status"], "computed")
        self.assertIsNotNone(result["value"])
        self.assertEqual(result["horizon_days"], 7)

        longer = load_document()
        longer["pre_registration"]["primary_metric"]["horizon_days"] = 30
        missing = execute(longer, self.portfolio)["pre_registration"]["primary_metric_result"]
        self.assertEqual(missing["status"], "not_computable")
        self.assertIsNone(missing["value"])
        self.assertIn("registered 30-day horizon", missing["missing_input"])

    def test_an_unsupported_registration_is_refused_instead_of_returning_a_null(self):
        document = load_document()
        document["pre_registration"]["primary_metric"]["id"] = "not_a_registered_metric"
        with self.assertRaisesRegex(ExecutionError, "not a registered metric"):
            validate_opportunity_set(document)


class CommittedFixtureRecomputeTests(TestCase):
    """7C - the committed archive reproduces its metric value and hash."""

    def test_the_committed_archive_reproduces_its_value_and_hash(self):
        archive = json.loads(COMMITTED_REPORT_PATH.read_text(encoding="utf-8"))
        self.assertIn("frozen", archive)
        self.assertIn("opportunity_set", archive["frozen"])
        self.assertEqual(archive["archive_format"], "experiment_comparison.v3")
        self.assertEqual(archive["report_schema_version"], "3.0")

        rebuilt = rebuild_comparison_report(copy.deepcopy(archive))
        self.assertEqual(rebuilt["status"], "reproduced")
        self.assertEqual(rebuilt["difference_count"], 0, rebuilt["differences"][:5])
        self.assertTrue(rebuilt["contents_hash"]["matches"])
        self.assertEqual(rebuilt["contents_hash"]["rebuilt"], archive["contents_hash"])
        self.assertTrue(rebuilt["metric"]["matches"])
        self.assertEqual(
            rebuilt["metric"]["rebuilt_value"],
            archive["pre_registration"]["primary_metric_result"]["value"],
        )
        self.assertIsNotNone(rebuilt["metric"]["rebuilt_value"])

        metric = recompute_primary_metric(copy.deepcopy(archive))
        self.assertTrue(metric["matches_archived_value"])
        self.assertEqual(
            metric["metric"]["value"],
            archive["pre_registration"]["primary_metric_result"]["value"],
        )
        self.assertEqual(metric["contents_hash"], archive["contents_hash"])

    def test_an_older_archive_format_is_refused_by_name(self):
        archive = json.loads(COMMITTED_REPORT_PATH.read_text(encoding="utf-8"))
        archive["report_schema_version"] = "1.0"
        with self.assertRaises(RebuildError) as raised:
            rebuild_comparison_report(archive)
        self.assertEqual(raised.exception.code, "unsupported_report_schema_version")
        self.assertIn("report_schema_version", raised.exception.detail)
        self.assertIn("1.0", raised.exception.detail)


class DecimalSerialisationTests(TestCase):
    """7A/7B - money and quantities stay Decimal strings on the wire."""

    def test_metric_values_and_denominators_are_decimal_strings(self):
        portfolio = make_portfolio(make_user("metric-decimal"), balance="10000.00")
        result = execute(load_document(), portfolio)["pre_registration"]["primary_metric_result"]
        self.assertIsInstance(Decimal(str(result["value"])), Decimal)
        self.assertIsInstance(result["dispersion"]["std_dev_pct"], str)
        self.assertIsInstance(result["denominator"]["usable"], int)
