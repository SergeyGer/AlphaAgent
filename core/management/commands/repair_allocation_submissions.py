"""Find portfolio-analysis submissions a worker will not finish.

The analysis worker owns a submission by claiming it under a lease. If the
process dies, the row stays ``running`` and nothing retries it; if the broker
never delivered it, the row stays ``queued``. This command is the explicit
repair path the asynchronous design needs.

Dry run is the default. It reports every stale row, the action it would take and
the reason, and writes nothing. ``--execute`` performs the action:

* a row past its deadline becomes ``timed_out``;
* a running row whose provider call started but whose result was never recorded
  becomes ``failed`` with ``provider_call_unreconciled`` - a retry would spend a
  second time, which the record cannot justify;
* a row whose worker merely disappeared before the paid call is returned to
  ``queued`` and dispatched again.

A terminal row is never reopened. Nothing in this command creates a plan or
applies a trade.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from services.allocation_submission import recover_stuck_submissions


class Command(BaseCommand):
    help = (
        "Report submissions stuck in queued/running past their deadline and, with "
        "--execute, time them out, fail an unreconciled provider call, or re-queue "
        "a lost worker. Dry run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Apply the reported repair. Without this flag nothing is written.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report only; this is the default, and it wins if given with --execute.",
        )
        parser.add_argument(
            "--stale-seconds",
            type=int,
            default=None,
            help="Queue/heartbeat age that counts as stuck (default from settings).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum rows to inspect in one pass.",
        )

    def handle(self, *args, **options):
        apply = options["execute"] and not options["dry_run"]
        report = recover_stuck_submissions(
            dry_run=not apply,
            stale_after_seconds=options["stale_seconds"],
            limit=options["limit"],
        )
        payload = {
            "dry_run": not apply,
            "count": len(report),
            "submissions": report,
        }
        self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        # Machine-readable JSON is the only stdout content; the human summary
        # goes to stderr so a caller can pipe the report straight into a parser.
        if apply:
            self.stderr.write(
                self.style.SUCCESS(f"Repaired {sum(1 for row in report if row['applied'])} row(s).")
            )
        else:
            self.stderr.write(
                self.style.WARNING(
                    f"Dry run: {len(report)} row(s) would be repaired. Pass --execute to apply."
                )
            )
