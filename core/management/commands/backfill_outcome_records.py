"""Bridge matured legacy outcomes into the Phase 1 ``OutcomeRecord`` contract.

The live evaluator already writes both rows (see ``services.attribution``); this
command exists for decisions scored before the bridge existed. It is idempotent:
the unique ``(decision, horizon, method_version)`` constraint plus
``get_or_create`` means running it twice changes nothing, and it never invents a
value the attribution row does not contain.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from core.decision_models import DecisionRun, OutcomeRecord
from services.decision_outcomes import OUTCOME_METHOD_VERSION, sync_outcomes_for_run


class Command(BaseCommand):
    help = (
        "Backfill OutcomeRecord rows from matured DecisionOutcome rows for linked "
        f"decision runs (method {OUTCOME_METHOD_VERSION})."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many links would be bridged without writing.",
        )

    def handle(self, *args, **options):
        runs = (
            DecisionRun.objects.filter(legacy_log__isnull=False, decision__isnull=False)
            .select_related("decision")
            .order_by("started_at")
        )
        linked = 0
        synced = 0
        for run in runs.iterator(chunk_size=200):
            linked += 1
            if options["dry_run"]:
                continue
            synced += sync_outcomes_for_run(run)
        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: {linked} linked run(s) would be scanned; no rows written."
                )
            )
            return
        total = OutcomeRecord.objects.filter(method_version=OUTCOME_METHOD_VERSION).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Scanned {linked} linked run(s); bridged {synced} outcome row(s); "
                f"{total} {OUTCOME_METHOD_VERSION} record(s) now stored."
            )
        )
