"""P1 remediation step 6: portfolio analysis is a recoverable job.

Reproduced before the repair:

* ``POST /api/decision-runs/`` called the provider inside the request. A slow
  model blocked the 202, and a process death left an ``AllocationSubmission``
  stuck at ``running`` with no worker, no attempt count, no owner and no status
  a client could poll;
* a retry had no durable provider checkpoint, so a second attempt would spend a
  second time and could create a second plan;
* cancellation did not exist, so a queued row was executed anyway;
* there was no command to find a submission whose worker had disappeared.

These tests drive the worker explicitly. The dispatch seam in the API view is
patched by `AllocationWorkflowFixture`, so no broker is contacted and the
"return before the model is called" assertion is real rather than a timing race.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from core.allocation_models import AllocationSubmission, AllocationSubmissionStatus
from core.models import PortfolioPlan, Transaction
from core.tests.test_allocation_workflow import (
    AllocationWorkflowFixture,
    stub_payload,
)
from services.allocation_provider import BudgetedAllocationClient
from services.allocation_submission import (
    cancel_submission,
    process_submission,
)
from services.debate_protocol import ModelUsage, ProtocolBudget

D = Decimal


class CountingStage:
    """A scripted provider that counts calls; it never touches the network."""

    provider = "anthropic"
    model = "test-model"
    parameters = {"max_output_tokens": 2048}

    def __init__(self) -> None:
        self.calls = 0
        self.last_usage = ModelUsage(
            input_tokens=100,
            output_tokens=50,
            cost_usd=None,
            reported=True,
            tokens_measured=True,
            price_table_cost_usd=D("0.0001"),
            cost_unknown_reason="provider_reported_cost_unavailable",
        )

    def complete(self, stage, prompt):
        self.calls += 1
        payload = stub_payload()
        marker = "BRIEF (the only permitted source of facts):\n"
        parsed = json.loads(prompt.split(marker, 1)[1].strip())
        payload["portfolio_version"] = parsed["portfolio_version"]
        return json.dumps(payload)


def budgeted(stage: CountingStage) -> BudgetedAllocationClient:
    return BudgetedAllocationClient(
        stage,
        budget=ProtocolBudget(
            stage_timeout_seconds=10,
            run_deadline_seconds=30,
            max_input_tokens=100_000,
            max_output_tokens=10_000,
            provider_retries=0,
            monetary_budget_usd=D("1.00"),
        ),
    )


class AsyncSubmissionTests(AllocationWorkflowFixture):
    def submit_live(self, key: str):
        return self.submit(key, mode="live", stub_response=None)

    def status_of(self, run_id: str):
        return self.client.get(reverse("core:decision-run-status", args=[str(run_id)]))

    def test_6a_the_response_returns_before_the_model_is_called(self):
        stage = CountingStage()
        with patch(
            "services.allocation_submission.build_allocation_client",
            return_value=budgeted(stage),
        ):
            response = self.submit_live("6a-key")
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
            # The durable record exists; the provider has not been touched.
            self.assertEqual(response.data["status"], AllocationSubmissionStatus.QUEUED)
            self.assertEqual(response.data["attempt_count"], 0)
            self.assertIsNone(response.data["plan_id"])
            self.assertFalse(response.data["terminal"])
            self.assertIn("/status/", response.data["status_url"])
            self.assertEqual(stage.calls, 0)
            self.assertEqual(PortfolioPlan.objects.count(), 0)
            self.assertEqual(len(self.dispatched), 1)

            submission = process_submission(response.data["run_id"], owner="worker-6a")

        self.assertEqual(stage.calls, 1)
        self.assertEqual(submission.status, AllocationSubmissionStatus.SUCCEEDED)
        self.assertEqual(submission.attempt_count, 1)
        self.assertEqual(submission.claimed_by, "")
        self.assertIsNotNone(submission.plan_id)
        self.assertEqual(PortfolioPlan.objects.count(), 1)
        self.assertEqual(
            PortfolioPlan.objects.filter(submission_key=f"analysis:{submission.id}").count(), 1
        )
        self.assertEqual(submission.provider, "anthropic")
        self.assertFalse(submission.synthetic_response)
        self.assertTrue(submission.provider_result)
        self.assertIsNotNone(submission.provider_completed_at)
        self.assertEqual(
            Transaction.objects.filter(portfolio=self.portfolio).count(),
            0,
            "an analysis plan is not a trade",
        )

        polled = self.status_of(response.data["run_id"])
        self.assertEqual(polled.status_code, status.HTTP_200_OK)
        self.assertEqual(polled.data["status"], AllocationSubmissionStatus.SUCCEEDED)
        self.assertTrue(polled.data["terminal"])
        self.assertIsNotNone(polled.data["terminal_at"])
        self.assertEqual(polled.data["attempt_count"], 1)
        self.assertIsNone(polled.data["failure_reason"])
        self.assertNotIn("Retry-After", polled)

    def test_6b_a_crash_after_the_provider_result_resumes_without_a_second_spend(self):
        stage = CountingStage()
        with patch(
            "services.allocation_submission.build_allocation_client",
            return_value=budgeted(stage),
        ):
            response = self.submit_live("6b-key")
            submission = AllocationSubmission.objects.get(pk=response.data["run_id"])
            with (
                patch(
                    "services.allocation_submission.create_plan",
                    side_effect=SystemExit("simulated worker death"),
                ),
                self.assertRaises(SystemExit),
            ):
                process_submission(submission, owner="worker-6b-first")

            submission.refresh_from_db()
            self.assertEqual(submission.status, AllocationSubmissionStatus.RUNNING)
            self.assertEqual(stage.calls, 1)
            self.assertEqual(submission.attempt_count, 1)
            self.assertTrue(submission.provider_result)
            self.assertIsNone(submission.plan_id)
            self.assertEqual(PortfolioPlan.objects.count(), 0)
            self.assertIsNotNone(submission.provider_call_started_at)
            self.assertIsNotNone(submission.provider_completed_at)

            # The worker is gone: its lease has expired, which is what the
            # repair path and a new worker both look at.
            AllocationSubmission.objects.filter(pk=submission.pk).update(
                lease_expires_at=timezone.now() - timedelta(seconds=1)
            )
            submission.refresh_from_db()
            retried = process_submission(submission, owner="worker-6b-second")

        self.assertEqual(stage.calls, 1, "the retry must not call the provider again")
        self.assertEqual(retried.status, AllocationSubmissionStatus.SUCCEEDED)
        self.assertEqual(retried.attempt_count, 2)
        self.assertEqual(PortfolioPlan.objects.count(), 1)
        self.assertIsNotNone(retried.plan_id)
        self.assertEqual(retried.plan_id, PortfolioPlan.objects.get().id)

        polled = self.status_of(response.data["run_id"])
        self.assertEqual(polled.data["status"], AllocationSubmissionStatus.SUCCEEDED)
        self.assertTrue(polled.data["terminal"])
        self.assertEqual(polled.data["plan_id"], str(retried.plan_id))

        # A third delivery is a no-op on a terminal row.
        again = process_submission(retried, owner="worker-6b-third")
        self.assertEqual(again.status, AllocationSubmissionStatus.SUCCEEDED)
        self.assertEqual(PortfolioPlan.objects.count(), 1)
        self.assertEqual(stage.calls, 1)

    def test_6b_a_timed_out_submission_creates_no_plan_and_applies_no_trade(self):
        stage = CountingStage()
        with patch(
            "services.allocation_submission.build_allocation_client",
            return_value=budgeted(stage),
        ):
            response = self.submit_live("6b-timeout-key")
            AllocationSubmission.objects.filter(pk=response.data["run_id"]).update(
                deadline_at=timezone.now() - timedelta(seconds=1)
            )
            submission = AllocationSubmission.objects.get(pk=response.data["run_id"])
            processed = process_submission(submission, owner="worker-6b-timeout")

        self.assertEqual(processed.status, AllocationSubmissionStatus.TIMED_OUT)
        self.assertEqual(processed.error_code, "analysis_timeout")
        self.assertIsNotNone(processed.completed_at)
        self.assertEqual(stage.calls, 0, "a past-deadline row must not spend")
        self.assertEqual(PortfolioPlan.objects.count(), 0)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

        polled = self.status_of(response.data["run_id"])
        self.assertEqual(polled.data["status"], AllocationSubmissionStatus.TIMED_OUT)
        self.assertTrue(polled.data["terminal"])
        self.assertEqual(polled.data["failure_reason"], "analysis_timeout")
        self.assertIsNotNone(polled.data["terminal_at"])
        self.assertIsNone(polled.data["plan_id"])

        # Nothing can apply a trade later: there is no plan, and the terminal
        # row is not reopened.
        again = process_submission(processed, owner="worker-6b-timeout-retry")
        self.assertEqual(again.status, AllocationSubmissionStatus.TIMED_OUT)
        self.assertEqual(PortfolioPlan.objects.count(), 0)
        self.assertEqual(stage.calls, 0)

    def test_6c_a_cancelled_queued_submission_never_runs(self):
        stage = CountingStage()
        with patch(
            "services.allocation_submission.build_allocation_client",
            return_value=budgeted(stage),
        ):
            response = self.submit_live("6c-key")
            cancelled = self.client.post(
                reverse("core:decision-run-cancel", args=[response.data["run_id"]]),
                {"reason": "user_withdrew"},
                format="json",
            )
            self.assertEqual(cancelled.status_code, status.HTTP_200_OK, cancelled.data)
            self.assertTrue(cancelled.data["cancelled"])
            self.assertEqual(cancelled.data["status"], AllocationSubmissionStatus.CANCELLED)
            self.assertTrue(cancelled.data["terminal"])
            self.assertIsNotNone(cancelled.data["terminal_at"])

            process_submission(response.data["run_id"], owner="worker-6c")

        self.assertEqual(stage.calls, 0)
        self.assertEqual(PortfolioPlan.objects.count(), 0)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)
        polled = self.status_of(response.data["run_id"])
        self.assertEqual(polled.data["status"], AllocationSubmissionStatus.CANCELLED)
        self.assertEqual(polled.data["failure_reason"], "user_withdrew")
        self.assertTrue(polled.data["terminal"])

        # Cancelling a terminal row is a conflict, not a second transition.
        again = self.client.post(
            reverse("core:decision-run-cancel", args=[response.data["run_id"]]),
            {},
            format="json",
        )
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)

    def test_6c_a_cancelled_running_submission_cannot_attach_its_persisted_result(self):
        stage = CountingStage()
        with patch(
            "services.allocation_submission.build_allocation_client",
            return_value=budgeted(stage),
        ):
            response = self.submit_live("6c-running-key")
            submission = AllocationSubmission.objects.get(pk=response.data["run_id"])
            with (
                patch(
                    "services.allocation_submission.create_plan",
                    side_effect=SystemExit("simulated worker death"),
                ),
                self.assertRaises(SystemExit),
            ):
                process_submission(submission, owner="worker-6c-first")
            # The provider replied and the result was persisted before the
            # crash. Cancelling now must still prevent a plan.
            submission.refresh_from_db()
            self.assertTrue(submission.provider_result)
            cancelled, changed = cancel_submission(submission, reason="risk_withdrawn")
            self.assertTrue(changed)
            self.assertEqual(cancelled.status, AllocationSubmissionStatus.CANCELLED)

            retried = process_submission(cancelled, owner="worker-6c-second")

        self.assertEqual(retried.status, AllocationSubmissionStatus.CANCELLED)
        self.assertEqual(stage.calls, 1)
        self.assertEqual(PortfolioPlan.objects.count(), 0)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

    def test_6d_the_repair_command_reports_a_stuck_worker_without_changing_it(self):
        response = self.submit("6d-key", mode="stub")
        run_id = response.data["run_id"]
        AllocationSubmission.objects.filter(pk=run_id).update(
            status=AllocationSubmissionStatus.RUNNING,
            claimed_by="dead-worker",
            claimed_at=timezone.now() - timedelta(minutes=10),
            heartbeat_at=timezone.now() - timedelta(minutes=10),
            lease_expires_at=timezone.now() - timedelta(minutes=5),
            deadline_at=timezone.now() - timedelta(minutes=1),
            attempt_count=1,
        )
        before = AllocationSubmission.objects.get(pk=run_id)

        out = StringIO()
        call_command("repair_allocation_submissions", "--dry-run", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["count"], 1)
        row = payload["submissions"][0]
        self.assertEqual(row["run_id"], str(run_id))
        self.assertEqual(row["status"], AllocationSubmissionStatus.RUNNING)
        self.assertEqual(row["action"], "time_out")
        self.assertEqual(row["reason"], "analysis_timeout")
        self.assertFalse(row["applied"])

        after = AllocationSubmission.objects.get(pk=run_id)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.claimed_by, before.claimed_by)
        self.assertEqual(after.completed_at, before.completed_at)
        self.assertEqual(after.error_code, "")
        self.assertEqual(PortfolioPlan.objects.count(), 0)

        # The explicit execute path performs the reported transition.
        out = StringIO()
        call_command("repair_allocation_submissions", "--execute", stdout=out)
        repaired = AllocationSubmission.objects.get(pk=run_id)
        self.assertEqual(repaired.status, AllocationSubmissionStatus.TIMED_OUT)
        self.assertEqual(repaired.error_code, "analysis_timeout")
        self.assertTrue(repaired.is_terminal)
        self.assertEqual(PortfolioPlan.objects.count(), 0)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

    def test_6d_a_lost_worker_before_the_paid_call_is_requeued(self):
        response = self.submit("6d-requeue-key", mode="stub")
        run_id = response.data["run_id"]
        AllocationSubmission.objects.filter(pk=run_id).update(
            status=AllocationSubmissionStatus.RUNNING,
            claimed_by="dead-worker",
            claimed_at=timezone.now() - timedelta(minutes=10),
            heartbeat_at=timezone.now() - timedelta(minutes=10),
            lease_expires_at=timezone.now() - timedelta(minutes=5),
            deadline_at=timezone.now() + timedelta(minutes=5),
            attempt_count=1,
        )
        self.dispatched.clear()
        out = StringIO()
        with patch(
            "services.allocation_submission.dispatch_submission",
            side_effect=self.dispatched.append,
        ):
            call_command(
                "repair_allocation_submissions",
                "--execute",
                "--stale-seconds",
                "60",
                stdout=out,
            )
        row = json.loads(out.getvalue())["submissions"][0]
        self.assertEqual(row["action"], "requeue")
        self.assertEqual(row["reason"], "worker_lost")
        repaired = AllocationSubmission.objects.get(pk=run_id)
        self.assertEqual(repaired.status, AllocationSubmissionStatus.QUEUED)
        self.assertEqual(repaired.claimed_by, "")
        self.assertIsNone(repaired.lease_expires_at)
        self.assertEqual(len(self.dispatched), 1)

    def test_6d_an_unreconciled_provider_call_is_failed_not_retried(self):
        response = self.submit("6d-unreconciled-key", mode="stub")
        run_id = response.data["run_id"]
        AllocationSubmission.objects.filter(pk=run_id).update(
            status=AllocationSubmissionStatus.RUNNING,
            claimed_by="dead-worker",
            claimed_at=timezone.now() - timedelta(minutes=10),
            heartbeat_at=timezone.now() - timedelta(minutes=10),
            lease_expires_at=timezone.now() - timedelta(minutes=5),
            deadline_at=timezone.now() + timedelta(minutes=5),
            attempt_count=1,
            provider_call_started_at=timezone.now() - timedelta(minutes=9),
        )
        out = StringIO()
        call_command(
            "repair_allocation_submissions", "--execute", "--stale-seconds", "60", stdout=out
        )
        row = json.loads(out.getvalue())["submissions"][0]
        self.assertEqual(row["action"], "fail")
        self.assertEqual(row["reason"], "provider_call_unreconciled")
        repaired = AllocationSubmission.objects.get(pk=run_id)
        self.assertEqual(repaired.status, AllocationSubmissionStatus.FAILED)
        self.assertEqual(repaired.error_code, "provider_call_unreconciled")
        self.assertFalse(repaired.provider_result)
        self.assertEqual(PortfolioPlan.objects.count(), 0)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)


class CancellationServiceTests(AllocationWorkflowFixture):
    def test_the_reason_must_be_a_stable_code(self):
        response = self.submit("reason-key")
        bad = self.client.post(
            reverse("core:decision-run-cancel", args=[response.data["run_id"]]),
            {"reason": "because I felt like it\nforged"},
            format="json",
        )
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            AllocationSubmission.objects.get(pk=response.data["run_id"]).status,
            AllocationSubmissionStatus.QUEUED,
        )

    def test_another_users_submission_is_a_404(self):
        response = self.submit("owner-key")
        from core.tests.helpers import auth_client, make_user

        outsider = auth_client(make_user("cancel-outsider"))
        denied = outsider.post(
            reverse("core:decision-run-cancel", args=[response.data["run_id"]]),
            {},
            format="json",
        )
        self.assertEqual(denied.status_code, status.HTTP_404_NOT_FOUND)

    def test_cancelling_an_unknown_id_is_a_404(self):
        missing = self.client.post(
            reverse("core:decision-run-cancel", args=[str(uuid.uuid4())]),
            {},
            format="json",
        )
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)
