"""Provenance for committed recording artefacts (P1 remediation step 10).

A recorded artefact is only honest if a reader can tell which commit and which
verified acceptance record produced it. This module is the one place that:

* reads the acceptance record written by ``scripts/verify_application.py``;
* refuses to start a recording command from a dirty tree, or from a commit with
  no passing record;
* attaches the commit, record path and digest to an artefact (the Step 5 export
  and the Step 11 demo recording); and
* checks the pairing after the fact, failing with a stable reason when an
  artefact names a different commit, a different digest, or no record at all.

The check is deliberately asymmetric. An artefact may be older than the current
HEAD - it was produced under the commit it names, and that commit's record is
the authority. Re-verifying the *current* commit is a separate question and is
answered by running the verifier again, not by rewriting an old artefact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from scripts.verify_application import RecordingRefused

__all__ = [
    "RECORDED_ARTIFACTS",
    "ArtifactPairing",
    "RecordingRefused",
    "acceptance_record_path",
    "check_recording_pairing",
    "find_acceptance_record",
    "load_acceptance_record",
    "recording_provenance",
    "require_clean_for_recording",
]

#: Committed artefacts that claim to be a recording. The export sample is the
#: Step 5 artefact; the FIX-08 live allocation is the real provider recording
#: (Step 11's demo recording uses the same mechanism). Paths are relative to the
#: repository root so the check works from any working directory.
RECORDED_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("decision-run-export.v1.json", "docs/spec/fixtures/decision-run-export.v1.json"),
    ("fix08-live-allocation.v1.json", "docs/spec/fixtures/fix08-live-allocation.v1.json"),
)


@dataclass(frozen=True)
class ArtifactPairing:
    name: str
    path: str
    ok: bool
    code: str
    detail: str
    commit: str = ""
    acceptance_digest: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "ok": self.ok,
            "code": self.code,
            "detail": self.detail,
            "commit": self.commit or None,
            "acceptance_digest": self.acceptance_digest or None,
        }


def _root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    from django.conf import settings

    return Path(settings.BASE_DIR).resolve()


def _acceptance_relpath(commit: str) -> str:
    return f"docs/spec/acceptance/{_safe_commit(commit)}.json"


def _unexpected_dirty_paths(paths: list[str], commit: str) -> list[str]:
    """Dirty paths other than the acceptance record for this very commit.

    The record describes the commit; it cannot be committed *in* that commit
    without changing its hash. Treating its untracked file as a dirty working
    tree would make the documented order - verify, then record - impossible.
    Every other change is still refused.
    """
    expected = {_acceptance_relpath(commit)}
    return [path for path in paths if path not in expected]


def _git_state(root: Path):
    from scripts.verify_application import git_state

    return git_state(root)


def acceptance_record_path(root: Path | str, commit: str) -> Path:
    """The commit-keyed record path, matching the verifier's convention."""
    from scripts.verify_application import acceptance_record_path as path_for

    return path_for(_root(root), commit)


def _candidate_record_paths(root: Path, commit: str, directory: Path | str | None) -> list[Path]:
    """Where a record for ``commit`` may live.

    ``.runtime/acceptance`` is the recording-state location: it is gitignored,
    so writing the record for HEAD does not dirty the tree before the recording
    command runs. ``docs/spec/acceptance`` is the published, committed history
    used by the pairing check. A caller may point at one directory explicitly.
    """
    if directory is not None:
        return [Path(directory) / f"{_safe_commit(commit)}.json"]
    return [
        Path(root) / ".runtime" / "acceptance" / f"{_safe_commit(commit)}.json",
        Path(root) / "docs" / "spec" / "acceptance" / f"{_safe_commit(commit)}.json",
    ]


def find_acceptance_record(
    commit: str,
    *,
    root: Path | str | None = None,
    directory: Path | str | None = None,
) -> tuple[Path, dict] | tuple[None, None]:
    """Find and parse the record for ``commit``, or return ``(None, None)``."""
    root_path = _root(root)
    for path in _candidate_record_paths(root_path, commit, directory):
        if not path.exists():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        if document.get("schema_version") != "application.acceptance.v1":
            continue
        if document.get("commit") != commit:
            continue
        return path, document
    return None, None


def load_acceptance_record(
    commit: str,
    *,
    root: Path | str | None = None,
    directory: Path | str | None = None,
) -> dict | None:
    """Load one commit's acceptance record, or ``None`` when it is absent.

    A malformed record is absent as well: a partial JSON document is not an
    acceptance, and treating it as one would let an interrupted write pass.
    """
    _path, document = find_acceptance_record(commit, root=root, directory=directory)
    return document


def _safe_commit(commit: str) -> str:
    allowed = set("0123456789abcdefABCDEF")
    return "".join(character for character in str(commit) if character in allowed) or "unknown"


def recording_provenance(*, root: Path | str | None = None) -> dict:
    """Best-effort provenance block for an artefact being produced now.

    The shape is stable - every string field is a string, never a nullable type
    that a consumer has to special-case - so an export's structure does not
    change between "verified" and "not yet accepted". ``verified`` is the only
    field a reader needs in order to know whether the claim is complete.
    """
    root_path = _root(root)
    try:
        state = _git_state(root_path)
    except Exception:
        return _empty_provenance("git_state_unavailable")

    record = None
    record_relpath = ""
    if state.commit:
        found_path, record = find_acceptance_record(state.commit, root=root_path)
        if found_path is not None:
            try:
                record_relpath = str(found_path.relative_to(root_path))
            except ValueError:
                record_relpath = str(found_path)

    unexpected = _unexpected_dirty_paths(state.dirty_paths, state.commit)
    status = "unverified"
    if not state.available:
        status = "git_unavailable"
    elif unexpected:
        status = "dirty_working_tree"
    elif record is None:
        status = "acceptance_record_missing"
    elif record.get("verdict") != "pass":
        status = "acceptance_record_not_passed"
    elif not record.get("tree_clean"):
        status = "acceptance_record_not_clean"
    else:
        status = "verified"

    return {
        "commit": state.commit or "",
        "acceptance_commit": str((record or {}).get("commit") or ""),
        "acceptance_record": record_relpath,
        "acceptance_digest": str((record or {}).get("output_digest") or ""),
        "tree_clean": not unexpected,
        "status": status,
        "verified": status == "verified",
        "usable_for_recording": status == "verified",
    }


def _empty_provenance(status: str) -> dict:
    return {
        "commit": "",
        "acceptance_commit": "",
        "acceptance_record": "",
        "acceptance_digest": "",
        "tree_clean": False,
        "status": status,
        "verified": False,
        "usable_for_recording": False,
    }


def require_clean_for_recording(*, root: Path | str | None = None) -> dict:
    """Refuse to record unless HEAD is clean and has a passing acceptance.

    Returns the provenance block to embed in the artefact. The refusal reasons
    are stable codes so a command can print one without inventing prose:
    ``git_commit_unavailable``, ``dirty_working_tree``,
    ``acceptance_record_missing``, ``acceptance_record_not_passed`` and
    ``acceptance_record_not_clean``.
    """
    root_path = _root(root)
    state = _git_state(root_path)
    if not state.available or not state.commit:
        raise RecordingRefused(
            "git_commit_unavailable",
            "The commit hash could not be read; a recording cannot name the state it came from.",
        )
    unexpected = _unexpected_dirty_paths(state.dirty_paths, state.commit)
    if unexpected:
        raise RecordingRefused(
            "dirty_working_tree",
            (
                "The working tree has uncommitted changes. A recording made from it could "
                "not be reproduced from the commit, so it is refused. Commit the changes "
                "and run the verifier with --record first."
            ),
        )
    record = load_acceptance_record(state.commit, root=root_path)
    if record is None:
        raise RecordingRefused(
            "acceptance_record_missing",
            (
                f"No acceptance record exists for commit {state.commit}. Run "
                "`venv/bin/python scripts/verify_application.py --record` on a clean tree "
                "before recording an artefact."
            ),
        )
    if record.get("verdict") != "pass":
        raise RecordingRefused(
            "acceptance_record_not_passed",
            f"The acceptance record for {state.commit} has verdict {record.get('verdict')!r}.",
        )
    if not record.get("tree_clean"):
        raise RecordingRefused(
            "acceptance_record_not_clean",
            (
                f"The acceptance record for {state.commit} was written from a dirty tree "
                "and is not usable as recording provenance. Re-run the verifier on a clean "
                "tree with --record."
            ),
        )
    return recording_provenance(root=root_path)


def check_recording_pairing(
    *,
    root: Path | str | None = None,
    artifacts: list[tuple[str, str]] | None = None,
) -> dict:
    """Check every committed recording against the acceptance record it names.

    A mismatch is a failed check, not a warning: an artefact that names a
    different commit (or a digest that the record does not contain) is evidence
    that the code and the recording drifted apart.
    """
    root_path = _root(root)
    paths = artifacts if artifacts is not None else list(RECORDED_ARTIFACTS)
    results: list[ArtifactPairing] = []
    for name, relative in paths:
        path = root_path / relative
        if not path.exists():
            results.append(
                ArtifactPairing(
                    name=name,
                    path=relative,
                    ok=False,
                    code="artifact_missing",
                    detail=f"{relative} does not exist.",
                )
            )
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            results.append(
                ArtifactPairing(
                    name=name,
                    path=relative,
                    ok=False,
                    code="artifact_unreadable",
                    detail=f"{relative} is not valid JSON.",
                )
            )
            continue
        block = document.get("recording") if isinstance(document, dict) else None
        if not isinstance(block, dict):
            results.append(
                ArtifactPairing(
                    name=name,
                    path=relative,
                    ok=False,
                    code="recording_provenance_missing",
                    detail=(
                        "The artefact has no `recording` block naming the commit and "
                        "acceptance digest it was produced under."
                    ),
                )
            )
            continue
        commit = str(block.get("commit") or "")
        digest = str(block.get("acceptance_digest") or "")
        if not commit or not digest:
            results.append(
                ArtifactPairing(
                    name=name,
                    path=relative,
                    ok=False,
                    code="recording_provenance_incomplete",
                    detail="The recording block does not name both a commit and a digest.",
                )
            )
            continue
        record = load_acceptance_record(
            commit, root=root_path, directory=root_path / "docs/spec/acceptance"
        )
        if record is None:
            results.append(
                ArtifactPairing(
                    name=name,
                    path=relative,
                    ok=False,
                    code="acceptance_record_missing",
                    detail=f"No application.acceptance.v1 record exists for {commit}.",
                    commit=commit,
                    acceptance_digest=digest,
                )
            )
            continue
        if record.get("commit") != commit:
            results.append(
                ArtifactPairing(
                    name=name,
                    path=relative,
                    ok=False,
                    code="acceptance_commit_mismatch",
                    detail=(
                        f"The artefact names {commit}, but the acceptance record "
                        f"names {record.get('commit')}."
                    ),
                    commit=commit,
                    acceptance_digest=digest,
                )
            )
            continue
        if record.get("output_digest") != digest:
            results.append(
                ArtifactPairing(
                    name=name,
                    path=relative,
                    ok=False,
                    code="acceptance_digest_mismatch",
                    detail=(
                        "The artefact's acceptance digest is not the digest stored in the "
                        "acceptance record for that commit."
                    ),
                    commit=commit,
                    acceptance_digest=digest,
                )
            )
            continue
        if record.get("verdict") != "pass":
            results.append(
                ArtifactPairing(
                    name=name,
                    path=relative,
                    ok=False,
                    code="acceptance_not_passed",
                    detail="The named acceptance record does not have verdict 'pass'.",
                    commit=commit,
                    acceptance_digest=digest,
                )
            )
            continue
        results.append(
            ArtifactPairing(
                name=name,
                path=relative,
                ok=True,
                code="paired",
                detail="The artefact names the commit and digest of a passing acceptance record.",
                commit=commit,
                acceptance_digest=digest,
            )
        )
    return {
        "schema_version": "application.recording-pairing.v1",
        "ok": all(row.ok for row in results),
        "checked": len(results),
        "failed": sum(1 for row in results if not row.ok),
        "artifacts": [row.as_dict() for row in results],
    }
