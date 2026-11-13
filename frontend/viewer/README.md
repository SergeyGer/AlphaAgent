# AlphaAgent offline decision viewer (REP-02)

A static, read-only page that replays a **versioned, checksummed, redacted**
decision-run bundle. It needs no Django process, no PostgreSQL, no Redis, no
login, no API key and no network connection.

It reuses the real `DecisionDetail`, `DebateTimeline`, `ReplayControls` and
`EvidenceDrawer` components from `frontend/src/`. The replay scene is the same
as-of-sequence reduction the authenticated application uses
(`buildAsOfPresentation`), so the two surfaces cannot drift into two different
explanations of one run.

## Build and run

```bash
cd /root/projects/AlphaAgent
npm --prefix frontend run build:viewer
cd docs/viewer
python3 -m http.server 8777
# open http://127.0.0.1:8777/
```

`build:viewer`:

1. converts `docs/spec/fixtures/decision-runs.v1.json` into
   `frontend/viewer/public/decision-runs.v1.bundle.json`;
2. recomputes the manifest's per-fixture checksums first and refuses to build
   from a modified fixture;
3. runs the free-text/URL export review;
4. builds with `vite.viewer.config.ts`;
5. inlines the JS and CSS into `docs/viewer/index.html` and embeds the default
   bundle, then deletes the separate JS/CSS chunks.

The result is a self-contained `docs/viewer/index.html` plus the relative
`decision-runs.v1.bundle.json`. Opening `index.html` directly from disk works
because the default bundle is embedded; a served copy additionally fetches the
relative JSON file, so the fetch path is exercised in the documented launch.

## Importing another bundle

Use **Load bundle file**. The file is read locally; nothing is uploaded.

Accepted formats:

| `bundle_kind` | Shape |
| --- | --- |
| `alphaagent.offline-viewer` | Multi-run fixture bundle (`bundle_version` + `schema_version`, per-run checksums, envelope checksum) |
| `alphaagent.decision-run` | The redacted export from `GET /api/decision-runs/<id>/export/` (one run, checksum over the object with the checksum member removed) |

Validation is all-or-nothing. The viewer refuses:

- a missing or unsupported `bundle_version` / `schema_version`;
- an unknown `bundle_kind`;
- an envelope checksum mismatch;
- a per-run checksum mismatch;
- a malformed JSON document or a missing detail/events array.

The rejection message names the reason and the file is not partially rendered.
When a previous bundle is already loaded, a rejected file leaves it untouched.

Checksums are SHA-256 over sorted-key, compact, UTF-8 JSON with the `checksum`
member removed. The browser implementation is pure JavaScript so `file://` and
non-secure contexts work; it is verified against the standard SHA-256 test
vectors.

## Source labels

Every run carries an explicit label; it is not inferred from the ticker or the
file name:

| Label | Meaning |
| --- | --- |
| **Real recorded** | The run record says `run.origin.kind: "provider_run"` with `provider_backed: true`. A recorded/live mode alone is not enough: only a provider-backed run record establishes that a debate happened. The viewer cannot independently re-authenticate the recording; it displays the label the bundle declares. |
| **Synthetic** | Authored fixture data (`authored_fixture`), including an older export that carries a `fixture_id` on a recorded label. Not a market decision; no outcome should be inferred. |
| **Heuristic** | The deterministic fallback. The bull/bear debate did not run, so absent claims are absent by construction. |
| **Legacy** | A report recorded before the claim/evidence/rebuttal protocol. Missing sections were never produced in this shape. |

Every run also shows its `run.origin` kind, whether it is provider-backed, and
the origin's limitations. Three claims stay distinct: the bundle/export
`checksum` is integrity, `recording` is build verification, and `run.origin` is
the content producer. A verified checksum or recording never upgrades an authored
sample to a provider debate.

The committed fixture bundle is entirely **Synthetic**, per the manifest's own
honesty block. The committed API export sample
(`docs/spec/fixtures/decision-run-export.v1.json`) is authored and therefore also
Synthetic; the superseded recorded-label sample is retained as a conformance file
and imports as Synthetic. The real-recorded path is exercised by importing
`decision-run-export.provider-backed.v1.json`, which is explicitly test data for
the Step 11 recording path. `viewer/bundle.test.ts`,
`viewer/exportParity.test.ts` and `viewer/ViewerRunView.test.tsx` test the
labels independently.

## What it deliberately cannot do

- No execution, approval, autonomy toggle, recommendation decision, run-agent
  command or live refresh is present. There is no import of `api/client`.
- No provider endpoint, API base URL, WebSocket URL or model key is compiled in.
- External `http(s)` evidence links are rendered as non-clickable text; the
  `EvidenceDrawer` receives `allowExternalLinks={false}`.
- The page enforces a CSP (`default-src 'none'`, no external script/style/font/
  image/connect) and installs a `fetch` guard that permits only the relative
  fixture bundle.
- The export review scans **every string**, not only sensitive key names, for
  credentials, emails, external URLs, loopback hosts and developer paths, and
  parses URL fields against an allowed `fixture:` scheme.

## Export review

```bash
cd /root/projects/AlphaAgent/frontend
npm run viewer:review
# REVIEW OK: viewer/public/decision-runs.v1.bundle.json
```

The review is part of `build:viewer`; a finding exits non-zero and stops the
build before any HTML is produced.

## Limitations

- The viewer replays stored payloads. It does **not** re-evaluate the guard, re-run
  attribution or verify that a synthetic outcome is a measurement.
- A `real recorded` label is a bundle claim. Authenticity is established by the
  export endpoint and the reviewer's handling of the file, not by this page.
- The committed default bundle is synthetic; no authentic recorded run is
  committed to the repository.
- The fixture conversion assigns deterministic ids so event payloads can join
  claims, rebuttals, evidence, guard and outcomes. It does not invent any
  narrative content from the manifest.
