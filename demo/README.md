# AlphaAgent - offline demo (Streamlit)

An interactive, **offline** tour of AlphaAgent's risk guard: no API keys, no
database, no Docker, no network calls. The sliders drive the project's **real**
guard function, `services/execution.py::evaluate_proposal()` - the same code the
Celery worker, REST API and Telegram bot call before any trade reaches the
ledger. Nothing about the guardrails is reimplemented for the demo.

## What it shows

1. **Guard playground** - set cash, `max_trade_allocation_pct`, `daily_loss_limit_usd`,
   realised P&L today, asset price, side and size; see the guard's verdict
   (`ALLOWED` / `REJECTED`), its verbatim reason string and the raw `GuardDecision`
   it returns. Five one-click presets cover a normal trade, a clamp at the
   allocation ceiling, a ceiling that leaves no budget, a breached daily loss
   limit (including the fact that SELL still works while new risk is frozen) and
   an oversized SELL clamped to the held position.
2. **The three-agent debate** the guard is guarding - bull researcher, bear
   short-seller, CIO adjudicator - and the one-way pipeline from proposal to ledger.
3. A link to the repository.

## Run locally

From the repository root (the demo imports `services/` and `core/` from there):

```bash
pip install -r demo/requirements.txt
streamlit run demo/app.py
```

## Deploy to Streamlit Community Cloud

1. <https://share.streamlit.io> → **Create app** → **Deploy a public app from GitHub**.
2. Repository: `SergeyGer/AlphaAgent`, branch: `main`.
3. **Main file path:** `demo/app.py`.
4. Deploy. **No secrets are required** - leave the secrets box empty.

Community Cloud picks the dependency file from the entrypoint's directory first
and only then from the repository root, so `demo/requirements.txt` (two pins) is
used instead of the platform's hash-pinned root lockfile. See
[App dependencies](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies).
If a build ever reports installing hundreds of packages, set the dependency file
explicitly in **Advanced settings** to `demo/requirements.txt`.

Python 3.12 is recommended (matching the project), selectable in the same
advanced settings.

## How it works without a database

`services/execution.py` imports Django models, so the app configures a minimal
Django settings module at import time (`INSTALLED_APPS` = contenttypes, auth,
core; `DATABASES = {}`) and calls the guard with **unsaved** `Portfolio` /
`PositionState` instances. The guard is a pure function over those objects, so a
database would add nothing - and `DATABASES = {}` makes an accidental query fail
loudly instead of silently.

## Known limitations

- The AI proposal is modelled by a two-field stand-in (`action`, `amount`) that
  mirrors `ai_agent.TradeProposal`. Importing the real Pydantic model would pull
  in CrewAI, requests and defusedxml for no behavioural difference - the guard
  reads only those two attributes.
- The asset price comes from a widget; in production it comes from `yfinance` and
  the guard rejects any trade without a live price.
- `apply_trade()` (the database-mutating half of `services/execution.py`) is not
  exercised: it needs PostgreSQL and a ledger. The demo covers the decision, not
  persistence.
- The LLM agents are explained but not run - that would require API keys.

MIT licensed, like the rest of the project.
