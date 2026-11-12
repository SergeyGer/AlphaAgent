"""P1 remediation step 8: monotonic simulated time and archived-report rebuilds.

The defects these tests exercise were reproduced against the pre-fix tree:

* a decision at 14:30 whose first executable mark was at 14:35 had its fill
  applied immediately, so a second decision at 14:31 already saw the reduced
  cash and the new position - the fill was priced from the future relative to
  the decision that followed it;
* no trajectory carried an auditable timeline at all;
* an archived comparison report could not be rebuilt: there was no command, and
  the report did not embed the opportunity-set document, snapshot payloads or
  decisions a rebuild would need;
* an altered input had no way to be detected or named.

The tests hold the repair: a monotonic event clock with a stable deferral reason,
a deterministic step record, byte-identical replay, a dry-run-by-default rebuild
command that names differences, and synthetic labelling that survives the
rebuild.
"""

from __future__ import annotations

import copy
import json
import tempfile
from dataclasses import replace
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.tests.helpers import make_portfolio, make_user
from services.experiment_execution import (
    ExecutionError,
    execute_opportunity_set,
    load_opportunity_set,
    validate_opportunity_set,
)
from services.experiment_rebuild import RebuildError, rebuild_comparison_report
from services.experiment_simulation import REASON_FILL_DEFERRED

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


def execute(document: dict, portfolio, *, provider_mode: str = "stub") -> dict:
    return execute_opportunity_set(
        opportunity_set=validate_opportunity_set(copy.deepcopy(document)),
        portfolio=portfolio,
        provider_mode=provider_mode,
    )


def same_day_document(*, fill_mark_at: str) -> dict:
    """AAPL at 14:30, a second decision at 14:31, and AAPL fill at ``fill_mark_at``."""
    document = load_document()
    msft = next(item for item in document["opportunities"] if item["ticker"] == "MSFT")
    msft = copy.deepcopy(msft)
    msft["key"] = "msft-same-day"
    msft["scheduled_at"] = "2026-02-03T14:31:00Z"
    msft["snapshot"]["analysis_cutoff"] = "2026-02-03T14:31:00Z"
    msft["snapshot"]["captured_at"] = "2026-02-03T14:30:55Z"
    msft["snapshot"]["quotes"]["MSFT"] = {
        "price_usd": "410.00",
        "change_pct": "0.00",
        "currency": "USD",
        "as_of": "2026-02-03T14:30:55Z",
        "source": "declared_fixture_quote",
    }
    msft["evidence"] = []
    document["opportunities"] = [
        item for item in document["opportunities"] if item["ticker"] == "AAPL"
    ] + [msft]
    document["marks"] = [
        mark
        for mark in document["marks"]
        if mark["at"] == "2026-02-11T20:00:00Z"
        or (mark["ticker"] == "AAPL" and mark["at"] == fill_mark_at)
        or (mark["ticker"] == "MSFT" and mark["at"] == "2026-02-03T14:31:00Z")
    ]
    document["marks"].sort(key=lambda mark: (mark["at"], mark["ticker"]))
    return document


def v0_report(document: dict, portfolio) -> dict:
    report = execute(document, portfolio)
    return next(arm for arm in report["arms"] if arm["key"] == "V0")


def steps(report: dict, arm: str = "V0") -> list[dict]:
    block = next(item for item in report["arms"] if item["key"] == arm)
    return block["trajectory"]["simulation"]["steps"]


class MonotonicSimulatedTimeTests(TestCase):
    """8A - a later-priced fill is deferred, never applied at decision time."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("clock-user"), balance="10000.00")

    def test_a_fill_priced_after_the_clock_is_deferred_and_invisible_to_the_next_decision(self):
        report = v0_report(same_day_document(fill_mark_at="2026-02-03T14:35:00Z"), self.portfolio)
        trajectory = report["trajectory"]
        aapl_order, msft_order = trajectory["orders"][0], trajectory["orders"][1]

        self.assertEqual(aapl_order["opportunity"], "aapl-2026-02-03")
        self.assertEqual(aapl_order["fill_rule_status"], "deferred")
        self.assertEqual(aapl_order["fill_reason"], REASON_FILL_DEFERRED)
        self.assertEqual(aapl_order["scheduled_fill_at"], "2026-02-03T14:35:00Z")
        self.assertEqual(aapl_order["fill"]["at"], "2026-02-03T14:35:00Z")
        self.assertGreater(aapl_order["fill"]["at"], aapl_order["decision_at"])

        # The 14:31 decision is before the 14:35 fill: it must see the opening
        # cash and no AAPL position. The pre-fix tree showed 9749.44 and a
        # position here.
        self.assertEqual(msft_order["decision_at"], "2026-02-03T14:31:00Z")
        self.assertEqual(msft_order["cash_before_usd"], "10000.00")
        self.assertEqual(msft_order["equity_before_usd"], "10000.00")
        self.assertEqual(msft_order["positions_before"], {})
        self.assertEqual(msft_order["cash_after_decision_usd"], "10000.00")
        self.assertIsNone(msft_order["fill"])

        clocks = [step["clock"] for step in trajectory["simulation"]["steps"]]
        self.assertEqual(clocks, sorted(clocks))
        fill_index = next(
            index
            for index, step in enumerate(trajectory["simulation"]["steps"])
            if step["event"] == "fill"
        )
        decision_index = next(
            index
            for index, step in enumerate(trajectory["simulation"]["steps"])
            if step["event"] == "decision" and step["opportunity"] == "msft-same-day"
        )
        self.assertLess(decision_index, fill_index)
        self.assertEqual(
            trajectory["simulation"]["steps"][fill_index]["clock"],
            "2026-02-03T14:35:00Z",
        )
        self.assertTrue(trajectory["simulation"]["monotonic"])

    def test_the_same_sequence_with_a_price_at_the_decision_fills_immediately(self):
        report = v0_report(same_day_document(fill_mark_at="2026-02-03T14:30:00Z"), self.portfolio)
        aapl_order = report["trajectory"]["orders"][0]
        self.assertEqual(aapl_order["fill_rule_status"], "filled")
        self.assertIsNone(aapl_order["fill_reason"])
        self.assertEqual(aapl_order["applied_at"], "2026-02-03T14:30:00Z")
        self.assertEqual(aapl_order["fill"]["at"], "2026-02-03T14:30:00Z")

    def test_a_decision_snapshot_from_its_own_future_is_rejected_with_a_stable_reason(self):
        document = same_day_document(fill_mark_at="2026-02-03T14:30:00Z")
        aapl = next(item for item in document["opportunities"] if item["ticker"] == "AAPL")
        aapl["snapshot"]["analysis_cutoff"] = "2026-02-03T14:31:00Z"
        with self.assertRaisesRegex(ExecutionError, "analysis_cutoff after"):
            validate_opportunity_set(document)

        opportunity_set = load_opportunity_set(OPPORTUNITY_SET_PATH)
        broken = replace(
            opportunity_set,
            opportunities=tuple(
                replace(
                    opportunity,
                    snapshot={
                        **opportunity.snapshot,
                        "analysis_cutoff": "2026-02-03T14:31:00Z",
                    },
                )
                if opportunity.key == "aapl-2026-02-03"
                else opportunity
                for opportunity in opportunity_set.opportunities
            ),
        )
        report = execute_opportunity_set(
            opportunity_set=broken,
            portfolio=self.portfolio,
            provider_mode="stub",
        )
        order = next(item for item in steps(report, "V0") if item["event"] == "decision")
        self.assertEqual(order["order"]["fill_rule_status"], "rejected")
        self.assertEqual(order["order"]["fill_reason"], "decision_input_after_simulated_clock")


class ReplayDeterminismTests(TestCase):
    """8B - replaying the committed inputs produces byte-identical records."""

    def test_replaying_twice_produces_byte_identical_decision_records(self):
        portfolio = make_portfolio(make_user("replay-user"), balance="10000.00")
        first = execute(load_document(), portfolio)
        second = execute(load_document(), portfolio)

        self.assertEqual(first["execution"]["namespace"], second["execution"]["namespace"])
        for arm in ("V0", "V1", "V2", "V3"):
            first_steps = steps(first, arm)
            second_steps = steps(second, arm)
            self.assertEqual(
                json.dumps(first_steps, sort_keys=True),
                json.dumps(second_steps, sort_keys=True),
                f"{arm} timeline is not byte-identical",
            )
            self.assertEqual(
                next(item for item in first_steps if item["event"] == "decision"),
                next(item for item in second_steps if item["event"] == "decision"),
            )
            first_block = next(item for item in first["arms"] if item["key"] == arm)
            second_block = next(item for item in second["arms"] if item["key"] == arm)
            self.assertEqual(
                first_block["trajectory"]["simulation"]["steps_hash"],
                second_block["trajectory"]["simulation"]["steps_hash"],
            )
            self.assertEqual(
                json.dumps(first_block["trajectory"]["orders"], sort_keys=True),
                json.dumps(second_block["trajectory"]["orders"], sort_keys=True),
            )
            self.assertEqual(
                first_block["trajectory"]["simulation"]["decision_records_hash"],
                second_block["trajectory"]["simulation"]["decision_records_hash"],
            )

    def test_independent_replays_have_identical_decision_records(self):
        first = execute(load_document(), make_portfolio(make_user("replay-a"), balance="10000.00"))
        second = execute(load_document(), make_portfolio(make_user("replay-b"), balance="10000.00"))
        self.assertNotEqual(first["execution"]["namespace"], second["execution"]["namespace"])
        for arm in ("V0", "V1", "V2", "V3"):
            first_block = next(item for item in first["arms"] if item["key"] == arm)
            second_block = next(item for item in second["arms"] if item["key"] == arm)
            self.assertEqual(
                first_block["trajectory"]["simulation"]["decision_records"],
                second_block["trajectory"]["simulation"]["decision_records"],
                f"{arm} decisions differ across independent replays",
            )
            self.assertEqual(
                first_block["trajectory"]["simulation"]["decision_records_hash"],
                second_block["trajectory"]["simulation"]["decision_records_hash"],
            )


class ArchivedReportRebuildTests(TestCase):
    """8C/8D - rebuild from recorded inputs, name differences, stay synthetic."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("rebuild-user"), balance="10000.00")
        self.archive = json.loads(json.dumps(execute(load_document(), self.portfolio)))

    def test_rebuild_reproduces_the_numbers_and_hash(self):
        rebuilt = rebuild_comparison_report(copy.deepcopy(self.archive))
        self.assertEqual(rebuilt["status"], "reproduced")
        self.assertEqual(rebuilt["difference_count"], 0, rebuilt["differences"][:5])
        self.assertTrue(rebuilt["contents_hash"]["matches"])
        self.assertTrue(rebuilt["metric"]["matches"])
        self.assertEqual(rebuilt["rebuilt_report"]["contents_hash"], self.archive["contents_hash"])
        self.assertEqual(
            rebuilt["rebuilt_report"]["pre_registration"]["primary_metric_result"],
            self.archive["pre_registration"]["primary_metric_result"],
        )
        self.assertEqual(
            rebuilt["rebuilt_report"]["arms"][0]["trajectory"]["fills"],
            self.archive["arms"][0]["trajectory"]["fills"],
        )

    def test_an_altered_input_names_the_difference(self):
        altered = copy.deepcopy(self.archive)
        mark = next(
            item
            for item in altered["frozen"]["opportunity_set"]["marks"]
            if item["ticker"] == "AAPL" and item["at"] == "2026-02-03T14:30:00Z"
        )
        mark["price"] = "226.00"
        altered["frozen"]["opportunity_set_hash"] = validate_opportunity_set(
            altered["frozen"]["opportunity_set"]
        ).content_hash
        rebuilt = rebuild_comparison_report(altered)
        self.assertEqual(rebuilt["status"], "differs")
        paths = [difference["path"] for difference in rebuilt["differences"]]
        self.assertTrue(
            any("trajectory" in path for path in paths),
            f"no trajectory difference was named: {paths[:10]}",
        )
        self.assertTrue(
            any("price" in path for path in paths),
            f"no price difference was named: {paths[:10]}",
        )

    def test_a_tampered_snapshot_is_refused_by_hash(self):
        altered = copy.deepcopy(self.archive)
        first_key = next(iter(altered["execution"]["snapshots_used"]))
        altered["execution"]["snapshots_used"][first_key]["quotes"]["AAPL"]["price_usd"] = "999.00"
        with self.assertRaises(RebuildError) as raised:
            rebuild_comparison_report(altered)
        self.assertEqual(raised.exception.code, "snapshot_hash_mismatch")
        self.assertIn("content_hash", raised.exception.detail)

    def test_an_unsupported_format_version_is_reported_by_name(self):
        altered = copy.deepcopy(self.archive)
        altered["report_schema_version"] = "9.9"
        with self.assertRaises(RebuildError) as raised:
            rebuild_comparison_report(altered)
        self.assertEqual(raised.exception.code, "unsupported_report_schema_version")
        self.assertIn("report_schema_version", raised.exception.detail)
        self.assertIn("9.9", raised.exception.detail)

        altered = copy.deepcopy(self.archive)
        altered["archive_format"] = "experiment_comparison.v1"
        with self.assertRaises(RebuildError) as raised:
            rebuild_comparison_report(altered)
        self.assertEqual(raised.exception.code, "unsupported_archive_format")
        self.assertIn("archive_format", raised.exception.detail)

    def test_rebuilt_synthetic_content_stays_labelled_everywhere(self):
        rebuilt = rebuild_comparison_report(copy.deepcopy(self.archive))
        report = rebuilt["rebuilt_report"]
        self.assertTrue(report["synthetic"])
        self.assertTrue(report["honesty"]["simulated_content"])
        self.assertEqual(report["honesty"]["synthetic_label"], "synthetic_simulation")
        self.assertFalse(report["honesty"]["real_market_outcome"])
        self.assertFalse(report["honesty"]["comparative_protocol_claim"])
        for arm in report["arms"]:
            if arm["key"] == "V4":
                continue
            self.assertTrue(arm["synthetic"])
            self.assertTrue(arm["trajectory"]["provenance"]["synthetic"])
        self.assertTrue(rebuilt["synthetic"])
        self.assertEqual(rebuilt["synthetic_label"], "synthetic_simulation")

    @override_settings(AI_CONFIG=NO_PROVIDER_CONFIG)
    def test_an_unexecuted_arm_rebuilds_with_the_same_denominator(self):
        portfolio = make_portfolio(make_user("rebuild-auto"), balance="10000.00")
        archive = json.loads(json.dumps(execute(load_document(), portfolio, provider_mode="auto")))
        self.assertEqual(archive["denominators"]["not_executed"], 9)
        rebuilt = rebuild_comparison_report(copy.deepcopy(archive))
        self.assertEqual(rebuilt["status"], "reproduced", rebuilt["differences"][:10])
        self.assertEqual(rebuilt["difference_count"], 0, rebuilt["differences"][:5])
        self.assertEqual(
            rebuilt["rebuilt_report"]["denominators"],
            archive["denominators"],
        )

    def test_the_rebuild_command_is_dry_run_by_default_and_execute_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "comparison.json"
            archive_path.write_text(json.dumps(self.archive), encoding="utf-8")
            output = StringIO()
            call_command(
                "rebuild_experiment_report",
                path=str(archive_path),
                stdout=output,
            )
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["status"], "reproduced")
            self.assertTrue(summary["dry_run"])
            self.assertTrue(summary["synthetic"])
            self.assertTrue(summary["honesty"]["simulated_content"])
            self.assertEqual(summary["synthetic_label"], "synthetic_simulation")
            self.assertEqual(summary["changed_top_level_blocks"], [])
            self.assertFalse(Path(summary["would_write"]).exists())

            rebuilt_path = Path(directory) / "comparison.rebuilt.json"
            output = StringIO()
            call_command(
                "rebuild_experiment_report",
                path=str(archive_path),
                output=str(rebuilt_path),
                execute=True,
                stdout=output,
            )
            summary = json.loads(output.getvalue())
            self.assertFalse(summary["dry_run"])
            self.assertTrue(rebuilt_path.exists())
            written = json.loads(rebuilt_path.read_text(encoding="utf-8"))
            self.assertTrue(written["honesty"]["simulated_content"])
            self.assertEqual(written["contents_hash"], self.archive["contents_hash"])

            # A rebuild never overwrites the archive it is checking.
            with self.assertRaises(CommandError):
                call_command(
                    "rebuild_experiment_report",
                    path=str(archive_path),
                    output=str(archive_path),
                    execute=True,
                    stdout=StringIO(),
                )
