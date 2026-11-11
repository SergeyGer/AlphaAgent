# AlphaAgent offline viewer

Open `index.html` directly, or serve this directory:

```bash
cd docs/viewer
python3 -m http.server 8777
# http://127.0.0.1:8777/
```

No database, server application, login, model key or network access is needed.
The default fixture bundle is embedded in `index.html`; the relative
`decision-runs.v1.bundle.json` is also present for the served fetch path.

Use **Load bundle file** to open another versioned bundle or a redacted
`GET /api/decision-runs/<id>/export/` capture. Schema and SHA-256 checksums are
verified before anything is rendered. The source label (Real recorded,
Synthetic, Heuristic, Legacy) is shown on every run.

This page has no execution, approval or live-analysis capability. Its CSP and
fetch guard permit only the relative fixture bundle; external evidence links are
shown as text, never as clickable links.

Full build, bundle-format and review documentation:
`frontend/viewer/README.md`.
