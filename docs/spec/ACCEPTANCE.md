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
final `VERIFY_SUMMARY {json}` line, plus `VERIFY_COVERAGE` when the selected
stages cannot be a release acceptance. It exits non-zero when a required stage
fails, when a required stage was skipped, or when the acceptance record cannot
be written. `--only` and `--skip` select stages for local iteration;
`--inject-failure <id>` deliberately fails a stage so the non-zero exit path can
be checked. **A selected-stage run is a developer run, not release acceptance**
(see §2); the full stage set is required for `usable_for_recording`.

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
| `verdict` | `pass` / `fail` / `incomplete` for the stages that ran |
| `stages[]` | Each stage's `id`, `status`, `exit_code` and parsed counts |
| `counts` | `backend_tests` and `frontend_tests` |
| `required_stages` | The eight stage ids release acceptance requires |
| `coverage` | `complete`, plus `missing`, `duplicates`, `unexpected`, `failed`, `skipped`, `passing_once` and the stage counts |
| `stage_count` | How many stage results the record contains |
| `output_digest` | SHA-256 over sorted compact JSON of the schema, commit, tree state, verdict and stage summaries |
| `recorded_at` | UTC timestamp |
| `usable_for_recording` | `true` only when the tree was clean, the verdict passed **and** each of the eight stages ran exactly once with a passing result |
| `release_acceptance` | The same boolean, named for what it gates |

The digest excludes durations, timestamps, host paths and raw command output, so
re-running the same commit with the same stage outcomes produces the same
digest and verdict. Raw output is printed to the terminal and is intentionally
not stored as the acceptance evidence.

A `--only` run, a `--skip`, a failed stage and a duplicated stage all write a
record (unless the caller asks for `--allow-for-developer`) but it is marked
`usable_for_recording: false` with the reasons in `coverage`. Such a record is
evidence of what that developer run did, never of a release. The verifier
refuses to overwrite an existing usable record for the same commit with an
unusable one unless `--allow-overwrite` is explicit.

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
| `acceptance_record_digest_invalid` | The record's stages no longer reproduce its `output_digest` |
| `acceptance_record_not_usable` | The record does not cover all eight stages exactly once with a passing result (a `--only`, skipped, failed or duplicated stage) |

The pairing check (`manage.py check_recordings`) uses the additional stable code
`acceptance_stages_incomplete` when the record's stage set is not the eight
declared stages exactly once.

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
  "scope": "build_verification_not_run_origin",
  "status": "verified",
  "verified": true,
  "usable_for_recording": true
}
```

The Step 5 export bundle (`GET /api/decision-runs/<id>/export/`) carries the
same block. When the working tree is dirty or no record exists, the shape is
unchanged: the strings are empty and `status` names the reason, so a consumer
never has to distinguish a missing block from a verified one.

`scope` is explicit: this block is **build verification**, not run origin. A
verified build says the checks passed on that commit; it does not say the
document's content came from a provider. The export's `run.origin` states that
separately, and only `provider_run` with `provider_backed: true` is evidence of
a provider debate.

Check every committed recording against its named record:

```bash
venv/bin/python manage.py check_recordings
venv/bin/python manage.py check_recordings --json
```

The check fails, with a stable reason, when an artefact names a commit with no
record (`acceptance_record_missing`), a digest that the record does not contain
(`acceptance_digest_mismatch`), a record whose verdict is not `pass`
(`acceptance_not_passed`), an edited record (`acceptance_record_digest_invalid`),
a record whose stage set is not the eight declared stages exactly once
(`acceptance_stages_incomplete`), a record that is otherwise marked unusable
(`acceptance_record_not_usable`), or no provenance block at all
(`recording_provenance_missing`). This is the mechanism that makes a recorded
artefact drift visible instead of silent.

## 4. What the check does and does not establish

It establishes that the named commit passed the **eight** application checks,
that the recorded artefact names that commit and digest, and that the fields are
internally consistent. It does **not** re-run the model, prove a provider
response is authentic, or establish profitability. A recording can still be
honest about a synthetic input - the artefact's own `synthetic_input_data`,
`mode` and `source` fields remain the authority for that.

## 5. Committed records and the release gate

Two committed records predate the release gate. They are read with the same
derivation the guard uses (`acceptance_record_usable`), not by trusting the
stored boolean:

| Record | Commit | Stages | Counts | What it supports |
| --- | --- | --- | --- | --- |
| `4e6c71f7e01c50b4e43ff44b40e90265837a3a0a.json` | `4e6c71f` | all eight | 928 backend / 275 frontend | A full pass at the steps 7–8 commit; usable |
| `03308ca6cff5ba6cc36c3d72869c45447193f518.json` | `03308ca` (pre-merge branch commit) | four: backend-tests, lint, format, migration-check | 955 backend / **0** frontend | Backend and static checks only; **not usable for recording** |

The four-stage record's stored `usable_for_recording` was `true` when it was
written. That was the defect: `usable_for_recording` was set from
`tree_clean and verdict == "pass"` and never checked the stage set. The record
file now says `usable_for_recording: false` with `superseded_reason`, and the
guard derives the same verdict from its four stages even if an older copy says
otherwise. It is not evidence that the release commit passed frontend tests,
typecheck, build or smoke.

A release record must be generated at a clean commit with the full eight-stage
command. Because a commit cannot contain the record that names its own hash, the
documented order in §2 is commit, verify `--record`, record the artefact, then
commit the artefact and record together. The untracked record is ignored by the
dirty-tree guard for exactly that ordering. A record produced from a dirty tree,
a `--only` run or a failed/skipped/duplicate stage is developer evidence only.

## 6. Release evidence and the Step 11 recording manifest

Step 10's release evidence is the full eight-stage acceptance record plus the
three evidence sources the smoke test cannot exercise:

| Evidence | What must be attached | How it is checked |
| --- | --- | --- |
| CI | The `ci.yml` run for the release commit: lint/format, Django check, migrations, the backend suite and the coverage gate | The local equivalents are the `lint`, `format`, `migration-check` and `backend-tests` stages; the remote run URL is recorded in the release manifest |
| Docs | The `docs.yml` link, wiki-reference and referenced-media checks for the commit | Local documentation tests plus the workflow's own commands |
| CodeQL | The `security-and-quality` analysis for the commit, with zero alerts | A local CodeQL database/analysis or the workflow run URL; the count is recorded, not assumed |
| Workflows the health endpoint cannot reach | An authenticated browser session that approves refreshed terms and replays a completed decision; a recovery run for an interrupted allocation job | Screenshots/records under `.runtime/release-evidence/`, named in the release manifest |

The **release manifest** is the document Step 11 records against. It is JSON
(`docs/spec/fixtures/release-evidence.v1.json` is the committed example) with
this shape:

| Field | Meaning |
| --- | --- |
| `schema_version` | `application.release-evidence.v1` |
| `commit` | The full HEAD hash the evidence was produced on |
| `subject` | That commit's subject line |
| `tree_clean` | Whether the evidence was produced from the commit itself |
| `release_ready` | `true` only for a clean, usable full-stage record; a dirty developer run is never release-ready |
| `build` | The tested build: commit, subject, tree state, a digest of the working diff and the dirty paths |
| `limitations` | Anything that could not be attached or run, named rather than omitted |
| `acceptance_record` | Path to the acceptance record, plus its `output_digest` and `usable_for_recording` |
| `counts` | `backend_tests` and `frontend_tests` from that record |
| `stages[]` | The record's per-stage `id`, `status`, `exit_code` and counts |
| `evidence.ci` / `.docs` / `.codeql` | `{status, url, detail}`; `status` is `passed`, `failed`, `not_available` or `not_run`, so a missing remote run is never silently treated as passed |
| `workflows[]` | `{name, status, evidence}` for terms approval, decision replay and interrupted-job recovery |
| `steps[]` | `{step, status, evidence, skipped_reason}` for Steps 1–9; a skipped case names its reason |
| `recording_manifest` | The Step 11 inputs: the artefact paths, run ids, provider mode, and the `run.origin` claim each artefact must carry |

The manifest is not an acceptance record and cannot substitute for one: its
`acceptance_record.usable_for_recording` must be `true`, the commit and digest
must match, and the pair is checked by `check_recordings`. A manifest that names
a different commit, a digest the record does not contain, or an incomplete
stage set fails the same pairing check.

At the time of writing, the tree contains the Step 9/10 changes but is **not
committed**, as the task requires. A publishable release record therefore cannot
be produced yet: `git_state` reports a dirty tree, and `--record` refuses it.
The full command, the exact stages and the manifest shape above are what the
release commit must run; the local evidence is in
`.runtime/release-evidence/` and is explicitly not a release acceptance.
