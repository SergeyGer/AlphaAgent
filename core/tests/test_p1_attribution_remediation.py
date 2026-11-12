"""P1 remediation steps 3A-3C: attribution basis, horizons and backfill.

The defects these tests exercise were reproduced against the pre-fix tree:

* ``_start_price`` read ``Transaction.created_at``, a column that does not exist
  on ``Transaction`` (the model stores ``timestamp``), so a fill-linked decision
  raised ``AttributeError`` before any outcome was written;
* ``evaluate_pending`` excluded every decision with *any* outcome, so a day-one
  score made the day-7 and day-30 horizons permanently ineligible;
* a proposal approved later was scored from the proposal-day close instead of
  the recorded fill;
* the scoring path wrote no outcome event or outbox row, so a completed run's
  later horizon could not reach an open view.

Money is ``Decimal`` and timestamps are UTC throughout. The tests build a real
Django ``Transaction`` rather than a stub, because the whole point of the repair
is that the fill's stored price and timestamp are the measurement basis.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.decision_models import (
    DecisionRecord,
    OutcomeEvaluation,
    OutcomeEvaluationState,
    OutcomeMaturity,
    OutcomeRecord,
    RunEvent,
    RunEventType,
)
from core.models import (
    AgentDecisionLog,
    DecisionOutcome,
    RecommendationStatus,
    TradeRecommendation,
    Transaction,
)
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio, make_user
from services import attribution
from services.attribution import (
    BASIS_EXECUTION_FILL,
    BASIS_PROPOSAL_CLOSE,
    OUTCOME_METHOD_VERSION,
    evaluate_pending,
)
from services.decision_runs import create_run

D = Decimal


def price_history(points_by_ticker):
    """A deterministic ``get_price_history`` replacement, keyed by ticker."""
    from services.fundamentals import PriceHistory

    def loader(ticker, period="1y"):
        points = points_by_ticker.get(ticker, [])
        return PriceHistory(
            ticker=ticker,
            period=period,
            points=[(stamp, D(price)) for stamp, price in points],
            latest=D(points[-1][1]) if points else None,
        )

    return loader


def points(base, offsets_prices):
    return [
        ((base + timedelta(days=days)).date().isoformat(), price) for days, price in offsets_prices
    ]


def make_decision(portfolio, *, ticker="AAPL", created_at, action="BUY 2 AAPL"):
    decision = AgentDecisionLog.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        action_taken=action,
        reasoning="recorded reasoning",
    )
    AgentDecisionLog.objects.filter(pk=decision.pk).update(created_at=created_at)
    decision.refresh_from_db()
    return decision


def attach_run(decision, *, action="BUY", quantity="2"):
    run = create_run(
        portfolio=decision.portfolio,
        ticker=decision.ticker,
        protocol_version="test.v1",
        mode="recorded",
        source="fixture",
        legacy_log=decision,
        started_at=decision.created_at,
    )
    DecisionRecord.objects.create(
        run=run,
        action=action,
        requested_quantity=D(quantity) if action != "HOLD" else None,
        concise_rationale="Measured rationale.",
        engine_source="fixture",
    )
    return run


def attach_fill(decision, *, price="130", timestamp, observed_at=None, ticker=None):
    fill = Transaction.objects.create(
        portfolio=decision.portfolio,
        ticker=ticker or decision.ticker,
        tx_type="BUY",
        amount=D("2"),
        price=D(price),
        executed_by="AI",
        price_observed_at=observed_at or timestamp,
        price_source="test_broker_fill",
    )
    Transaction.objects.filter(pk=fill.pk).update(timestamp=timestamp)
    fill.refresh_from_db()
    return fill


@override_settings(CACHES=TEST_CACHES)
class FillAttributionAcceptanceTests(TestCase):
    """3A - a delayed approval is measured from the fill that actually happened."""

    def setUp(self):
        self.user = make_user("fill-attribution")
        self.portfolio = make_portfolio(self.user)
        self.base = timezone.now() - timedelta(days=20)
        self.decision = make_decision(self.portfolio, created_at=self.base)
        self.run = attach_run(self.decision)
        self.fill_at = self.base + timedelta(days=2)
        self.fill = attach_fill(
            self.decision,
            price="130",
            timestamp=self.fill_at,
            observed_at=self.fill_at + timedelta(minutes=1),
        )
        TradeRecommendation.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            action="BUY",
            amount=D("2"),
            price=D("100"),
            notional_usd=D("200"),
            status=RecommendationStatus.EXECUTED,
            decision_log=self.decision,
            decision_run=self.run,
            transaction=self.fill,
            decided_at=self.fill_at,
        )
        self.history = price_history(
            {
                "AAPL": points(
                    self.base,
                    [(0, "100"), (1, "101"), (2, "110"), (3, "143"), (9, "156"), (19, "170")],
                ),
                "SPY": points(
                    self.base,
                    [(0, "400"), (1, "401"), (2, "402"), (3, "404"), (9, "410"), (19, "420")],
                ),
            }
        )
        self.now = self.base + timedelta(days=10)

    def _evaluate(self):
        with patch("services.fundamentals.get_price_history", side_effect=self.history):
            return evaluate_pending(now=self.now)

    def test_the_unmocked_start_price_is_the_recorded_fill_not_the_proposal_close(self):
        counts = self._evaluate()
        self.assertGreaterEqual(counts["scored"], 1)

        outcome = DecisionOutcome.objects.get(decision=self.decision, horizon_days=1)
        self.assertEqual(outcome.price_at_decision, D("130"))
        self.assertEqual(outcome.measurement_basis, BASIS_EXECUTION_FILL)
        self.assertEqual(outcome.fill_id, self.fill.id)
        self.assertEqual(outcome.reference_at, self.fill_at)
        self.assertEqual(outcome.price_observed_at_decision, self.fill_at + timedelta(minutes=1))
        # 130 -> 143 is the fill-based return. The proposal close would have
        # produced 143/100 - 1 = 43%, which is the defect being excluded.
        self.assertEqual(outcome.forward_return_pct, D("10.0000"))

        detail = auth_client(self.user).get(
            reverse("core:decision-run-detail-rich", args=[str(self.run.id)])
        )
        self.assertEqual(detail.status_code, 200)
        row = next(item for item in detail.json()["outcomes"] if item["horizon_days"] == 1)
        self.assertEqual(row["price_at_decision"], "130.00000000")
        self.assertEqual(row["measurement_basis"]["basis"], BASIS_EXECUTION_FILL)
        self.assertEqual(row["measurement_basis"]["fill_id"], self.fill.id)
        self.assertEqual(row["measurement_basis"]["fill_price"], "130.00")
        self.assertFalse(row["measurement_basis"]["portfolio_performance"])
        self.assertIsNotNone(row["price_observed_at_decision"])

    def test_the_scorecard_publishes_that_this_is_a_signal_diagnostic(self):
        self._evaluate()

        body = auth_client(self.user).get(reverse("core:portfolio-attribution")).json()
        horizon_one = next(item for item in body["horizons"] if item["horizon_days"] == 1)
        self.assertGreaterEqual(horizon_one["evaluated"], 1)
        self.assertGreaterEqual(horizon_one["basis_counts"][BASIS_EXECUTION_FILL], 1)
        basis = horizon_one["measurement_basis"]
        self.assertEqual(basis["family"], "signal_diagnostics")
        self.assertFalse(basis["portfolio_performance"])
        self.assertFalse(basis["summable"])
        self.assertEqual(basis["method_version"], OUTCOME_METHOD_VERSION)

        recent = next(item for item in body["recent"] if item["horizon_days"] == 1)
        self.assertEqual(recent["measurement_basis"], BASIS_EXECUTION_FILL)
        self.assertEqual(recent["method_version"], OUTCOME_METHOD_VERSION)

    def test_a_hold_keeps_a_proposal_basis_and_has_no_fabricated_fill(self):
        hold = make_decision(
            self.portfolio, ticker="MSFT", created_at=self.base, action="HOLD - no execution"
        )
        run = attach_run(hold, action="HOLD")
        history = price_history(
            {
                "MSFT": points(self.base, [(0, "300"), (1, "303"), (9, "330")]),
                "SPY": points(self.base, [(0, "400"), (1, "401"), (9, "410")]),
            }
        )
        with patch("services.fundamentals.get_price_history", side_effect=history):
            evaluate_pending(now=self.now, decision_ids=[hold.id])

        outcome = DecisionOutcome.objects.get(decision=hold, horizon_days=1)
        self.assertEqual(outcome.measurement_basis, BASIS_PROPOSAL_CLOSE)
        self.assertIsNone(outcome.fill_id)
        self.assertEqual(outcome.decision_return_pct, D("0.0000"))
        self.assertIsNone(outcome.directional_hit)

        detail = (
            auth_client(self.user)
            .get(reverse("core:decision-run-detail-rich", args=[str(run.id)]))
            .json()
        )
        row = next(item for item in detail["outcomes"] if item["horizon_days"] == 1)
        self.assertEqual(row["measurement_basis"]["basis"], BASIS_PROPOSAL_CLOSE)
        self.assertIsNone(row["measurement_basis"]["fill_id"])
        self.assertIsNone(row["measurement_basis"]["fill_price"])


@override_settings(CACHES=TEST_CACHES)
class HorizonSchedulerAcceptanceTests(TestCase):
    """3B - each missing horizon matures and is recorded exactly once."""

    def setUp(self):
        self.user = make_user("horizons")
        self.portfolio = make_portfolio(self.user)
        self.base = timezone.now() - timedelta(days=31)
        self.decision = make_decision(self.portfolio, created_at=self.base)
        self.run = attach_run(self.decision)
        self.unpriced = make_decision(
            self.portfolio, ticker="MISSING", created_at=self.base, action="BUY 2 MISSING"
        )
        self.unpriced_run = attach_run(self.unpriced)
        self.history = price_history(
            {
                "AAPL": points(
                    self.base,
                    [(0, "100"), (1, "110"), (7, "120"), (30, "150")],
                ),
                "SPY": points(
                    self.base,
                    [(0, "400"), (1, "401"), (7, "404"), (30, "420")],
                ),
            }
        )
        self.client = auth_client(self.user)
        self.day1 = self.base + timedelta(days=1, hours=1)
        self.day3 = self.base + timedelta(days=3, hours=1)
        self.day7 = self.base + timedelta(days=7, hours=1)
        self.day30 = self.base + timedelta(days=30, hours=1)

    def _run(self, now):
        with patch("services.fundamentals.get_price_history", side_effect=self.history):
            return evaluate_pending(now=now)

    def _detail_outcomes(self):
        body = self.client.get(
            reverse("core:decision-run-detail-rich", args=[str(self.run.id)])
        ).json()
        return body["outcomes"]

    def _scorecard(self):
        return {
            item["horizon_days"]: item
            for item in self.client.get(reverse("core:portfolio-attribution")).json()["horizons"]
        }

    def _evaluated_horizons(self):
        rows = [item for item in self._detail_outcomes() if item["maturity"] == "evaluated"]
        return sorted(item["horizon_days"] for item in rows)

    def test_a_day_one_score_does_not_prevent_days_seven_and_thirty(self):
        counts = self._run(self.day1)
        self.assertGreaterEqual(counts["scored"], 1)
        self.assertEqual(self._evaluated_horizons(), [1])
        self.assertEqual(self._scorecard()[1]["evaluated"], 1)
        self.assertEqual(self._scorecard()[7]["evaluated"], 0)
        self.assertEqual(self._scorecard()[30]["evaluated"], 0)
        pending_rows = {
            item["horizon_days"]: item
            for item in self._detail_outcomes()
            if item["maturity"] == "pending"
        }
        self.assertEqual(sorted(pending_rows), [7, 30])
        self.assertIn("matures at", pending_rows[7]["unavailable_reason"])

        # A tick between day 1 and day 7 changes nothing: day 7 is not eligible.
        self._run(self.day3)
        self.assertEqual(self._evaluated_horizons(), [1])
        self.assertEqual(self._scorecard()[7]["evaluated"], 0)

        self._run(self.day7)
        self.assertEqual(self._evaluated_horizons(), [1, 7])
        self.assertEqual(self._scorecard()[7]["evaluated"], 1)
        self.assertEqual(self._scorecard()[30]["evaluated"], 0)

        self._run(self.day30)
        self.assertEqual(self._evaluated_horizons(), [1, 7, 30])
        self.assertEqual([self._scorecard()[h]["evaluated"] for h in (1, 7, 30)], [1, 1, 1])

        # Exactly one projection per horizon, not one per scheduler run.
        horizons = [item["horizon_days"] for item in self._detail_outcomes()]
        self.assertEqual(sorted(horizons), [1, 7, 30])
        self.assertEqual(DecisionOutcome.objects.filter(decision=self.decision).count(), 3)

    def test_retried_and_overlapping_ticks_are_idempotent(self):
        for now in (self.day1, self.day1, self.day7, self.day7, self.day30, self.day30):
            self._run(now)

        self.assertEqual(DecisionOutcome.objects.filter(decision=self.decision).count(), 3)
        self.assertEqual(OutcomeEvaluation.objects.filter(decision=self.decision).count(), 3)
        rows = OutcomeRecord.objects.filter(decision__run=self.run, maturity="evaluated")
        self.assertEqual(sorted(rows.values_list("horizon_days", flat=True)), [1, 7, 30])
        events = RunEvent.objects.filter(run=self.run, event_type=RunEventType.OUTCOME_RECORDED)
        payloads = [event.payload for event in events]
        self.assertEqual(sorted(payload["horizon_days"] for payload in payloads), [1, 7, 30])
        # A retry of identical values has the same revision and dedupe key.
        for payload in payloads:
            self.assertEqual(payload["revision"], 1)

    def test_an_unpriceable_decision_does_not_stop_the_valid_one(self):
        counts = self._run(self.day1)

        self.assertGreaterEqual(counts["scored"], 1)
        self.assertGreaterEqual(counts["unpriced"], 1)
        self.assertEqual(counts["failed"], 0)
        self.assertEqual(DecisionOutcome.objects.filter(decision=self.decision).count(), 1)
        self.assertFalse(DecisionOutcome.objects.filter(decision=self.unpriced).exists())

        evaluation = OutcomeEvaluation.objects.get(decision=self.unpriced, horizon_days=1)
        self.assertEqual(evaluation.state, OutcomeEvaluationState.MATURED)
        self.assertIn("no_entry_price_at_or_after_reference", evaluation.reason)
        self.assertIsNotNone(evaluation.next_attempt_at)

        # A retry before the backoff returns nothing new; advancing time spends
        # the bounded attempt count and leaves a visible unavailable row.
        self._run(self.day1)
        self._run(self.day7)
        self._run(self.day30)
        evaluation.refresh_from_db()
        self.assertEqual(evaluation.state, OutcomeEvaluationState.UNAVAILABLE)
        self.assertGreaterEqual(evaluation.attempt_count, 2)
        self.assertIn("No usable price", evaluation.reason)

        unpriced_detail = self.client.get(
            reverse("core:decision-run-detail-rich", args=[str(self.unpriced_run.id)])
        ).json()
        projected = {item["horizon_days"]: item for item in unpriced_detail["outcomes"]}
        self.assertEqual(projected[1]["maturity"], "unavailable")
        self.assertIn("No usable price", projected[1]["unavailable_reason"])
        self.assertEqual(projected[30]["maturity"], "matured")
        self.assertIn("next retry", projected[30]["unavailable_reason"])

    def test_a_malformed_decision_is_isolated_from_the_batch(self):
        broken = make_decision(
            self.portfolio, ticker="BOOM", created_at=self.base, action="BUY 1 BOOM"
        )
        real_resolve = attribution.resolve_measurement_reference

        def resolve(decision):
            if decision.ticker == "BOOM":
                raise RuntimeError("malformed decision row")
            return real_resolve(decision)

        with (
            patch("services.fundamentals.get_price_history", side_effect=self.history),
            patch.object(attribution, "resolve_measurement_reference", side_effect=resolve),
        ):
            counts = evaluate_pending(now=self.day1)

        self.assertEqual(counts["failed"], 1)
        self.assertGreaterEqual(counts["scored"], 1)
        self.assertTrue(DecisionOutcome.objects.filter(decision=self.decision).exists())
        self.assertFalse(DecisionOutcome.objects.filter(decision=broken).exists())


@override_settings(CACHES=TEST_CACHES)
class AttributionBackfillAcceptanceTests(TestCase):
    """3C - a bounded, dry-run-first, idempotent repair of legacy data."""

    def setUp(self):
        self.user = make_user("backfill")
        self.portfolio = make_portfolio(self.user)
        self.base = timezone.now() - timedelta(days=20)
        self.decision = make_decision(self.portfolio, created_at=self.base)
        self.run = attach_run(self.decision)
        self.fill_at = self.base + timedelta(days=2)
        self.fill = attach_fill(self.decision, price="130", timestamp=self.fill_at)
        TradeRecommendation.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            action="BUY",
            amount=D("2"),
            price=D("100"),
            notional_usd=D("200"),
            status=RecommendationStatus.EXECUTED,
            decision_log=self.decision,
            decision_run=self.run,
            transaction=self.fill,
            decided_at=self.fill_at,
        )
        # The legacy one-day score: measured from the proposal close (100) with
        # no basis recorded. Days 7 and 30 were never evaluated because the old
        # scheduler excluded any decision with an outcome.
        self.legacy = DecisionOutcome.objects.create(
            decision=self.decision,
            horizon_days=1,
            ticker="AAPL",
            action="BUY",
            price_at_decision=D("100"),
            price_at_horizon=D("143"),
            forward_return_pct=D("43"),
            decision_return_pct=D("43"),
            directional_hit=True,
            method_version="attribution.v1",
        )
        OutcomeRecord.objects.create(
            decision=self.run.decision,
            horizon_days=1,
            method_version="attribution.v1",
            maturity=OutcomeMaturity.EVALUATED,
            price_at_decision=D("100"),
            price_at_horizon=D("143"),
            forward_return_pct=D("43"),
            decision_return_pct=D("43"),
            evaluated_at=self.base + timedelta(days=1),
        )
        # An executed state with no resolvable fill: reported, never measured.
        self.unresolved = make_decision(
            self.portfolio, ticker="MSFT", created_at=self.base, action="BUY 1 MSFT"
        )
        TradeRecommendation.objects.create(
            portfolio=self.portfolio,
            ticker="MSFT",
            action="BUY",
            amount=D("1"),
            price=D("300"),
            notional_usd=D("300"),
            status=RecommendationStatus.EXECUTED,
            decision_log=self.unresolved,
        )
        self.history = price_history(
            {
                "AAPL": points(
                    self.base,
                    [(0, "100"), (1, "101"), (2, "110"), (3, "143"), (9, "156")],
                ),
                "SPY": points(
                    self.base,
                    [(0, "400"), (1, "401"), (2, "402"), (3, "404"), (9, "410")],
                ),
            }
        )
        self.now = self.base + timedelta(days=10)

    def _command(self, *args):
        out = StringIO()
        with (
            patch("services.fundamentals.get_price_history", side_effect=self.history),
            patch.object(attribution.timezone, "now", return_value=self.now),
        ):
            call_command("backfill_outcome_records", *args, "--json", stdout=out)
        return json.loads(out.getvalue())

    def test_dry_run_reports_the_missing_horizon_and_the_wrong_basis_without_writing(self):
        before = DecisionOutcome.objects.filter(decision=self.decision).count()
        payload = self._command("--dry-run")

        self.assertTrue(payload["dry_run"])
        kinds = {(item["kind"], item["horizon_days"]) for item in payload["candidates"]}
        self.assertIn(("fill_attribution", 1), kinds)
        self.assertIn(("missing_horizon", 7), kinds)
        self.assertNotIn(("missing_horizon", 30), kinds)  # not matured at day 10
        self.assertEqual(DecisionOutcome.objects.filter(decision=self.decision).count(), before)
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.measurement_basis, "")
        self.assertEqual(self.legacy.price_at_decision, D("100"))

        correction = next(
            item for item in payload["candidates"] if item["kind"] == "fill_attribution"
        )
        self.assertEqual(correction["current_price"], "100.00000000")
        self.assertEqual(correction["fill_price"], "130.00")
        self.assertEqual(correction["intended"], "re_evaluate_from_fill")
        unresolved = payload["unresolved"]
        self.assertTrue(any(item["decision_id"] == self.unresolved.id for item in unresolved))

    def test_apply_recovers_horizons_preserves_provenance_and_is_idempotent(self):
        payload = self._command("--apply")
        self.assertFalse(payload["dry_run"])
        self.assertGreaterEqual(payload["summary"]["applied"]["results_corrected"], 1)
        self.assertGreaterEqual(payload["summary"]["applied"]["results_created"], 1)

        corrected = DecisionOutcome.objects.get(decision=self.decision, horizon_days=1)
        self.assertEqual(corrected.price_at_decision, D("130"))
        self.assertEqual(corrected.measurement_basis, BASIS_EXECUTION_FILL)
        self.assertEqual(corrected.reference_at, self.fill_at)
        self.assertEqual(corrected.revision, 2)
        prior = corrected.provenance["prior_revisions"]
        self.assertEqual(prior[0]["measurement_basis"], "")
        self.assertEqual(prior[0]["price_at_decision"], "100.00000000")
        self.assertEqual(prior[0]["forward_return_pct"], "43.0000")

        recovered = DecisionOutcome.objects.get(decision=self.decision, horizon_days=7)
        self.assertEqual(recovered.measurement_basis, BASIS_EXECUTION_FILL)

        # The v1 projection is left in place as history; v2 is the current one.
        v2 = OutcomeRecord.objects.get(
            decision=self.run.decision,
            horizon_days=1,
            method_version=OUTCOME_METHOD_VERSION,
        )
        self.assertEqual(v2.price_at_decision, D("130"))
        self.assertTrue(
            OutcomeRecord.objects.filter(
                decision=self.run.decision, horizon_days=1, method_version="attribution.v1"
            ).exists()
        )

        detail = (
            auth_client(self.user)
            .get(reverse("core:decision-run-detail-rich", args=[str(self.run.id)]))
            .json()
        )
        current = {item["horizon_days"]: item for item in detail["outcomes"]}
        self.assertEqual(current[1]["method_version"], OUTCOME_METHOD_VERSION)
        self.assertEqual(len([item for item in detail["outcomes"] if item["horizon_days"] == 1]), 1)
        self.assertTrue(any(item["horizon_days"] == 1 for item in detail["superseded_outcomes"]))

        # A second apply finds no missing work and no correction to repeat.
        outcomes_before = DecisionOutcome.objects.filter(decision=self.decision).count()
        records_before = OutcomeRecord.objects.filter(decision__run=self.run).count()
        second = self._command("--apply")
        summary = second["summary"]
        self.assertEqual(summary["missing_horizons"], 0)
        self.assertEqual(summary["fill_attribution_corrections"], 0)
        self.assertEqual(summary["applied"]["results_created"], 0)
        self.assertEqual(summary["applied"]["results_corrected"], 0)
        self.assertEqual(
            DecisionOutcome.objects.filter(decision=self.decision).count(), outcomes_before
        )
        self.assertEqual(
            OutcomeRecord.objects.filter(decision__run=self.run).count(), records_before
        )
        corrected.refresh_from_db()
        self.assertEqual(corrected.revision, 2)
        self.assertFalse(DecisionOutcome.objects.filter(decision=self.unresolved).exists())
