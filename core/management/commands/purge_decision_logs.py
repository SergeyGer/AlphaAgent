"""Prune the AI decision audit trail.

The audit trail is append-only by design, so it grows without bound and a
misconfiguration (a revoked key, a retired model) can flood it with thousands of
identical failure rows. This command removes noise while keeping the records
that explain what the system actually did.

Safety: an explicit selector is required. Running it with no arguments lists the
current contents and exits rather than deleting anything.

    manage.py purge_decision_logs                          # report only
    manage.py purge_decision_logs --errors-only --dry-run  # preview
    manage.py purge_decision_logs --errors-only            # delete failures
    manage.py purge_decision_logs --older-than 90          # retention policy
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q
from django.utils import timezone

from core.models import AgentDecisionLog, PortfolioSnapshot, Transaction

ERROR_PREFIX = "ERROR"


class Command(BaseCommand):
    help = "Delete AI decision-log rows that are noise, keeping the audit trail meaningful."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--errors-only",
            action="store_true",
            help="Delete only rows recording a failed agent run.",
        )
        parser.add_argument(
            "--older-than",
            type=int,
            metavar="DAYS",
            help="Delete rows older than DAYS (retention policy).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Delete every decision log. Requires --older-than or --errors-only "
            "to be omitted deliberately; use with care.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )
        parser.add_argument(
            "--prune-snapshots",
            type=int,
            metavar="DAYS",
            help="Also delete portfolio snapshots older than DAYS.",
        )

    def handle(self, *args, **options) -> None:
        queryset = AgentDecisionLog.objects.all()

        selectors = []
        if options["errors_only"]:
            selectors.append(Q(action_taken__startswith=ERROR_PREFIX))
        if options["older_than"]:
            cutoff = timezone.now() - timedelta(days=options["older_than"])
            selectors.append(Q(created_at__lt=cutoff))
        if options["all"]:
            selectors.append(Q())

        self._report_current_state()

        if not selectors:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "No selector given, so nothing was deleted.\n"
                    "Choose one of: --errors-only, --older-than DAYS, --all"
                )
            )
            return

        combined = selectors[0]
        for extra in selectors[1:]:
            combined &= extra
        targets = queryset.filter(combined)

        count = targets.count()
        if count == 0:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Nothing matched - the log is already clean."))
        else:
            self._preview(targets, count, dry_run=options["dry_run"])

            if options["dry_run"]:
                self.stdout.write("")
                self.stdout.write(self.style.WARNING("Dry run: nothing was deleted."))
            else:
                # `AgentDecisionLog` cascades from Portfolio but is referenced by
                # TradeRecommendation.decision_log with SET_NULL, so deleting is
                # safe: recommendations keep their own copy of the reasoning.
                deleted, _detail = targets.delete()
                self.stdout.write("")
                self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} row(s)."))

        if options["prune_snapshots"]:
            self._prune_snapshots(options["prune_snapshots"], dry_run=options["dry_run"])

        self._report_current_state()

    # -- helpers -----------------------------------------------------------
    def _report_current_state(self) -> None:
        total = AgentDecisionLog.objects.count()
        errors = AgentDecisionLog.objects.filter(action_taken__startswith=ERROR_PREFIX).count()
        self.stdout.write(
            f"Decision logs: {total} total, {errors} failed-run, {total - errors} substantive"
        )

    def _preview(self, targets, count: int, *, dry_run: bool) -> None:
        verb = "Would delete" if dry_run else "Deleting"
        self.stdout.write("")
        self.stdout.write(f"{verb} {count} row(s):")

        by_action = targets.values("action_taken").annotate(n=Count("id")).order_by("-n")[:5]
        for row in by_action:
            self.stdout.write(f"  {row['n']:>6}  {row['action_taken'][:70]}")

        oldest = targets.order_by("created_at").values_list("created_at", flat=True).first()
        newest = targets.order_by("-created_at").values_list("created_at", flat=True).first()
        if oldest and newest:
            self.stdout.write(f"  range: {oldest:%Y-%m-%d %H:%M} -> {newest:%Y-%m-%d %H:%M}")

        protected = targets.exclude(transaction__isnull=True).count()
        if protected:
            raise CommandError(
                f"{protected} of the targeted rows are linked to executed trades. "
                "Refusing to delete records of real executions; narrow the selector."
            )

    def _prune_snapshots(self, days: int, *, dry_run: bool) -> None:
        cutoff = timezone.now() - timedelta(days=days)
        stale = PortfolioSnapshot.objects.filter(captured_at__lt=cutoff)
        count = stale.count()
        if not count:
            self.stdout.write(f"Snapshots: nothing older than {days} day(s).")
            return
        if dry_run:
            self.stdout.write(f"Snapshots: would delete {count} row(s) older than {days} day(s).")
            return
        deleted, _ = stale.delete()
        self.stdout.write(self.style.SUCCESS(f"Snapshots: deleted {deleted} row(s)."))

    def _executions(self) -> int:
        return Transaction.objects.count()
