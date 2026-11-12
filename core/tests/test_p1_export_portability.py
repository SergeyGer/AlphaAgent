"""P1 remediation step 5: an exported run is portable.

Reproduced before the repair:

* a real export hashed its payload in Python and the viewer hashed the same
  document after JSON parsing, where ``90.0`` is the number ``90``. Every export
  with a float in a guard's numerical checks, a protocol parameter or an event
  payload failed the viewer's checksum and could not be imported;
* the importer rebuilt the run from a narrow list of fields, so the cost,
  failure, protocol, stage-state and execution blocks of a real export were
  dropped on the way in;
* an export whose outcome basis recorded ``reference_at`` as an ISO string
  raised ``AttributeError`` in `outcome_payload` and returned HTTP 500. That is
  a real-run export failure independent of the digest.

The shared vector fixture ``docs/spec/fixtures/canonical-json.v1.json`` is read
by this module and by ``frontend/viewer/exportParity.test.ts``. The committed
export sample is produced by the authenticated endpoint from
`core.tests.export_sample`; this module checks its digest and its shape, and the
Vitest suite imports the same bytes unchanged.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from core.decision_json import (
    CanonicalJSONError,
    ExportVerificationError,
    canonical_decimal_text,
    canonical_export_json,
    export_content_hash,
    verify_export_bundle,
)
from core.tests.export_sample import create_export_sample_run
from core.tests.helpers import auth_client, make_portfolio, make_user

FIXTURES = Path(settings.BASE_DIR) / "docs/spec/fixtures"
VECTORS_PATH = FIXTURES / "canonical-json.v1.json"
EXPORT_SAMPLE_PATH = FIXTURES / "decision-run-export.v1.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def float_event(bundle: dict) -> dict:
    """The stage event carrying the float/Unicode settings block."""
    for event in bundle["events"]:
        payload = event.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("settings"), dict):
            return event
    raise AssertionError("The sample has no event with a settings payload.")


def shape_of(value):
    """Recursive shape without leaf values: keys, array lengths and JSON types."""
    if isinstance(value, dict):
        return {key: shape_of(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [shape_of(item) for item in value]
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    return type(value).__name__


class SharedCanonicalVectorTests(TestCase):
    """The same file, the same bytes, in both runtimes."""

    def test_the_vector_fixture_declares_the_canonical_form(self):
        document = load_json(VECTORS_PATH)
        self.assertEqual(document["vector_version"], "1.0")
        self.assertTrue(document["vectors"])

    def test_every_shared_vector_matches_python(self):
        document = load_json(VECTORS_PATH)
        for vector in document["vectors"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(canonical_export_json(vector["value"]), vector["canonical"])
                self.assertEqual(export_content_hash(vector["value"]), vector["sha256"])

    def test_a_float_and_its_integer_spelling_hash_the_same(self):
        # 90.0 is a JSON number; the browser parses it to 90 and JSON.stringify
        # writes "90". Python must write the same text.
        self.assertEqual(canonical_export_json({"v": 90.0}), '{"v":90}')
        self.assertEqual(canonical_export_json({"v": 90}), '{"v":90}')

    def test_decimal_values_have_one_declared_form(self):
        self.assertEqual(canonical_decimal_text(Decimal("90.0")), "90")
        self.assertEqual(canonical_decimal_text(Decimal("300.00")), "300")
        self.assertEqual(canonical_decimal_text(Decimal("-12.50")), "-12.5")
        self.assertEqual(canonical_decimal_text(Decimal("0.00000000")), "0")
        self.assertEqual(
            canonical_export_json({"quantity": Decimal("90.0")}),
            '{"quantity":"90"}',
        )

    def test_unsupported_values_are_refused_instead_of_guessed(self):
        with self.assertRaises(CanonicalJSONError):
            canonical_export_json({"v": float("nan")})
        with self.assertRaises(CanonicalJSONError):
            canonical_export_json({"v": 2**60})


class ExportSampleVerificationTests(TestCase):
    """The captured endpoint document verifies, and tampering is detected."""

    def setUp(self):
        self.sample = load_json(EXPORT_SAMPLE_PATH)

    def test_the_sample_is_verified_from_the_stored_payload(self):
        self.assertIs(verify_export_bundle(self.sample), self.sample)
        self.assertEqual(self.sample["bundle_kind"], "alphaagent.decision-run")
        self.assertEqual(self.sample["bundle_version"], "1.0")
        self.assertEqual(self.sample["schema_version"], "1.0")
        self.assertEqual(self.sample["checksum"]["algorithm"], "sha256")
        self.assertIn("canonicalisation", self.sample["checksum"])

        # The digest describes the document as parsed from the file, not a
        # re-serialised object: recomputing it over the same payload matches.
        payload = copy.deepcopy(self.sample)
        declared = payload.pop("checksum")["value"]
        self.assertEqual(export_content_hash(payload), declared)

    def test_a_semantic_edit_anywhere_fails_verification(self):
        event_index = self.sample["events"].index(float_event(self.sample))
        edits = [
            (
                "event payload setting",
                ("events", event_index, "payload", "settings", "max_position_pct"),
                91.0,
            ),
            ("guard allowed quantity", ("guard_evaluations", 1, "allowed_quantity"), "5.00000000"),
            ("unicode rationale", ("verdict", "concise_rationale"), "A changed rationale."),
            ("run status", ("run", "status"), "failed"),
            (
                "superseded outcome method",
                ("superseded_outcomes", 0, "method_version"),
                "attribution.v0",
            ),
        ]
        for label, path, replacement in edits:
            tampered = copy.deepcopy(self.sample)
            target = tampered
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            with self.subTest(edit=label):
                with self.assertRaises(ExportVerificationError) as caught:
                    verify_export_bundle(tampered)
                self.assertEqual(caught.exception.code, "checksum")
                self.assertIn("Checksum mismatch", caught.exception.detail)

    def test_a_representation_change_is_not_a_semantic_change(self):
        # 90.0 and 90 are the same JSON number. The canonical form normalises
        # the spelling, so this edit still verifies - and a value change does not.
        same = copy.deepcopy(self.sample)
        event_index = same["events"].index(float_event(same))
        same["events"][event_index]["payload"]["settings"]["max_position_pct"] = 90
        verify_export_bundle(same)

    def test_an_unsupported_version_is_reported_by_name(self):
        for field, value in (("bundle_version", "2.0"), ("schema_version", "9.9")):
            tampered = copy.deepcopy(self.sample)
            tampered[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ExportVerificationError) as caught:
                    verify_export_bundle(tampered)
                self.assertEqual(caught.exception.code, "unsupported_version")
                self.assertIn(field, caught.exception.detail)
                self.assertIn(value, caught.exception.detail)

    def test_an_unsupported_kind_is_refused(self):
        tampered = copy.deepcopy(self.sample)
        tampered["bundle_kind"] = "alphaagent.offline-viewer"
        with self.assertRaises(ExportVerificationError) as caught:
            verify_export_bundle(tampered)
        self.assertEqual(caught.exception.code, "unsupported_kind")

    def test_the_sample_preserves_the_two_guard_case_and_superseded_outcome(self):
        guards = self.sample["guard_evaluations"]
        self.assertEqual(len(guards), 2)
        self.assertEqual(guards[0]["status"], "approved")
        self.assertEqual(guards[0]["requested_quantity"], "10.00000000")
        self.assertEqual(guards[0]["allowed_quantity"], "10.00000000")
        self.assertEqual(guards[1]["status"], "clamped")
        self.assertEqual(guards[1]["requested_quantity"], "10.00000000")
        self.assertEqual(guards[1]["allowed_quantity"], "4.00000000")
        self.assertEqual(self.sample["guard_comparison"]["id"], guards[1]["id"])
        self.assertEqual([row["id"] for row in self.sample["guards"]], [g["id"] for g in guards])

        superseded = self.sample["superseded_outcomes"]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["horizon_days"], 7)
        self.assertEqual(superseded[0]["method_version"], "attribution.v1")
        current = {row["horizon_days"]: row for row in self.sample["outcomes"]}
        self.assertEqual(current[7]["method_version"], "attribution.v2")
        self.assertEqual(current[1]["maturity"], "unavailable")
        self.assertIn("unavailable_reason", current[1])
        self.assertEqual(self.sample["outcome_schedule"]["pending"], True)

        # Evidence references survive as identifiers, and a non-redistributable
        # body is omitted with its length recorded rather than exported.
        evidence_ids = {row["id"] for row in self.sample["evidence"]}
        for claim in self.sample["cases"]["bull"] + self.sample["cases"]["bear"]:
            self.assertTrue(set(claim["evidence_ids"]).issubset(evidence_ids))
        omitted = [row for row in self.sample["evidence"] if row.get("excerpt_omitted")]
        self.assertEqual(len(omitted), 1)
        self.assertIsNone(omitted[0]["excerpt"])
        self.assertIsNone(omitted[0]["published_at"])
        self.assertGreater(omitted[0]["excerpt_chars"], 0)

    def test_the_sample_contains_the_float_and_unicode_settings(self):
        raw = EXPORT_SAMPLE_PATH.read_text(encoding="utf-8")
        self.assertIn("90.0", raw)
        self.assertIn("300.0", raw)
        self.assertIn("0.0025", raw)
        self.assertIn("ünïcode 你好", raw)
        payload = float_event(self.sample)["payload"]
        self.assertEqual(payload["settings"]["max_position_pct"], 90.0)
        self.assertEqual(payload["settings"]["timeout_seconds"], 300.0)
        self.assertEqual(payload["unicode"], "ünïcode 你好")
        self.assertIsNone(self.sample["evidence"][1]["published_at"])


class ExportEndpointShapeTests(TestCase):
    """The committed sample cannot drift from what the endpoint emits."""

    def setUp(self):
        self.user = make_user("export-shape")
        self.portfolio = make_portfolio(self.user)
        self.run = create_export_sample_run(self.portfolio)
        self.client = auth_client(self.user)
        self.response = self.client.get(
            reverse("core:decision-run-export", args=[str(self.run.id)])
        )

    def test_the_endpoint_returns_a_verifiable_document(self):
        self.assertEqual(self.response.status_code, 200, self.response.content[:400])
        bundle = self.response.json()
        self.assertIs(verify_export_bundle(bundle), bundle)
        self.assertEqual(bundle["schema_version"], "1.0")
        self.assertEqual(bundle["run"]["status"], "completed")
        self.assertEqual(len(bundle["guard_evaluations"]), 2)
        self.assertEqual(len(bundle["superseded_outcomes"]), 1)

    def test_the_endpoint_shape_matches_the_committed_sample(self):
        fresh = self.response.json()
        sample = load_json(EXPORT_SAMPLE_PATH)
        self.assertEqual(shape_of(fresh), shape_of(sample))

    def test_the_checksum_covers_the_document_the_client_received(self):
        # Re-parse the bytes exactly as a browser would, then verify. This is the
        # payload that is actually stored, not a rebuilt copy.
        received = json.loads(self.response.content.decode("utf-8"))
        declared = received["checksum"]["value"]
        payload = {key: value for key, value in received.items() if key != "checksum"}
        self.assertEqual(export_content_hash(payload), declared)
        verify_export_bundle(received)

        edited = copy.deepcopy(received)
        edited["run"]["ticker"] = "MSFT"
        with self.assertRaises(ExportVerificationError):
            verify_export_bundle(edited)

    def test_the_synthetic_fixtures_still_load_under_the_supported_format(self):
        # The export change is additive: it did not alter the fixture manifest
        # loader or the committed synthetic fixtures.
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("load_decision_fixtures", stdout=out)
        self.assertIn("3 created, 0 skipped", out.getvalue())
