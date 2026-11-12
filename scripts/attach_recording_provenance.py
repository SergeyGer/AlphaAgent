#!/usr/bin/env python
"""Attach commit + acceptance provenance to committed recording artefacts.

New recordings write their ``recording`` block at creation time. This script is
for artefacts that predate the mechanism (the Step 5 export sample and the
FIX-08 live-allocation recording): it reads the published acceptance record for
a commit and attaches the exact commit, record path and digest. It does not
re-run the recording and does not claim it did.

    venv/bin/python scripts/attach_recording_provenance.py --commit <40-hex>

The export sample's checksum is recomputed over the edited document, because the
recording block is part of the exported payload. The FIX-08 artefact is not a
checksummed bundle; it gains a ``provenance_kind`` field saying the block was
attached at remediation rather than emitted by the recorder.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.decision_json import export_content_hash  # noqa: E402
from services.recording_provenance import (  # noqa: E402
    RECORDED_ARTIFACTS,
    RecordingRefused,
    find_acceptance_record,
)

EXPORT_ARTIFACT = "docs/spec/fixtures/decision-run-export.v1.json"


def provenance_block(record: dict, record_path: Path) -> dict:
    try:
        relative = str(record_path.relative_to(PROJECT_ROOT))
    except ValueError:
        relative = str(record_path)
    return {
        "commit": str(record["commit"]),
        "acceptance_commit": str(record["commit"]),
        "acceptance_record": relative,
        "acceptance_digest": str(record["output_digest"]),
        "tree_clean": bool(record.get("tree_clean")),
        "status": "attached_at_remediation",
        "verified": bool(record.get("verdict") == "pass" and record.get("tree_clean")),
        "usable_for_recording": bool(record.get("usable_for_recording")),
    }


def attach(relative: str, record: dict, record_path: Path) -> None:
    path = PROJECT_ROOT / relative
    document = json.loads(path.read_text(encoding="utf-8"))
    document["recording"] = provenance_block(record, record_path)
    if relative == EXPORT_ARTIFACT:
        payload = {key: value for key, value in document.items() if key != "checksum"}
        document.setdefault("checksum", {})["value"] = export_content_hash(payload)
        path.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    else:
        document["recording"]["provenance_kind"] = "attached_at_remediation"
        document["recording"]["note"] = (
            "This artefact predates the Step 10 recorder guard. The block names the "
            "commit whose clean acceptance record was used; it was attached by "
            "scripts/attach_recording_provenance.py, not emitted by the recorder."
        )
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"ATTACHED {relative} commit={record['commit']} digest={record['output_digest']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="Commit whose acceptance record to use.")
    args = parser.parse_args()
    record_path, record = find_acceptance_record(
        args.commit, root=PROJECT_ROOT, directory=PROJECT_ROOT / "docs/spec/acceptance"
    )
    if record is None or record_path is None:
        print(f"REFUSED acceptance_record_missing: no published record for {args.commit}")
        return 2
    if record.get("verdict") != "pass":
        print(f"REFUSED acceptance_record_not_passed: verdict={record.get('verdict')!r}")
        return 2
    try:
        for _name, relative in RECORDED_ARTIFACTS:
            attach(relative, record, record_path)
    except RecordingRefused as exc:
        print(f"REFUSED {exc.code}: {exc.detail}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
