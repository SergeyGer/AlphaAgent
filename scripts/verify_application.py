#!/usr/bin/env python
"""Verify the whole application before an acceptance record is written.

One command runs every check a reviewer needs:

    venv/bin/python scripts/verify_application.py

The stages are, in order: backend tests, frontend tests, the frontend type
check and production build, Ruff lint and format, the migration check, and an
HTTP smoke test against a running stack. Each stage runs in its own subprocess
and is sequential: two overlapping ``manage.py test`` processes share the test
database, so running them in parallel is not safe.

The command prints one compact line per stage and a final machine-readable
``VERIFY_SUMMARY`` line. It exits non-zero if any required stage fails, if a
required stage was skipped, or if the acceptance record cannot be written.

Acceptance records
------------------
``--record`` writes ``docs/spec/acceptance/<commit>.json``. It contains the
commit hash, the stage results, the parsed test counts, a deterministic
``output_digest`` over the stage results, and the UTC timestamp. The digest
deliberately excludes durations, timestamps and host paths, so re-running on
the same commit with the same stage outcomes produces the same digest.

A dirty working tree is refused by default because an artefact recorded from a
tree that is not the commit cannot be reproduced. ``--allow-dirty`` is an
explicit escape hatch for a developer's own evidence; the record says
``tree_clean: false`` and ``usable_for_recording: false``, and the recording
commands in ``services.recording_provenance`` refuse it.

Usage examples:

    # The full verdict, with an acceptance record for a clean tree.
    venv/bin/python scripts/verify_application.py --record

    # A subset while iterating (the verdict is still non-zero if it fails).
    venv/bin/python scripts/verify_application.py --only lint --only smoke

    # A fault-injection check that the command really fails a stage.
    venv/bin/python scripts/verify_application.py --only lint --inject-failure lint

The running-stack smoke test reads ``ALPHA_VERIFY_BASE_URL`` (default
``http://127.0.0.1:8080/healthz/``). Start the stack with
``docker compose up -d`` before the full run, or use ``--base-url``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

SCHEMA_VERSION = "application.verification.v1"
ACCEPTANCE_SCHEMA_VERSION = "application.acceptance.v1"
VERIFIER_VERSION = "1.1"
DEFAULT_SMOKE_URL = "http://127.0.0.1:8080/healthz/"

#: The eight stages release acceptance requires, each exactly once with a
#: passing result. ``--only`` and ``--skip`` are developer conveniences and
#: cannot satisfy this set; a record missing, duplicating, failing or skipping
#: any of them is not usable for recording.
REQUIRED_STAGE_IDS: tuple[str, ...] = (
    "backend-tests",
    "frontend-tests",
    "typecheck",
    "build",
    "lint",
    "format",
    "migration-check",
    "smoke",
)

#: Exit code when a required stage fails or is skipped.
EXIT_FAILED = 1
#: Exit code when the record could not be written (dirty tree, failed verdict).
EXIT_REFUSED = 2


@dataclass(frozen=True)
class Stage:
    """One check the verification command runs."""

    id: str
    title: str
    command: tuple[str, ...] = ()
    cwd: str = "."
    kind: str = "command"  # command | smoke
    url: str = ""
    parser: str = ""  # unittest | vitest
    timeout_seconds: int = 1800

    def command_text(self) -> str:
        if self.kind == "smoke":
            return f"HTTP GET {self.url}"
        return " ".join(self.command)


@dataclass
class StageResult:
    """The outcome of one stage, with the parsed counts kept separate."""

    id: str
    title: str
    status: str  # pass | fail | skipped
    exit_code: int | None = None
    duration_seconds: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    detail: str = ""
    command: str = ""

    def summary(self) -> dict:
        """The deterministic part of the result, used for the digest."""
        return {
            "id": self.id,
            "status": self.status,
            "exit_code": self.exit_code,
            "counts": dict(sorted(self.counts.items())),
        }

    def readable(self) -> dict:
        return {
            **self.summary(),
            "title": self.title,
            "duration_seconds": round(self.duration_seconds, 2),
            "command": self.command,
            "detail": self.detail,
        }


def _stage_field(stage, name: str, default=None):
    if isinstance(stage, dict):
        return stage.get(name, default)
    return getattr(stage, name, default)


def stage_coverage(stages, required: tuple[str, ...] = REQUIRED_STAGE_IDS) -> dict:
    """Summarise whether the eight release stages ran exactly once and passed.

    ``--only`` is a developer convenience, not a release acceptance: a run that
    selects a subset is a passing run of that subset, but the record must not be
    usable for recording. The same is true of a skipped, failed, duplicated or
    unexpected stage. ``complete`` is the single boolean a caller should trust;
    the lists name every reason it is false.
    """
    ids = [str(_stage_field(stage, "id", "")) for stage in stages]
    counts: dict[str, int] = {}
    for stage_id in ids:
        counts[stage_id] = counts.get(stage_id, 0) + 1
    by_id: dict[str, list] = {}
    for stage in stages:
        by_id.setdefault(str(_stage_field(stage, "id", "")), []).append(stage)

    missing = [stage_id for stage_id in required if counts.get(stage_id, 0) == 0]
    duplicates = sorted(stage_id for stage_id, count in counts.items() if count > 1)
    unexpected = sorted(stage_id for stage_id in counts if stage_id not in required)
    failed = sorted(
        {
            str(_stage_field(stage, "id", ""))
            for stage in stages
            if _stage_field(stage, "status") == "fail"
        }
    )
    skipped = sorted(
        {
            str(_stage_field(stage, "id", ""))
            for stage in stages
            if _stage_field(stage, "status") == "skipped"
        }
    )
    passing_once = [
        stage_id
        for stage_id in required
        if counts.get(stage_id, 0) == 1 and _stage_field(by_id[stage_id][0], "status") == "pass"
    ]
    complete = (
        not missing
        and not duplicates
        and not unexpected
        and not failed
        and not skipped
        and len(passing_once) == len(required)
    )
    return {
        "required": list(required),
        "present": list(dict.fromkeys(ids)),
        "missing": missing,
        "duplicates": duplicates,
        "unexpected": unexpected,
        "failed": failed,
        "skipped": skipped,
        "passing_once": passing_once,
        "complete": complete,
        "stage_count": len(ids),
        "required_count": len(required),
    }


@dataclass
class VerificationResult:
    """The outcome of a whole run and the deterministic digest over it."""

    stages: list[StageResult]
    commit: str
    subject: str
    tree_clean: bool
    dirty_paths: list[str]
    verdict: str
    output_digest: str
    recorded_at: str
    exit_code: int
    required_stage_ids: tuple[str, ...] = REQUIRED_STAGE_IDS
    coverage: dict = field(default_factory=dict)
    usable_for_recording: bool = False

    def deterministic_payload(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "commit": self.commit or "unknown",
            "tree_clean": self.tree_clean,
            "verdict": self.verdict,
            "stages": [stage.summary() for stage in self.stages],
        }

    def summary(self) -> dict:
        counts = {
            "backend_tests": next(
                (
                    stage.counts.get("tests", 0)
                    for stage in self.stages
                    if stage.id == "backend-tests"
                ),
                0,
            ),
            "frontend_tests": next(
                (
                    stage.counts.get("tests", 0)
                    for stage in self.stages
                    if stage.id == "frontend-tests"
                ),
                0,
            ),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "commit": self.commit or "unknown",
            "subject": self.subject,
            "tree_clean": self.tree_clean,
            "dirty_paths": self.dirty_paths,
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "output_digest": self.output_digest,
            "recorded_at": self.recorded_at,
            "counts": counts,
            "required_stages": list(self.required_stage_ids),
            "coverage": self.coverage,
            "usable_for_recording": self.usable_for_recording,
            "stages": [stage.readable() for stage in self.stages],
        }


# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------
def _executable(root: Path, name: str) -> str:
    local = root / "venv" / "bin" / name
    if local.exists():
        return str(local)
    return shutil.which(name) or name


def default_stages(root: Path | None = None, *, smoke_url: str = DEFAULT_SMOKE_URL) -> list[Stage]:
    """The full check, in the order the acceptance criterion names it."""
    root = Path(root or Path(__file__).resolve().parent.parent)
    python = sys.executable
    npm = _executable(root, "npm")
    npx = _executable(root, "npx")
    ruff = _executable(root, "ruff")
    return [
        Stage(
            "backend-tests",
            "Backend tests (sequential, --noinput)",
            (python, "manage.py", "test", "core.tests", "--noinput"),
            parser="unittest",
            timeout_seconds=2400,
        ),
        Stage(
            "frontend-tests",
            "Frontend tests (Vitest, single run)",
            (npm, "test", "--", "--run"),
            cwd="frontend",
            parser="vitest",
            timeout_seconds=1200,
        ),
        Stage(
            "typecheck",
            "Frontend type check",
            (npx, "tsc", "--noEmit"),
            cwd="frontend",
            timeout_seconds=600,
        ),
        Stage(
            "build",
            "Frontend production build",
            (npm, "run", "build"),
            cwd="frontend",
            timeout_seconds=1200,
        ),
        Stage(
            "lint",
            "Ruff lint",
            (ruff, "check", ".", "--line-length", "100"),
            timeout_seconds=600,
        ),
        Stage(
            "format",
            "Ruff format check",
            (ruff, "format", "--check", "."),
            timeout_seconds=600,
        ),
        Stage(
            "migration-check",
            "No uncommitted model migrations",
            (python, "manage.py", "makemigrations", "--check", "--dry-run"),
            timeout_seconds=300,
        ),
        Stage(
            "smoke",
            "HTTP smoke test against the running stack",
            kind="smoke",
            url=smoke_url,
            timeout_seconds=30,
        ),
    ]


# ---------------------------------------------------------------------------
# Running stages
# ---------------------------------------------------------------------------
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_VITEST_TESTS = re.compile(r"\bTests\s+(\d+)\s+passed\b")
_VITEST_TESTS_ALT = re.compile(r"\b(\d+)\s+passed\b")


def parse_counts(parser: str, output: str) -> dict[str, int]:
    """Parse the test count without treating a parse failure as a pass."""
    if parser == "unittest":
        match = _UNITTEST_RAN.search(output)
        return {"tests": int(match.group(1))} if match else {}
    if parser == "vitest":
        match = _VITEST_TESTS.search(output) or _VITEST_TESTS_ALT.search(output)
        return {"tests": int(match.group(1))} if match else {}
    return {}


def _tail(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def run_command_stage(
    stage: Stage,
    root: Path,
    *,
    timeout_seconds: int | None = None,
) -> StageResult:
    """Run one subprocess stage and capture its exit status and counts."""
    started = time.monotonic()
    cwd = (root / stage.cwd).resolve()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(stage.command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds or stage.timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        return StageResult(
            id=stage.id,
            title=stage.title,
            status="fail",
            exit_code=127,
            duration_seconds=time.monotonic() - started,
            detail=f"Command not found: {type(exc).__name__}: {exc}",
            command=stage.command_text(),
        )
    except subprocess.TimeoutExpired:
        return StageResult(
            id=stage.id,
            title=stage.title,
            status="fail",
            exit_code=124,
            duration_seconds=time.monotonic() - started,
            detail=f"Timed out after {timeout_seconds or stage.timeout_seconds}s.",
            command=stage.command_text(),
        )

    output = f"{completed.stdout}\n{completed.stderr}"
    counts = parse_counts(stage.parser, output)
    status = "pass" if completed.returncode == 0 else "fail"
    detail = ""
    if status == "fail":
        detail = _tail(output) or f"Command exited with status {completed.returncode}."
    return StageResult(
        id=stage.id,
        title=stage.title,
        status=status,
        exit_code=completed.returncode,
        duration_seconds=time.monotonic() - started,
        counts=counts,
        detail=detail,
        command=stage.command_text(),
    )


def run_smoke_stage(stage: Stage, *, timeout_seconds: int | None = None) -> StageResult:
    """GET the health endpoint and accept only a healthy JSON response."""
    started = time.monotonic()
    parsed = urlparse(stage.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return StageResult(
            id=stage.id,
            title=stage.title,
            status="fail",
            exit_code=2,
            duration_seconds=time.monotonic() - started,
            detail="Smoke URL must be an http(s) URL.",
            command=stage.command_text(),
        )
    try:
        with urlopen(stage.url, timeout=timeout_seconds or stage.timeout_seconds) as response:  # noqa: S310
            body = response.read(4096).decode("utf-8", errors="replace")
            http_status = int(response.status)
    except HTTPError as exc:
        return StageResult(
            id=stage.id,
            title=stage.title,
            status="fail",
            exit_code=1,
            duration_seconds=time.monotonic() - started,
            detail=f"HTTP {exc.code} from {stage.url}",
            command=stage.command_text(),
        )
    except URLError as exc:
        return StageResult(
            id=stage.id,
            title=stage.title,
            status="fail",
            exit_code=1,
            duration_seconds=time.monotonic() - started,
            detail=f"Could not reach {stage.url}: {exc.reason}",
            command=stage.command_text(),
        )
    except OSError as exc:
        return StageResult(
            id=stage.id,
            title=stage.title,
            status="fail",
            exit_code=1,
            duration_seconds=time.monotonic() - started,
            detail=f"Smoke request failed: {type(exc).__name__}: {exc}",
            command=stage.command_text(),
        )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    healthy = http_status == 200 and payload.get("status") == "ok"
    return StageResult(
        id=stage.id,
        title=stage.title,
        status="pass" if healthy else "fail",
        exit_code=0 if healthy else 1,
        duration_seconds=time.monotonic() - started,
        counts={"http_status": http_status},
        detail="" if healthy else _tail(body),
        command=stage.command_text(),
    )


def _injected_failure(stage: Stage) -> StageResult:
    return StageResult(
        id=stage.id,
        title=stage.title,
        status="fail",
        exit_code=1,
        duration_seconds=0.0,
        detail=(
            "Injected failure for acceptance criterion 10A: this stage was configured to "
            "fail, so the verification command must exit non-zero."
        ),
        command=stage.command_text(),
    )


def canonical_digest(payload: dict) -> str:
    """SHA-256 over sorted, compact UTF-8 JSON."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Git state and acceptance records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GitState:
    commit: str
    subject: str
    dirty: bool
    dirty_paths: list[str]
    available: bool = True


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", *args],  # noqa: S607 - resolved via PATH, argument list is fixed
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return completed.stdout.rstrip("\n") if completed.returncode == 0 else ""


def git_state(root: Path) -> GitState:
    """HEAD, subject, and whether tracked or untracked files are modified."""
    root = Path(root)
    commit = _git(root, "rev-parse", "HEAD")
    if not commit:
        return GitState(
            commit="",
            subject="",
            dirty=True,
            dirty_paths=["git_unavailable_or_not_a_repository"],
            available=False,
        )
    subject = _git(root, "log", "-1", "--format=%s")
    porcelain = _git(root, "status", "--porcelain", "--untracked-files=normal")
    dirty_paths = [line[3:] if len(line) > 3 else line for line in porcelain.splitlines() if line]
    return GitState(
        commit=commit,
        subject=subject,
        dirty=bool(dirty_paths),
        dirty_paths=dirty_paths[:50],
        available=True,
    )


def acceptance_record_path(root: Path, commit: str, *, directory: Path | None = None) -> Path:
    base = Path(directory) if directory is not None else Path(root) / "docs/spec/acceptance"
    safe = re.sub(r"[^0-9a-fA-F]", "", commit) or "unknown"
    return base / f"{safe}.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def build_result(
    stage_results: list[StageResult],
    *,
    git: GitState,
    allow_skipped: bool = False,
    required_stage_ids: tuple[str, ...] = REQUIRED_STAGE_IDS,
) -> VerificationResult:
    """Compute the verdict, coverage and deterministic digest from stage results.

    ``verdict`` answers "did the stages that ran pass?". It is deliberately not
    the release gate: a developer ``--only lint`` run passes its own stages.
    ``usable_for_recording`` additionally requires a clean tree and each of the
    eight required stages exactly once with a passing result.
    """
    required_failed = any(stage.status == "fail" for stage in stage_results)
    required_skipped = any(stage.status == "skipped" for stage in stage_results)
    verdict = "pass"
    if required_failed or (required_skipped and not allow_skipped):
        verdict = "fail"
    elif required_skipped:
        verdict = "incomplete"
    exit_code = 0 if verdict == "pass" else EXIT_FAILED
    coverage = stage_coverage(stage_results, required_stage_ids)
    provisional = VerificationResult(
        stages=stage_results,
        commit=git.commit,
        subject=git.subject,
        tree_clean=not git.dirty,
        dirty_paths=git.dirty_paths,
        verdict=verdict,
        output_digest="",
        recorded_at=_utc_now(),
        exit_code=exit_code,
        required_stage_ids=required_stage_ids,
        coverage=coverage,
        usable_for_recording=bool(coverage["complete"] and verdict == "pass" and not git.dirty),
    )
    provisional.output_digest = canonical_digest(provisional.deterministic_payload())
    return provisional


def run_verification(
    stages: list[Stage],
    root: Path,
    *,
    only: list[str] | None = None,
    skip: list[str] | None = None,
    inject_failure: list[str] | None = None,
    smoke_url: str | None = None,
    allow_skipped: bool = False,
    stage_runner: Callable[[Stage], StageResult] | None = None,
) -> VerificationResult:
    """Run the selected stages sequentially and return the verdict."""
    root = Path(root)
    only_ids = {item for item in (only or []) if item}
    skip_ids = {item for item in (skip or []) if item}
    injected = {item for item in (inject_failure or []) if item}
    selected: list[Stage] = []
    explicitly_skipped: list[Stage] = []
    for stage in stages:
        if only_ids and stage.id not in only_ids:
            continue
        if stage.id in skip_ids:
            explicitly_skipped.append(stage)
            continue
        if smoke_url and stage.kind == "smoke":
            stage = Stage(
                id=stage.id,
                title=stage.title,
                kind=stage.kind,
                url=smoke_url,
                parser=stage.parser,
                timeout_seconds=stage.timeout_seconds,
            )
        selected.append(stage)
    if not selected:
        raise ValueError("No stages selected: check --only/--skip.")

    results: list[StageResult] = [
        StageResult(
            id=stage.id,
            title=stage.title,
            status="skipped",
            detail="Explicitly skipped by --skip; a required skip is not a pass.",
            command=stage.command_text(),
        )
        for stage in explicitly_skipped
    ]
    for stage in selected:
        if stage.id in injected:
            results.append(_injected_failure(stage))
            continue
        if stage_runner is not None:
            results.append(stage_runner(stage))
            continue
        if stage.kind == "smoke":
            results.append(run_smoke_stage(stage))
        else:
            results.append(run_command_stage(stage, root))

    order = {stage.id: index for index, stage in enumerate(stages)}
    results.sort(key=lambda result: order.get(result.id, len(order)))
    git = git_state(root)
    return build_result(results, git=git, allow_skipped=allow_skipped)


def write_acceptance_record(
    result: VerificationResult,
    root: Path,
    *,
    directory: Path | None = None,
    allow_dirty: bool = False,
    allow_for_developer: bool = False,
    allow_overwrite: bool = False,
) -> tuple[Path, dict]:
    """Write the commit-keyed acceptance record, refusing an unusable tree.

    A record is written for a passing developer run (including ``--only``), but
    it is marked ``usable_for_recording: false`` unless each of the eight
    required stages ran exactly once and passed. ``allow_for_developer`` also
    writes the record for a failed or skipped run so the evidence exists; it
    can never make that record usable. An unusable record will not overwrite a
    published usable one unless ``allow_overwrite`` is explicit.
    """
    root = Path(root)
    if not result.commit:
        raise RecordingRefused("git_commit_unavailable", "The commit hash could not be read.")
    if result.verdict != "pass" and not allow_for_developer:
        raise RecordingRefused(
            "verification_did_not_pass",
            f"The verification verdict is {result.verdict!r}; a failed run is not an acceptance.",
        )
    if not result.tree_clean and not allow_dirty:
        raise RecordingRefused(
            "dirty_working_tree",
            (
                "The working tree has uncommitted changes, so the record could not be "
                "reproduced from the commit. Commit them or pass --allow-dirty for "
                "developer-only evidence (the record is then marked unusable for "
                "recording)."
            ),
        )
    path = acceptance_record_path(root, result.commit, directory=directory)
    if path.exists() and not result.usable_for_recording and not allow_overwrite:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if isinstance(existing, dict) and acceptance_record_usable(existing):
            raise RecordingRefused(
                "would_overwrite_passing_record",
                (
                    f"{path} is a usable acceptance record for this commit. Refusing to "
                    "replace it with a developer run; pass --allow-overwrite only if the "
                    "published record is known to be wrong."
                ),
            )
    record = {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "commit": result.commit,
        "subject": result.subject,
        "tree_clean": result.tree_clean,
        "dirty_paths": result.dirty_paths,
        "verdict": result.verdict,
        "output_digest": result.output_digest,
        "recorded_at": result.recorded_at,
        "verifier_version": VERIFIER_VERSION,
        "verifier": "scripts/verify_application.py",
        "command": "venv/bin/python scripts/verify_application.py --record",
        "counts": result.summary()["counts"],
        "required_stages": list(result.required_stage_ids),
        "coverage": result.coverage,
        "stage_count": len(result.stages),
        "stages": [
            {
                **stage.summary(),
                "title": stage.title,
                "command": stage.command,
            }
            for stage in result.stages
        ],
        # The release gate. A passing verdict for a subset is a developer run,
        # not evidence that the application passed the eight declared checks.
        "usable_for_recording": bool(result.usable_for_recording),
        "release_acceptance": bool(result.usable_for_recording),
        "digest_scope": (
            "sha256 of compact sorted-key JSON containing the schema version, commit, "
            "tree_clean, verdict and each stage's id/status/exit_code/counts. Durations, "
            "timestamps, host paths, command output and the derived coverage are excluded "
            "so the same commit reproduces the same digest."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path, record


def acceptance_record_coverage(record: dict) -> dict:
    """Coverage of an acceptance record, derived from its stored stage list."""
    stages = record.get("stages")
    if not isinstance(stages, list):
        stages = []
    return stage_coverage(stages)


def acceptance_record_usable(record: dict) -> bool:
    """True only for a clean, passing record covering all eight stages once."""
    if not isinstance(record, dict):
        return False
    if record.get("verdict") != "pass" or record.get("tree_clean") is not True:
        return False
    if record.get("usable_for_recording") is False:
        return False
    return bool(acceptance_record_coverage(record)["complete"])


def acceptance_record_digest_valid(record: dict) -> bool:
    """Recompute the digest over a record's stored stage summaries."""
    stages = record.get("stages")
    if not isinstance(stages, list):
        return False
    payload = {
        "schema_version": SCHEMA_VERSION,
        "commit": record.get("commit") or "unknown",
        "tree_clean": record.get("tree_clean"),
        "verdict": record.get("verdict"),
        "stages": [
            {
                "id": stage.get("id"),
                "status": stage.get("status"),
                "exit_code": stage.get("exit_code"),
                "counts": dict(sorted((stage.get("counts") or {}).items())),
            }
            for stage in stages
            if isinstance(stage, dict)
        ],
    }
    return canonical_digest(payload) == record.get("output_digest")


class RecordingRefused(Exception):
    """A recording path refused because its provenance is not honest."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_ids(values: list[str] | None) -> list[str]:
    parsed: list[str] = []
    for value in values or []:
        parsed.extend(item.strip() for item in str(value).split(",") if item.strip())
    return parsed


def _print_results(result: VerificationResult) -> None:
    for stage in result.stages:
        counts = " ".join(f"{key}={value}" for key, value in sorted(stage.counts.items()))
        print(
            f"VERIFY_STAGE {stage.id}={stage.status}"
            f" exit={stage.exit_code if stage.exit_code is not None else '-'}"
            f" {counts}".rstrip()
        )
    coverage = result.coverage
    if not coverage.get("complete"):
        reasons = []
        for key in ("missing", "duplicates", "unexpected", "failed", "skipped"):
            values = coverage.get(key) or []
            if values:
                reasons.append(f"{key}={','.join(values)}")
        print(
            "VERIFY_COVERAGE not_release_acceptance "
            + (" ".join(reasons) if reasons else "required_stages_not_exactly_once")
        )
    print("VERIFY_SUMMARY " + json.dumps(result.summary(), sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Run only these stage ids (repeatable/comma-separated).",
    )
    parser.add_argument("--skip", action="append", default=[], help="Skip these stage ids.")
    parser.add_argument(
        "--inject-failure",
        action="append",
        default=[],
        help="Force these stage ids to fail without running them (fault-injection check).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ALPHA_VERIFY_BASE_URL", DEFAULT_SMOKE_URL),
        help="Health URL for the running-stack smoke stage.",
    )
    parser.add_argument("--json-out", default="", help="Also write the full summary JSON here.")
    parser.add_argument("--record", action="store_true", help="Write the acceptance record.")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow --record on a dirty tree; the record is marked unusable for recording.",
    )
    parser.add_argument(
        "--allow-for-developer",
        action="store_true",
        help=(
            "Write the record even when a selected stage failed or was skipped. It is "
            "still marked unusable for recording; this exists so developer evidence is "
            "kept, not so a failed run can be published."
        ),
    )
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help=(
            "Allow an unusable developer record to replace an existing usable record for "
            "the same commit. Off by default."
        ),
    )
    parser.add_argument(
        "--allow-skipped",
        action="store_true",
        help="Treat explicitly skipped stages as an incomplete, not failed, run.",
    )
    parser.add_argument(
        "--record-dir",
        default="docs/spec/acceptance",
        help=(
            "Directory for the acceptance record (default docs/spec/acceptance). "
            "Use .runtime/acceptance for developer-only evidence that must not "
            "overwrite the published record."
        ),
    )
    parser.add_argument("--list-stages", action="store_true", help="Print stage ids and exit.")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    stages = default_stages(root, smoke_url=args.base_url)
    if args.list_stages:
        for stage in stages:
            print(f"{stage.id}\t{stage.title}\t{stage.command_text()}")
        return 0

    try:
        result = run_verification(
            stages,
            root,
            only=_parse_ids(args.only),
            skip=_parse_ids(args.skip),
            inject_failure=_parse_ids(args.inject_failure),
            smoke_url=args.base_url,
            allow_skipped=args.allow_skipped,
        )
    except ValueError as exc:
        print(f"VERIFY_ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    _print_results(result)
    if args.json_out:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result.summary(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    if args.record:
        try:
            record_dir = Path(args.record_dir)
            if not record_dir.is_absolute():
                record_dir = root / record_dir
            path, record = write_acceptance_record(
                result,
                root,
                directory=record_dir,
                allow_dirty=args.allow_dirty,
                allow_for_developer=args.allow_for_developer,
                allow_overwrite=args.allow_overwrite,
            )
        except RecordingRefused as exc:
            print(
                f"VERIFY_REFUSED {exc.code}: {exc.detail}",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        print(
            f"VERIFY_RECORD {path.relative_to(root)} digest={record['output_digest']} "
            f"usable_for_recording={str(record['usable_for_recording']).lower()}"
        )

    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
