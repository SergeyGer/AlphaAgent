"""Check that committed recording artefacts name a passing acceptance record.

    venv/bin/python manage.py check_recordings
    venv/bin/python manage.py check_recordings --json

Exit status is non-zero when any artefact names a different commit or digest,
has no record, or has no provenance block at all. The JSON form is intended for
the verification pipeline; the human form prints one line per artefact.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from services.recording_provenance import check_recording_pairing


class Command(BaseCommand):
    help = "Verify recording artefact -> acceptance record pairings."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--json", action="store_true", help="Print the check payload as JSON.")

    def handle(self, *args, **options) -> None:
        result = check_recording_pairing()
        if options["json"]:
            self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
        else:
            for row in result["artifacts"]:
                marker = "OK" if row["ok"] else "FAIL"
                self.stdout.write(f"{marker} {row['name']}: {row['code']} - {row['detail']}")
            self.stdout.write(f"{result['checked']} checked, {result['failed']} failed")
        if not result["ok"]:
            raise CommandError(
                "Recording provenance check failed: "
                + ", ".join(
                    f"{row['name']}={row['code']}" for row in result["artifacts"] if not row["ok"]
                )
            )
