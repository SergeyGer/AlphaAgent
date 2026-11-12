"""P1 remediation steps 4A-4C: guard replay order and late outcome delivery.

Reproduced before the repair:

* the detail payload carried every guard record but the shared replay reducer
  always painted ``detail.guard`` (the final evaluation) from the first
  ``guard.evaluated`` event, so a preflight approval of 10 was replaced by a
  later rejection before the rejection event was visible;
* the scoring path appended no ``outcome.recorded`` event and no outbox row, so
  a completed run had no way to tell an open view that a later horizon had
  matured;
* the export bundle dropped all but the summary guard when imported, which is
  why the offline viewer could not show the preflight evaluation either.

These tests hold the backend half: the guard collection and stable identifiers
in the detail/export contracts, the versioned outcome event written with its
result and detail projection, and delivery through the existing outbox after an
interrupted dispatch.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.decision_models import (
    AgentRole,
    DecisionRecord,
    DecisionRun,
    DecisionStatus,
    GuardRecord,
    OutcomeMaturity,
    OutcomeRecord,
    RunEvent,
    RunEventType,
    RunStage,
)
from core.event_models import EventOutbox, OutboxStatus
from core.models import AgentDecisionLog, DecisionOutcome
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio, make_user
from services.attribution import (
    OUTCOME_METHOD_VERSION,
    evaluate_decision_horizons,
    evaluate_pending,
)
from services.decision_runs import append_event, create_run
from services.event_outbox import catch_up_events, flush_outbox

D = Decimal


def price_history(points_by_ticker):
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


def make_run(portfolio, *, ticker="AAPL", created_at, with_decision=True):
    decision = AgentDecisionLog.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        action_taken=f"BUY 2 {ticker}",
        reasoning="recorded reasoning",
    )
    AgentDecisionLog.objects.filter(pk=decision.pk).update(created_at=created_at)
    decision.refresh_from_db()
    run = create_run(
        portfolio=portfolio,
        ticker=ticker,
        protocol_version="test.v1",
        mode="recorded",
        source="fixture",
        legacy_log=decision,
        started_at=created_at,
    )
    if with_decision:
        DecisionRecord.objects.create(
            run=run,
            action="BUY",
            requested_quantity=D("2"),
            concise_rationale="Measured rationale.",
            engine_source="fixture",
        )
    return run, decision


@override_settings(CACHES=TEST_CACHES)
class GuardEvaluationExposureTests(TestCase):
    """4A - every evaluation is addressable, and the summary is the newest."""

    def setUp(self):
        self.user = make_user("guard-exposure")
        self.portfolio = make_portfolio(self.user)
        self.base = timezone.now() - timedelta(days=3)
        self.run, _decision = make_run(self.portfolio, created_at=self.base)
        self.decision = self.run.decision
        self.preflight = GuardRecord.objects.create(
            proposal=self.decision,
            policy_version="guard.v1",
            evaluated_portfolio_version="v1",
            quote_time=self.base,
            quote_source="test_quote",
            status="approved",
            requested_quantity=D("10"),
            allowed_quantity=D("10"),
            binding_rule="max_trade_allocation_pct",
            reason_codes=[],
            evaluation_key="guard:preflight",
            created_at=self.base,
        )
        self.final = GuardRecord.objects.create(
            proposal=self.decision,
            policy_version="guard.v1",
            evaluated_portfolio_version="v2",
            quote_time=self.base + timedelta(days=1),
            quote_source="test_quote",
            status="rejected",
            requested_quantity=D("10"),
            allowed_quantity=D("0"),
            binding_rule="approval_terms",
            reason_codes=["approval_terms_changed"],
            evaluation_key="guard:final",
            created_at=self.base + timedelta(days=1),
        )
        for guard in (self.preflight, self.final):
            append_event(
                self.run,
                RunEventType.GUARD_EVALUATED,
                stage=RunStage.VALIDATING,
                agent_role=AgentRole.SYSTEM,
                payload={
                    "guard_id": str(guard.id),
                    "status": guard.status,
                    "requested_quantity": str(guard.requested_quantity),
                    "allowed_quantity": str(guard.allowed_quantity),
                    "binding_rule": guard.binding_rule,
                    "quote_time": guard.quote_time.isoformat(),
                },
                occurred_at=guard.created_at,
                dedupe_key=f"guard_evaluated:{guard.id}",
            )
        self.client = auth_client(self.user)

    def test_detail_publishes_every_evaluation_with_its_identifier(self):
        body = self.client.get(
            reverse("core:decision-run-detail-rich", args=[str(self.run.id)])
        ).json()

        self.assertEqual(len(body["guard_evaluations"]), 2)
        self.assertEqual(len(body["guards"]), 2)
        self.assertEqual(
            [row["id"] for row in body["guard_evaluations"]],
            [str(self.preflight.id), str(self.final.id)],
        )
        preflight = next(
            row for row in body["guard_evaluations"] if row["id"] == str(self.preflight.id)
        )
        self.assertEqual(preflight["requested_quantity"], "10.00000000")
        self.assertEqual(preflight["allowed_quantity"], "10.00000000")
        self.assertEqual(preflight["status"], "approved")
        self.assertIsNotNone(preflight["quote_time"])

        # The summary field is the most recent evaluation, kept for finished views.
        self.assertEqual(body["guard"]["id"], str(self.final.id))
        self.assertEqual(body["guard_comparison"]["id"], str(self.final.id))
        self.assertEqual(D(body["guard_comparison"]["allowed_quantity"]), D("0"))

    def test_the_export_preserves_the_whole_collection(self):
        body = self.client.get(reverse("core:decision-run-export", args=[str(self.run.id)])).json()

        self.assertEqual(
            [row["id"] for row in body["guard_evaluations"]],
            [str(self.preflight.id), str(self.final.id)],
        )
        self.assertEqual(body["guard_comparison"]["id"], str(self.final.id))

    def test_the_guard_events_carry_the_same_identifiers(self):
        events = RunEvent.objects.filter(
            run=self.run, event_type=RunEventType.GUARD_EVALUATED
        ).order_by("sequence")
        self.assertEqual(
            [event.payload["guard_id"] for event in events],
            [str(self.preflight.id), str(self.final.id)],
        )


@override_settings(CACHES=TEST_CACHES)
class LateOutcomeEventTests(TestCase):
    """4B/4C - a completed run receives a versioned late outcome, once."""

    def setUp(self):
        self.user = make_user("late-outcome")
        self.portfolio = make_portfolio(self.user)
        self.base = timezone.now() - timedelta(days=10)
        self.run, self.decision = make_run(self.portfolio, created_at=self.base)
        DecisionRun.objects.filter(pk=self.run.pk).update(
            status=DecisionStatus.COMPLETED,
            execution_state="executed",
            completed_at=self.base + timedelta(minutes=5),
        )
        self.run.refresh_from_db()
        self.history = price_history(
            {
                "AAPL": points(
                    self.base,
                    [(0, "100"), (1, "110"), (7, "120"), (10, "130")],
                ),
                "SPY": points(
                    self.base,
                    [(0, "400"), (1, "401"), (7, "404"), (10, "410")],
                ),
            }
        )
        self.now = self.base + timedelta(days=7, hours=1)

    def _evaluate(self):
        with patch("services.fundamentals.get_price_history", side_effect=self.history):
            return evaluate_pending(now=self.now)

    def test_a_completed_run_keeps_its_execution_state_and_records_the_late_outcome(self):
        self._evaluate()

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, DecisionStatus.COMPLETED)
        self.assertEqual(self.run.execution_state, "executed")
        records = OutcomeRecord.objects.filter(
            decision__run=self.run, maturity=OutcomeMaturity.EVALUATED
        )
        self.assertEqual(sorted(records.values_list("horizon_days", flat=True)), [1, 7])

        events = RunEvent.objects.filter(run=self.run, event_type=RunEventType.OUTCOME_RECORDED)
        self.assertEqual(sorted(event.payload["horizon_days"] for event in events), [1, 7])
        for event in events:
            payload = event.payload
            self.assertEqual(payload["event_schema"], "outcome.recorded.v1")
            self.assertEqual(payload["method_version"], OUTCOME_METHOD_VERSION)
            self.assertTrue(payload["outcome_id"])
            self.assertIsNotNone(payload["price_observed_at"])
            self.assertIn("values", payload)
            self.assertEqual(payload["revision"], 1)

    def test_a_retry_does_not_append_a_second_event(self):
        self._evaluate()
        before = RunEvent.objects.filter(
            run=self.run, event_type=RunEventType.OUTCOME_RECORDED
        ).count()
        self._evaluate()
        after = RunEvent.objects.filter(
            run=self.run, event_type=RunEventType.OUTCOME_RECORDED
        ).count()
        self.assertEqual(before, after)
        self.assertEqual(before, 2)

    def test_a_genuine_correction_gets_a_new_idempotency_key_and_revision(self):
        self._evaluate()
        first = RunEvent.objects.filter(
            run=self.run,
            event_type=RunEventType.OUTCOME_RECORDED,
            payload__horizon_days=1,
        ).first()
        self.assertIsNotNone(first)
        self.assertEqual(first.payload["revision"], 1)

        # The recorded values change: this is a correction, not a retry. The
        # dedupe key contains the revision and content hash, so it is a new event.
        corrected_history = price_history(
            {
                "AAPL": points(
                    self.base,
                    [(0, "100"), (1, "150"), (7, "160"), (10, "170")],
                ),
                "SPY": points(
                    self.base,
                    [(0, "400"), (1, "401"), (7, "404"), (10, "410")],
                ),
            }
        )
        with patch(
            "services.fundamentals.get_price_history",
            side_effect=corrected_history,
        ):
            evaluate_pending(now=self.now, decision_ids=[self.decision.id])
            # The scheduler treats an evaluated horizon as done, so the explicit
            # correction path is the same one the backfill uses.
            from services.attribution import evaluate_decision_horizons

            evaluate_decision_horizons(self.decision, now=self.now, horizons=[1], force=True)

        for_horizon = RunEvent.objects.filter(
            run=self.run,
            event_type=RunEventType.OUTCOME_RECORDED,
            payload__horizon_days=1,
        ).order_by("sequence")
        self.assertEqual(for_horizon.count(), 2)
        self.assertEqual([event.payload["revision"] for event in for_horizon], [1, 2])
        self.assertNotEqual(
            for_horizon[0].payload["content_hash"],
            for_horizon[1].payload["content_hash"],
        )

    def test_an_interrupted_dispatch_is_delivered_later_from_the_outbox(self):
        # The test transaction does not commit, so the after-commit live delivery
        # never runs: this is exactly the worker dying between commit and dispatch.
        self._evaluate()
        outcome_events = list(
            RunEvent.objects.filter(run=self.run, event_type=RunEventType.OUTCOME_RECORDED)
        )
        self.assertEqual(len(outcome_events), 2)
        queued = EventOutbox.objects.filter(payload__event_type=RunEventType.OUTCOME_RECORDED)
        self.assertEqual(queued.count(), 2)
        self.assertTrue(all(row.status == OutboxStatus.PENDING for row in queued))

        # A reconnect reconstructs the timeline from the outbox rows even though
        # no live frame was ever sent.
        caught_up = catch_up_events(self.user.id)
        recorded = [
            row
            for row in caught_up
            if row["payload"].get("event_type") == RunEventType.OUTCOME_RECORDED
        ]
        self.assertEqual(len(recorded), 2)

        with patch("services.event_outbox._send", return_value=True):
            result = flush_outbox()
        self.assertGreaterEqual(result["delivered"], 2)
        self.assertTrue(
            all(
                row.status == OutboxStatus.DELIVERED
                for row in EventOutbox.objects.filter(
                    payload__event_type=RunEventType.OUTCOME_RECORDED
                )
            )
        )
        # Delivery did not duplicate the event log.
        self.assertEqual(
            RunEvent.objects.filter(run=self.run, event_type=RunEventType.OUTCOME_RECORDED).count(),
            2,
        )


@override_settings(CACHES=TEST_CACHES)
class OutcomeAtomicityTests(TestCase):
    """4.4 - the result, its projection and its event commit or roll back together."""

    def test_an_event_write_failure_rolls_back_the_result_and_projection(self):
        portfolio = make_portfolio(make_user("outcome-atomicity"))
        base = timezone.now() - timedelta(days=10)
        run, decision = make_run(portfolio, ticker="MSFT", created_at=base)
        history = price_history(
            {
                "MSFT": points(base, [(0, "300"), (1, "330"), (10, "350")]),
                "SPY": points(base, [(0, "400"), (1, "401"), (10, "410")]),
            }
        )
        now = base + timedelta(days=2)

        with (
            patch("services.fundamentals.get_price_history", side_effect=history),
            patch(
                "services.attribution.append_event",
                side_effect=RuntimeError("event store unavailable"),
            ),
        ):
            counts = evaluate_decision_horizons(decision, now=now, horizons=[1])

        self.assertEqual(counts["unpriced"], 1)
        self.assertFalse(DecisionOutcome.objects.filter(decision=decision).exists())
        self.assertFalse(
            OutcomeRecord.objects.filter(
                decision__run=run, method_version=OUTCOME_METHOD_VERSION
            ).exists()
        )
        self.assertFalse(
            RunEvent.objects.filter(run=run, event_type=RunEventType.OUTCOME_RECORDED).exists()
        )
        # The work row exists so a retry can try again; it is not marked done.
        from core.decision_models import OutcomeEvaluation

        evaluation = OutcomeEvaluation.objects.get(decision=decision, horizon_days=1)
        self.assertNotEqual(evaluation.state, "evaluated")
        self.assertEqual(evaluation.attempt_count, 0)
        self.assertIsNone(evaluation.outcome_id)
        self.assertIsNone(evaluation.outcome_record_id)
