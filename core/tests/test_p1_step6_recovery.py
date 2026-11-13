"""P1 step 6 follow-up: allocation jobs recover without a user retry.

Reproduced before this change:

* a broker publish failure after the durable ``AllocationSubmission`` commit left
  the row ``queued`` with ``last_dispatched_at`` set and no discovery path: the
  repair command saw zero candidates, so the job waited for another POST;
* resuming after the provider result was saved rebuilt the brief from the
  changed market. Changing AAPL from 100 to 120 between attempts produced a plan
  whose input price was 120 while the saved response had answered 100.

The repair service existed but nothing ran it. These tests drive the scheduled
task directly, which is exactly what Celery Beat dispatches; no second POST and
no manual management command is used to recover.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.allocation_models import AllocationSubmission, AllocationSubmissionStatus
from core.models import InputSnapshot, PortfolioPlan, Transaction
from core.tests.test_allocation_workflow import (
    AllocationWorkflowFixture,
    fake_quote,
)
from core.tests.test_p1_allocation_jobs import CountingStage, budgeted
from services.allocation_submission import dispatch_submission, process_submission
from tasks import recover_allocation_submissions_task

D = Decimal


class DispatchRecoveryTests(AllocationWorkflowFixture):
    """6A: the commit/publish gap is discovered automatically."""

    def status_of(self, run_id: str):
        return self.client.get(reverse("core:decision-run-status", args=[str(run_id)]))

    def _fail_publication(self, key: str):
        from services import allocation_submission as submission_service

        with (
            patch(
                "core.decision_views.dispatch_submission",
                side_effect=submission_service.dispatch_submission,
            ),
            patch(
                "tasks.run_allocation_submission_task.delay",
                side_effect=RuntimeError("broker down"),
            ),
        ):
            return self.submit(key, mode="stub")

    def test_6a_a_broker_failure_progresses_without_another_post_or_command(self):
        response = self._fail_publication("6a-recovered")
        self.assertEqual(response.status_code, 202, response.data)
        submission = AllocationSubmission.objects.get(pk=response.data["run_id"])
        self.assertEqual(submission.status, AllocationSubmissionStatus.QUEUED)
        self.assertEqual(submission.dispatch_attempt_count, 1)
        self.assertEqual(submission.last_dispatch_error, "broker_unavailable")
        self.assertIsNotNone(submission.last_dispatched_at)

        # The existing status endpoint already exposes the gap.
        polled = self.status_of(response.data["run_id"])
        self.assertEqual(polled.status_code, 200)
        self.assertEqual(polled.data["status"], AllocationSubmissionStatus.QUEUED)
        self.assertEqual(polled.data["dispatch"]["attempts"], 1)
        self.assertEqual(polled.data["dispatch"]["last_error"], "broker_unavailable")
        self.assertFalse(polled.data["dispatch"]["published"])
        self.assertFalse(polled.data["recovered"])

        # The broker is back. One scheduled tick publishes the durable row; the
        # eager worker stands in for the worker the broker would notify.
        processed: list[str] = []

        def eager_publish(run_id: str) -> None:
            processed.append(run_id)
            process_submission(run_id, owner="recovered-worker")

        with patch("tasks.run_allocation_submission_task.delay", side_effect=eager_publish):
            result = recover_allocation_submissions_task(limit=10)

        self.assertEqual(processed, [str(submission.id)])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dispatched"], 1)
        submission.refresh_from_db()
        self.assertEqual(submission.status, AllocationSubmissionStatus.SUCCEEDED)
        self.assertEqual(submission.dispatch_attempt_count, 2)
        self.assertEqual(submission.last_dispatch_error, "")
        self.assertIsNotNone(submission.plan_id)
        self.assertEqual(PortfolioPlan.objects.count(), 1)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

        final = self.status_of(response.data["run_id"])
        self.assertTrue(final.data["terminal"])
        self.assertEqual(final.data["status"], AllocationSubmissionStatus.SUCCEEDED)
        self.assertEqual(final.data["plan_id"], str(submission.plan_id))
        self.assertEqual(final.data["dispatch"]["attempts"], 2)
        self.assertIsNone(final.data["dispatch"]["last_error"])
        self.assertTrue(final.data["dispatch"]["published"])
        self.assertTrue(final.data["recovered"])
        self.assertEqual(final.data["recovery"]["reason"], "dispatch_failed")

    def test_6a_a_past_deadline_row_recovers_to_a_typed_expiry(self):
        response = self._fail_publication("6a-expired")
        AllocationSubmission.objects.filter(pk=response.data["run_id"]).update(
            queued_at=timezone.now() - timedelta(hours=1),
            deadline_at=timezone.now() - timedelta(seconds=1),
        )
        with patch(
            "tasks.run_allocation_submission_task.delay",
            side_effect=AssertionError("a past-deadline row must not be published"),
        ):
            result = recover_allocation_submissions_task(limit=10)

        submission = AllocationSubmission.objects.get(pk=response.data["run_id"])
        self.assertEqual(submission.status, AllocationSubmissionStatus.TIMED_OUT)
        self.assertEqual(submission.error_code, "analysis_timeout")
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(PortfolioPlan.objects.count(), 0)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

        polled = self.status_of(response.data["run_id"])
        self.assertTrue(polled.data["terminal"])
        self.assertEqual(polled.data["failure_reason"], "analysis_timeout")
        self.assertTrue(polled.data["recovered"])
        self.assertEqual(polled.data["recovery"]["reason"], "analysis_timeout")

    def test_6a_a_never_dispatched_row_is_discovered_immediately(self):
        # The process may die between the row commit and the dispatch call: no
        # attempt is recorded at all. A fresh row must still be discovered.
        response = self.submit("6a-never-published", mode="stub")
        self.assertEqual(response.data["status"], AllocationSubmissionStatus.QUEUED)
        AllocationSubmission.objects.filter(pk=response.data["run_id"]).update(
            last_dispatched_at=None, dispatch_attempt_count=0, last_dispatch_error=""
        )
        with patch(
            "tasks.run_allocation_submission_task.delay",
            side_effect=AssertionError("the broker is not part of this test"),
        ) as publish:
            recover_allocation_submissions_task(limit=10)
        # The test fixture patched dispatch at the view, so this row was never
        # dispatched by the POST. The sweep must find it even though it is new.
        self.assertEqual(publish.call_count, 1)


class RecoveryUnderLockTests(AllocationWorkflowFixture):
    """6B: recovery rechecks the lease, deadline and state before acting."""

    def _submission(self, key: str) -> AllocationSubmission:
        response = self.submit(key, mode="stub")
        return AllocationSubmission.objects.get(pk=response.data["run_id"])

    def test_6b_a_valid_lease_is_kept_and_only_the_expired_one_is_recovered(self):
        now = timezone.now()
        alive = self._submission("6b-alive")
        expired = self._submission("6b-expired")
        AllocationSubmission.objects.filter(pk=alive.pk).update(
            status=AllocationSubmissionStatus.RUNNING,
            claimed_by="alive-worker",
            claimed_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(minutes=5),
            deadline_at=now + timedelta(minutes=5),
            attempt_count=1,
        )
        AllocationSubmission.objects.filter(pk=expired.pk).update(
            status=AllocationSubmissionStatus.RUNNING,
            claimed_by="dead-worker",
            claimed_at=now - timedelta(minutes=10),
            heartbeat_at=now - timedelta(minutes=10),
            lease_expires_at=now - timedelta(minutes=5),
            deadline_at=now + timedelta(minutes=5),
            attempt_count=1,
        )
        published: list[str] = []
        with patch(
            "tasks.run_allocation_submission_task.delay",
            side_effect=lambda key: published.append(key),
        ):
            result = recover_allocation_submissions_task(limit=10)

        alive.refresh_from_db()
        expired.refresh_from_db()
        self.assertEqual(alive.status, AllocationSubmissionStatus.RUNNING)
        self.assertEqual(alive.claimed_by, "alive-worker")
        self.assertGreater(alive.lease_expires_at, now)
        self.assertEqual(expired.status, AllocationSubmissionStatus.QUEUED)
        self.assertEqual(expired.claimed_by, "")
        self.assertIsNone(expired.lease_expires_at)
        self.assertEqual(published, [str(expired.id)])
        self.assertEqual(result["inspected"], 1)
        self.assertEqual(result["actions"][0]["run_id"], str(expired.id))
        self.assertEqual(result["actions"][0]["reason"], "worker_lost")
        self.assertEqual(expired.worker_metadata["recovery"]["action"], "requeue")

    def test_6b_an_expired_lease_with_an_unknown_paid_attempt_is_failed_not_retried(self):
        now = timezone.now()
        submission = self._submission("6b-unreconciled")
        AllocationSubmission.objects.filter(pk=submission.pk).update(
            status=AllocationSubmissionStatus.RUNNING,
            claimed_by="dead-worker",
            heartbeat_at=now - timedelta(minutes=10),
            lease_expires_at=now - timedelta(minutes=5),
            deadline_at=now + timedelta(minutes=5),
            attempt_count=1,
            provider_call_started_at=now - timedelta(minutes=9),
            provider_result={},
        )
        with patch(
            "tasks.run_allocation_submission_task.delay",
            side_effect=AssertionError("an unreconciled paid call must not be retried"),
        ):
            recover_allocation_submissions_task(limit=10)
        submission.refresh_from_db()
        self.assertEqual(submission.status, AllocationSubmissionStatus.FAILED)
        self.assertEqual(submission.error_code, "provider_call_unreconciled")
        self.assertEqual(
            submission.worker_metadata["recovery"]["reason"], "provider_call_unreconciled"
        )
        self.assertEqual(PortfolioPlan.objects.count(), 0)

    def test_6b_the_attempt_limit_is_enforced_before_a_requeue(self):
        now = timezone.now()
        submission = self._submission("6b-exhausted")
        AllocationSubmission.objects.filter(pk=submission.pk).update(
            status=AllocationSubmissionStatus.RUNNING,
            claimed_by="dead-worker",
            heartbeat_at=now - timedelta(minutes=10),
            lease_expires_at=now - timedelta(minutes=5),
            deadline_at=now + timedelta(minutes=5),
            attempt_count=2,
            max_attempts=2,
        )
        with patch(
            "tasks.run_allocation_submission_task.delay",
            side_effect=AssertionError("an exhausted row must not be requeued"),
        ):
            recover_allocation_submissions_task(limit=10)
        submission.refresh_from_db()
        self.assertEqual(submission.status, AllocationSubmissionStatus.FAILED)
        self.assertEqual(submission.error_code, "attempts_exhausted")

    def test_6b_a_terminal_row_is_never_reopened(self):
        now = timezone.now()
        submission = self._submission("6b-terminal")
        AllocationSubmission.objects.filter(pk=submission.pk).update(
            status=AllocationSubmissionStatus.CANCELLED,
            claimed_by="",
            lease_expires_at=None,
            completed_at=now,
            error_code="cancelled_by_user",
            last_dispatch_error="broker_unavailable",
        )
        with patch(
            "tasks.run_allocation_submission_task.delay",
            side_effect=AssertionError("a terminal row must not be dispatched"),
        ):
            result = recover_allocation_submissions_task(limit=10)
        submission.refresh_from_db()
        self.assertEqual(submission.status, AllocationSubmissionStatus.CANCELLED)
        self.assertEqual(submission.error_code, "cancelled_by_user")
        self.assertEqual(result["inspected"], 0)

    def test_the_dispatch_slot_is_counted_even_when_publication_fails(self):
        response = self.submit("6b-counted", mode="stub")
        submission = AllocationSubmission.objects.get(pk=response.data["run_id"])
        with patch(
            "tasks.run_allocation_submission_task.delay",
            side_effect=RuntimeError("broker down"),
        ):
            self.assertFalse(dispatch_submission(submission))
        submission.refresh_from_db()
        self.assertEqual(submission.dispatch_attempt_count, 1)
        self.assertEqual(submission.last_dispatch_error, "broker_unavailable")
        self.assertEqual(submission.status, AllocationSubmissionStatus.QUEUED)


class CheckpointResumeTests(AllocationWorkflowFixture):
    """6C: the saved response keeps the input it answered."""

    def _crash_after_provider_result(self, key: str) -> tuple[CountingStage, AllocationSubmission]:
        stage = CountingStage()
        with patch(
            "services.allocation_submission.build_allocation_client",
            return_value=budgeted(stage),
        ):
            response = self.submit(key, mode="live", stub_response=None)
            submission = AllocationSubmission.objects.get(pk=response.data["run_id"])
            with (
                patch(
                    "services.allocation_submission.create_plan",
                    side_effect=SystemExit("worker death after provider result"),
                ),
                self.assertRaises(SystemExit),
            ):
                process_submission(submission, owner=f"{key}-first")
        submission.refresh_from_db()
        self.assertEqual(submission.status, AllocationSubmissionStatus.RUNNING)
        self.assertTrue(submission.provider_result)
        self.assertIsNotNone(submission.input_snapshot_id)
        self.assertTrue(submission.checkpoint_context)
        self.assertIsNone(submission.plan_id)
        return stage, submission

    def _expire_lease(self, submission: AllocationSubmission) -> None:
        AllocationSubmission.objects.filter(pk=submission.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

    def test_6c_the_original_snapshot_still_explains_the_saved_response(self):
        stage, submission = self._crash_after_provider_result("6c-snapshot")
        checkpoint_brief = dict(submission.checkpoint_context["brief"])
        original_price = next(
            row["price"] for row in checkpoint_brief["holdings"] if row["ticker"] == "AAPL"
        )
        self.assertEqual(original_price, "100")
        original_snapshot_id = submission.input_snapshot_id
        self._expire_lease(submission)
        submission.refresh_from_db()

        def changed_quote(ticker: str, **kwargs):
            quote = fake_quote(ticker)
            return replace(quote, price=D("120.00")) if quote is not None else None

        with patch("services.allocation_submission.get_latest_quote", side_effect=changed_quote):
            resumed = process_submission(submission, owner="6c-second")

        self.assertEqual(stage.calls, 1, "the saved response must not be priced twice")
        self.assertEqual(resumed.status, AllocationSubmissionStatus.SUCCEEDED)
        plan = PortfolioPlan.objects.get(pk=resumed.plan_id)
        self.assertEqual(plan.snapshot_id, original_snapshot_id)
        self.assertEqual(plan.brief["holdings"], checkpoint_brief["holdings"])
        self.assertEqual(plan.brief["weights"], checkpoint_brief["weights"])
        self.assertEqual(
            next(row["price"] for row in plan.brief["holdings"] if row["ticker"] == "AAPL"),
            "100",
        )
        provenance = plan.model_metadata["input_provenance"]
        self.assertEqual(provenance["input_source"], "provider_result_checkpoint")
        self.assertEqual(provenance["snapshot_id"], str(original_snapshot_id))
        self.assertEqual(
            provenance["snapshot_content_hash"], submission.input_snapshot.content_hash
        )
        self.assertEqual(provenance["provider_config"]["provider"], "anthropic")
        self.assertEqual(provenance["provider_config"]["model"], "test-model")
        self.assertNotIn("api_key", provenance["provider_config"])
        self.assertEqual(
            resumed.data_quality["input"]["input_source"], "provider_result_checkpoint"
        )
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

    def test_6c_a_fresh_evaluation_is_labelled_when_no_checkpoint_input_exists(self):
        stage, submission = self._crash_after_provider_result("6c-fresh")
        original_snapshot_id = submission.input_snapshot_id
        # Simulate a row written before this change: the provider result exists,
        # but the input checkpoint does not. The only honest option is a fresh
        # evaluation, and it must say so.
        AllocationSubmission.objects.filter(pk=submission.pk).update(
            checkpoint_context={},
            input_snapshot=None,
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        submission.refresh_from_db()

        def changed_quote(ticker: str, **kwargs):
            quote = fake_quote(ticker)
            return replace(quote, price=D("120.00")) if quote is not None else None

        with patch("services.allocation_submission.get_latest_quote", side_effect=changed_quote):
            resumed = process_submission(submission, owner="6c-fresh-second")

        self.assertEqual(stage.calls, 1)
        self.assertEqual(resumed.status, AllocationSubmissionStatus.SUCCEEDED)
        plan = PortfolioPlan.objects.get(pk=resumed.plan_id)
        self.assertNotEqual(plan.snapshot_id, original_snapshot_id)
        self.assertEqual(
            next(row["price"] for row in plan.brief["holdings"] if row["ticker"] == "AAPL"),
            "120.00",
        )
        self.assertEqual(
            plan.model_metadata["input_provenance"]["input_source"],
            "fresh_evaluation_no_checkpoint",
        )
        self.assertEqual(
            resumed.data_quality["input"]["input_source"],
            "fresh_evaluation_no_checkpoint",
        )

    def test_6c_a_resumed_submission_keeps_one_plan_identity(self):
        stage, submission = self._crash_after_provider_result("6c-one-plan")
        self._expire_lease(submission)
        submission.refresh_from_db()
        resumed = process_submission(submission, owner="6c-one-plan-second")
        again = process_submission(resumed, owner="6c-one-plan-third")
        self.assertEqual(stage.calls, 1)
        self.assertEqual(again.status, AllocationSubmissionStatus.SUCCEEDED)
        self.assertEqual(again.plan_id, resumed.plan_id)
        self.assertEqual(PortfolioPlan.objects.count(), 1)
        self.assertEqual(
            PortfolioPlan.objects.filter(submission_key=f"analysis:{submission.id}").count(),
            1,
        )

    def test_the_checkpoint_persists_an_immutable_snapshot_and_provider_config(self):
        _, submission = self._crash_after_provider_result("6c-checkpoint")
        snapshot = InputSnapshot.objects.get(pk=submission.input_snapshot_id)
        self.assertEqual(
            submission.checkpoint_context["snapshot"]["content_hash"], snapshot.content_hash
        )
        self.assertEqual(
            submission.checkpoint_context["schema_version"], "allocation.input_checkpoint.v1"
        )
        config = submission.provider_config
        self.assertEqual(config["provider"], "anthropic")
        self.assertEqual(config["model"], "test-model")
        self.assertIn("budget", config)
        self.assertNotIn("api_key", config)
        self.assertNotIn("api_key", submission.checkpoint_context["provider_config"])


class ScheduledRecoveryRegistrationTests(TestCase):
    def test_the_beat_schedule_runs_the_recovery_task(self):
        schedule = settings.CELERY_BEAT_SCHEDULE
        self.assertIn("recover-allocation-submissions", schedule)
        entry = schedule["recover-allocation-submissions"]
        self.assertEqual(entry["task"], "tasks.recover_allocation_submissions_task")

    def test_the_task_is_importable_and_named(self):
        from tasks import recover_allocation_submissions_task as task

        self.assertEqual(task.name, "tasks.recover_allocation_submissions_task")
