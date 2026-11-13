"""P1 steps 4A-4C — replay by event revision and terminal-unavailable delivery.

Reproduced before the repair:

* the detail endpoint published every outcome revision through the same mutable
  ``OutcomeRecord`` id, so a replay cursor at revision 1 read the current
  revision 2 values (2% was replaced by 4% before the correction was visible);
* a partial event page (sequences 1 and 2 of a server tail of 10) was treated as
  the complete tail, revealing the final verdict, guard and outcomes;
* a ``status.changed`` event carries its destination in ``payload.to``, but the
  reducer read only ``payload.status``, so a real transition was never applied;
* exhausting the pricing retries wrote an unavailable projection and no event,
  so an open completed run could never learn the horizon would not be scored
  (observed: ``state: unavailable, attempts: 3, record: True, events: 0``).

Money is ``Decimal`` and timestamps are UTC. An unavailable result is a measured
absence with a stated reason, never a zero.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.decision_models import (
    DecisionRecord,
    DecisionStatus,
    OutcomeEvaluation,
    OutcomeEvaluationState,
    OutcomeMaturity,
    OutcomeRecord,
    RunEvent,
    RunEventType,
)
from core.event_models import EventOutbox, OutboxStatus
from core.models import AgentDecisionLog
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio, make_user
from services.attribution import (
    OUTCOME_METHOD_VERSION,
    evaluate_decision_horizons,
    evaluate_pending,
)
from services.decision_runs import create_run
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


def make_run(portfolio, *, ticker="AAPL", base):
    decision = AgentDecisionLog.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        action_taken=f"BUY 1 {ticker}",
        reasoning="recorded reasoning",
    )
    AgentDecisionLog.objects.filter(pk=decision.pk).update(created_at=base)
    decision.refresh_from_db()
    run = create_run(
        portfolio=portfolio,
        ticker=ticker,
        protocol_version="test.v1",
        mode="recorded",
        source="fixture",
        legacy_log=decision,
        started_at=base,
    )
    DecisionRecord.objects.create(
        run=run,
        action="BUY",
        requested_quantity=D("1"),
        concise_rationale="Measured rationale.",
        engine_source="fixture",
    )
    return run, decision


@override_settings(CACHES=TEST_CACHES)
class OutcomeRevisionReplayTests(TestCase):
    """4A — each event carries the values of the revision it records."""

    def setUp(self):
        self.user = make_user("outcome-revision")
        self.portfolio = make_portfolio(self.user)
        self.base = timezone.now() - timedelta(days=10)
        self.run, self.decision = make_run(self.portfolio, base=self.base)
        self.now = self.base + timedelta(days=8)

    def _evaluate(self, horizon_price, *, force=False):
        history = price_history(
            {
                "AAPL": points(
                    self.base,
                    [(0, "100"), (7, horizon_price)],
                ),
                "SPY": points(
                    self.base,
                    [(0, "400"), (7, "410")],
                ),
            }
        )
        with patch("services.fundamentals.get_price_history", side_effect=history):
            return evaluate_decision_horizons(
                self.decision, now=self.now, horizons=[7], force=force
            )

    def test_4a_a_correction_appends_a_second_event_that_carries_its_own_values(self):
        self._evaluate("102")
        first = RunEvent.objects.get(
            run=self.run,
            event_type=RunEventType.OUTCOME_RECORDED,
            payload__horizon_days=7,
        )
        self.assertEqual(first.payload["revision"], 1)
        self.assertEqual(first.payload["values"]["decision_return_pct"], "2.0000")
        self.assertEqual(D(first.payload["values"]["price_at_horizon"]), D("102"))
        self.assertIsNotNone(first.payload["price_observed_at"])
        self.assertIsNotNone(first.payload["price_observed_at_decision"])
        self.assertEqual(first.payload["measurement_basis"], "proposal_close")
        self.assertEqual(first.payload["maturity"], OutcomeMaturity.EVALUATED)

        # A genuine correction: same projection row, new revision and values.
        self._evaluate("104", force=True)
        events = RunEvent.objects.filter(
            run=self.run,
            event_type=RunEventType.OUTCOME_RECORDED,
            payload__horizon_days=7,
        ).order_by("sequence")
        self.assertEqual([event.payload["revision"] for event in events], [1, 2])
        self.assertEqual(
            [event.payload["values"]["decision_return_pct"] for event in events],
            ["2.0000", "4.0000"],
        )
        self.assertEqual(
            [D(event.payload["values"]["price_at_horizon"]) for event in events],
            [D("102"), D("104")],
        )
        current = OutcomeRecord.objects.get(
            decision=self.run.decision,
            horizon_days=7,
            method_version=OUTCOME_METHOD_VERSION,
        )
        self.assertEqual(current.revision, 2)
        self.assertEqual(current.decision_return_pct, D("4.0000"))
        # Replaying revision 1 can still read 2% from its own event; the mutable
        # projection has moved to revision 2.
        self.assertEqual(events[0].payload["values"]["decision_return_pct"], "2.0000")

    def test_4a_the_events_endpoint_reports_the_server_tail(self):
        self._evaluate("102")
        client = auth_client(self.user)
        body = client.get(
            reverse("core:decision-run-events", args=[str(self.run.id)]),
            {"after_seq": 0, "limit": 1},
        ).json()
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["last_sequence"], body["events"][0]["seq"])
        self.assertGreaterEqual(body["latest_sequence"], body["last_sequence"])


@override_settings(CACHES=TEST_CACHES)
class UnavailableOutcomeDeliveryTests(TestCase):
    """4C — pending -> unavailable is one durable, versioned event."""

    def setUp(self):
        self.user = make_user("unavailable-delivery")
        self.portfolio = make_portfolio(self.user)
        self.base = timezone.now() - timedelta(days=10)
        self.run, self.decision = make_run(self.portfolio, ticker="MISSING", base=self.base)
        # A completed execution is exactly the case that used to stop looking.
        from core.decision_models import DecisionRun

        DecisionRun.objects.filter(pk=self.run.pk).update(
            status=DecisionStatus.COMPLETED,
            execution_state="executed",
            completed_at=self.base + timedelta(minutes=5),
        )
        self.run.refresh_from_db()
        self.day1 = self.base + timedelta(days=1)
        self.history = price_history(
            {
                "SPY": points(self.base, [(0, "400"), (1, "401"), (7, "404")]),
            }
        )

    def _attempts(self, count=3):
        with patch("services.fundamentals.get_price_history", side_effect=self.history):
            for offset in (
                timedelta(0),
                timedelta(minutes=16),
                timedelta(minutes=46),
            )[:count]:
                evaluate_pending(now=self.day1 + offset)

    def test_4c_exhausted_pricing_writes_one_event_one_outbox_row_and_a_reason(self):
        self._attempts()

        evaluation = OutcomeEvaluation.objects.get(decision=self.decision, horizon_days=1)
        self.assertEqual(evaluation.state, OutcomeEvaluationState.UNAVAILABLE)
        self.assertEqual(evaluation.attempt_count, 3)
        projection = OutcomeRecord.objects.get(
            decision=self.run.decision,
            horizon_days=1,
            method_version=OUTCOME_METHOD_VERSION,
        )
        self.assertEqual(projection.maturity, OutcomeMaturity.UNAVAILABLE)
        self.assertIn("no_entry_price_at_or_after_reference", projection.unavailable_reason)
        self.assertIn("No usable price", projection.unavailable_reason)

        events = RunEvent.objects.filter(
            run=self.run,
            event_type=RunEventType.OUTCOME_RECORDED,
            payload__horizon_days=1,
        )
        self.assertEqual(events.count(), 1)
        payload = events.get().payload
        self.assertEqual(payload["event_schema"], "outcome.recorded.v1")
        self.assertEqual(payload["maturity"], OutcomeMaturity.UNAVAILABLE)
        self.assertIn("no_entry_price_at_or_after_reference", payload["unavailable_reason"])
        self.assertEqual(payload["attempt_count"], 3)
        self.assertIsNone(payload["values"]["decision_return_pct"])
        self.assertEqual(
            EventOutbox.objects.filter(payload__event_type=RunEventType.OUTCOME_RECORDED).count(),
            1,
        )

        # The completed run stays completed and executed while the late outcome
        # is appended; the reason reaches the detail projection.
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, DecisionStatus.COMPLETED)
        self.assertEqual(self.run.execution_state, "executed")
        body = (
            auth_client(self.user)
            .get(reverse("core:decision-run-detail-rich", args=[str(self.run.id)]))
            .json()
        )
        row = next(item for item in body["outcomes"] if item["horizon_days"] == 1)
        self.assertEqual(row["maturity"], "unavailable")
        self.assertIn("No usable price", row["unavailable_reason"])

        # A reconnect reconstructs the record from the outbox when live delivery
        # was interrupted, and a retry of the tick adds nothing.
        caught_up = catch_up_events(self.user.id)
        self.assertEqual(
            sum(
                1
                for item in caught_up
                if item["payload"].get("event_type") == RunEventType.OUTCOME_RECORDED
            ),
            1,
        )
        with patch("services.event_outbox._send", return_value=True):
            flush_outbox()
        self.assertTrue(
            all(
                row.status == OutboxStatus.DELIVERED
                for row in EventOutbox.objects.filter(
                    payload__event_type=RunEventType.OUTCOME_RECORDED
                )
            )
        )
        self._attempts()
        self.assertEqual(
            RunEvent.objects.filter(
                run=self.run,
                event_type=RunEventType.OUTCOME_RECORDED,
                payload__horizon_days=1,
            ).count(),
            1,
        )

    def test_4c_a_failed_unavailable_event_rolls_back_the_state_and_projection(self):
        self._attempts(count=2)  # two failed attempts, still retryable
        before = OutcomeEvaluation.objects.get(decision=self.decision, horizon_days=1)
        self.assertEqual(before.state, OutcomeEvaluationState.MATURED)
        self.assertEqual(before.attempt_count, 2)
        projection_before = OutcomeRecord.objects.get(decision=self.run.decision, horizon_days=1)
        self.assertNotEqual(projection_before.maturity, OutcomeMaturity.UNAVAILABLE)

        with (
            patch("services.fundamentals.get_price_history", side_effect=self.history),
            patch(
                "services.attribution.append_event",
                side_effect=RuntimeError("event store unavailable"),
            ),
        ):
            counts = evaluate_decision_horizons(
                self.decision, now=self.day1 + timedelta(minutes=46), horizons=[1]
            )
        self.assertEqual(counts["unpriced"], 1)
        after = OutcomeEvaluation.objects.get(decision=self.decision, horizon_days=1)
        self.assertEqual(after.state, OutcomeEvaluationState.MATURED)
        self.assertEqual(after.attempt_count, 2)
        projection = OutcomeRecord.objects.get(decision=self.run.decision, horizon_days=1)
        self.assertNotEqual(projection.maturity, OutcomeMaturity.UNAVAILABLE)
        self.assertFalse(
            RunEvent.objects.filter(run=self.run, event_type=RunEventType.OUTCOME_RECORDED).exists()
        )
        self.assertFalse(
            EventOutbox.objects.filter(payload__event_type=RunEventType.OUTCOME_RECORDED).exists()
        )
