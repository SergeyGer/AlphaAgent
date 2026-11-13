"""Bounded backfill for missing horizons and fill-attribution corrections.

Two classes of damage are repaired, and nothing else:

* **A matured horizon has no result.** A historic scheduler skipped any decision
  that already had an outcome, so a day-one score prevented day 7 and day 30
  from ever being measured. The backfill evaluates each missing matured horizon
  through the same evaluator the scheduler uses.
* **A result was measured from the wrong reference.** A legacy score started
  from the proposal-day close even though a linked ``Transaction`` recorded the
  price that was actually paid. The backfill re-evaluates from the fill and
  keeps the superseded values in the row's provenance (and the older
  ``OutcomeRecord`` revision) rather than overwriting them into silence.

The command is **dry-run by default**. It reports every candidate and the
intended change, plus the fills that cannot be resolved and therefore need a
human. ``--apply`` performs the changes. It is bounded by ``--limit`` and
``--days`` and is idempotent: a second apply finds the first apply's work done.

Examples::

    python manage.py backfill_outcome_records --dry-run --json
    python manage.py backfill_outcome_records --apply --json
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.decision_json import utc_iso
from core.decision_models import (
    DecisionExecutionState,
    DecisionRun,
    OutcomeEvaluation,
    OutcomeEvaluationState,
)
from core.log_safety import log_safe
from core.models import AgentDecisionLog, DecisionOutcome, RecommendationStatus, TradeRecommendation
from services.attribution import (
    BASIS_EXECUTION_FILL,
    BASIS_PROPOSAL_CLOSE,
    OUTCOME_METHOD_VERSION,
    evaluate_decision_horizons,
    horizon_timeline,
    resolve_linked_fill,
)

__all__ = ["Command"]

logger = logging.getLogger("alphaagent.attribution_backfill")


class Command(BaseCommand):
    help = (
        "Dry-run or apply the bounded attribution backfill: missing matured horizons "
        "and scores measured from the proposal close when a recorded fill exists."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report candidates and intended changes without writing (the default).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the reported changes.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit a machine-readable JSON document.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=500,
            help="Maximum due-pair work items to inspect (default 500).",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=180,
            help="How far back to look for decisions (default 180 days).",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply")) and not options.get("dry_run")
        limit = max(int(options.get("limit") or 500), 1)
        days = max(int(options.get("days") or 180), 1)
        now = timezone.now()
        report = self._inspect(limit=limit, days=days, now=now)
        if apply_changes:
            report["applied"] = self._apply(report, now=now)

        payload = {
            "dry_run": not apply_changes,
            "method_version": OUTCOME_METHOD_VERSION,
            "generated_at": utc_iso(now),
            "candidates": report["candidates"],
            "unresolved": report["unresolved"],
            "summary": {
                "decisions_inspected": report["inspected"],
                "missing_horizons": sum(
                    1 for item in report["candidates"] if item["kind"] == "missing_horizon"
                ),
                "fill_attribution_corrections": sum(
                    1 for item in report["candidates"] if item["kind"] == "fill_attribution"
                ),
                "unresolved_fills": len(report["unresolved"]),
                "limit_reached": report["limit_reached"],
                "applied": report.get("applied"),
            },
        }
        if options.get("as_json"):
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return

        mode = "APPLY" if apply_changes else "DRY RUN"
        self.stdout.write(f"backfill_outcome_records [{mode}] method={OUTCOME_METHOD_VERSION}")
        summary = payload["summary"]
        self.stdout.write(
            "  inspected {decisions_inspected} decision(s); "
            "{missing_horizons} missing horizon(s); "
            "{fill_attribution_corrections} fill-attribution correction(s); "
            "{unresolved_fills} unresolved fill(s)".format(**summary)
        )
        for item in report["candidates"]:
            if item["kind"] == "missing_horizon":
                self.stdout.write(
                    "    decision {decision_id} {ticker} +{horizon_days}d -> create "
                    "({basis}, reference {reference_at})".format(**item)
                )
            else:
                self.stdout.write(
                    "    decision {decision_id} {ticker} +{horizon_days}d -> correct "
                    "({current_basis}/{current_price} -> execution_fill/{fill_price})".format(
                        **item
                    )
                )
        for item in report["unresolved"]:
            self.stdout.write(
                "    UNRESOLVED decision {decision_id} ({ticker}): {reason}".format(**item)
            )
        if not apply_changes:
            self.stdout.write("  no changes written; pass --apply to repair the candidates")
        else:
            self.stdout.write(f"  applied: {json.dumps(report['applied'], sort_keys=True)}")

    # -- inspection -------------------------------------------------------
    def _inspect(self, *, limit: int, days: int, now) -> dict:
        """Find missing due pairs and wrong-basis scores, bounded by work.

        ``--limit`` bounds the candidate work items, not the number of decision
        rows that happen to be newest. The scan is oldest-first and stops once
        it has collected ``limit`` candidates, so a recent completed decision
        cannot fill the budget and hide an older due horizon. A completed or
        terminal-unavailable work row, and a retry that is still in backoff, is
        not a candidate: those are the same due-pair rules the scheduler uses.
        """
        decisions = (
            AgentDecisionLog.objects.filter(
                ticker__gt="",
                created_at__gte=now - timedelta(days=days),
                created_at__lte=now,
            )
            .select_related("transaction")
            .order_by("created_at", "id")
        )
        total = decisions.count()
        scan_cap = max(limit * 20, 1000)
        candidates: list[dict] = []
        unresolved: list[dict] = []
        inspected = 0
        scan_truncated = False

        for decision in decisions.iterator(chunk_size=200):
            if len(candidates) >= limit:
                break
            if inspected >= scan_cap:
                scan_truncated = True
                break
            inspected += 1
            fill = resolve_linked_fill(decision)
            ticker = (decision.ticker or "").strip().upper()
            if fill is not None and (fill.ticker or "").strip().upper() != ticker:
                unresolved.append(
                    {
                        "decision_id": decision.id,
                        "ticker": ticker,
                        "reason": (
                            f"linked transaction {fill.id} is for "
                            f"{(fill.ticker or '').strip().upper() or 'an unknown ticker'}"
                        ),
                    }
                )
                fill = None
            if fill is None and self._collect_unresolved(decision, unresolved):
                # An executed state whose fill cannot be resolved is left for
                # follow-up, not measured from the proposal close: that would
                # present a proposal diagnostic as execution.
                continue

            basis = BASIS_EXECUTION_FILL if fill is not None else BASIS_PROPOSAL_CLOSE
            reference_at = fill.timestamp if fill is not None else decision.created_at
            outcomes = {
                row.horizon_days: row
                for row in DecisionOutcome.objects.filter(
                    decision=decision, horizon_days__in=DecisionOutcome.HORIZONS
                )
            }
            evaluations = {
                row.horizon_days: row
                for row in OutcomeEvaluation.objects.filter(
                    decision=decision, method_version=OUTCOME_METHOD_VERSION
                )
            }

            for horizon in DecisionOutcome.HORIZONS:
                if len(candidates) >= limit:
                    break
                _target, mature_at = horizon_timeline(reference_at, horizon)
                if now < mature_at:
                    continue
                existing = outcomes.get(horizon)
                if existing is None:
                    evaluation = evaluations.get(horizon)
                    if evaluation is not None:
                        if evaluation.state in (
                            OutcomeEvaluationState.UNAVAILABLE,
                            OutcomeEvaluationState.EVALUATED,
                        ):
                            # Terminal, or already completed by the work ledger.
                            # Re-running either would not be idempotent.
                            continue
                        if (
                            evaluation.state == OutcomeEvaluationState.MATURED
                            and evaluation.next_attempt_at is not None
                            and evaluation.next_attempt_at > now
                        ):
                            # A deferred retry is not due for this batch.
                            continue
                    candidates.append(
                        {
                            "kind": "missing_horizon",
                            "decision_id": decision.id,
                            "ticker": ticker,
                            "horizon_days": horizon,
                            "basis": basis,
                            "reference_at": utc_iso(reference_at),
                            "filled": fill is not None,
                            "intended": "evaluate_and_record",
                            "run_id": self._run_id(decision),
                        }
                    )
                    continue

                if fill is None:
                    continue
                if self._is_correct_attribution(existing, fill):
                    continue
                candidates.append(
                    {
                        "kind": "fill_attribution",
                        "decision_id": decision.id,
                        "ticker": ticker,
                        "horizon_days": horizon,
                        "basis": basis,
                        "reference_at": utc_iso(reference_at),
                        "filled": True,
                        "intended": "re_evaluate_from_fill",
                        "run_id": self._run_id(decision),
                        "current_basis": existing.measurement_basis or "unrecorded_legacy",
                        "current_price": str(existing.price_at_decision),
                        "current_reference_at": utc_iso(existing.reference_at),
                        "current_fill_id": existing.fill_id,
                        "fill_id": fill.id,
                        "fill_price": str(fill.price),
                        "fill_timestamp": utc_iso(fill.timestamp),
                    }
                )

        return {
            "inspected": inspected,
            "total_available": total,
            "limit_reached": len(candidates) >= limit or scan_truncated,
            "candidates": candidates,
            "unresolved": unresolved,
        }

    def _run_id(self, decision: AgentDecisionLog) -> str | None:
        run_id = (
            DecisionRun.objects.filter(legacy_log_id=decision.id)
            .values_list("id", flat=True)
            .first()
        )
        return str(run_id) if run_id is not None else None

    def _is_correct_attribution(self, outcome: DecisionOutcome, fill) -> bool:
        if outcome.measurement_basis != BASIS_EXECUTION_FILL:
            return False
        if outcome.fill_id != fill.id:
            return False
        if outcome.price_at_decision != fill.price:
            return False
        if outcome.reference_at is None:
            return False
        return outcome.reference_at == fill.timestamp

    def _collect_unresolved(self, decision: AgentDecisionLog, unresolved: list[dict]) -> bool:
        """Name executed states whose fill cannot be resolved.

        A recommendation marked executed with no transaction, or a run that says
        it executed with no linked attempt, is an attribution hole. It is
        reported for follow-up rather than guessed from the proposal close and
        presented as an execution.
        """
        reasons = []
        recommendation = TradeRecommendation.objects.filter(
            decision_log_id=decision.id,
            status=RecommendationStatus.EXECUTED,
            transaction__isnull=True,
        ).first()
        if recommendation is not None:
            reasons.append(f"recommendation {recommendation.id} is EXECUTED but has no transaction")
        run = DecisionRun.objects.filter(legacy_log_id=decision.id).first()
        if run is not None and run.execution_state == DecisionExecutionState.EXECUTED:
            reasons.append(f"run {run.id} is executed but no linked fill resolves")
        if not reasons:
            return False
        reason = "; ".join(reasons)
        unresolved.append(
            {
                "decision_id": decision.id,
                "ticker": (decision.ticker or "").strip().upper(),
                "reason": reason,
            }
        )
        # Decision id and a bounded, control-character-free reason; no free-form
        # exception text can forge a log line.
        logger.warning(
            "Attribution backfill: unresolved fill decision=%s reason=%s",
            decision.id,
            log_safe(reason),
        )
        return True

    # -- application ------------------------------------------------------
    def _apply(self, report: dict, *, now) -> dict:
        by_decision: dict[int, list[dict]] = {}
        for item in report["candidates"]:
            by_decision.setdefault(item["decision_id"], []).append(item)

        created = 0
        corrected = 0
        failed = 0
        for decision_id, items in by_decision.items():
            decision = AgentDecisionLog.objects.filter(pk=decision_id).first()
            if decision is None:
                continue
            missing = sorted(
                item["horizon_days"] for item in items if item["kind"] == "missing_horizon"
            )
            corrections = sorted(
                item["horizon_days"] for item in items if item["kind"] == "fill_attribution"
            )
            try:
                if missing:
                    before = set(
                        DecisionOutcome.objects.filter(
                            decision=decision, horizon_days__in=missing
                        ).values_list("horizon_days", flat=True)
                    )
                    evaluate_decision_horizons(decision, now=now, horizons=missing)
                    after = set(
                        DecisionOutcome.objects.filter(
                            decision=decision, horizon_days__in=missing
                        ).values_list("horizon_days", flat=True)
                    )
                    created += len(after - before)
                if corrections:
                    revisions_before = dict(
                        DecisionOutcome.objects.filter(
                            decision=decision, horizon_days__in=corrections
                        ).values_list("horizon_days", "revision")
                    )
                    evaluate_decision_horizons(decision, now=now, horizons=corrections, force=True)
                    revisions_after = dict(
                        DecisionOutcome.objects.filter(
                            decision=decision, horizon_days__in=corrections
                        ).values_list("horizon_days", "revision")
                    )
                    corrected += sum(
                        1
                        for horizon in corrections
                        if revisions_after.get(horizon, 0) > revisions_before.get(horizon, 0)
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Attribution backfill failed decision=%s error=%s",
                    decision.id,
                    type(exc).__name__,
                )

        return {
            "decisions_changed": len(by_decision),
            "results_created": created,
            "results_corrected": corrected,
            "decisions_failed": failed,
        }
