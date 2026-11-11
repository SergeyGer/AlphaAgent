"""Build or replay an experiment report from stored outputs (no model calls).

Usage::

    python manage.py build_experiment
    python manage.py build_experiment --path docs/spec/fixtures/experiments/phase3-engineering.v1.json
    python manage.py build_experiment --username demo
    python manage.py build_experiment --force   # replace a manifest whose checksum drifted

What it guarantees
------------------
* **Stored outputs only.** The command reads ``DecisionRun`` rows, the ledger,
  cash flows, snapshots and the manifest's declared accounting fixture. It never
  imports the agent or the debate protocol, and it cannot spend money.
* **Replayable.** Running it twice on the same manifest updates the experiment,
  rebuilds the report from stored rows, and increments ``report_version``. It is
  the replay path, not a one-shot seeder.
* **Denominators are honest.** Every manifest run is registered, including one
  that names a run which is not present or a run that failed. Unexecuted runs
  stay in the completion/failure/cost denominator.
* **A manifest edit is not silent.** The manifest checksum is verified; a
  different checksum is refused unless ``--force`` is passed.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from core.models import Portfolio
from services.experiments import (
    ExperimentError,
    ManifestError,
    build_experiment,
    load_manifest,
)

DEFAULT_MANIFEST = Path("docs/spec/fixtures/experiments/phase3-engineering.v1.json")


class Command(BaseCommand):
    help = "Build or replay a versioned experiment report from stored outputs (no LLM calls)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--path",
            default=str(DEFAULT_MANIFEST),
            help=f"Manifest path, relative to BASE_DIR (default: {DEFAULT_MANIFEST}).",
        )
        parser.add_argument(
            "--username",
            default="demo",
            help="User whose portfolio owns the experiment (default: demo).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Replace an existing experiment whose manifest checksum has drifted. "
                "Without it, drift is an error rather than a silent mix of old and new data."
            ),
        )

    def handle(self, *args, **options) -> None:
        manifest_path = Path(options["path"])
        if not manifest_path.is_absolute():
            manifest_path = Path(settings.BASE_DIR) / manifest_path
        if not manifest_path.exists():
            raise CommandError(f"Experiment manifest not found: {manifest_path}")

        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            raise CommandError(str(exc)) from exc

        username = (options["username"] or "").strip()
        if not username:
            raise CommandError("--username must not be blank.")

        user, user_created = User.objects.get_or_create(username=username, defaults={"email": ""})
        if user_created:
            # A measurement account is not a login.
            user.set_unusable_password()
            user.save(update_fields=["password"])
        portfolio, _ = Portfolio.objects.get_or_create(user=user)

        self.stdout.write(
            f"Experiment manifest {manifest_path.name} "
            f"(v{manifest['manifest_version']}, schema {manifest['schema_version']})"
        )
        self.stdout.write(f"Experiment: {manifest['experiment_id']}")
        self.stdout.write(
            f"Target portfolio: {portfolio.id} for user '{username}'"
            + (" (created)" if user_created else "")
        )
        self.stdout.write(f"Manifest checksum: {manifest['checksum']}")
        self.stdout.write("")

        try:
            experiment, report = build_experiment(
                manifest=manifest, portfolio=portfolio, force=options["force"]
            )
        except (ManifestError, ExperimentError) as exc:
            raise CommandError(str(exc)) from exc

        integrity = report["sample_integrity"]
        counts = integrity["counts"]
        self.stdout.write(
            self.style.SUCCESS(
                f"BUILT  {experiment.experiment_key} -> experiment {experiment.id} "
                f"(report v{experiment.report_version})"
            )
        )
        self.stdout.write(
            "  scheduled={scheduled} completed={completed} failed={failed} "
            "not_executed={not_executed}".format(
                scheduled=counts["scheduled"],
                completed=counts["completed"],
                failed=counts["failed"],
                not_executed=counts["not_executed"],
            )
        )
        for variant in report["variants"]:
            self.stdout.write(
                f"  {variant['key']} {variant['label']}: {variant['status']} "
                f"(scheduled={variant['counts']['scheduled']}, "
                f"completed={variant['counts']['completed']}, "
                f"failed={variant['counts']['failed']})"
            )
        self.stdout.write(
            "  signal observations={observations} evaluated={evaluated} "
            "unresolved={unresolved}".format(
                observations=len(report["signal_diagnostics"]["rows"]),
                evaluated=sum(
                    summary["evaluated"]
                    for summary in report["signal_diagnostics"]["summary_by_horizon"].values()
                ),
                unresolved=report["signal_diagnostics"]["unresolved"]["count"],
            )
        )
        performance = report.get("portfolio_performance") or {}
        if performance.get("available") is False:
            self.stdout.write(
                self.style.WARNING(
                    f"  portfolio performance unavailable: {performance.get('reason')}"
                )
            )
        else:
            self.stdout.write(
                "  portfolio performance: twr={twr}% net_of_llm={net}% "
                "max_drawdown={dd}% approximate={approx}".format(
                    twr=performance.get("time_weighted_return_pct"),
                    net=performance.get("net_of_llm_return_pct"),
                    dd=performance.get("max_drawdown_pct"),
                    approx=performance.get("approximate"),
                )
            )
        self.stdout.write(
            "  report: "
            + json.dumps(
                {
                    "experiment_id": report["experiment"]["experiment_id"],
                    "report_version": report.get("report_version"),
                    "engineering_validation_only": report["engineering_validation_only"],
                    "no_profitability_claim": report["no_profitability_claim"],
                }
            )
        )
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "Done. No LLM or market-data request was made; the report was built from "
                "stored rows and the manifest."
            )
        )
