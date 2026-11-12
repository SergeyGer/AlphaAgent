"""Rebuild an archived experiment-comparison report from its recorded inputs.

Usage::

    python manage.py rebuild_experiment_report --path /tmp/comparison.json
    python manage.py rebuild_experiment_report --path /tmp/comparison.json --execute

The command is a **dry run by default**: it reports the rebuild status, the
metric comparison, the content hash and every difference it found, and writes
nothing. ``--execute`` writes the rebuilt document to ``--output`` (default:
``<archive>.rebuilt.json``); the archived input is never overwritten.

A malformed or unsupported archive is a typed error naming the field and value.
A rebuilt synthetic archive stays labelled synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from services.experiment_rebuild import RebuildError, rebuild_comparison_report


def _resolve(path: str) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = Path(settings.BASE_DIR) / target
    return target


def _default_output(path: Path) -> Path:
    if path.name.endswith(".json"):
        return path.with_name(path.name[: -len(".json")] + ".rebuilt.json")
    return path.with_name(path.name + ".rebuilt.json")


def _top_level_paths(differences: list[dict]) -> list[str]:
    seen: list[str] = []
    for difference in differences:
        path = str(difference.get("path") or "")
        head = path.split(".", 1)[0].split("[", 1)[0]
        if head and head not in seen:
            seen.append(head)
    return seen


class Command(BaseCommand):
    help = (
        "Rebuild an archived comparison report from its recorded inputs and report "
        "differences. Dry run by default; --execute writes the rebuilt JSON."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--path",
            required=True,
            help="Archived comparison report (JSON), relative to BASE_DIR or absolute.",
        )
        parser.add_argument(
            "--output",
            default="",
            help="Where --execute writes the rebuilt JSON (default: <archive>.rebuilt.json).",
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Write the rebuilt report. Without this flag the command is a dry run.",
        )

    def handle(self, *args, **options) -> None:
        archive_path = _resolve(options["path"])
        if not archive_path.exists():
            raise CommandError(f"Archived report not found: {archive_path}")
        try:
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Archived report is not readable JSON: {exc}") from exc
        try:
            result = rebuild_comparison_report(archive)
        except RebuildError as exc:
            raise CommandError(f"{exc.code}: {exc.detail}") from exc

        execute = bool(options["execute"])
        output = _resolve(options["output"]) if options["output"] else _default_output(archive_path)
        if execute and output.resolve() == archive_path.resolve():
            raise CommandError(
                "--output resolves to the archived report; a rebuild never overwrites the "
                "archive it is checking. Choose a different output path."
            )
        summary = {
            "status": result["status"],
            "rebuild_method": result["rebuild_method"],
            "archive_path": str(archive_path),
            "archive_format": result["archive_format"],
            "report_schema_version": result["report_schema_version"],
            "synthetic": result["synthetic"],
            "synthetic_label": result["synthetic_label"],
            "honesty": result["honesty"],
            "metric": result["metric"],
            "contents_hash": result["contents_hash"],
            "differences": result["differences"],
            "difference_count": result["difference_count"],
            "changed_top_level_blocks": _top_level_paths(result["differences"]),
            "dry_run": not execute,
            "would_write": str(output),
        }
        if execute:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(result["rebuilt_report"], indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            summary["dry_run"] = False
            summary["wrote"] = str(output)
        else:
            summary["note"] = "Dry run: no file was written. Pass --execute to write the rebuild."
        self.stdout.write(json.dumps(summary, indent=2, ensure_ascii=False))
