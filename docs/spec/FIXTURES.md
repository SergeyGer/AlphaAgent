# Decision-run fixtures (REP-02 / REP-03)

The Phase 1 demo fixtures live in [`decision-runs.v1.json`](fixtures/decision-runs.v1.json)
and are loaded with:

```bash
python manage.py load_decision_fixtures
```

The loader creates a `demo` account (unusable password) with a portfolio, then
creates one `DecisionRun` per fixture. It is **idempotent**: a fixture run is
keyed by its stable `fixture_id`, so running the command twice creates nothing
the second time and reports the runs it skipped.

```text
$ python manage.py load_decision_fixtures
...
CREATE risk-intervention-aapl-2026-02-03 -> run <uuid> [risk_intervention, AAPL, completed]
...
Done: 3 created, 0 skipped, 3 fixture(s) in the manifest.

$ python manage.py load_decision_fixtures
SKIP   risk-intervention-aapl-2026-02-03 (already loaded as run <uuid>)
...
Done: 0 created, 3 skipped, 3 fixture(s) in the manifest.
```

## Offline viewer bundle

The same manifest also drives the REP-02 static viewer. `npm --prefix frontend
run build:viewer` converts it to a versioned, checksummed,
`alphaagent.offline-viewer` bundle and emits a self-contained `docs/viewer/`.
The conversion is mechanical: deterministic ids join event payloads to claims,
evidence, rebuttals, guard and outcomes, and no narrative content is invented.
All three runs remain `synthetic` and the viewer displays that label explicitly.
See [`../../frontend/viewer/README.md`](../../frontend/viewer/README.md) and
[`DEMO.md`](DEMO.md) §5.

The viewer also accepts a redacted `GET /api/decision-runs/<id>/export/`
(`alphaagent.decision-run`) file, so a reviewer can capture a real recorded run
while the API is available and replay it later with no database or login.

Options:

| Flag | Meaning |
| --- | --- |
| `--path PATH` | Manifest path relative to `BASE_DIR` (default `docs/spec/fixtures/decision-runs.v1.json`). |
| `--username NAME` | Account whose portfolio receives the runs (default `demo`). |
| `--force` | Delete and recreate a fixture run whose manifest checksum has drifted. Without it, drift is an error rather than a silent mix of old and new demo data. |

## Honesty rules

1. **All three fixtures are `mode: "synthetic"`.** The current pipeline has no
   rebuttal stage, and no live run could be recorded when these were authored.
   The API exposes `mode`, `is_synthetic` and a `provenance` block on every run
   and every detail payload, so a UI can and should label them.
2. **The disagreement fixture's rebuttals are synthetic.** They exist to exercise
   the EVID-03 consumer contract for Phase 2. They are not a recording of a real
   debate, and the run's `protocol_version` is `synthetic.v1`.
3. **Synthetic outcome figures are illustrative, not measured performance.**
   Every outcome carries `method_version: "attribution.synthetic.v1"` and
   `signal_diagnostics.synthetic: true`. They must not be used for comparative
   claims. The uncertainty fixture records its outcome as `unavailable` rather
   than inventing a price.
4. **Source URIs use the `fixture://` scheme.** They are citations for the
   fixture's own authored evidence, not links to third-party material.
5. **Unknown publication dates stay unknown.** The uncertainty fixture's
   fundamentals evidence has `published_at: null`; the loader preserves null and
   sets availability to the retrieval time, which is the earliest instant that
   can be defended.

## Manifest schema (version 1, schema 1.0)

Top level:

```json
{
  "manifest_version": 1,
  "schema_version": "1.0",
  "generated_for": "Phase 1 decision review demo (REP-02, REP-03)",
  "checksum": {
    "algorithm": "sha256",
    "scope": "each fixtures[] entry with its 'checksum' member removed",
    "canonicalisation": "JSON with sorted keys, separators (',' ':'), UTF-8"
  },
  "honesty": {
    "all_fixtures_synthetic": true,
    "why": "...",
    "rebuttal_note": "...",
    "outcome_note": "..."
  },
  "fixtures": [Fixture, ...]
}
```

`manifest_version` and `schema_version` are checked by the loader. If
`honesty.all_fixtures_synthetic` is true, the loader refuses a manifest that
contains a non-synthetic fixture.

### Fixture

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `fixture_id` | string | yes | Stable natural key; unique; drives idempotency. |
| `label` | string | no | Human description for tooling/output. |
| `scenario` | string | yes | `risk_intervention`, `disagreement` or `uncertainty`. |
| `mode` | string | yes | One of the `RunMode` values. These fixtures use `synthetic`. |
| `source` | string | yes | One of the `RunSource` values. These fixtures use `fixture`. |
| `recording_date` | ISO date | yes | Must match the UTC date of `started_at`. |
| `protocol_version` | string | yes | Protocol identity; these use `synthetic.v1`. |
| `ticker` | string | yes | Instrument the run is about. |
| `started_at` | ISO-8601 UTC | yes | When the run began; also the `run.created` event time. |
| `checksum` | string | yes | SHA-256 over the fixture with `checksum` removed. |
| `snapshot` | object | yes | Captured inputs (below). |
| `timeline` | array | yes | Ordered steps (below). Must end in a terminal status and contain exactly one `decision` step. |

### `snapshot`

| Field | Meaning |
| --- | --- |
| `portfolio_version` | Opaque version label of the portfolio state. |
| `analysis_cutoff` | UTC instant after which evidence is not available to the run. Claims citing evidence available later are refused. |
| `captured_at` | UTC capture time. |
| `holdings` | JSON array; quantities/prices are decimal strings. |
| `cash` | JSON object; decimal strings. |
| `quotes` | JSON object keyed by ticker; decimal strings. |

The loader computes `content_hash` from the canonical snapshot payload; it is
not authored in the manifest.

### `timeline[]` steps

All steps have `kind` and `at` (ISO-8601 UTC). The other fields depend on the
kind. Every step writes exactly one ordered `RunEvent`, so the loader cannot
create a record without its event.

| `kind` | Fields | Effect |
| --- | --- | --- |
| `transition` | `status`, optional `payload` | Validated against `LEGAL_TRANSITIONS`, then written as a `status.changed` event. |
| `event` | `event_type`, `stage`, optional `agent_role`, optional `payload` | Free-form typed event, e.g. `stage.failed`. |
| `evidence` | `key`, `source_type`, `source_uri`, `publisher`, `published_at` (nullable), `retrieved_at`, optional `available_at`, `excerpt` or `observation`, `units`, `currency`, `is_redistributable` | Creates an `EvidenceItem` and an `evidence.recorded` event. |
| `claim` | `key`, `agent_role`, `stance`, `claim_type`, `statement`, `evidence_keys`, optional `horizon`, `limitations`, `case_round` | Creates a `Claim` (statement ≤ 280 chars; evidence must be available at the cutoff) and a `claim.created` event. |
| `rebuttal` | `key`, `responding_agent`, `target_claim_key`, `response_type`, `explanation`, `evidence_keys`, optional `position_change` | Creates a `Rebuttal`; the target must be the opposing initial claim in this run. Writes `rebuttal.created`. |
| `decision` | `action`, `requested_quantity` (nullable for HOLD), `concise_rationale`, `decisive_claim_keys`, `strongest_objection`, `thesis`, `invalidation_condition`, `engine_source` | Creates the run's single `DecisionRecord` and a `decision.proposed` event. |
| `guard` | `policy_version`, `evaluated_portfolio_version`, `quote_time`, `status`, `requested_quantity`, `allowed_quantity`, `binding_rule`, `reason_codes`, `numerical_checks`, `checks` | Creates a `GuardRecord` against the decision and a `guard.evaluated` event. `checks` is the typed array (`rule_code`, `outcome` = `pass`/`fail`/`not_evaluated`, `threshold`, `actual`, `unit`). It is stored inside `numerical_checks` and lifted back out by the API. |
| `outcome` | `horizon_days`, `method_version`, `maturity`, optional prices/returns, `benchmark_ticker`, `benchmark_return_pct`, `signal_diagnostics`, `unavailable_reason`, `evaluated_at` | Creates an `OutcomeRecord` and an `outcome.recorded` event. |

Decimal values are authored as strings in JSON. The loader converts them to
`Decimal` before saving and the API serialises them back to strings (DATA-02).

## Checksums

Each `fixtures[]` entry declares a checksum over itself with the `checksum`
member removed, canonicalised as sorted-key, compact JSON in UTF-8. The loader
recomputes it before writing any row and refuses a mismatch. To regenerate after
an intentional edit:

```bash
python - <<'PY'
import json
from pathlib import Path
from core.decision_json import content_hash

path = Path("docs/spec/fixtures/decision-runs.v1.json")
manifest = json.loads(path.read_text(encoding="utf-8"))
for fixture in manifest["fixtures"]:
    payload = {k: v for k, v in fixture.items() if k != "checksum"}
    fixture["checksum"] = content_hash(payload)
path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
```

Then run `load_decision_fixtures --force` to replace the existing runs.

## Fixture catalogue

| `fixture_id` | Scenario | Ticker | Recording date | Demonstrates |
| --- | --- | --- | --- | --- |
| `risk-intervention-aapl-2026-02-03` | `risk_intervention` | AAPL | 2026-02-03 | A BUY for 40 shares clamped by `max_trade_allocation_pct` to 2 shares; requested vs allowed, difference and binding rule are explicit. The rebutting stage is `not_run`, because the current pipeline has none. |
| `disagreement-tsla-2026-02-04` | `disagreement` | TSLA | 2026-02-04 | Two bull and two bear claims with cited evidence, two rebuttals (dispute and qualify), and an explicit HOLD verdict with decisive claims and the strongest objection. Rebuttals are synthetic. |
| `uncertainty-msft-2026-02-05` | `uncertainty` | MSFT | 2026-02-05 | An undated source (`published_at: null`), a `stage.failed` event leaving the analysing stage `degraded`, a HOLD verdict, and an `unavailable` outcome with a reason instead of an invented price. |

## What the API exposes

All endpoints are under `/api/decision-runs/` and are authenticated and scoped
to the caller's portfolios:

| Endpoint | Relevant fixture guarantee |
| --- | --- |
| `GET /api/decision-runs/` | Each item has `mode`, `is_synthetic`, `source`, `cost`, `failure`. |
| `GET /api/decision-runs/<id>/` | `stage_states` marks skipped stages `not_run` and degraded stages `degraded`; `failure` is present for failed runs. |
| `GET /api/decision-runs/<id>/detail/` | Flat contract: `rationale`, `decisive_claim_ids`, `strongest_objection`, `claims`, `rebuttals`, `evidence`, `guard`, `outcomes`, `has_rebuttals`. The nested `cases`, `verdict`, `guard_comparison` and `limitations` forms are served alongside for richer audit views. `run.provenance` carries the synthetic label. |
| `GET /api/decision-runs/<id>/events/?after_seq=N` | `events` array of `{event_id, seq, type, stage, agent_role, occurred_at, payload}`, plus `next`, `last_sequence` and `latest_sequence`. |
| `GET /api/decision-runs/<id>/export/` | Versioned, checksummed bundle. Account identifiers and credentials are excluded; evidence excerpts are omitted unless `is_redistributable` is true. |

A run belonging to another user returns `404` with the project's standard error
envelope, so ids cannot be probed for ownership.

## Phase 4 paper allocation fixture

[`phase4-allocation.v1.json`](fixtures/phase4-allocation.v1.json) is a **separate**
fixture for the Phase 4 paper evaluation; it is not a decision-run manifest and
no Phase 1 loader reads it. It declares a demo portfolio (cash, one AAPL
holding), explicit sector classifications, quotes, a deterministic synthetic
daily price series, three candidate cases with claims, and one stubbed CIO
response whose `portfolio_version` is filled from the brief at run time.

```bash
python manage.py run_allocation --reset --apply
python manage.py run_allocation --reset --apply --fail-leg AAPL
```

The command labels the provider `stub`, the source `fixture`, and computes the
covariance/correlation values from the supplied series rather than inventing
them. The series, prices, classifications and candidate cases are synthetic;
this is engineering/paper evidence, not realised performance.

A captured example of the command's compact output is kept at
[`phase4-allocation.v1.output.json`](fixtures/phase4-allocation.v1.output.json).
Like the Phase 3 report snapshot, it is an example output, is not loaded by any
command, and can be regenerated.


## Export portability fixtures (P1 remediation step 5)

These committed files make the browser and Python agree about an exported run.

### `canonical-json.v1.json`

The shared canonical-JSON vectors. Both
`core/tests/test_p1_export_portability.py` and
`frontend/viewer/exportParity.test.ts` read this exact file and assert the same
canonical text and SHA-256 for every vector. The form is:

* object keys sorted by UTF-16 code unit;
* compact separators, no insignificant whitespace, UTF-8;
* JSON numbers formatted like ECMAScript `Number::toString`, so the Python float
  `90.0` and the JavaScript number `90` both hash as `90`;
* `Decimal` values normalised to fixed-point strings with no exponent and no
  trailing fractional zeros (`Decimal("90.0")` -> `"90"`);
* strings escaped the way `JSON.stringify` escapes them.

The vectors cover nested data with reordered keys, decimal strings, nullable
times, Unicode, extreme and fractional numbers, escaping, and UTF-16 key order.
Changing either implementation alone fails one of the two suites.

### `decision-run-export.v1.json`

An **authored** sample captured from `GET /api/decision-runs/<id>/export/` by
`venv/bin/python scripts/build_export_sample.py --force`. It is produced from the
run in `core/tests/export_sample.py`, which carries two guard evaluations, a
superseded outcome, a pending/unavailable outcome schedule, nullable publication
times, nested decimal strings and Unicode settings. The run is
`mode: "synthetic"`, `source: "fixture"`, `protocol_version: "synthetic.v1"`; its
claims, rebuttal, guard results and provider-shaped fields are authored test
data. Its `run.origin` says `kind: "authored_fixture"` with
`provider_backed: false`, so the viewer labels it **Synthetic**, not Real
recorded.

The Django suite verifies the file's checksum from the stored document and
compares its recursive shape with a fresh endpoint export; the Vitest suite
imports the same bytes unchanged through the viewer's importer and renders the
round trip. A newly emitted field therefore fails the Python shape test until the
sample is regenerated, and the browser test proves the regeneration is
importable.

The export now carries `schema_version`, names its canonicalisation in
`checksum.canonicalisation`, and separates three claims explicitly:

* `checksum` — the document is unmodified (integrity);
* `recording` — which commit and acceptance record the build was verified
  against, `scope: build_verification_not_run_origin`;
* `run.origin` — how the content was produced. Only `provider_run` with
  `provider_backed: true` is evidence of a provider debate.

### `decision-run-export.v0-superseded.json`

The previous sample, retained as a compatibility conformance file. It is the
defect: authored claims, rebuttal, guards and provider fields labelled
`mode: "recorded"`, `source: "crewai"`, with no origin block. It still verifies
under format 1.0, but the viewer does not upgrade it to a provider debate: the
carried `fixture_id` identifies authored fixture content, so it imports as
**Synthetic** with a "superseded export label" note. A modified payload fails
verification. It is not in `RECORDED_ARTIFACTS`; it is a conformance fixture, not
a build recording.

### `decision-run-export.provider-backed.v1.json`

Test data for the Step 11 recording path: an export from a run record with
`mode: "live"`, `source: "crewai"`, provider `anthropic`, model
`claude-haiku-5-5` and a usage block. Its `run.origin` is
`kind: "provider_run"`, `provider_backed: true`; the viewer renders **Real
recorded** and shows the origin's limitations. It is explicitly test data
(`origin.limitations` says so): the record shape is real, no live provider call
was made for this file. Regenerate it with
`venv/bin/python scripts/build_export_sample.py --provider-backed --force`.

## FIX-08 recorded provider run

[`fix08-live-allocation.v1.json`](fixtures/fix08-live-allocation.v1.json) is a
**real recorded model run**, not a fixture response. One Anthropic CIO call was
made through `services.allocation_provider.BudgetedAllocationClient` on
2026-11-12 with model `claude-haiku-5-5`; the artifact records the exact prompt
(and its hash), the prompt-template version and parameters, the validated
response, token counts, price-table cost estimate, the proposed plan and its
review payload. It says `mode: "live"`, `source: "anthropic"`,
`run_kind: "real_recorded_model_run"`, `synthetic_response: false`.

The artifact requires one honest qualification, which it states itself: the
**input brief** is operator-supplied demonstration data, not market history
([`fix08-live-input.v1.json`](fixtures/fix08-live-input.v1.json) declares
`input_data_is_synthetic: true`, `realised_history: false`). The response is a
real sample from the provider; the data it reasoned over is authored.

```bash
# One real call, no retry and no stub fallback. Refuses to overwrite by default.
python manage.py record_live_allocation
```

If the call fails, the command writes a failure artifact and exits non-zero; it
never substitutes a deterministic answer. The provider reported 5005 input and
3020 output tokens; billed cost is `null` (the adapter receives tokens, not
dollars) and the price-table estimate is `$0.002010`. The plan is `proposed`,
unapproved and untraded.
