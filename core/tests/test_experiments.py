"""Phase 3 experiment, report and leakage tests (EXP-01..05, MET-04/05).

The report is built only from stored rows. These tests deliberately never mock a
market price into the report: everything comes from ``DecisionRun``, outcome,
guard, ledger or a manifest-declared accounting fixture.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.decision_models import (
    AgentRole,
    Claim,
    ClaimStance,
    ClaimType,
    DecisionAction,
    DecisionRecord,
    DecisionStatus,
    GuardRecord,
    GuardStatus,
    OutcomeMaturity,
    OutcomeRecord,
    RebuttalResponseType,
    RunMode,
    RunSource,
)
from core.measurement_models import (
    Experiment,
    ExperimentRun,
    ExperimentRunStatus,
)
from core.models import AgentDecisionLog, ExecutedBy, PortfolioSnapshot, Transaction, TxType
from core.tests.helpers import auth_client, make_portfolio, make_user
from services.attribution import signed_return
from services.decision_runs import (
    create_run,
    link_legacy_log,
    record_failure,
    transition_status,
)
from services.evidence import (
    create_claim,
    create_evidence_item,
    create_rebuttal,
    create_snapshot,
)
from services.experiments import (
    DEFAULT_VARIANTS,
    ManifestError,
    build_experiment,
    checksum_of,
    load_manifest,
    validate_manifest,
)
from services.signal_diagnostics import HOLD_ZERO_CONVENTION

D = Decimal
CUTOFF = datetime(2026, 2, 3, 14, 30, tzinfo=UTC)
DAY = timedelta(days=1)
WINDOW_START = "2026-02-01T00:00:00Z"
WINDOW_END = "2026-03-31T00:00:00Z"
AS_OF = "2026-04-01T00:00:00Z"


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def make_snapshot(portfolio, *, ticker="AAPL", price="100.00", cash="10000.00", holdings=None):
    return create_snapshot(
        portfolio=portfolio,
        portfolio_version="v1",
        analysis_cutoff=CUTOFF,
        captured_at=CUTOFF - timedelta(seconds=5),
        holdings=holdings or [],
        cash={"balance_usd": cash, "currency": "USD", "as_of": iso(CUTOFF - timedelta(seconds=10))},
        quotes={
            ticker: {
                "price_usd": price,
                "currency": "USD",
                "as_of": iso(CUTOFF - timedelta(seconds=10)),
                "source": "test_snapshot",
            }
        },
    )


def make_debate_run(
    portfolio,
    *,
    ticker="AAPL",
    action=DecisionAction.BUY,
    quantity="10",
    with_snapshot=True,
    cost=None,
    fixture_id="",
    started_at=CUTOFF,
):
    snapshot = make_snapshot(portfolio, ticker=ticker) if with_snapshot else None
    run = create_run(
        portfolio=portfolio,
        ticker=ticker,
        protocol_version="test_protocol.v1",
        mode=RunMode.LIVE,
        source=RunSource.CREWAI,
        snapshot=snapshot,
        started_at=started_at,
        fixture_id=fixture_id,
        total_cost_usd=cost,
    )
    transition_status(run, DecisionStatus.COLLECTING, occurred_at=started_at + timedelta(seconds=1))
    transition_status(run, DecisionStatus.ANALYSING, occurred_at=started_at + timedelta(seconds=2))
    transition_status(
        run, DecisionStatus.ADJUDICATING, occurred_at=started_at + timedelta(seconds=3)
    )
    decision = DecisionRecord.objects.create(
        run=run,
        action=action,
        requested_quantity=D(quantity) if action != DecisionAction.HOLD else None,
        concise_rationale="test rationale",
        engine_source=RunSource.CREWAI,
        created_at=started_at + timedelta(minutes=1),
    )
    transition_status(run, DecisionStatus.VALIDATING, occurred_at=started_at + timedelta(seconds=4))
    transition_status(run, DecisionStatus.COMPLETED, occurred_at=started_at + timedelta(seconds=5))
    run.refresh_from_db()
    return run, decision


def add_outcome(
    decision,
    *,
    action="BUY",
    horizon=7,
    forward="10.00",
    benchmark="1.00",
    maturity=OutcomeMaturity.EVALUATED,
    evaluated_at=None,
    price_observed_at=None,
    method_version="test.method.v1",
):
    forward_value = D(forward)
    decision_at = decision.created_at or CUTOFF
    evaluated = evaluated_at or (decision_at + timedelta(days=horizon))
    observed = price_observed_at or evaluated
    if maturity == OutcomeMaturity.EVALUATED:
        return OutcomeRecord.objects.create(
            decision=decision,
            horizon_days=horizon,
            method_version=method_version,
            maturity=maturity,
            price_at_decision=D("100.00"),
            price_at_horizon=(D("100.00") * (1 + forward_value / D("100"))).quantize(D("0.0001")),
            price_observed_at=observed,
            forward_return_pct=forward_value,
            decision_return_pct=signed_return(action, forward_value),
            benchmark_ticker="SPY" if benchmark is not None else "",
            benchmark_return_pct=(None if benchmark is None else D(str(benchmark))),
            evaluated_at=evaluated,
        )
    return OutcomeRecord.objects.create(
        decision=decision,
        horizon_days=horizon,
        method_version=method_version,
        maturity=maturity,
        unavailable_reason="No price was recorded for this test outcome.",
        evaluated_at=evaluated,
    )


def add_guard(decision, *, status_value=GuardStatus.REJECTED, allowed="0", requested="10"):
    return GuardRecord.objects.create(
        proposal=decision,
        policy_version="guard.v1",
        evaluated_portfolio_version="v1",
        quote_time=CUTOFF,
        status=status_value,
        requested_quantity=D(requested),
        allowed_quantity=D(allowed),
        reason_codes=["test_rejection"] if status_value == GuardStatus.REJECTED else [],
        binding_rule="max_trade_allocation_pct" if status_value != GuardStatus.APPROVED else "",
    )


def add_transaction(portfolio, run, *, ticker="AAPL", side=TxType.BUY, amount="1", price="100"):
    transaction = Transaction.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        tx_type=side,
        amount=D(amount),
        price=D(price),
        executed_by=ExecutedBy.AI,
    )
    log = AgentDecisionLog.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        transaction=transaction,
        reasoning="executed test decision",
        action_taken=f"{side} {amount} {ticker}",
    )
    link_legacy_log(run, log)
    return transaction


def variant_entries(**overrides):
    entries = []
    for default in DEFAULT_VARIANTS:
        entry = {
            "key": default["key"],
            "label": default["label"],
            "engine": default["engine"],
            "protocol_version": default["protocol_version"],
            "requires_rebuttals": default["requires_rebuttals"],
            "implemented": default["implemented"],
            "budget": {"max_cost_usd": "1.00"},
        }
        if default["key"] == "V4":
            entry["unimplemented_reason"] = default["unimplemented_reason"]
        if default["key"] in {"V1", "V2", "V3"}:
            entry["model_configuration"] = {
                "provider": "test",
                "model": "test-model",
                "parameters": {},
            }
        entry.update(overrides.get(default["key"], {}))
        entries.append(entry)
    return entries


def run_entry(run_key, variant, run=None, *, scheduled_at=CUTOFF, failure_code=""):
    entry = {
        "run_key": run_key,
        "variant": variant,
        "scheduled_at": iso(scheduled_at),
    }
    if run is not None:
        entry["decision_run_id"] = str(run.id)
    else:
        entry["fixture_id"] = f"missing-{run_key}"
        if failure_code:
            entry["failure_code"] = failure_code
    return entry


def base_manifest(
    runs,
    *,
    experiment_id="phase3-test.v1",
    primary=None,
    sample=None,
    accounting=None,
    honesty=None,
    window_end=WINDOW_END,
    as_of=AS_OF,
    frozen=None,
):
    return {
        "manifest_version": 1,
        "schema_version": "1.0",
        "experiment_id": experiment_id,
        "label": "Test experiment",
        "description": "A test manifest built from stored rows.",
        "generated_for": "Phase 3 tests",
        "declared_at": iso(CUTOFF),
        "observation_window": {"start": WINDOW_START, "end": window_end},
        "evaluation_mode": "historical",
        "as_of": as_of,
        "primary_metric": primary
        or {
            "id": "mean_action_score_pct",
            "family": "signal_diagnostics",
            "horizon_days": 7,
            "direction": "higher_is_better",
        },
        "sample_requirements": sample
        or {
            "minimum_scheduled_runs": 0,
            "minimum_completed_runs": 0,
            "minimum_observations": 0,
        },
        "benchmark": {
            "ticker": "SPY",
            "asset_class": "equity",
            "selection_rule": "declared_equity_benchmark",
        },
        "frozen": frozen or {},
        "variants": variant_entries(),
        "runs": runs,
        "accounting": accounting or {"source": "stored_ledger"},
        "honesty": honesty or {"all_runs_synthetic": False},
    }


class ManifestValidationTests(TestCase):
    def test_the_manifest_must_define_all_five_variants(self):
        manifest = base_manifest([run_entry("x", "V2")])
        manifest["variants"] = [entry for entry in manifest["variants"] if entry["key"] != "V4"]
        with self.assertRaisesRegex(ManifestError, "must define every EXP-01 variant"):
            validate_manifest(manifest)

    def test_v4_must_not_be_marked_implemented(self):
        manifest = base_manifest([run_entry("x", "V2")])
        for entry in manifest["variants"]:
            if entry["key"] == "V4":
                entry["implemented"] = True
        with self.assertRaisesRegex(ManifestError, "unimplemented"):
            validate_manifest(manifest)

    def test_a_checksum_drift_is_refused(self):
        manifest = base_manifest([run_entry("x", "V2")])
        value = checksum_of(manifest)
        manifest["checksum"] = {"value": value}
        self.assertEqual(validate_manifest(manifest)["checksum"], value)
        manifest["label"] = "Edited after freezing"
        with self.assertRaisesRegex(ManifestError, "checksum does not match"):
            validate_manifest(manifest)

    def test_manifest_fixture_accounting_requires_an_opening_state(self):
        manifest = base_manifest(
            [run_entry("x", "V2")],
            accounting={"source": "manifest_fixture", "fills": [], "flows": [], "marks": []},
        )
        with self.assertRaisesRegex(ManifestError, "opening_state"):
            validate_manifest(manifest)

    def test_unknown_run_variant_is_rejected(self):
        manifest = base_manifest([run_entry("x", "V9")])
        with self.assertRaisesRegex(ManifestError, "unknown variant"):
            validate_manifest(manifest)

    def test_the_versioned_example_manifest_is_valid(self):
        path = Path(settings.BASE_DIR) / "docs/spec/fixtures/experiments/phase3-engineering.v1.json"
        manifest = load_manifest(path)
        self.assertEqual(manifest["experiment_id"], "phase3-engineering.v1")
        self.assertEqual(
            [variant["key"] for variant in manifest["variants"]],
            ["V0", "V1", "V2", "V3", "V4"],
        )
        self.assertEqual(manifest["accounting"]["source"], "manifest_fixture")
        self.assertEqual(len(manifest["runs"]), 4)
        self.assertTrue(manifest["honesty"]["all_runs_synthetic"])


class ExperimentBuildTests(TestCase):
    def setUp(self):
        self.user = make_user("experiment-user")
        self.portfolio = make_portfolio(self.user, balance="10000.00")

    def build(self, manifest, *, force=False):
        return build_experiment(manifest=manifest, portfolio=self.portfolio, force=force)

    def test_build_is_idempotent_and_replay_increments_the_report_version(self):
        run, decision = make_debate_run(self.portfolio, action=DecisionAction.BUY)
        add_outcome(decision, forward="10.00")
        manifest = base_manifest([run_entry("v2-aapl", "V2", run)])

        experiment, report = self.build(manifest)
        self.assertEqual(report["report_version"], 1)
        self.assertEqual(Experiment.objects.count(), 1)
        self.assertEqual(ExperimentRun.objects.filter(experiment=experiment).count(), 1)

        experiment, replayed = self.build(manifest)
        self.assertEqual(Experiment.objects.count(), 1)
        self.assertEqual(ExperimentRun.objects.filter(experiment=experiment).count(), 1)
        self.assertEqual(replayed["report_version"], 2)
        self.assertEqual(experiment.status, "ready")

    def test_a_manifest_change_without_force_is_refused(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision)
        manifest = base_manifest([run_entry("v2-aapl", "V2", run)])
        self.build(manifest)
        manifest["label"] = "Changed label"
        with self.assertRaisesRegex(ManifestError, "different manifest checksum"):
            self.build(manifest)
        _experiment, report = self.build(manifest, force=True)
        self.assertEqual(report["experiment"]["label"], "Changed label")

    def test_a_failed_run_stays_in_the_completion_and_cost_denominator(self):
        completed, decision = make_debate_run(
            self.portfolio, action=DecisionAction.BUY, cost=D("0.10")
        )
        add_outcome(decision)
        failed = create_run(
            portfolio=self.portfolio,
            ticker="MSFT",
            protocol_version="test_protocol.v1",
            mode=RunMode.LIVE,
            source=RunSource.CREWAI,
            started_at=CUTOFF + DAY,
            total_cost_usd=D("0.20"),
        )
        record_failure(
            failed,
            error_code="stage_timeout",
            error_detail="deliberate failure",
            occurred_at=CUTOFF + DAY + timedelta(seconds=5),
        )
        manifest = base_manifest(
            [
                run_entry("completed", "V2", completed),
                run_entry("failed", "V1", failed, scheduled_at=CUTOFF + DAY),
                run_entry("never-ran", "V0", None, scheduled_at=CUTOFF + 2 * DAY),
            ]
        )
        experiment, report = self.build(manifest)

        counts = report["sample_integrity"]["counts"]
        self.assertEqual(counts["scheduled"], 3)
        self.assertEqual(counts["completed"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["not_executed"], 1)
        self.assertEqual(report["sample_integrity"]["completion_rate_pct"], "33.3")
        self.assertEqual(report["sample_integrity"]["failure_rate_pct"], "33.3")
        self.assertEqual(report["sample_integrity"]["denominator"]["scheduled"], 3)
        # The failed run's recorded cost is part of the experiment cost total,
        # not dropped with its failure.
        self.assertEqual(
            report["portfolio_performance"]["llm_costs"]["recorded_cost_usd"], "0.300000"
        )
        self.assertEqual(
            ExperimentRun.objects.get(experiment=experiment, run_key="failed").status,
            ExperimentRunStatus.FAILED,
        )
        failed_variant = next(item for item in report["variants"] if item["key"] == "V1")
        self.assertEqual(failed_variant["failure_rate_pct"], "100.0")
        self.assertEqual(failed_variant["operational"]["api_cost_usd"], "0.200000")

    def test_a_deliberate_failure_is_not_silently_dropped_from_the_report(self):
        failed = create_run(
            portfolio=self.portfolio,
            ticker="MSFT",
            protocol_version="test_protocol.v1",
            mode=RunMode.LIVE,
            source=RunSource.CREWAI,
            started_at=CUTOFF,
            total_cost_usd=D("0.05"),
        )
        record_failure(
            failed,
            error_code="provider_error",
            error_detail="deliberate",
            occurred_at=CUTOFF + timedelta(seconds=5),
        )
        manifest = base_manifest([run_entry("failed", "V2", failed)])
        _, report = self.build(manifest)
        self.assertEqual(report["sample_integrity"]["counts"]["failed"], 1)
        variant = next(item for item in report["variants"] if item["key"] == "V2")
        self.assertEqual(variant["status"], "no_completed_runs")
        self.assertEqual(variant["operational"]["failure_codes"], {"provider_error": 1})

    def test_building_a_report_never_calls_an_llm(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision)
        manifest = base_manifest([run_entry("v2-aapl", "V2", run)])
        with (
            patch(
                "services.debate_protocol.DebateProtocol.run",
                side_effect=AssertionError("the report must not call the protocol"),
            ),
            patch(
                "ai_agent.run_alpha_agent",
                side_effect=AssertionError("the report must not call the agent"),
            ),
        ):
            _, report = self.build(manifest)
        self.assertEqual(report["sample_integrity"]["counts"]["completed"], 1)

    def test_all_variants_are_published_and_v4_is_null_not_zero(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision)
        _, report = self.build(base_manifest([run_entry("v2", "V2", run)]))
        self.assertEqual(
            [item["key"] for item in report["variants"]], ["V0", "V1", "V2", "V3", "V4"]
        )
        v4 = next(item for item in report["variants"] if item["key"] == "V4")
        self.assertEqual(v4["status"], "unimplemented")
        self.assertFalse(v4["implemented"])
        self.assertIsNone(v4["primary_metric"]["value"])
        self.assertIn("V4", report["fairness"]["equal_budget"]["declared"])

    def test_the_two_metric_families_are_separate_and_the_diagnostic_series_refuses_a_total(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision, forward="10.00")
        _, report = self.build(base_manifest([run_entry("v2", "V2", run)]))

        self.assertIn("signal_diagnostics", report)
        self.assertIn("portfolio_performance", report)
        self.assertFalse(report["metric_families"]["signal_diagnostics"]["summable"])
        self.assertIn("separation", report["metric_families"])
        self.assertNotIn("total", report["signal_diagnostics"]["summary_by_horizon"]["7"])
        self.assertNotIn(
            "portfolio_return_from_decisions",
            json.dumps(report),
        )


class SignalDiagnosticReportTests(TestCase):
    def setUp(self):
        self.user = make_user("signal-user")
        self.portfolio = make_portfolio(self.user, balance="10000.00")

    def build(self, manifest):
        return build_experiment(manifest=manifest, portfolio=self.portfolio)

    def row(self, report, run_key):
        return next(
            row for row in report["signal_diagnostics"]["rows"] if row["run_key"] == run_key
        )

    def test_a_hold_has_no_directional_hit_and_its_zero_is_a_convention(self):
        run, decision = make_debate_run(self.portfolio, action=DecisionAction.HOLD, quantity=None)
        add_outcome(decision, action="HOLD", forward="5.00")
        _, report = self.build(base_manifest([run_entry("hold", "V2", run)]))
        row = self.row(report, "hold")

        self.assertEqual(row["proposal_action"], "HOLD")
        self.assertIsNone(row["directional_hit"])
        self.assertEqual(row["action_score_pct"], "0.0000")
        self.assertEqual(row["action_score_convention"], HOLD_ZERO_CONVENTION)
        summary = report["signal_diagnostics"]["summary_by_horizon"]["7"]
        self.assertEqual(summary["directional_calls"], 0)
        self.assertIsNone(summary["hit_rate_pct"])
        self.assertTrue(summary["hold_zero_is_convention"])

    def test_proposal_and_executed_action_are_distinct(self):
        rejected, rejected_decision = make_debate_run(self.portfolio, ticker="AAPL")
        add_guard(rejected_decision, status_value=GuardStatus.REJECTED, allowed="0")
        add_outcome(rejected_decision, forward="5.00")

        executed, executed_decision = make_debate_run(self.portfolio, ticker="MSFT")
        add_transaction(self.portfolio, executed, ticker="MSFT")
        add_outcome(executed_decision, forward="2.00")

        _, report = self.build(
            base_manifest(
                [
                    run_entry("rejected", "V1", rejected),
                    run_entry("executed", "V2", executed),
                ]
            )
        )
        rejected_row = self.row(report, "rejected")
        executed_row = self.row(report, "executed")

        self.assertEqual(rejected_row["proposal_action"], "BUY")
        self.assertIsNone(rejected_row["executed_action"])
        self.assertEqual(rejected_row["execution_status"], "rejected_by_guard")
        self.assertEqual(rejected_row["measurement"], "counterfactual")

        self.assertEqual(executed_row["proposal_action"], "BUY")
        self.assertEqual(executed_row["executed_action"], "BUY")
        self.assertEqual(executed_row["execution_status"], "executed")
        self.assertEqual(executed_row["measurement"], "signal")

    def test_a_rejected_proposal_is_a_counterfactual_not_executed_performance(self):
        run, decision = make_debate_run(self.portfolio, ticker="AAPL", quantity="10")
        add_guard(decision, status_value=GuardStatus.REJECTED, allowed="0", requested="10")
        add_outcome(decision, forward="5.00")
        manifest = base_manifest(
            [run_entry("rejected", "V2", run)],
            accounting={
                "source": "stored_ledger",
                "counterfactual_rule": {
                    "holding_period_days": 7,
                    "horizons_days": [1, 7, 30],
                    "quantity_basis": "requested_quantity",
                },
            },
        )
        _, report = self.build(manifest)
        block = report["counterfactuals"]
        self.assertEqual(block["rejected_proposal_count"], 1)
        row = block["rows"][0]
        self.assertEqual(row["portfolio_effect"], "none_no_fill")
        self.assertTrue(row["counterfactual"]["established"])
        self.assertTrue(row["counterfactual"]["entry"]["fill_rule"])
        self.assertIsNone(row["counterfactual"]["uncertainty"]["confidence_interval"])
        self.assertEqual(
            row["counterfactual"]["uncertainty"]["method"],
            "declared_horizon_sensitivity.v1",
        )
        # The counterfactual did not create a trade or move equity.
        self.assertEqual(report["portfolio_performance"]["trade_count"], 0)
        self.assertEqual(report["signal_diagnostics"]["summary_by_horizon"]["7"]["evaluated"], 0)

    def test_without_a_declared_rule_loss_prevention_is_not_established(self):
        run, decision = make_debate_run(self.portfolio, ticker="AAPL", quantity="10")
        add_guard(decision, status_value=GuardStatus.REJECTED, allowed="0")
        add_outcome(decision, forward="5.00")
        _, report = self.build(base_manifest([run_entry("rejected", "V2", run)]))
        row = report["counterfactuals"]["rows"][0]
        self.assertFalse(row["counterfactual"]["established"])
        self.assertEqual(row["counterfactual"]["reason"], "no_declared_holding_exit_rule")

    def test_an_outcome_evaluated_after_as_of_is_excluded(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(
            decision,
            forward="10.00",
            evaluated_at=CUTOFF + 40 * DAY,
            price_observed_at=CUTOFF + 40 * DAY,
        )
        manifest = base_manifest(
            [run_entry("late", "V2", run)],
            as_of=iso(CUTOFF + 20 * DAY),
            window_end=iso(CUTOFF + 60 * DAY),
        )
        _, report = self.build(manifest)
        row = self.row(report, "late")
        self.assertEqual(row["maturity"], "unavailable")
        self.assertIn("future_evaluation_excluded", row["flags"])
        self.assertEqual(report["signal_diagnostics"]["summary_by_horizon"]["7"]["evaluated"], 0)

    def test_relative_returns_use_only_matched_pairs(self):
        matched, matched_decision = make_debate_run(self.portfolio, ticker="AAPL")
        add_outcome(matched_decision, forward="5.00", benchmark="1.00")
        unmatched, unmatched_decision = make_debate_run(self.portfolio, ticker="MSFT")
        add_outcome(unmatched_decision, forward="4.00", benchmark=None)
        _, report = self.build(
            base_manifest(
                [
                    run_entry("matched", "V2", matched),
                    run_entry("unmatched", "V2", unmatched),
                ]
            )
        )
        summary = report["signal_diagnostics"]["summary_by_horizon"]["7"]
        self.assertEqual(summary["evaluated"], 2)
        self.assertEqual(summary["matched_benchmark_pairs"], 1)
        self.assertEqual(summary["unmatched_benchmark_rows_excluded"], 1)
        self.assertEqual(summary["mean_benchmark_return_pct"], "1.0000")
        self.assertEqual(summary["mean_relative_forward_return_pct"], "4.0000")

    def test_method_version_horizon_policy_and_benchmark_rule_are_displayed(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision)
        _, report = self.build(base_manifest([run_entry("policy", "V2", run)]))
        row = self.row(report, "policy")
        policy = report["signal_diagnostics"]

        self.assertEqual(policy["method_version"], "signal_diagnostics.v1")
        self.assertEqual(policy["horizon_policy"]["horizons_days"], [1, 7, 30])
        self.assertEqual(policy["horizon_policy"]["timezone"], "UTC")
        self.assertIn("session_policy", policy["horizon_policy"])
        self.assertEqual(row["method_version"], "test.method.v1")
        self.assertEqual(
            policy["benchmark_interval_rule"],
            "stored_benchmark_return_uses_the_same_decision_and_horizon_as_the_strategy.v1",
        )
        self.assertFalse(policy["benchmark_interval_verified"])
        self.assertFalse(row["benchmark"]["interval_verified"])

    def test_pending_outcomes_are_unresolved_not_scored_as_zero(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision, maturity=OutcomeMaturity.PENDING)
        _, report = self.build(base_manifest([run_entry("pending", "V2", run)]))
        row = self.row(report, "pending")
        summary = report["signal_diagnostics"]["summary_by_horizon"]["7"]

        self.assertEqual(row["maturity"], "pending")
        self.assertIsNone(row["forward_return_pct"])
        self.assertIsNone(row["action_score_pct"])
        self.assertEqual(summary["evaluated"], 0)
        self.assertEqual(summary["pending"], 1)
        self.assertIsNone(summary["mean_action_score_pct"])
        self.assertEqual(report["signal_diagnostics"]["unresolved"]["count"], 1)

    def test_portfolio_snapshot_mismatch_is_a_reconciliation_error(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision)
        snapshot = PortfolioSnapshot.objects.create(
            portfolio=self.portfolio,
            total_equity_usd=D("12000.00"),
            cash_balance_usd=D("12000.00"),
            positions_value_usd=D("0.00"),
        )
        PortfolioSnapshot.objects.filter(pk=snapshot.pk).update(captured_at=CUTOFF + 2 * DAY)
        _, report = self.build(base_manifest([run_entry("recon", "V2", run)]))
        reconciliation = report["portfolio_performance"]["reconciliation"]
        self.assertEqual(reconciliation["status"], "mismatched")
        self.assertTrue(
            any(anchor["status"] == "mismatched" for anchor in reconciliation["anchors"])
        )
        self.assertTrue(
            any(
                item["code"] == "snapshot_reconciliation_mismatch" for item in report["limitations"]
            )
        )

    def test_input_snapshot_holdings_mismatch_is_reported(self):
        snapshot = make_snapshot(
            self.portfolio,
            holdings=[
                {
                    "ticker": "MSFT",
                    "quantity": "5.00000000",
                    "average_cost_usd": "400.00",
                    "as_of": iso(CUTOFF - timedelta(seconds=10)),
                }
            ],
        )
        run = create_run(
            portfolio=self.portfolio,
            ticker="AAPL",
            protocol_version="test_protocol.v1",
            mode=RunMode.LIVE,
            source=RunSource.CREWAI,
            snapshot=snapshot,
            started_at=CUTOFF,
        )
        transition_status(run, DecisionStatus.COLLECTING, occurred_at=CUTOFF + timedelta(seconds=1))
        transition_status(run, DecisionStatus.ANALYSING, occurred_at=CUTOFF + timedelta(seconds=2))
        transition_status(
            run, DecisionStatus.ADJUDICATING, occurred_at=CUTOFF + timedelta(seconds=3)
        )
        decision = DecisionRecord.objects.create(
            run=run,
            action=DecisionAction.BUY,
            requested_quantity=D("1"),
            concise_rationale="test",
            engine_source=RunSource.CREWAI,
        )
        transition_status(run, DecisionStatus.VALIDATING, occurred_at=CUTOFF + timedelta(seconds=4))
        transition_status(run, DecisionStatus.COMPLETED, occurred_at=CUTOFF + timedelta(seconds=5))
        add_outcome(decision)
        _, report = self.build(base_manifest([run_entry("holdings", "V2", run)]))
        check = report["portfolio_performance"]["reconciliation"]["input_snapshots"]
        self.assertEqual(check["status"], "mismatched")
        self.assertEqual(check["runs"][0]["differences"]["MSFT"]["snapshot"], "5.00000000")

    def test_primary_metric_publishes_a_time_block_interval(self):
        runs = []
        for index, forward in enumerate(("10.00", "5.00", "2.00", "8.00")):
            run, decision = make_debate_run(
                self.portfolio,
                ticker=["AAPL", "MSFT", "TSLA", "NVDA"][index],
                started_at=CUTOFF + index * DAY,
            )
            add_outcome(decision, forward=forward)
            runs.append(run_entry(f"run-{index}", "V2", run))
        _, report = self.build(
            base_manifest(
                runs,
                sample={
                    "minimum_scheduled_runs": 4,
                    "minimum_completed_runs": 4,
                    "minimum_observations": 2,
                },
            )
        )
        primary = report["uncertainty"]["primary_metric"]
        self.assertEqual(primary["observations"], 4)
        self.assertEqual(primary["value"], "6.2500")
        self.assertTrue(primary["interval_established"])
        self.assertIsNotNone(primary["interval"]["lower"])
        self.assertIsNotNone(primary["interval"]["upper"])
        self.assertTrue(primary["sample_sufficient"])
        self.assertEqual(report["uncertainty"]["method"]["method"], "time_block_bootstrap.v1")
        self.assertIsNone(report["uncertainty"]["profitability_threshold"])

    def test_a_small_sample_is_engineering_only_and_asserts_no_threshold(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision, forward="10.00")
        _, report = self.build(
            base_manifest(
                [run_entry("one", "V2", run)],
                sample={
                    "minimum_scheduled_runs": 3,
                    "minimum_completed_runs": 3,
                    "minimum_observations": 3,
                },
            )
        )
        self.assertTrue(report["engineering_validation_only"])
        self.assertFalse(report["sample_integrity"]["minimum_usable_sample"]["met"])
        self.assertEqual(report["uncertainty"]["gates"]["sample"]["status"], "not_met")
        self.assertFalse(report["uncertainty"]["primary_metric"]["sample_sufficient"])
        self.assertIsNone(report["profitability_threshold"])
        self.assertTrue(report["no_profitability_claim"])

    def test_missing_snapshot_makes_strict_historical_claims_ineligible(self):
        run, decision = make_debate_run(self.portfolio, with_snapshot=False)
        add_outcome(decision)
        _, report = self.build(base_manifest([run_entry("no-snapshot", "V2", run)]))
        eligibility = report["sample_integrity"]["eligibility"]
        self.assertFalse(eligibility["strict_historical_claims"])
        self.assertTrue(any("no input snapshot" in reason for reason in eligibility["reasons"]))
        self.assertTrue(
            any(item["code"] == "not_strict_historical" for item in report["limitations"])
        )

    def test_evidence_available_after_the_cutoff_makes_a_run_ineligible(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision)
        create_evidence_item(
            snapshot=run.snapshot,
            source_type="news_article",
            retrieved_at=CUTOFF + timedelta(hours=1),
            excerpt="arrived after the decision could have used it",
            available_at=CUTOFF + timedelta(hours=1),
        )
        _, report = self.build(base_manifest([run_entry("late-evidence", "V2", run)]))
        eligibility = report["sample_integrity"]["eligibility"]
        self.assertFalse(eligibility["strict_historical_claims"])
        self.assertEqual(eligibility["missing_as_of_evidence"], 1)
        self.assertTrue(
            any("available after its cutoff" in reason for reason in eligibility["reasons"])
        )

    def test_evidence_coverage_and_unsupported_claim_flags_are_reported(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision)
        evidence = create_evidence_item(
            snapshot=run.snapshot,
            source_type="news_article",
            retrieved_at=CUTOFF - timedelta(hours=1),
            excerpt="a cited fact",
            source_uri="https://example.test/story",
            available_at=CUTOFF - timedelta(hours=1),
        )
        create_claim(
            run=run,
            agent_role=AgentRole.BULL,
            stance=ClaimStance.BULL,
            statement="Supported claim",
            claim_type=ClaimType.OBSERVED_FACT,
            evidence_ids=[evidence.id],
        )
        # A stored claim with no evidence cannot be created through the EVID-02
        # service, but a legacy/imported row can exist. The report must flag it
        # rather than pretend every stored claim is supported.
        Claim.objects.create(
            run=run,
            agent_role=AgentRole.BULL,
            stance=ClaimStance.BULL,
            statement="Unsupported claim",
            claim_type=ClaimType.INTERPRETATION,
        )
        _, report = self.build(base_manifest([run_entry("claims", "V2", run)]))
        variant = next(item for item in report["variants"] if item["key"] == "V2")
        coverage = variant["evidence"]
        self.assertEqual(coverage["total_claims"], 2)
        self.assertEqual(coverage["claims_with_evidence"], 1)
        self.assertEqual(coverage["evidence_coverage_ratio"], "0.5000")
        self.assertEqual(coverage["claims_without_evidence"], 1)
        self.assertEqual(len(coverage["unsupported_claim_flags"]), 1)

    def test_action_changes_after_rebuttal_are_reported_with_their_basis(self):
        run, decision = make_debate_run(self.portfolio, action=DecisionAction.HOLD, quantity=None)
        add_outcome(decision, action="HOLD", forward="3.00")
        evidence = create_evidence_item(
            snapshot=run.snapshot,
            source_type="news_article",
            retrieved_at=CUTOFF - timedelta(hours=1),
            excerpt="a cited fact",
            available_at=CUTOFF - timedelta(hours=1),
        )
        bull_one = create_claim(
            run=run,
            agent_role=AgentRole.BULL,
            stance=ClaimStance.BULL,
            statement="Bull case one",
            claim_type=ClaimType.INTERPRETATION,
            evidence_ids=[evidence.id],
        )
        create_claim(
            run=run,
            agent_role=AgentRole.BULL,
            stance=ClaimStance.BULL,
            statement="Bull case two",
            claim_type=ClaimType.INTERPRETATION,
            evidence_ids=[evidence.id],
        )
        create_claim(
            run=run,
            agent_role=AgentRole.BEAR,
            stance=ClaimStance.BEAR,
            statement="Bear case",
            claim_type=ClaimType.INTERPRETATION,
            evidence_ids=[evidence.id],
        )
        create_rebuttal(
            run=run,
            responding_agent=AgentRole.BEAR,
            target_claim=bull_one,
            response_type=RebuttalResponseType.INSUFFICIENT_EVIDENCE,
            explanation="The bull case is not supported by the captured evidence.",
            evidence_ids=[],
            position_change="The evidence is now two-sided, so HOLD is appropriate.",
        )
        _, report = self.build(base_manifest([run_entry("debate", "V3", run)]))
        variant = next(item for item in report["variants"] if item["key"] == "V3")
        changes = variant["action_changes_after_rebuttal"]
        self.assertEqual(changes["runs_with_rebuttals"], 1)
        self.assertEqual(changes["declared_position_changes"], 1)
        self.assertEqual(changes["action_changes_after_rebuttal"], 1)
        self.assertEqual(changes["basis"], "final_action_vs_initial_case_lean.v1")
        self.assertIn("not a recorded action diff", changes["note"])

    def test_fairness_publishes_equal_opportunity_budget_and_freeze_checks(self):
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision)
        manifest = base_manifest(
            [run_entry("one", "V2", run)],
            frozen={
                "instruments": ["AAPL"],
                "starting_portfolio_state": {"cash": "10000.00", "positions": {}},
                "risk_rules": {
                    "max_trade_allocation_pct": "5.00",
                    "max_portfolio_exposure_pct": "90.00",
                    "max_ticker_concentration_pct": "30.00",
                    "daily_loss_limit_usd": "500.00",
                },
                "execution_assumptions": {"guard_policy_version": "guard.v1"},
            },
        )
        _, report = self.build(manifest)
        fairness = report["fairness"]
        checks = {check["check"]: check for check in fairness["checks"]}
        self.assertEqual(checks["instruments"]["status"], "match")
        self.assertEqual(checks["portfolio_starting_state"]["status"], "match")
        self.assertEqual(checks["risk_rules"]["status"], "match")
        self.assertEqual(checks["benchmark"]["status"], "match")
        self.assertIn("V4", fairness["equal_budget"]["declared"])
        self.assertIn("equal", fairness["equal_opportunity"])
        self.assertIn("comparable_for_protocol_claims", fairness)


class ReportApiTests(APITestCase):
    def setUp(self):
        self.user = make_user("report-owner")
        self.portfolio = make_portfolio(self.user)
        run, decision = make_debate_run(self.portfolio)
        add_outcome(decision)
        self.experiment, self.report = build_experiment(
            manifest=base_manifest([run_entry("one", "V2", run)]),
            portfolio=self.portfolio,
        )
        self.client = auth_client(self.user)
        self.url = reverse("core:experiment-report", args=[str(self.experiment.id)])

    def test_the_report_endpoint_returns_the_stored_report(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["experiment"]["experiment_id"], "phase3-test.v1")
        self.assertEqual(response["X-Report-Version"], "1")
        self.assertIn("signal_diagnostics", response.data)
        self.assertIn("portfolio_performance", response.data)
        self.assertIn("sample_integrity", response.data)

    def test_another_users_experiment_is_a_404(self):
        other = make_user("other-owner")
        make_portfolio(other)
        response = auth_client(other).get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_unbuilt_experiment_returns_409_not_an_empty_report(self):
        Experiment.objects.filter(pk=self.experiment.pk).update(report={}, report_version=0)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(response.data["error"])

    def test_an_invalid_id_is_a_404(self):
        response = self.client.get(reverse("core:experiment-report", args=[str(uuid4())]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_authentication_is_required(self):
        response = APIClient().get(self.url)
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class BuildExperimentCommandTests(TestCase):
    def setUp(self):
        self.user = make_user("command-user")
        self.portfolio = make_portfolio(self.user, balance="10000.00")
        self.run, self.decision = make_debate_run(self.portfolio)
        add_outcome(self.decision)

    def _write(self, manifest: dict) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(manifest))
            path = Path(handle.name)
        return path

    def test_the_command_builds_and_replays_from_a_manifest_file(self):
        path = self._write(base_manifest([run_entry("one", "V2", self.run)]))
        output = StringIO()
        call_command(
            "build_experiment",
            path=str(path),
            username=self.user.username,
            stdout=output,
        )
        experiment = Experiment.objects.get(experiment_key="phase3-test.v1")
        self.assertTrue(experiment.has_report)
        self.assertEqual(experiment.report_version, 1)
        self.assertIn("BUILT", output.getvalue())
        self.assertIn("No LLM or market-data request", output.getvalue())

        call_command(
            "build_experiment",
            path=str(path),
            username=self.user.username,
            stdout=StringIO(),
        )
        experiment.refresh_from_db()
        self.assertEqual(experiment.report_version, 2)
        self.assertEqual(Experiment.objects.count(), 1)

    def test_the_command_refuses_a_drifted_manifest_without_force(self):
        manifest = base_manifest([run_entry("one", "V2", self.run)])
        path = self._write(manifest)
        call_command(
            "build_experiment",
            path=str(path),
            username=self.user.username,
            stdout=StringIO(),
        )
        manifest["label"] = "different label"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(CommandError):
            call_command(
                "build_experiment",
                path=str(path),
                username=self.user.username,
                stdout=StringIO(),
            )

    def test_the_command_can_be_given_a_versioned_manifest_checksum(self):
        manifest = base_manifest([run_entry("one", "V2", self.run)])
        manifest["checksum"] = {"value": checksum_of(manifest)}
        path = self._write(manifest)
        call_command(
            "build_experiment",
            path=str(path),
            username=self.user.username,
            stdout=StringIO(),
        )
        experiment = Experiment.objects.get(experiment_key="phase3-test.v1")
        self.assertEqual(experiment.manifest_checksum, manifest["checksum"]["value"])
