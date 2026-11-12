"""P1 remediation step 10: verify the application before recording.

Reproduced before the repair:

* there was no single command that ran the backend suite, the frontend suite,
  the type check, build, lint, migration check and a live smoke test, so a
  reviewer had to assemble the list from three documents;
* no acceptance record tied a verdict to a commit, so a recorded artefact could
  silently drift from the code that produced it;
* ``record_live_allocation`` would happily record from a dirty working tree -
  the recording could not be reproduced from any commit;
* nothing checked that a committed recording named a commit that passed.

These tests exercise the verifier as a program (subprocess stages, not mocks of
subprocess), the commit-keyed acceptance record, a real temporary git repository
for the dirty-tree refusal, and the artefact/record pairing check.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from scripts.verify_application import (
    GitState,
    RecordingRefused,
    Stage,
    build_result,
    main,
    run_command_stage,
    run_verification,
    write_acceptance_record,
)
from services.recording_provenance import (
    RECORDED_ARTIFACTS,
    check_recording_pairing,
    load_acceptance_record,
    require_clean_for_recording,
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def py_stage(stage_id: str, code: str, *, parser: str = "") -> Stage:
    return Stage(
        id=stage_id,
        title=f"test stage {stage_id}",
        command=(sys.executable, "-c", code),
        parser=parser,
        timeout_seconds=30,
    )


def stage_result(stage: Stage, status: str = "pass", exit_code: int = 0):
    return run_command_stage(stage, Path.cwd()) if status == "pass" else _failed(stage)


def _failed(stage: Stage):
    return run_command_stage(py_stage(stage.id, "import sys; sys.exit(7)"), Path.cwd())


class VerifierStageTests(SimpleTestCase):
    """10A: the command fails a failing stage and passes a passing one."""

    def test_a_passing_stage_reports_its_exit_and_count(self):
        result = run_verification(
            [py_stage("backend-tests", "print('Ran 7 tests in 0.01s')", parser="unittest")],
            Path.cwd(),
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.stages[0].status, "pass")
        self.assertEqual(result.stages[0].counts, {"tests": 7})
        self.assertEqual(result.summary()["counts"]["backend_tests"], 7)
        self.assertTrue(result.output_digest)

    def test_a_failing_stage_exits_non_zero_with_the_stage_named(self):
        result = run_verification(
            [py_stage("frontend-tests", "import sys; sys.exit(3)")],
            Path.cwd(),
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.stages[0].id, "frontend-tests")
        self.assertEqual(result.stages[0].status, "fail")
        self.assertEqual(result.stages[0].exit_code, 3)
        self.assertIn("exit", result.stages[0].detail.lower())

    def test_an_injected_failure_makes_the_verdict_non_zero(self):
        result = run_verification(
            [py_stage("lint", "print('this would pass')")],
            Path.cwd(),
            inject_failure=["lint"],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stages[0].status, "fail")
        self.assertIn("Injected failure", result.stages[0].detail)

    def test_the_cli_returns_non_zero_and_prints_a_machine_readable_summary(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = main(["--only", "lint", "--inject-failure", "lint"])
        self.assertEqual(return_code, 1)
        output = stdout.getvalue()
        self.assertIn("VERIFY_STAGE lint=fail", output)
        self.assertIn("VERIFY_SUMMARY ", output)
        summary = json.loads(output.split("VERIFY_SUMMARY ", 1)[1].strip())
        self.assertEqual(summary["verdict"], "fail")
        self.assertEqual(summary["exit_code"], 1)
        self.assertEqual(summary["stages"][0]["id"], "lint")

    def test_a_skipped_required_stage_is_not_a_pass(self):
        result = run_verification(
            [
                py_stage("lint", "print('ok')"),
                py_stage("smoke", "print('ok')"),
            ],
            Path.cwd(),
            skip=["smoke"],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual([stage.id for stage in result.stages], ["lint", "smoke"])
        self.assertEqual(result.stages[1].status, "skipped")

        # Selecting nothing is a configuration error, not a silent pass.
        with self.assertRaises(ValueError):
            run_verification([py_stage("lint", "print('ok')")], Path.cwd(), skip=["lint"])


class AcceptanceRecordTests(SimpleTestCase):
    """10B: the record names the commit, stages, counts and a stable digest."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1-10b-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.git = GitState(commit=COMMIT_A, subject="test commit", dirty=False, dirty_paths=[])

    def _result(self, *, duration: float = 0.0):
        stages = [
            run_command_stage(
                py_stage("backend-tests", "print('Ran 928 tests in 0.01s')", parser="unittest"),
                Path.cwd(),
            ),
            run_command_stage(
                py_stage("frontend-tests", "print('Tests  275 passed (275)')", parser="vitest"),
                Path.cwd(),
            ),
        ]
        for stage in stages:
            stage.duration_seconds = duration
        return build_result(stages, git=self.git)

    def test_the_record_contains_the_commit_stages_counts_and_digest(self):
        result = self._result()
        path, record = write_acceptance_record(result, self.tmp, directory=self.tmp / "acceptance")
        self.assertTrue(path.exists())
        self.assertEqual(path.name, f"{COMMIT_A}.json")
        self.assertEqual(record["commit"], COMMIT_A)
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["counts"], {"backend_tests": 928, "frontend_tests": 275})
        self.assertEqual(
            [stage["id"] for stage in record["stages"]], ["backend-tests", "frontend-tests"]
        )
        self.assertEqual(record["output_digest"], result.output_digest)
        self.assertTrue(record["tree_clean"])
        self.assertTrue(record["usable_for_recording"])
        self.assertEqual(record["schema_version"], "application.acceptance.v1")

        loaded = load_acceptance_record(COMMIT_A, directory=self.tmp / "acceptance")
        self.assertEqual(loaded, record)

    def test_re_running_the_same_commit_reproduces_the_verdict_and_digest(self):
        first = self._result(duration=0.1)
        second = self._result(duration=9.9)
        self.assertEqual(first.verdict, second.verdict)
        self.assertEqual(first.output_digest, second.output_digest)

        _path, record_a = write_acceptance_record(first, self.tmp, directory=self.tmp / "a")
        _path, record_b = write_acceptance_record(second, self.tmp, directory=self.tmp / "b")
        self.assertEqual(record_a["output_digest"], record_b["output_digest"])
        self.assertEqual(record_a["stages"], record_b["stages"])

    def test_a_failed_verdict_is_not_written_as_an_acceptance(self):
        stages = [run_command_stage(py_stage("lint", "import sys; sys.exit(4)"), Path.cwd())]
        result = build_result(stages, git=self.git)
        with self.assertRaises(RecordingRefused) as caught:
            write_acceptance_record(result, self.tmp, directory=self.tmp / "acceptance")
        self.assertEqual(caught.exception.code, "verification_did_not_pass")

    def test_a_dirty_tree_is_refused_by_default_and_labelled_when_forced(self):
        dirty = GitState(
            commit=COMMIT_A,
            subject="test commit",
            dirty=True,
            dirty_paths=["services/budget.py"],
        )
        result = build_result(
            [run_command_stage(py_stage("lint", "print('ok')"), Path.cwd())],
            git=dirty,
        )
        with self.assertRaises(RecordingRefused) as caught:
            write_acceptance_record(result, self.tmp, directory=self.tmp / "clean")
        self.assertEqual(caught.exception.code, "dirty_working_tree")

        path, record = write_acceptance_record(
            result,
            self.tmp,
            directory=self.tmp / "dirty",
            allow_dirty=True,
        )
        self.assertTrue(path.exists())
        self.assertFalse(record["tree_clean"])
        self.assertFalse(record["usable_for_recording"])
        self.assertEqual(record["dirty_paths"], ["services/budget.py"])


class RecordingGuardTests(TestCase):
    """10C: dirty trees are refused; a clean, accepted tree is allowed."""

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

    def _record(self, root: Path, commit: str, *, digest: str = "d" * 64) -> None:
        directory = root / ".runtime" / "acceptance"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{commit}.json").write_text(
            json.dumps(
                {
                    "schema_version": "application.acceptance.v1",
                    "commit": commit,
                    "verdict": "pass",
                    "tree_clean": True,
                    "usable_for_recording": True,
                    "output_digest": digest,
                }
            ),
            encoding="utf-8",
        )

    def test_a_clean_tree_with_a_passing_record_is_allowed(self):
        root = Path(tempfile.mkdtemp(prefix="p1-10c-clean-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        commit = self._repo(root)
        self._record(root, commit)
        provenance = require_clean_for_recording(root=root)
        self.assertEqual(provenance["commit"], commit)
        self.assertTrue(provenance["verified"])
        self.assertTrue(provenance["usable_for_recording"])
        self.assertEqual(provenance["acceptance_digest"], "d" * 64)

    def test_an_uncommitted_change_is_refused_before_recording(self):
        root = Path(tempfile.mkdtemp(prefix="p1-10c-dirty-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        commit = self._repo(root)
        self._record(root, commit)
        (root / "app.py").write_text("print('two')\n", encoding="utf-8")
        with self.assertRaises(RecordingRefused) as caught:
            require_clean_for_recording(root=root)
        self.assertEqual(caught.exception.code, "dirty_working_tree")

    def test_a_clean_tree_without_an_acceptance_record_is_refused(self):
        root = Path(tempfile.mkdtemp(prefix="p1-10c-missing-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self._repo(root)
        with self.assertRaises(RecordingRefused) as caught:
            require_clean_for_recording(root=root)
        self.assertEqual(caught.exception.code, "acceptance_record_missing")


class RecordingPairingTests(TestCase):
    """10D: an artefact that names another commit/digest fails the pairing check."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p1-10d-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.directory = self.root / "docs" / "spec" / "acceptance"
        self.directory.mkdir(parents=True)
        (self.directory / f"{COMMIT_A}.json").write_text(
            json.dumps(
                {
                    "schema_version": "application.acceptance.v1",
                    "commit": COMMIT_A,
                    "verdict": "pass",
                    "tree_clean": True,
                    "output_digest": "1" * 64,
                }
            ),
            encoding="utf-8",
        )
        self.artifact = self.root / "demo.json"
        self._write_artifact(COMMIT_A, "1" * 64)

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
        return check_recording_pairing(
            root=self.root,
            artifacts=[("demo", "demo.json")],
        )

    def test_a_matching_artefact_passes(self):
        result = self._check()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["artifacts"][0]["code"], "paired")

    def test_a_different_commit_fails(self):
        self._write_artifact(COMMIT_B, "1" * 64)
        result = self._check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["artifacts"][0]["code"], "acceptance_record_missing")

    def test_a_different_digest_fails(self):
        self._write_artifact(COMMIT_A, "2" * 64)
        result = self._check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["artifacts"][0]["code"], "acceptance_digest_mismatch")

    def test_a_missing_provenance_block_fails(self):
        self.artifact.write_text(json.dumps({"schema_version": "demo.record.v1"}), encoding="utf-8")
        result = self._check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["artifacts"][0]["code"], "recording_provenance_missing")


class CommittedRecordingPairingTests(TestCase):
    """The repository's committed recordings pair with their acceptance record."""

    def test_every_committed_recording_names_a_passing_record(self):
        result = check_recording_pairing()
        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["checked"], len(RECORDED_ARTIFACTS))

    def test_the_export_bundle_carries_a_provenance_block(self):
        from pathlib import Path as _Path

        from django.conf import settings

        path = _Path(settings.BASE_DIR) / "docs/spec/fixtures/decision-run-export.v1.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("recording", document)
        block = document["recording"]
        for field in (
            "commit",
            "acceptance_digest",
            "acceptance_record",
            "status",
            "verified",
            "usable_for_recording",
        ):
            self.assertIn(field, block)
