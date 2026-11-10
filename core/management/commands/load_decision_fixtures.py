"""Load the Phase 1 decision-run fixtures (REP-02, REP-03).

Usage::

    python manage.py load_decision_fixtures
    python manage.py load_decision_fixtures --username demo
    python manage.py load_decision_fixtures --path docs/spec/fixtures/decision-runs.v1.json
    python manage.py load_decision_fixtures --force   # recreate drifted fixtures

What it guarantees
------------------
* **Idempotent.** A fixture run is keyed by its stable ``fixture_id``. Running
  the command twice creates nothing the second time; it reports the runs it
  skipped. If the manifest content for an existing fixture has changed, the
  command refuses rather than silently mixing old and new demo data - pass
  ``--force`` to delete and recreate that run.
* **Integrity-checked.** Every fixture carries a SHA-256 checksum over its own
  canonical JSON. A hand-edited manifest fails before a single row is written.
* **Honest.** Every fixture must state ``mode``, ``source`` and ``recording_date``
  and the run is created with exactly those values. The Phase 1 fixtures are
  ``mode="synthetic"`` because the live pipeline has no rebuttal stage; the API
  exposes ``mode``/``is_synthetic`` so a UI labels them.
* **Through the real services.** Snapshot capture, claims, rebuttals, status
  transitions and event appends all go through ``services.evidence`` and
  ``services.decision_runs``, so the fixtures cannot encode a state the domain
  layer would refuse to produce.
"""

from __future__ import annotations

import json
from datetime import UTC
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.decision_json import content_hash as hash_of
from core.decision_json import utc_iso
from core.decision_models import (
    DecisionRecord,
    DecisionRun,
    GuardRecord,
    GuardStatus,
    OutcomeMaturity,
    OutcomeRecord,
    RunEventType,
    RunMode,
    RunSource,
    RunStage,
)
from core.models import Portfolio
from services.decision_runs import append_event, create_run, transition_status
from services.evidence import create_claim, create_evidence_item, create_rebuttal, create_snapshot

DEFAULT_MANIFEST = Path("docs/spec/fixtures/decision-runs.v1.json")
SUPPORTED_MANIFEST_VERSION = 1
SUPPORTED_SCHEMA_VERSION = "1.0"


class Command(BaseCommand):
    help = "Load the versioned Phase 1 decision-run fixtures into a demo portfolio."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--path",
            default=str(DEFAULT_MANIFEST),
            help=f"Manifest path, relative to BASE_DIR (default: {DEFAULT_MANIFEST}).",
        )
        parser.add_argument(
            "--username",
            default="demo",
            help="User whose portfolio receives the fixture runs (default: demo).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Delete and recreate a fixture run whose checksum has drifted.",
        )

    def handle(self, *args, **options) -> None:
        manifest_path = Path(options["path"])
        if not manifest_path.is_absolute():
            manifest_path = Path(settings.BASE_DIR) / manifest_path
        if not manifest_path.exists():
            raise CommandError(f"Fixture manifest not found: {manifest_path}")

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Fixture manifest is not valid JSON: {exc}") from exc

        self._validate_manifest(manifest, manifest_path)

        username = options["username"].strip()
        if not username:
            raise CommandError("--username must not be blank.")

        user, user_created = User.objects.get_or_create(username=username, defaults={"email": ""})
        if user_created:
            # No password: a fixture account is not a login.
            user.set_unusable_password()
            user.save(update_fields=["password"])

        portfolio, _ = Portfolio.objects.get_or_create(user=user)
        self.stdout.write(
            f"Fixture manifest {manifest_path.name} "
            f"(v{manifest['manifest_version']}, schema {manifest['schema_version']})"
        )
        self.stdout.write(
            f"Target portfolio: {portfolio.id} for user '{username}'"
            + (" (created)" if user_created else "")
        )
        self.stdout.write("")

        created = skipped = 0
        for fixture in manifest["fixtures"]:
            fixture_id = fixture["fixture_id"]
            checksum = self._expected_checksum(fixture)
            existing = DecisionRun.objects.filter(fixture_id=fixture_id).first()

            if existing is not None:
                if existing.fixture_checksum == checksum:
                    skipped += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"SKIP   {fixture_id} (already loaded as run {existing.id})"
                        )
                    )
                    continue
                if not options["force"]:
                    raise CommandError(
                        f"Fixture {fixture_id!r} already exists with a different checksum "
                        f"({existing.fixture_checksum or 'empty'} != {checksum}). Re-run with "
                        f"--force to replace it."
                    )
                self._delete_fixture_run(existing)
                self.stdout.write(
                    self.style.WARNING(f"RESET  {fixture_id} (checksum changed, --force)")
                )

            run = self._load_fixture(fixture, portfolio=portfolio, checksum=checksum)
            created += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"CREATE {fixture_id} -> run {run.id} "
                    f"[{fixture['scenario']}, {run.ticker}, {run.status}]"
                )
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {created} created, {skipped} skipped, "
                f"{len(manifest['fixtures'])} fixture(s) in the manifest."
            )
        )
        if created == 0:
            self.stdout.write("Nothing was written; the fixture runs are already present.")

    # -- manifest validation ------------------------------------------------
    def _validate_manifest(self, manifest: dict, manifest_path: Path) -> None:
        if not isinstance(manifest, dict):
            raise CommandError("The fixture manifest must be a JSON object.")
        if manifest.get("manifest_version") != SUPPORTED_MANIFEST_VERSION:
            raise CommandError(
                f"Unsupported manifest_version {manifest.get('manifest_version')!r}; "
                f"this loader supports {SUPPORTED_MANIFEST_VERSION}."
            )
        if manifest.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
            raise CommandError(
                f"Unsupported schema_version {manifest.get('schema_version')!r}; "
                f"this loader supports {SUPPORTED_SCHEMA_VERSION}."
            )
        fixtures = manifest.get("fixtures")
        if not isinstance(fixtures, list) or not fixtures:
            raise CommandError("The fixture manifest must contain a non-empty 'fixtures' list.")

        seen: set[str] = set()
        for fixture in fixtures:
            if not isinstance(fixture, dict):
                raise CommandError("Each fixture must be a JSON object.")
            fixture_id = fixture.get("fixture_id")
            if not fixture_id or not isinstance(fixture_id, str):
                raise CommandError("Each fixture needs a string 'fixture_id'.")
            if fixture_id in seen:
                raise CommandError(f"Duplicate fixture_id {fixture_id!r} in the manifest.")
            seen.add(fixture_id)
            for field in ("scenario", "recording_date", "protocol_version", "ticker"):
                if not fixture.get(field):
                    raise CommandError(f"Fixture {fixture_id!r} is missing '{field}'.")
            if fixture.get("mode") not in RunMode.values:
                raise CommandError(
                    f"Fixture {fixture_id!r} has unknown mode {fixture.get('mode')!r}."
                )
            if fixture.get("source") not in RunSource.values:
                raise CommandError(
                    f"Fixture {fixture_id!r} has unknown source {fixture.get('source')!r}."
                )
            if not isinstance(fixture.get("snapshot"), dict):
                raise CommandError(f"Fixture {fixture_id!r} has no snapshot object.")
            if not isinstance(fixture.get("timeline"), list) or not fixture["timeline"]:
                raise CommandError(f"Fixture {fixture_id!r} has an empty timeline.")

            recorded = parse_datetime(str(fixture["started_at"]))
            if recorded is None or timezone.is_naive(recorded):
                raise CommandError(
                    f"Fixture {fixture_id!r} started_at must be an ISO-8601 UTC timestamp."
                )
            if recorded.astimezone(UTC).date().isoformat() != fixture["recording_date"]:
                raise CommandError(
                    f"Fixture {fixture_id!r} started_at is not on its recording_date "
                    f"{fixture['recording_date']}."
                )

        honesty = manifest.get("honesty") or {}
        if honesty.get("all_fixtures_synthetic") and any(
            fixture["mode"] != RunMode.SYNTHETIC for fixture in fixtures
        ):
            raise CommandError(
                "The manifest declares all fixtures synthetic but at least one is not. "
                "Fix the manifest or remove the honesty declaration."
            )

    def _expected_checksum(self, fixture: dict) -> str:
        payload = {key: value for key, value in fixture.items() if key != "checksum"}
        return hash_of(payload)

    def _verify_checksum(self, fixture: dict) -> None:
        expected = self._expected_checksum(fixture)
        declared = (fixture.get("checksum") or "").strip()
        if not declared:
            raise CommandError(
                f"Fixture {fixture['fixture_id']!r} has no checksum. "
                f"Recompute the manifest checksums (expected {expected})."
            )
        if declared != expected:
            raise CommandError(
                f"Fixture {fixture['fixture_id']!r} failed its checksum: "
                f"declared {declared}, computed {expected}. The manifest was edited "
                "without updating the checksum."
            )

    # -- loading ------------------------------------------------------------
    def _delete_fixture_run(self, run: DecisionRun) -> None:
        snapshot_id = run.snapshot_id
        run.delete()
        if snapshot_id is not None:
            from core.decision_models import InputSnapshot

            snapshot = InputSnapshot.objects.filter(pk=snapshot_id).first()
            if snapshot is not None and not snapshot.runs.exists():
                snapshot.delete()

    def _load_fixture(self, fixture: dict, *, portfolio: Portfolio, checksum: str) -> DecisionRun:
        self._verify_checksum(fixture)
        fixture_id = fixture["fixture_id"]

        with transaction.atomic():
            snapshot = self._create_snapshot(fixture, portfolio=portfolio)
            run = create_run(
                portfolio=portfolio,
                ticker=fixture["ticker"],
                protocol_version=fixture["protocol_version"],
                mode=fixture["mode"],
                source=fixture["source"],
                snapshot=snapshot,
                started_at=self._at(fixture, "started_at"),
                fixture_id=fixture_id,
                fixture_checksum=checksum,
            )

            evidence: dict[str, object] = {}
            claims: dict[str, object] = {}
            decision: DecisionRecord | None = None

            for index, step in enumerate(fixture["timeline"]):
                kind = step.get("kind")
                at = self._at(step, "at", context=f"{fixture_id} step {index}")
                if kind == "transition":
                    transition_status(
                        run,
                        step["status"],
                        occurred_at=at,
                        payload=step.get("payload") or {},
                    )
                elif kind == "event":
                    append_event(
                        run,
                        step["event_type"],
                        stage=step["stage"],
                        agent_role=step.get("agent_role") or "",
                        payload=step.get("payload") or {},
                        occurred_at=at,
                    )
                elif kind == "evidence":
                    item = self._create_evidence(snapshot, step)
                    evidence[step["key"]] = item
                    append_event(
                        run,
                        RunEventType.EVIDENCE_RECORDED,
                        stage=RunStage.COLLECTING,
                        agent_role="system",
                        occurred_at=at,
                        payload={
                            "evidence_id": str(item.id),
                            "key": step["key"],
                            "source_type": item.source_type,
                            "published_at": utc_iso(item.published_at),
                            "available_at": utc_iso(item.available_at),
                        },
                    )
                elif kind == "claim":
                    claim = create_claim(
                        run=run,
                        agent_role=step["agent_role"],
                        stance=step["stance"],
                        statement=step["statement"],
                        claim_type=step["claim_type"],
                        evidence_ids=[
                            evidence[key].id for key in self._keys(step, "evidence_keys")
                        ],
                        horizon=step.get("horizon") or "",
                        limitations=step.get("limitations") or "",
                        case_round=int(step.get("case_round", 1)),
                        created_at=at,
                    )
                    claims[step["key"]] = claim
                    append_event(
                        run,
                        RunEventType.CLAIM_CREATED,
                        stage=RunStage.ANALYSING,
                        agent_role=claim.agent_role,
                        occurred_at=at,
                        payload={
                            "claim_id": str(claim.claim_id),
                            "key": step["key"],
                            "stance": claim.stance,
                            "claim_type": claim.claim_type,
                            "evidence_ids": [str(item.id) for item in claim.evidence.all()],
                        },
                    )
                elif kind == "rebuttal":
                    target = claims.get(step["target_claim_key"])
                    if target is None:
                        raise CommandError(
                            f"Fixture {fixture_id!r} rebuts unknown claim "
                            f"{step['target_claim_key']!r}."
                        )
                    rebuttal = create_rebuttal(
                        run=run,
                        responding_agent=step["responding_agent"],
                        target_claim=target,
                        response_type=step["response_type"],
                        explanation=step["explanation"],
                        evidence_ids=[
                            evidence[key].id for key in self._keys(step, "evidence_keys")
                        ],
                        position_change=step.get("position_change") or "",
                        created_at=at,
                    )
                    append_event(
                        run,
                        RunEventType.REBUTTAL_CREATED,
                        stage=RunStage.REBUTTING,
                        agent_role=rebuttal.responding_agent,
                        occurred_at=at,
                        payload={
                            "rebuttal_id": str(rebuttal.id),
                            "key": step["key"],
                            "target_claim_id": str(target.claim_id),
                            "response_type": rebuttal.response_type,
                        },
                    )
                elif kind == "decision":
                    if decision is not None:
                        raise CommandError(f"Fixture {fixture_id!r} has more than one decision.")
                    decisive = []
                    for key in self._keys(step, "decisive_claim_keys"):
                        if key not in claims:
                            raise CommandError(
                                f"Fixture {fixture_id!r} names unknown decisive claim {key!r}."
                            )
                        decisive.append(str(claims[key].claim_id))
                    decision = DecisionRecord.objects.create(
                        run=run,
                        action=step["action"],
                        requested_quantity=self._decimal_or_none(
                            step.get("requested_quantity"), field="requested_quantity"
                        ),
                        concise_rationale=step["concise_rationale"],
                        decisive_claim_ids=decisive,
                        strongest_objection=step.get("strongest_objection") or "",
                        thesis=step.get("thesis") or "",
                        invalidation_condition=step.get("invalidation_condition") or "",
                        engine_source=step["engine_source"],
                        created_at=at,
                    )
                    append_event(
                        run,
                        RunEventType.DECISION_PROPOSED,
                        stage=RunStage.ADJUDICATING,
                        agent_role="cio",
                        occurred_at=at,
                        payload={
                            "decision_id": str(decision.id),
                            "action": decision.action,
                            "requested_quantity": (
                                str(decision.requested_quantity)
                                if decision.requested_quantity is not None
                                else None
                            ),
                            "decisive_claim_ids": decisive,
                        },
                    )
                elif kind == "guard":
                    if decision is None:
                        raise CommandError(
                            f"Fixture {fixture_id!r} evaluates a guard before a decision."
                        )
                    if step["status"] not in GuardStatus.values:
                        raise CommandError(
                            f"Fixture {fixture_id!r} has unknown guard status {step['status']!r}."
                        )
                    numerical_checks = dict(step.get("numerical_checks") or {})
                    if step.get("checks"):
                        # The typed check list travels inside numerical_checks so
                        # the guard row keeps one JSON contract; the API lifts it
                        # back out as ``checks``.
                        numerical_checks["checks"] = step["checks"]
                    guard = GuardRecord.objects.create(
                        proposal=decision,
                        policy_version=step["policy_version"],
                        evaluated_portfolio_version=step["evaluated_portfolio_version"],
                        quote_time=self._at(step, "quote_time"),
                        status=step["status"],
                        requested_quantity=self._decimal_or_none(
                            step["requested_quantity"],
                            field="guard.requested_quantity",
                            required=True,
                        ),
                        allowed_quantity=self._decimal_or_none(
                            step["allowed_quantity"], field="guard.allowed_quantity", required=True
                        ),
                        reason_codes=list(step.get("reason_codes") or []),
                        binding_rule=step.get("binding_rule") or "",
                        numerical_checks=numerical_checks,
                        created_at=at,
                    )
                    append_event(
                        run,
                        RunEventType.GUARD_EVALUATED,
                        stage=RunStage.VALIDATING,
                        agent_role="risk",
                        occurred_at=at,
                        payload={
                            "guard_id": str(guard.id),
                            "status": guard.status,
                            "requested_quantity": str(guard.requested_quantity),
                            "allowed_quantity": str(guard.allowed_quantity),
                            "binding_rule": guard.binding_rule or None,
                            "reason_codes": list(guard.reason_codes),
                        },
                    )
                elif kind == "outcome":
                    if decision is None:
                        raise CommandError(
                            f"Fixture {fixture_id!r} records an outcome before a decision."
                        )
                    maturity = step["maturity"]
                    if maturity not in OutcomeMaturity.values:
                        raise CommandError(
                            f"Fixture {fixture_id!r} has unknown outcome maturity {maturity!r}."
                        )
                    outcome = OutcomeRecord.objects.create(
                        decision=decision,
                        horizon_days=int(step["horizon_days"]),
                        method_version=step["method_version"],
                        maturity=maturity,
                        price_at_decision=self._decimal_or_none(
                            step.get("price_at_decision"), field="price_at_decision"
                        ),
                        price_at_horizon=self._decimal_or_none(
                            step.get("price_at_horizon"), field="price_at_horizon"
                        ),
                        price_observed_at=self._optional_at(step, "price_observed_at"),
                        price_source=step.get("price_source") or "",
                        forward_return_pct=self._decimal_or_none(
                            step.get("forward_return_pct"), field="forward_return_pct"
                        ),
                        decision_return_pct=self._decimal_or_none(
                            step.get("decision_return_pct"), field="decision_return_pct"
                        ),
                        benchmark_ticker=step.get("benchmark_ticker") or "",
                        benchmark_return_pct=self._decimal_or_none(
                            step.get("benchmark_return_pct"), field="benchmark_return_pct"
                        ),
                        signal_diagnostics=step.get("signal_diagnostics") or {},
                        unavailable_reason=step.get("unavailable_reason") or "",
                        evaluated_at=self._optional_at(step, "evaluated_at"),
                        created_at=at,
                    )
                    append_event(
                        run,
                        RunEventType.OUTCOME_RECORDED,
                        stage=RunStage.COMPLETED,
                        agent_role="system",
                        occurred_at=at,
                        payload={
                            "outcome_id": str(outcome.id),
                            "horizon_days": outcome.horizon_days,
                            "maturity": outcome.maturity,
                            "method_version": outcome.method_version,
                        },
                    )
                else:
                    raise CommandError(
                        f"Fixture {fixture_id!r} step {index} has unknown kind {kind!r}."
                    )

            run.refresh_from_db()
            if decision is None:
                raise CommandError(f"Fixture {fixture_id!r} produced no decision record.")
            if not run.is_terminal:
                raise CommandError(
                    f"Fixture {fixture_id!r} ended in {run.status!r}; a fixture must "
                    "finish in a terminal state (completed, failed or cancelled)."
                )

        return run

    # -- value coercion -----------------------------------------------------
    def _create_snapshot(self, fixture: dict, *, portfolio: Portfolio):
        data = fixture["snapshot"]
        return create_snapshot(
            portfolio=portfolio,
            portfolio_version=data["portfolio_version"],
            analysis_cutoff=self._at(data, "analysis_cutoff"),
            captured_at=self._at(data, "captured_at"),
            holdings=data.get("holdings") or [],
            cash=data.get("cash") or {},
            quotes=data.get("quotes") or {},
        )

    def _create_evidence(self, snapshot, step: dict):
        return create_evidence_item(
            snapshot=snapshot,
            source_type=step["source_type"],
            source_uri=step.get("source_uri") or "",
            publisher=step.get("publisher") or "",
            published_at=self._optional_at(step, "published_at"),
            retrieved_at=self._at(step, "retrieved_at"),
            available_at=self._optional_at(step, "available_at"),
            excerpt=step.get("excerpt") or "",
            observation=step.get("observation"),
            units=step.get("units") or "",
            currency=step.get("currency") or "",
            is_redistributable=bool(step.get("is_redistributable", False)),
        )

    def _decimal_or_none(self, value, *, field: str, required: bool = False):
        if value is None:
            if required:
                raise CommandError(f"Fixture value '{field}' is required but missing.")
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise CommandError(f"Fixture value '{field}' is not a decimal: {value!r}") from exc

    def _at(self, data: dict, key: str, *, context: str = "fixture") -> object:
        value = self._optional_at(data, key)
        if value is None:
            raise CommandError(f"{context} is missing a valid ISO-8601 '{key}' timestamp.")
        return value

    def _optional_at(self, data: dict, key: str):
        raw = data.get(key)
        if raw in (None, ""):
            return None
        parsed = parse_datetime(str(raw))
        if parsed is None:
            raise CommandError(f"Fixture value '{key}' is not an ISO-8601 timestamp: {raw!r}")
        if timezone.is_naive(parsed):
            raise CommandError(f"Fixture value '{key}' must be timezone-aware: {raw!r}")
        return parsed

    def _keys(self, step: dict, field: str) -> list[str]:
        values = step.get(field) or []
        if not isinstance(values, list):
            raise CommandError(f"Fixture field '{field}' must be a list of keys.")
        return [str(value) for value in values]
