"""P1 remediation step 10: the release gate is not a developer run.

The first round added the verifier and a commit-keyed acceptance record, but
``usable_for_recording`` was set from ``tree_clean and verdict == "pass"``. A
``--only`` run that omitted six stages, or a four-stage run with
``frontend_tests: 0``, therefore produced a record that claimed to be usable.
The committed four-stage record for the steps 9-10 commit is exactly that
defect: it says ``usable_for_recording: true`` and names a pre-merge commit.

These tests separate two questions that the first round conflated:

* did the stages that ran pass? (the developer verdict), and
* did each of the eight declared stages run exactly once and pass? (the release
  gate, which alone makes a record usable).

10A proves every non-release shape is unusable; 10B proves a full record names
its commit, stages, counts and digest and reproduces them; 10C proves an
artefact cannot pair with a different commit or an incomplete stage set.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from core.tests.test_p1_acceptance_verification import (
    all_passing_stage_results,
    eight_stage_records,
)
from scripts.verify_application import (
    REQUIRED_STAGE_IDS,
    GitState,
    RecordingRefused,
    StageResult,
    acceptance_record_coverage,
    acceptance_record_digest_valid,
    acceptance_record_usable,
    build_result,
    canonical_digest,
    write_acceptance_record,
)
from services.recording_provenance import (
    check_recording_pairing,
    require_clean_for_recording,
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
CLEAN_GIT = GitState(commit=COMMIT_A, subject="release commit", dirty=False, dirty_paths=[])


def passing_stage(stage_id: str, *, tests: int = 0) -> StageResult:
    return StageResult(
        id=stage_id,
        title=f"test stage {stage_id}",
        status="pass",
        exit_code=0,
        counts={"tests": tests} if tests else {},
    )


def full_pass() -> list[StageResult]:
    return [passing_stage(stage_id) for stage_id in REQUIRED_STAGE_IDS]


def record_for(commit: str, *, stages: list[dict] | None = None) -> dict:
    stages = stages if stages is not None else eight_stage_records()
    digest = canonical_digest(
        {
            "schema_version": "application.verification.v1",
            "commit": commit,
            "tree_clean": True,
            "verdict": "pass",
            "stages": stages,
        }
    )
    return {
        "schema_version": "application.acceptance.v1",
        "commit": commit,
        "subject": "release commit",
        "tree_clean": True,
        "verdict": "pass",
        "usable_for_recording": True,
        "output_digest": digest,
        "stages": stages,
    }


class DeveloperRunIsNotReleaseAcceptanceTests(SimpleTestCase):
    """10A: --only, missing, failed, duplicate and skipped are all unusable."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1-10a-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_clean_only_run_passes_its_stage_but_is_not_usable_for_recording(self):
        result = build_result(
            [passing_stage("lint")],
            git=CLEAN_GIT,
        )
        self.assertEqual(result.verdict, "pass")
        self.assertFalse(result.coverage["complete"])
        self.assertEqual(
            result.coverage["missing"],
            [stage_id for stage_id in REQUIRED_STAGE_IDS if stage_id != "lint"],
        )
        self.assertFalse(result.usable_for_recording)

        path, record = write_acceptance_record(result, self.tmp, directory=self.tmp / "record")
        self.assertTrue(path.exists())
        self.assertFalse(record["usable_for_recording"])
        self.assertFalse(record["release_acceptance"])
        self.assertFalse(record["coverage"]["complete"])
        self.assertFalse(acceptance_record_usable(record))

    def test_a_record_that_omits_a_required_stage_is_unusable(self):
        stages = eight_stage_records()
        omitted = stages.pop(3)
        self.assertEqual(omitted["id"], "build")
        record = record_for(COMMIT_A, stages=stages)
        self.assertEqual(acceptance_record_coverage(record)["missing"], ["build"])
        self.assertFalse(acceptance_record_usable(record))

    def test_a_failed_stage_record_is_unusable(self):
        stages = full_pass()
        stages[1] = StageResult(
            id="frontend-tests",
            title="frontend",
            status="fail",
            exit_code=1,
            detail="one failure",
        )
        result = build_result(stages, git=CLEAN_GIT)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.coverage["failed"], ["frontend-tests"])
        self.assertFalse(result.usable_for_recording)

        _path, record = write_acceptance_record(
            result,
            self.tmp,
            directory=self.tmp / "failed",
            allow_for_developer=True,
        )
        self.assertFalse(record["usable_for_recording"])
        self.assertFalse(acceptance_record_usable(record))

    def test_a_duplicate_stage_record_is_unusable(self):
        stages = full_pass()
        stages.append(passing_stage("lint"))
        result = build_result(stages, git=CLEAN_GIT)
        # The developer verdict can still pass; the release gate cannot.
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.coverage["duplicates"], ["lint"])
        self.assertFalse(result.usable_for_recording)

        _path, record = write_acceptance_record(result, self.tmp, directory=self.tmp / "duplicate")
        self.assertFalse(record["usable_for_recording"])

    def test_a_skipped_stage_record_is_unusable(self):
        stages = full_pass()
        stages[6] = StageResult(
            id="migration-check",
            title="migrations",
            status="skipped",
            detail="explicitly skipped",
        )
        result = build_result(stages, git=CLEAN_GIT)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.coverage["skipped"], ["migration-check"])
        self.assertFalse(result.usable_for_recording)

        _path, record = write_acceptance_record(
            result,
            self.tmp,
            directory=self.tmp / "skipped",
            allow_for_developer=True,
        )
        self.assertFalse(record["usable_for_recording"])


class FullReleaseRecordTests(SimpleTestCase):
    """10B: a full record names its commit, stages, counts and digest."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1-10b-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _result(self, *, duration: float = 0.0):
        stages = all_passing_stage_results(backend_tests=1007, frontend_tests=289)
        for stage in stages:
            stage.duration_seconds = duration
        return build_result(stages, git=CLEAN_GIT)

    def test_the_record_carries_the_commit_every_stage_the_counts_and_a_digest(self):
        result = self._result()
        self.assertTrue(result.usable_for_recording)
        path, record = write_acceptance_record(result, self.tmp, directory=self.tmp / "a")

        self.assertEqual(path.name, f"{COMMIT_A}.json")
        self.assertEqual(record["commit"], COMMIT_A)
        self.assertEqual(record["verdict"], "pass")
        self.assertTrue(record["tree_clean"])
        self.assertTrue(record["usable_for_recording"])
        self.assertEqual(record["counts"], {"backend_tests": 1007, "frontend_tests": 289})
        self.assertEqual([stage["id"] for stage in record["stages"]], list(REQUIRED_STAGE_IDS))
        self.assertTrue(record["coverage"]["complete"])
        self.assertEqual(record["stage_count"], 8)
        self.assertEqual(len(record["output_digest"]), 64)
        for stage in record["stages"]:
            self.assertIn("status", stage)
            self.assertIn("exit_code", stage)
            self.assertIn("counts", stage)
        self.assertTrue(acceptance_record_usable(record))
        self.assertTrue(acceptance_record_digest_valid(record))

    def test_re_running_the_same_commit_reproduces_the_verdict_and_digest(self):
        first = self._result(duration=0.1)
        second = self._result(duration=99.0)
        self.assertEqual(first.verdict, second.verdict)
        self.assertEqual(first.output_digest, second.output_digest)
        self.assertEqual(first.coverage, second.coverage)

        _p1, record_a = write_acceptance_record(first, self.tmp, directory=self.tmp / "a")
        _p2, record_b = write_acceptance_record(second, self.tmp, directory=self.tmp / "b")
        self.assertEqual(record_a["output_digest"], record_b["output_digest"])
        self.assertEqual(record_a["stages"], record_b["stages"])
        self.assertEqual(record_a["verdict"], record_b["verdict"])


class PairingRejectsTheWrongCommitOrAnIncompleteSetTests(SimpleTestCase):
    """10C: a different commit or an incomplete stage set fails the check."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p1-10c-pairing-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.directory = self.root / "docs" / "spec" / "acceptance"
        self.directory.mkdir(parents=True)
        self.artifact = self.root / "demo.json"

    def _write_artifact(self, commit: str, digest: str) -> None:
        self.artifact.write_text(
            json.dumps(
                {
                    "schema_version": "demo.record.v1",
                    "recording": {
                        "commit": commit,
                        "acceptance_digest": digest,
                        "status": "verified",
                        "verified": True,
                    },
                }
            ),
            encoding="utf-8",
        )

    def _check(self):
        return check_recording_pairing(root=self.root, artifacts=[("demo", "demo.json")])

    def test_a_full_record_for_the_named_commit_passes(self):
        record = record_for(COMMIT_A)
        (self.directory / f"{COMMIT_A}.json").write_text(json.dumps(record), encoding="utf-8")
        self._write_artifact(COMMIT_A, record["output_digest"])
        result = self._check()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["artifacts"][0]["code"], "paired")

    def test_a_record_for_a_different_commit_fails(self):
        record = record_for(COMMIT_B)
        (self.directory / f"{COMMIT_B}.json").write_text(json.dumps(record), encoding="utf-8")
        self._write_artifact(COMMIT_A, record["output_digest"])
        result = self._check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["artifacts"][0]["code"], "acceptance_record_missing")

    def test_an_incomplete_stage_set_fails_even_with_a_matching_digest(self):
        stages = eight_stage_records()
        stages = [stage for stage in stages if stage["id"] != "smoke"]
        record = record_for(COMMIT_A, stages=stages)
        (self.directory / f"{COMMIT_A}.json").write_text(json.dumps(record), encoding="utf-8")
        self._write_artifact(COMMIT_A, record["output_digest"])
        result = self._check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["artifacts"][0]["code"], "acceptance_stages_incomplete")
        self.assertIn("smoke", result["artifacts"][0]["detail"])

    def test_a_four_stage_record_is_not_usable_even_though_its_verdict_passes(self):
        four = {
            "schema_version": "application.acceptance.v1",
            "commit": COMMIT_A,
            "verdict": "pass",
            "tree_clean": True,
            "usable_for_recording": True,
            "counts": {"backend_tests": 955, "frontend_tests": 0},
            "stages": [
                {"id": stage_id, "status": "pass", "exit_code": 0, "counts": {}}
                for stage_id in ("backend-tests", "lint", "format", "migration-check")
            ],
        }
        four["output_digest"] = canonical_digest(
            {
                "schema_version": "application.verification.v1",
                "commit": COMMIT_A,
                "tree_clean": True,
                "verdict": "pass",
                "stages": four["stages"],
            }
        )
        self.assertFalse(acceptance_record_usable(four))
        self.assertEqual(
            acceptance_record_coverage(four)["missing"],
            [
                "frontend-tests",
                "typecheck",
                "build",
                "smoke",
            ],
        )

    def test_a_tampered_stage_list_fails_the_digest_check(self):
        record = record_for(COMMIT_A)
        (self.directory / f"{COMMIT_A}.json").write_text(json.dumps(record), encoding="utf-8")
        tampered = dict(record)
        tampered["stages"] = [{**stage, "status": "fail"} for stage in record["stages"]]
        (self.directory / f"{COMMIT_A}.json").write_text(json.dumps(tampered), encoding="utf-8")
        self._write_artifact(COMMIT_A, record["output_digest"])
        result = self._check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["artifacts"][0]["code"], "acceptance_record_digest_invalid")


class ReleaseGuardRefusesIncompleteRecordsTests(TestCase):
    """10C: the recording guard refuses an incomplete record on a clean tree."""

    def _git(self, root: Path, *args: str) -> str:
        completed = subprocess.run(  # noqa: S603 - fixed git argv
            ["git", *args],  # noqa: S607 - test helper resolves git from PATH
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def _repo(self, root: Path) -> str:
        root.mkdir(parents=True, exist_ok=True)
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "test@example.invalid")
        self._git(root, "config", "user.name", "Test")
        (root / "app.py").write_text("print('one')\n", encoding="utf-8")
        (root / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
        self._git(root, "add", "app.py", ".gitignore")
        self._git(root, "commit", "-q", "-m", "clean state")
        return self._git(root, "rev-parse", "HEAD")

    def test_a_clean_tree_with_an_incomplete_record_is_refused(self):
        root = Path(tempfile.mkdtemp(prefix="p1-10c-guard-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        commit = self._repo(root)
        record = record_for(commit)
        record["stages"] = [stage for stage in record["stages"] if stage["id"] != "frontend-tests"]
        record["output_digest"] = canonical_digest(
            {
                "schema_version": "application.verification.v1",
                "commit": commit,
                "tree_clean": True,
                "verdict": "pass",
                "stages": record["stages"],
            }
        )
        directory = root / ".runtime" / "acceptance"
        directory.mkdir(parents=True)
        (directory / f"{commit}.json").write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaises(RecordingRefused) as caught:
            require_clean_for_recording(root=root)
        self.assertEqual(caught.exception.code, "acceptance_record_not_usable")
        self.assertIn("frontend-tests", caught.exception.detail)

    def test_a_clean_tree_with_a_full_record_is_allowed(self):
        root = Path(tempfile.mkdtemp(prefix="p1-10c-guard-ok-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        commit = self._repo(root)
        record = record_for(commit)
        directory = root / ".runtime" / "acceptance"
        directory.mkdir(parents=True)
        (directory / f"{commit}.json").write_text(json.dumps(record), encoding="utf-8")
        provenance = require_clean_for_recording(root=root)
        self.assertTrue(provenance["verified"])
        self.assertEqual(provenance["acceptance_digest"], record["output_digest"])
