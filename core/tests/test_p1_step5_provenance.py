"""P1 step 5 follow-up: an authored export is not a provider debate.

Reproduced before this change: the committed export sample was built from
authored claims, a rebuttal, two guard results and provider-shaped fields, but
carried ``mode="recorded"``, ``source="crewai"`` and a provider/model block. The
viewer derives its source label from the mode, so it rendered the sample as
**Real recorded / actually executed** - the exact conflation of an authored
fixture with a provider-backed debate that this module pins.

The fix keeps three claims separate:

* the export ``checksum`` (integrity);
* the ``recording`` block (build verification);
* ``run.origin`` (how the content was produced). Only ``provider_run`` is
  provider-backed.

The superseded sample is retained as a compatibility conformance file so an
older export can still be verified and is never upgraded to a provider debate by
the viewer. The provider-backed sample is test data for the Step 11 recording
path: its record shape is real, the call for the file is not.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from core.decision_json import (
    ExportVerificationError,
    verify_export_bundle,
)
from core.decision_serializers import build_run_metadata
from core.tests.export_sample import create_export_sample_run
from core.tests.helpers import auth_client, make_portfolio, make_user

FIXTURES = Path(settings.BASE_DIR) / "docs/spec/fixtures"
SAMPLE = FIXTURES / "decision-run-export.v1.json"
SUPERSEDED = FIXTURES / "decision-run-export.v0-superseded.json"
PROVIDER_SAMPLE = FIXTURES / "decision-run-export.provider-backed.v1.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class AuthoredExportSampleTests(TestCase):
    """5A: the committed authored sample verifies and says what it is."""

    def setUp(self):
        self.sample = load_json(SAMPLE)

    def test_the_sample_is_a_verified_authored_fixture(self):
        self.assertIs(verify_export_bundle(self.sample), self.sample)
        run = self.sample["run"]
        self.assertEqual(run["mode"], "synthetic")
        self.assertEqual(run["source"], "fixture")
        self.assertIs(run["is_synthetic"], True)
        self.assertEqual(run["protocol_version"], "synthetic.v1")

        origin = run["origin"]
        self.assertEqual(origin["kind"], "authored_fixture")
        self.assertIs(origin["provider_backed"], False)
        self.assertIs(origin["authored"], True)
        self.assertEqual(origin["run_record"], "fixture_manifest")
        self.assertEqual(origin["fixture_id"], "export-contract-aapl-2026-11-12")
        self.assertTrue(any("authored fixture data" in text for text in origin["limitations"]))
        self.assertTrue(any("neither establishes" in text for text in origin["limitations"]))

    def test_checksum_recording_and_run_origin_are_three_different_claims(self):
        claims = self.sample["provenance_claims"]
        self.assertEqual(claims["schema_version"], "decision_run.provenance_claims.v1")
        self.assertIs(claims["integrity"]["establishes_integrity"], True)
        self.assertIs(claims["integrity"]["establishes_provider_debate"], False)
        self.assertIs(claims["build_verification"]["establishes_run_origin"], False)
        self.assertIs(claims["build_verification"]["establishes_provider_debate"], False)
        self.assertIs(claims["run_origin"]["establishes_run_origin"], True)
        self.assertIs(claims["run_origin"]["establishes_provider_debate"], False)
        self.assertEqual(claims["run_origin"]["kind"], "authored_fixture")

        recording = self.sample["recording"]
        self.assertEqual(recording["scope"], "build_verification_not_run_origin")
        # The recording block is build provenance. It is not rewritten when the
        # run origin changes, and it does not claim the run was provider-backed.
        self.assertNotIn("provider_backed", recording)
        self.assertNotIn("origin", recording)

    def test_the_authored_sample_keeps_both_guards_and_the_superseded_revision(self):
        guards = self.sample["guard_evaluations"]
        self.assertEqual([row["status"] for row in guards], ["approved", "clamped"])
        self.assertTrue(all(row["id"] for row in guards))
        guard_ids = {row["id"] for row in guards}
        for event in self.sample["events"]:
            if event["event_type"] == "guard.evaluated":
                self.assertIn(event["payload"]["guard_id"], guard_ids)

        superseded = self.sample["superseded_outcomes"]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["method_version"], "attribution.v1")
        self.assertEqual(superseded[0]["revision"], 1)
        current = {row["horizon_days"]: row for row in self.sample["outcomes"]}
        self.assertEqual(current[7]["revision"], 2)
        self.assertEqual(current[7]["method_version"], "attribution.v2")

    def test_event_snapshots_and_redaction_survive_the_export(self):
        run = self.sample["run"]
        snapshot = self.sample["snapshot"]
        self.assertEqual(run["snapshot_id"], snapshot["id"])
        self.assertEqual(len(snapshot["content_hash"]), 64)
        self.assertTrue(self.sample["events"])
        for event in self.sample["events"]:
            self.assertTrue(event["event_id"])
            self.assertTrue(event["schema_version"])
            self.assertTrue(event["persisted_at"])

        omitted = [row for row in self.sample["evidence"] if row.get("excerpt_omitted")]
        self.assertEqual(len(omitted), 1)
        self.assertIsNone(omitted[0]["excerpt"])
        self.assertIsNone(omitted[0]["published_at"])
        self.assertGreater(omitted[0]["excerpt_chars"], 0)
        # The redaction block travels with the document; account identifiers are
        # not in the payload at all.
        self.assertEqual(self.sample["redaction"]["version"], "1.0")
        self.assertEqual(self.sample["redaction"]["evidence_bodies_omitted"], 1)

    def test_editing_the_origin_claim_breaks_verification(self):
        for label, path, value in (
            (
                "origin kind",
                ("run", "origin", "kind"),
                "provider_run",
            ),
            (
                "origin provider flag",
                ("run", "origin", "provider_backed"),
                True,
            ),
            (
                "origin limitation",
                ("run", "origin", "limitations"),
                ["A fabricated provider debate."],
            ),
        ):
            tampered = copy.deepcopy(self.sample)
            target = tampered
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(edit=label):
                with self.assertRaises(ExportVerificationError) as caught:
                    verify_export_bundle(tampered)
                self.assertEqual(caught.exception.code, "checksum")


class SupersededExportSampleTests(TestCase):
    """5B: an older sample still imports, but its recorded label is not upgraded."""

    def setUp(self):
        self.sample = load_json(SUPERSEDED)

    def test_the_superseded_sample_still_verifies(self):
        self.assertIs(verify_export_bundle(self.sample), self.sample)
        self.assertEqual(self.sample["bundle_version"], "1.0")
        self.assertEqual(self.sample["schema_version"], "1.0")
        run = self.sample["run"]
        # The exact defect: the recorded label on authored fixture content.
        self.assertEqual(run["mode"], "recorded")
        self.assertEqual(run["source"], "crewai")
        self.assertEqual(run["fixture_id"], "export-contract-aapl-2026-11-12")
        self.assertNotIn("origin", run)

    def test_the_server_treats_a_recorded_fixture_as_authored(self):
        user = make_user("superseded-origin")
        portfolio = make_portfolio(user)
        run = create_export_sample_run(portfolio)
        # Simulate a row written by the old producer: a recorded label on a run
        # that still carries a fixture_id.
        type(run).objects.filter(pk=run.pk).update(mode="recorded", source="crewai")
        run.refresh_from_db()
        origin = build_run_metadata(run)["origin"]
        self.assertEqual(origin["kind"], "authored_fixture")
        self.assertIs(origin["provider_backed"], False)
        self.assertTrue(
            any("recorded label is superseded" in text for text in origin["limitations"])
        )

    def test_a_modified_superseded_payload_fails_verification(self):
        tampered = copy.deepcopy(self.sample)
        tampered["run"]["mode"] = "live"
        with self.assertRaises(ExportVerificationError) as caught:
            verify_export_bundle(tampered)
        self.assertEqual(caught.exception.code, "checksum")


class ProviderBackedExportTests(TestCase):
    """5C: a provider-backed record keeps its source and its limitations."""

    def _export(self, *, provider_backed: bool) -> dict:
        user = make_user(f"origin-{provider_backed}")
        portfolio = make_portfolio(user)
        run = create_export_sample_run(portfolio, provider_backed=provider_backed)
        response = auth_client(user).get(reverse("core:decision-run-export", args=[str(run.id)]))
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def test_the_two_exports_do_not_share_a_provenance_claim(self):
        authored = self._export(provider_backed=False)
        provider = self._export(provider_backed=True)

        authored_origin = authored["run"]["origin"]
        provider_origin = provider["run"]["origin"]
        self.assertEqual(authored_origin["kind"], "authored_fixture")
        self.assertEqual(provider_origin["kind"], "provider_run")
        self.assertIs(authored_origin["provider_backed"], False)
        self.assertIs(provider_origin["provider_backed"], True)
        self.assertEqual(provider_origin["provider"], "anthropic")
        self.assertEqual(provider_origin["model"], "claude-haiku-5-5")
        self.assertEqual(provider_origin["run_record"], "provider_run_record")

        self.assertIs(
            authored["provenance_claims"]["run_origin"]["establishes_provider_debate"], False
        )
        self.assertIs(
            provider["provenance_claims"]["run_origin"]["establishes_provider_debate"], True
        )
        # Both still describe the same build-verification claim; origin is
        # independent of whether that build record exists.
        for bundle in (authored, provider):
            self.assertEqual(bundle["recording"]["scope"], "build_verification_not_run_origin")

    def test_the_provider_record_limitations_are_preserved(self):
        provider = self._export(provider_backed=True)
        limitations = provider["run"]["origin"]["limitations"]
        self.assertTrue(any("provider 'anthropic'" in text for text in limitations))
        self.assertTrue(any("price-table estimate is not a bill" in text for text in limitations))
        self.assertTrue(any("Test data" in text for text in limitations))

        claims = provider["provenance_claims"]
        self.assertIs(claims["build_verification"]["establishes_provider_debate"], False)
        self.assertIs(claims["integrity"]["establishes_provider_debate"], False)

    def test_the_committed_provider_sample_matches_a_fresh_endpoint_export(self):
        # The committed file is captured through the endpoint, not assembled by
        # hand. Its origin block is therefore a producer claim, not a viewer one.
        committed = load_json(PROVIDER_SAMPLE)
        self.assertIs(verify_export_bundle(committed), committed)
        self.assertEqual(committed["run"]["origin"]["kind"], "provider_run")
        self.assertIs(committed["run"]["origin"]["provider_backed"], True)
        # It is test data for the Step 11 path; the origin says both facts.
        self.assertTrue(
            any("Test data" in text for text in committed["run"]["origin"]["limitations"])
        )
