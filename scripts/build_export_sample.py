#!/usr/bin/env python
"""Regenerate the committed decision-run export contract sample.

The sample at ``docs/spec/fixtures/decision-run-export.v1.json`` is captured
from the authenticated ``GET /api/decision-runs/<id>/export/`` endpoint, not
assembled by hand. It carries every field the endpoint emits, including nested
decimal strings, nullable times, two guard evaluations, a superseded outcome and
Unicode text.

Two suites read the same file:

* ``core/tests/test_p1_export_portability.py`` verifies its checksum and compares
  its recursive shape with a fresh export, so the emitter cannot drift without a
  failing test;
* ``frontend/viewer/exportParity.test.ts`` imports it unchanged, with no
  network, and asserts the viewer preserves the visible data.

Run this only when the export shape changes intentionally, then review the diff:

    venv/bin/python scripts/build_export_sample.py --force
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

DEFAULT_OUTPUT = PROJECT_ROOT / "docs/spec/fixtures/decision-run-export.v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing sample. Without it a changed sample is refused.",
    )
    args = parser.parse_args()

    from runtime_env import ensure_writable_runtime_home

    ensure_writable_runtime_home()

    import django

    django.setup()

    from decimal import Decimal

    from django.contrib.auth.models import User
    from django.db import transaction
    from rest_framework.authtoken.models import Token
    from rest_framework.test import APIClient

    from core.models import Portfolio
    from core.tests.export_sample import create_export_sample_run
    from services.recording_provenance import RecordingRefused, require_clean_for_recording

    output = Path(args.out).resolve()
    if output.exists() and not args.force:
        print(f"REFUSED: {output} exists. Pass --force to regenerate it.", file=sys.stderr)
        return 2
    # A captured export is a recording. Refuse to produce one from a tree that
    # cannot be reproduced from the commit it would name.
    try:
        require_clean_for_recording()
    except RecordingRefused as exc:
        print(f"REFUSED {exc.code}: {exc.detail}", file=sys.stderr)
        return 2

    # Capture inside one transaction and roll it back: the export file is the
    # artifact, and a regeneration must not leave fixture rows in the database.
    with transaction.atomic():
        username = f"export-sample-{uuid.uuid4().hex[:12]}"
        user = User.objects.create_user(username=username, password=uuid.uuid4().hex)
        token, _ = Token.objects.get_or_create(user=user)
        portfolio = Portfolio.objects.create(
            user=user,
            balance_usd=Decimal("1000.0"),
            risk_profile="medium",
            max_trade_allocation_pct=Decimal("90.0"),
            max_portfolio_exposure_pct=Decimal("90.0"),
            max_ticker_concentration_pct=Decimal("30.0"),
            max_sector_concentration_pct=Decimal("40.0"),
            daily_loss_limit_usd=Decimal("500.0"),
        )
        run = create_export_sample_run(portfolio)
        client = APIClient()
        client.defaults["HTTP_HOST"] = "localhost"
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = client.get(f"/api/decision-runs/{run.id}/export/")
        if response.status_code != 200:
            print(
                f"FAILED: export endpoint returned HTTP {response.status_code}.",
                file=sys.stderr,
            )
            return 1
        body = response.content.decode("utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body + "\n", encoding="utf-8")
        transaction.set_rollback(True)
    print(f"WROTE {output.relative_to(PROJECT_ROOT)} ({len(body)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
