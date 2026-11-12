"""Inspect and repair decision executions whose audit is incomplete.

The command is dry-run by default. It answers two questions an operator can act
on without guessing:

* **A fill with no attempt link.** The ledger row exists, but no
  ``ExecutionAttempt`` says which run, recommendation or approval terms it
  belongs to. A uniquely attributable fill can be relinked; anything ambiguous
  is sent to review rather than assigned to the first plausible run.
* **An approval state with no actionable recommendation.** A run says it is
  ``awaiting_approval``, but there is no pending recommendation a human can act
  on. That run can never resolve; the repair records a terminal blocked outcome
  with the reason.

``--apply`` performs the unambiguous repairs. ``--dry-run`` (the default) writes
nothing. Both modes print a machine-readable JSON document when ``--json`` is
passed, so the dry run can be attached to an incident review.

Examples::

    python manage.py repair_execution_audit --dry-run
    python manage.py repair_execution_audit --dry-run --json
    python manage.py repair_execution_audit --apply
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import transaction

from core.decision_models import (
    DecisionExecutionState,
    DecisionRun,
    ExecutionAttempt,
)
from core.models import RecommendationStatus, TradeRecommendation, Transaction

__all__ = ["Command"]


class Command(BaseCommand):
    help = "Dry-run or apply repairs for fills and approval states with incomplete audit."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report findings without writing (the default).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the unambiguous repairs.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit a JSON document instead of human-readable lines.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=500,
            help="Maximum fills/approval states to inspect (default 500).",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply")) and not options.get("dry_run")
        limit = max(int(options.get("limit") or 500), 1)
        findings = self._findings(limit=limit)
        if apply_changes:
            self._apply(findings)
        if options.get("as_json"):
            self.stdout.write(
                json.dumps(
                    {
                        "dry_run": not apply_changes,
                        "findings": findings,
                        "summary": self._summary(findings),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        mode = "APPLY" if apply_changes else "DRY RUN"
        self.stdout.write(f"repair_execution_audit [{mode}]")
        fills = [row for row in findings if row["kind"] == "fill_missing_attempt"]
        approvals = [row for row in findings if row["kind"] == "approval_without_recommendation"]
        review = [row for row in findings if row["decision"] == "review"]
        if not findings:
            self.stdout.write("  no incomplete execution audits found")
            return
        self.stdout.write(f"  fills missing an attempt link: {len(fills)}")
        for row in fills:
            self.stdout.write(
                f"    transaction {row['transaction_id']} key={row['execution_key']!r} "
                f"-> {row['decision']} ({row['detail']})"
            )
        self.stdout.write(f"  approval states with no actionable recommendation: {len(approvals)}")
        for row in approvals:
            self.stdout.write(
                f"    run {row['run_id']} ({row['ticker']}) -> {row['decision']} ({row['detail']})"
            )
        if review:
            self.stdout.write(f"  sent to review (ambiguous, no write): {len(review)}")
        if not apply_changes:
            self.stdout.write("  no changes written; pass --apply to repair the unambiguous rows")

    # -- inspection -------------------------------------------------------
    def _findings(self, *, limit: int) -> list[dict]:
        findings: list[dict] = []
        fills = (
            Transaction.objects.filter(execution_key__isnull=False, execution_attempt__isnull=True)
            .order_by("id")
            .values("id", "execution_key", "ticker", "portfolio_id", "amount", "price")[:limit]
        )
        for row in fills:
            findings.append(self._classify_fill(row))

        awaiting = (
            DecisionRun.objects.filter(execution_state=DecisionExecutionState.AWAITING_APPROVAL)
            .order_by("-started_at")
            .values("id", "ticker", "portfolio_id")[:limit]
        )
        for row in awaiting:
            actionable = TradeRecommendation.objects.filter(
                portfolio_id=row["portfolio_id"],
                ticker=row["ticker"],
                status=RecommendationStatus.PENDING,
                expires_at__gt=_now(),
            ).exists()
            if actionable:
                continue
            findings.append(
                {
                    "kind": "approval_without_recommendation",
                    "run_id": str(row["id"]),
                    "ticker": row["ticker"],
                    "portfolio_id": row["portfolio_id"],
                    "decision": "repair",
                    "detail": (
                        "run is awaiting_approval but no pending recommendation can resolve it"
                    ),
                    "repairable": True,
                }
            )
        return findings

    def _classify_fill(self, row: dict) -> dict:
        key = str(row["execution_key"] or "")
        base = {
            "kind": "fill_missing_attempt",
            "transaction_id": row["id"],
            "execution_key": key,
            "ticker": row["ticker"],
            "portfolio_id": row["portfolio_id"],
            "amount": str(row["amount"]),
            "price": str(row["price"]),
        }
        matches: list[dict] = []
        if key.startswith("recommendation:"):
            try:
                recommendation_id = int(key.split(":", 1)[1])
            except (TypeError, ValueError):
                recommendation_id = None
            if recommendation_id is not None:
                recommendation = TradeRecommendation.objects.filter(
                    pk=recommendation_id, transaction_id=row["id"]
                ).first()
                if recommendation is not None:
                    matches.append(
                        {
                            "kind": "recommendation",
                            "id": recommendation.id,
                            "run_id": str(recommendation.decision_run_id)
                            if recommendation.decision_run_id
                            else None,
                        }
                    )
        elif key.startswith("plan:"):
            parts = key.split(":")
            if len(parts) >= 4 and parts[1]:
                matches.append({"kind": "plan_leg", "plan_id": parts[1], "sequence": parts[3]})
        elif key.startswith("run:"):
            run_id = key.split(":", 1)[1]
            if DecisionRun.objects.filter(pk=run_id).exists():
                matches.append({"kind": "run", "run_id": run_id})

        if len(matches) == 1:
            return {
                **base,
                "decision": "repair",
                "match": matches[0],
                "detail": "one unambiguous owner match",
                "repairable": True,
            }
        if len(matches) > 1:
            return {
                **base,
                "decision": "review",
                "match": matches,
                "detail": "multiple owner matches; ambiguous",
                "repairable": False,
            }
        return {
            **base,
            "decision": "review",
            "match": None,
            "detail": (
                "no owner could be inferred from the execution key; review the ledger row "
                "against its run before relinking"
            ),
            "repairable": False,
        }

    # -- repair -----------------------------------------------------------
    def _apply(self, findings: list[dict]) -> None:
        for row in findings:
            if row.get("decision") != "repair":
                continue
            if row["kind"] == "fill_missing_attempt":
                if (row.get("match") or {}).get("kind") == "plan_leg":
                    self._repair_plan_fill(row)
                else:
                    self._repair_fill(row)
            elif row["kind"] == "approval_without_recommendation":
                self._repair_approval_state(row)

    def _repair_fill(self, row: dict) -> None:
        match = row.get("match") or {}
        if match.get("kind") == "recommendation":
            recommendation = TradeRecommendation.objects.filter(pk=match["id"]).first()
            if recommendation is None:
                return
            with transaction.atomic():
                ExecutionAttempt.objects.update_or_create(
                    execution_key=row["execution_key"],
                    defaults={
                        "request_hash": "",
                        "source": "advisory",
                        "state": DecisionExecutionState.EXECUTED,
                        "transaction_id": row["transaction_id"],
                        "decision_run_id": recommendation.decision_run_id,
                        "recommendation": recommendation,
                        "approved_terms": {
                            "reference_price": str(recommendation.price),
                            "quantity": str(recommendation.amount),
                            "recovered_from": "execution_key",
                        },
                        "quote_observed_at": recommendation.quote_observed_at,
                        "quote_retrieved_at": recommendation.quote_retrieved_at,
                        "quote_source": recommendation.price_source or "",
                        "recovered": True,
                        "detail": "Repaired from the recommendation's execution key.",
                    },
                )
                if recommendation.decision_run_id:
                    from services.decision_lifecycle import complete_run
                    from services.execution_audit import terminal_event_for

                    run = DecisionRun.objects.get(pk=recommendation.decision_run_id)
                    if run.execution_state != DecisionExecutionState.EXECUTED:
                        complete_run(
                            run,
                            outcome=DecisionExecutionState.EXECUTED,
                            transaction_row=recommendation.transaction,
                            recommendation=recommendation,
                            reason="Repaired execution audit for an existing fill.",
                            via="repair",
                        )
                        run.refresh_from_db()
                    attempt = ExecutionAttempt.objects.filter(
                        execution_key=row["execution_key"]
                    ).first()
                    event = terminal_event_for(run, outcome=DecisionExecutionState.EXECUTED)
                    if (
                        attempt is not None
                        and event is not None
                        and attempt.terminal_event_id is None
                    ):
                        attempt.terminal_event = event
                        attempt.save(update_fields=["terminal_event"])

    def _repair_plan_fill(self, row: dict) -> None:
        from core.allocation_models import PortfolioPlan, PortfolioPlanLeg

        match = row.get("match") or {}
        plan = PortfolioPlan.objects.filter(pk=match.get("plan_id")).first()
        if plan is None:
            return
        leg = PortfolioPlanLeg.objects.filter(
            plan=plan,
            sequence=int(match.get("sequence") or 0),
            transaction_id=row["transaction_id"],
        ).first()
        if leg is None:
            return
        approval = plan.approvals.filter(
            decision="approved", plan_version=plan.applied_approval_version
        ).first()
        with transaction.atomic():
            ExecutionAttempt.objects.update_or_create(
                execution_key=row["execution_key"],
                defaults={
                    "source": "plan_leg",
                    "state": (
                        DecisionExecutionState.EXECUTED
                        if leg.transaction_id
                        else DecisionExecutionState.VALIDATING
                    ),
                    "transaction_id": row["transaction_id"],
                    "approved_terms": {
                        "plan_id": str(plan.id),
                        "plan_version": plan.plan_version,
                        "approval_id": approval.id if approval is not None else None,
                        "approval_terms_hash": (
                            approval.terms_hash if approval is not None else ""
                        ),
                        "reference_price": str(leg.price),
                        "quantity": str(leg.quantity),
                        "recovered_from": "execution_key",
                    },
                    "recovered": True,
                    "detail": "Repaired from the plan-leg execution key.",
                },
            )

    def _repair_approval_state(self, row: dict) -> None:
        run = DecisionRun.objects.filter(pk=row["run_id"]).first()
        if run is None:
            return
        from services.execution_audit import record_terminal_outcome

        with transaction.atomic():
            record_terminal_outcome(
                run=run,
                outcome=DecisionExecutionState.BLOCKED,
                reason=(
                    "Repair: the run was awaiting approval but no actionable recommendation "
                    "existed to resolve it."
                ),
                via="repair",
            )

    @staticmethod
    def _summary(findings: list[dict]) -> dict:
        return {
            "total": len(findings),
            "fills_missing_attempt": sum(
                1 for row in findings if row["kind"] == "fill_missing_attempt"
            ),
            "approval_without_recommendation": sum(
                1 for row in findings if row["kind"] == "approval_without_recommendation"
            ),
            "repairable": sum(1 for row in findings if row.get("repairable")),
            "review": sum(1 for row in findings if row["decision"] == "review"),
        }


def _now():
    from django.utils import timezone

    return timezone.now()
