"""Schedule V0-V3 against a frozen opportunity set and publish the comparison.

Usage::

    python manage.py run_experiment
    python manage.py run_experiment --path docs/spec/fixtures/experiments/alphaagent-arms.v1.json
    python manage.py run_experiment --provider stub --output /tmp/comparison.json
    python manage.py run_experiment --username demo

What it guarantees
------------------
* **A pre-registration precedes the run.** The opportunity set's
  ``pre_registration.registered_at`` must be at or before its first scheduled
  opportunity, and its sample plan, primary metric, cost ceiling and drawdown
  criterion are required. A missing registration is an error, not a default.
* **The same snapshot for every arm.** One ``InputSnapshot`` per opportunity is
  created and reused; the report compares the hashes instead of asserting them.
* **Distinct protocol ids.** Each arm records the protocol it actually ran
  (``heuristic_fallback.v1``, ``single_agent_baseline.v1``,
  ``debate.no_rebuttal.v1``, ``debate.v1``). A mismatch is published.
* **An arm that cannot run stays unexecuted.** With ``--provider auto`` and no
  configured model, V1-V3 are ``not_executed`` with the reason and remain in the
  denominator. ``--provider stub`` runs them through the real protocol with a
  scripted provider and labels the output ``engineering_validation_only``.
* **No model import when it does not run one.** V0 is deterministic and needs no
  provider.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from core.models import Portfolio
from services.experiment_execution import (
    ExecutionError,
    execute_opportunity_set,
    load_opportunity_set,
)

DEFAULT_OPPORTUNITY_SET = Path("docs/spec/fixtures/experiments/alphaagent-arms.v1.json")


class Command(BaseCommand):
    help = "Schedule V0-V3 against a frozen opportunity set and publish the comparison report."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--path",
            default=str(DEFAULT_OPPORTUNITY_SET),
            help=f"Opportunity set, relative to BASE_DIR (default: {DEFAULT_OPPORTUNITY_SET}).",
        )
        parser.add_argument(
            "--username",
            default="demo",
            help="User whose portfolio owns the runs (default: demo).",
        )
        parser.add_argument(
            "--provider",
            choices=("auto", "stub"),
            default="auto",
            help=(
                "auto: use the configured provider, leaving LLM arms not_executed when none "
                "is configured. stub: run them through the real protocol with a scripted "
                "provider, labelled simulated."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Re-run an (arm, opportunity) that already has a recorded run. Without "
                "this the command is idempotent and reuses the existing row, which makes "
                "a second execution look like it did work it did not do."
            ),
        )
        parser.add_argument(
            "--output",
            default="",
            help="Write the comparison JSON here instead of stdout.",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["path"])
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        if not path.exists():
            raise CommandError(f"Opportunity set not found: {path}")
        username = (options["username"] or "").strip()
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"Unknown user {username!r}; create it or pass --username.")
        portfolio = Portfolio.objects.filter(user=user).first()
        if portfolio is None:
            raise CommandError(f"User {username!r} has no portfolio.")

        try:
            display_path = path.resolve().relative_to(Path(settings.BASE_DIR).resolve())
        except ValueError:  # pragma: no cover - a set outside the repository
            display_path = path
        try:
            opportunity_set = load_opportunity_set(path)
            opportunity_set.source_path = str(display_path)
            report = execute_opportunity_set(
                opportunity_set=opportunity_set,
                portfolio=portfolio,
                provider_mode=options["provider"],
                force=options["force"],
            )
        except ExecutionError as exc:
            raise CommandError(str(exc)) from exc

        payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        output = (options["output"] or "").strip()
        if output:
            target = Path(output)
            if not target.is_absolute():
                target = Path(settings.BASE_DIR) / target
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
            self.stdout.write(f"Wrote comparison report to {target}")
        else:
            self.stdout.write(payload)
        denominators = report["denominators"]
        self.stdout.write(
            "scheduled={scheduled} completed={completed} failed={failed} "
            "not_executed={not_executed} content_hash={hash}".format(
                hash=report["contents_hash"], **denominators
            )
        )
