"""FIX-06 acceptance: durable event delivery, catch-up and isolation.

The domain record is authoritative; the socket is a hint. These tests stop the
delivery step, restart it, and reconstruct the timeline from the queue - exactly
the sequence a process crash or a Redis outage causes.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.decision_models import DecisionRun, DecisionStatus, RunEventType
from core.event_models import EventOutbox, OutboxStatus
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio
from services.decision_runs import append_event, create_run, record_failure
from services.event_outbox import (
    catch_up_events,
    enqueue_event,
    flush_outbox,
    outbox_metrics,
)
from services.events import EventType, publish

D = None  # keep ruff quiet about unused import shapes in this module


@override_settings(CACHES=TEST_CACHES)
class DurableDeliveryTests(TestCase):
    def setUp(self):
        self.portfolio = make_portfolio()
        self.user = self.portfolio.user

    def test_a_committed_event_is_delivered_after_a_restart(self):
        row = enqueue_event(
            self.user.id,
            "portfolio",
            EventType.TRADE_EXECUTED,
            {"ticker": "AAPL"},
            dedupe_key="run_event:restart-1",
        )
        self.assertEqual(row.status, OutboxStatus.PENDING)
        # The process is stopped here: nothing calls deliver_now. A restart
        # runs only the periodic flush, which must find the committed row.
        with patch("services.event_outbox._send", return_value=True) as send:
            result = flush_outbox()
        self.assertEqual(result["delivered"], 1)
        send.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.status, OutboxStatus.DELIVERED)
        self.assertIsNotNone(row.delivered_at)
        self.assertEqual(EventOutbox.objects.count(), 1)

    def test_redis_outage_restores_exactly_one_ordered_timeline(self):
        with patch("services.event_outbox._send", return_value=False):
            publish(self.user.id, "decision_run.event", {"sequence": 1}, dedupe_key="run:a")
            publish(self.user.id, "decision_run.event", {"sequence": 2}, dedupe_key="run:b")
            # A replayed notification for the first event must not add a row.
            publish(self.user.id, "decision_run.event", {"sequence": 1}, dedupe_key="run:a")
        self.assertEqual(EventOutbox.objects.count(), 2)
        self.assertEqual(outbox_metrics(user_id=self.user.id)["pending"], 2)

        # The failed attempts scheduled a retry; the restart happens after it.
        with patch("services.event_outbox._send", return_value=True):
            result = flush_outbox(now=timezone.now() + timezone.timedelta(minutes=5))
        self.assertEqual(result["delivered"], 2)

        events = catch_up_events(self.user.id)
        self.assertEqual([event["payload"]["sequence"] for event in events], [1, 2])
        self.assertTrue(all(event["delivery_status"] == OutboxStatus.DELIVERED for event in events))
        self.assertEqual(len({event["event_id"] for event in events}), 2)

    def test_terminal_run_events_are_recoverable_from_the_queue(self):
        run = create_run(
            portfolio=self.portfolio,
            ticker="AAPL",
            protocol_version="test.v1",
            mode="live",
            source="crewai",
        )
        with patch("services.event_outbox._send", return_value=False):
            record_failure(run, error_code="execution_failed", error_detail="boom")
        run.refresh_from_db()
        self.assertEqual(run.status, DecisionStatus.FAILED)
        pending = EventOutbox.objects.filter(
            dedupe_key__startswith="run_event:", status=OutboxStatus.PENDING
        )
        self.assertEqual(pending.count(), 2)  # run.created + status.changed

        # Persistence is the authority: the terminal state is visible in the
        # detail endpoint before any notification has been delivered.
        client = auth_client(self.user)
        detail = client.get(reverse("core:decision-run-detail", args=[str(run.id)])).json()
        self.assertEqual(detail["status"], DecisionStatus.FAILED)

        with patch("services.event_outbox._send", return_value=True):
            flush_outbox()
        timeline = catch_up_events(self.user.id)
        types = [event["event_type"] for event in timeline]
        self.assertIn(
            RunEventType.RUN_CREATED, [event["payload"]["event_type"] for event in timeline]
        )
        self.assertIn("decision_run.event", types)

        response = client.get(reverse("core:decision-run-events", args=[str(run.id)]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["events"])

    def test_a_retried_run_handler_cannot_duplicate_the_event_or_the_notification(self):
        run = create_run(
            portfolio=self.portfolio,
            ticker="AAPL",
            protocol_version="test.v1",
            mode="live",
            source="crewai",
        )
        for _ in range(2):
            append_event(
                run,
                RunEventType.LIMITATION_RECORDED,
                stage="collecting",
                payload={"limitation": "recorded once"},
                dedupe_key="durable-handler",
            )
        self.assertEqual(run.events.filter(event_type=RunEventType.LIMITATION_RECORDED).count(), 1)
        self.assertEqual(
            EventOutbox.objects.filter(dedupe_key__startswith="run_event:").count(),
            2,  # run.created + the one deduplicated limitation
        )

    def test_flush_task_delivers_and_reports_backlog(self):
        from tasks import flush_event_outbox_task

        with patch("services.event_outbox._send", return_value=False):
            publish(self.user.id, EventType.DECISION_CREATED, {"id": 1})
        result = flush_event_outbox_task.apply().get()
        # ``_send`` is restored to the real channel layer, which is unavailable
        # in this test environment: the task reports the backlog rather than
        # pretending delivery succeeded.
        self.assertEqual(result["status"], "ok")
        self.assertIn("pending", result)
        self.assertGreaterEqual(result["pending"], 1)


@override_settings(CACHES=TEST_CACHES)
class DeliveryIsolationTests(TestCase):
    def setUp(self):
        self.owner = make_portfolio()
        self.other = make_portfolio()

    def test_catch_up_is_scoped_to_the_authenticated_user(self):
        with patch("services.event_outbox._send", return_value=False):
            enqueue_event(
                self.owner.user_id, "portfolio", EventType.TRADE_EXECUTED, {"ticker": "SECRET"}
            )
            enqueue_event(
                self.other.user_id, "portfolio", EventType.TRADE_EXECUTED, {"ticker": "OTHER"}
            )

        client = auth_client(self.owner.user)
        response = client.get(reverse("core:event-catch-up"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["payload"]["ticker"], "SECRET")

        metrics = client.get(reverse("core:event-metrics")).json()
        self.assertEqual(metrics["pending"], 1)

    def test_anonymous_access_is_refused(self):
        from rest_framework.test import APIClient

        client = APIClient()
        for name in ("core:event-catch-up", "core:event-metrics"):
            self.assertIn(client.get(reverse(name)).status_code, (401, 403))

    def test_another_user_cannot_read_the_run_or_its_events(self):
        run = create_run(
            portfolio=self.owner,
            ticker="AAPL",
            protocol_version="test.v1",
            mode="live",
            source="crewai",
        )
        client = auth_client(self.other.user)
        for name in ("core:decision-run-detail", "core:decision-run-events"):
            response = client.get(reverse(name, args=[str(run.id)]))
            self.assertEqual(response.status_code, 404, name)
        self.assertTrue(DecisionRun.objects.filter(pk=run.pk).exists())
