# Verified acceptance before recording

A recording is only evidence if it names the code that produced it. This page
defines the one command that verifies the application, the commit-keyed
acceptance record it writes, and the check that ties every committed recording
to that record. It is the contract behind the P1 finding that the recorded demo
and its acceptance record were not tied to a verified application state.

## 1. The single verification command

```bash
cd /root/projects/AlphaAgent
export DOCKER_CONFIG=/root/projects/AlphaAgent/.runtime/docker
export XDG_CACHE_HOME=/root/projects/AlphaAgent/.runtime/cache
docker compose up -d db redis && sleep 15
# Start the web stack too when the smoke stage is included:
docker compose up -d web

venv/bin/python scripts/verify_application.py
# equivalent shortcuts:
make verify
make acceptance          # same, with --record
```

The smoke URL is read from `ALPHA_VERIFY_BASE_URL` (default
`http://127.0.0.1:8080/healthz/`) or passed as `--base-url`. The stages, in
order, are:

| Stage | What it runs |
| --- | --- |
| `backend-tests` | `venv/bin/python manage.py test core.tests --noinput` |
| `frontend-tests` | `npm test -- --run` in `frontend/` |
| `typecheck` | `npx tsc --noEmit` in `frontend/` |
| `build` | `npm run build` in `frontend/` |
| `lint` | `ruff check . --line-length 100` |
| `format` | `ruff format --check .` |
| `migration-check` | `manage.py makemigrations --check --dry-run` |
| `smoke` | `GET /healthz/` against the running stack |

Stages run **sequentially** in separate subprocesses. Two `manage.py test`
processes must never overlap: they share `test_alpha_agent_db`.

The command prints one `VERIFY_STAGE <id>=<status> ...` line per stage and one
final `VERIFY_SUMMARY {json}` line. It exits non-zero when a required stage
fails, when a required stage was skipped, or when the acceptance record cannot
be written. `--only` and `--skip` select stages for local iteration;
`--inject-failure <id>` deliberately fails a stage so the non-zero exit path can
be checked.

## 2. The acceptance record

```bash
# On a clean tree, after the commit exists:
venv/bin/python scripts/verify_application.py --record
```

This writes `docs/spec/acceptance/<commit>.json`:

| Field | Meaning |
| --- | --- |
| `commit` | Full HEAD hash the run was performed on |
| `tree_clean` | `true` only when the working tree had no unexpected changes |
| `verdict` | `pass` / `fail` / `incomplete` |
| `stages[]` | Each stage's `id`, `status`, `exit_code` and parsed counts |
| `counts` | `backend_tests` and `frontend_tests` |
| `output_digest` | SHA-256 over sorted compact JSON of the schema, commit, tree state, verdict and stage summaries |
| `recorded_at` | UTC timestamp |
| `usable_for_recording` | `true` only when the tree was clean and the verdict passed |

The digest excludes durations, timestamps, host paths and raw command output, so
re-running the same commit with the same stage outcomes produces the same
digest and verdict. Raw output is printed to the terminal and is intentionally
not stored as the acceptance evidence.

`--allow-dirty` writes a developer record marked `tree_clean: false` and
`usable_for_recording: false`. Publishing such a record over a clean one is a
mistake; use `--record-dir .runtime/acceptance` for local evidence.

### Why the record is not in the commit it verifies

A commit hash depends on its tree, so a file that contains that hash cannot be
added to the same commit without changing the hash it names. The documented
order is therefore:

1. commit the code;
2. run `scripts/verify_application.py --record` on that clean commit, which
   writes the record file (untracked at this point);
3. record the artefact while the tree is otherwise clean - the recording guard
   ignores exactly the untracked acceptance file for HEAD;
4. commit the recorded artefact **and** the acceptance record.

The guard is at `services/recording_provenance.require_clean_for_recording`. It
refuses with a stable code:

| Code | Condition |
| --- | --- |
| `git_commit_unavailable` | HEAD cannot be read |
| `dirty_working_tree` | Any change other than the expected record file is present |
| `acceptance_record_missing` | No record exists for HEAD |
| `acceptance_record_not_passed` | The record's verdict is not `pass` |
| `acceptance_record_not_clean` | The record itself was written from a dirty tree |

## 3. Recording provenance and pairing

`record_live_allocation` calls the guard **before** configuring the provider, so
a refused recording makes no paid call. `scripts/build_export_sample.py` (the
Step 5 sample capture) uses the same guard. A successful artefact carries:

```json
"recording": {
  "commit": "<40 hex>",
  "acceptance_commit": "<40 hex>",
  "acceptance_record": "docs/spec/acceptance/<commit>.json",
  "acceptance_digest": "<sha256>",
  "tree_clean": true,
  "status": "verified",
  "verified": true,
  "usable_for_recording": true
}
```

The Step 5 export bundle (`GET /api/decision-runs/<id>/export/`) carries the
same block. When the working tree is dirty or no record exists, the shape is
unchanged: the strings are empty and `status` names the reason, so a consumer
never has to distinguish a missing block from a verified one.

Check every committed recording against its named record:

```bash
venv/bin/python manage.py check_recordings
venv/bin/python manage.py check_recordings --json
```

The check fails, with a stable reason, when an artefact names a commit with no
record (`acceptance_record_missing`), a digest that the record does not contain
(`acceptance_digest_mismatch`), a record whose verdict is not `pass`
(`acceptance_not_passed`), or no provenance block at all
(`recording_provenance_missing`). This is the mechanism that makes a recorded
artefact drift visible instead of silent.

## 4. What the check does and does not establish

It establishes that the named commit passed the application checks, that the
recorded artefact names that commit and digest, and that the fields are
internally consistent. It does **not** re-run the model, prove a provider
response is authentic, or establish profitability. A recording can still be
honest about a synthetic input - the artefact's own `synthetic_input_data`,
`mode` and `source` fields remain the authority for that.

## 5. Committed records at HEAD `7969cd7` (2026-11-12)

The mechanism is complete; the evidence for the current release commit is not.
This is the state of the two committed records:

| Record | Commit | Stages | Counts | What it supports |
| --- | --- | --- | --- | --- |
| `4e6c71f7e01c50b4e43ff44b40e90265837a3a0a.json` | `4e6c71f` | all eight | 928 backend / 275 frontend | A full pass at the steps 7–8 commit |
| `03308ca6cff5ba6cc36c3d72869c45447193f518.json` | `03308ca` (pre-merge branch commit) | backend-tests, lint, format, migration-check | 955 backend / **0** frontend | The backend and static checks at the steps 9–10 pre-merge commit |

There is **no** `docs/spec/acceptance/7969cd7....json`. The four-stage record is
honest about what it ran - it records `frontend_tests: 0` - but it is not
evidence that the merged commit passed the frontend, typecheck, build or smoke
stages. Do not cite it as a full acceptance. Recording the full result at the
release commit is tracked as
[#65](https://github.com/SergeyGer/AlphaAgent/issues/65); until then this page
must not imply a complete eight-stage record exists for `7969cd7`.
