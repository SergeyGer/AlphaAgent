# AlphaAgent - offline demo (Streamlit)

An interactive, **offline** tour of AlphaAgent's risk guard: no API keys, no
database, no Docker, no network calls. The sliders drive the project's **real**
guard function, `services/execution.py::evaluate_proposal()` - the same code the
Celery worker, REST API and Telegram bot call before any trade reaches the
ledger. Nothing about the guardrails is reimplemented for the demo.

The page is shaped like the React terminal at `/api`: dark chrome, a top bar with
`LIVE` / `demo · MODERATE` pills, seven metric cards (the same order as the
dashboard), the two portfolio-ceiling meters, and four tabs.

## What it shows

1. **Guard playground** (first tab) - set cash, `max_trade_allocation_pct`,
   `daily_loss_limit_usd`, `max_portfolio_exposure_pct`,
   `max_ticker_concentration_pct`, realised P&L today, what else the portfolio
   holds, asset price, side and size; see the guard's verdict (`ALLOWED` /
   `REJECTED`), its verbatim reason string and the raw `GuardDecision` it returns.
   Nine one-click presets:
   - a normal trade, a clamp at the per-trade allocation ceiling, a ceiling that
     leaves no budget, a breached daily loss limit (including the fact that SELL
     still works while new risk is frozen) and an oversized SELL clamped to the
     held position;
   - a buy **clamped by the portfolio exposure ceiling**, a buy **clamped by the
     single-instrument concentration ceiling**, a portfolio where **both ceilings
     bind and the smaller headroom wins**, and a buy **refused because
     concentration is already at its ceiling**.

   The metric row is driven by the same inputs: equity, cash, positions, the
   P&L cards, the daily-loss budget, and the two ceiling meters all move with the
   sliders. `cost_basis` is the one display-only input - it exists for the
   UNREALISED P&L card and is never passed to the guard.
2. **The three-agent debate** the guard is guarding (About tab) - bull researcher,
   bear short-seller, CIO adjudicator - and the one-way pipeline from proposal to
   ledger.
3. **Decision quality** (tab) - outcome attribution, **explained but deliberately
   not executed**: decisions scored against forward returns at 1/7/30 days, scored
   by what the *action* was worth (a SELL is right when the price falls), HOLD
   excluded from the hit rate rather than counted as a miss, SPY as the benchmark.
4. **Position thesis** (tab) - **explained but deliberately not executed**: one
   written thesis plus an invalidation condition per open position, fed back to the
   agents on every run so an exit has to argue with the reason for entry.
5. **About** (tab) - the crew, the pipeline, the guard rules, the offline
   guarantees, the files actually exercised, the source proof, and the repository
   link.

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

## How the portfolio-level limits are driven

Every input the guard takes is supplied, **including `Exposure`**. That is the
point of the object: `evaluate_proposal` skips rules 6 and 7 - total exposure and
single-instrument concentration - when it is handed no exposure, so a page that
omitted it would drive only the per-trade half of the guard while looking
complete. The demo builds the three market values from the widgets, the same way
`services.portfolio_metrics.build_exposure()` derives them from the ledger - but
without fetching anything:

```
ticker_value_usd    = units held x price          this instrument, at market
positions_value_usd = that + other holdings       every holding, at market
equity_usd          = cash + positions_value_usd  the denominator for both ceilings
```

Both ceilings are percentages of that **equity** - not of cash, which is what
`max_trade_allocation_pct` uses - and a buy that does not fit is **sized down**,
with the note naming the ceiling that bound it. Only a buy with no headroom at
all is refused. Equity comes from `Exposure`, never from a database, so the page
keeps its no-database, no-network property.

**No new dependencies**: the exposure limits need nothing beyond the two pins
already in `demo/requirements.txt`.

## Known limitations

- **Attribution and theses are described, not executed.** `services/attribution.py`
  and `services/thesis.py` are deliberately **not imported**: both work through
  models that need a database (`DecisionOutcome`, `PositionThesis`) and attribution
  also reads price history from yfinance. The Decision quality and Position thesis
  tabs describe the real implementations and say on the page that they are not run.
  See the wiki:
  [Decision Attribution](https://github.com/SergeyGer/AlphaAgent/wiki/Decision-Attribution)
  and [Position Thesis](https://github.com/SergeyGer/AlphaAgent/wiki/Position-Thesis).
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
- The guard's **refusal** message, when a portfolio ceiling leaves no headroom, is
  its shared `Rejected: requested $X exceeds the $0.00 per-trade ceiling and
  cannot be sized down.` wording; only the *clamp* note names which ceiling bound
  the trade. The demo shows the guard's strings verbatim rather than paraphrasing
  them, so preset 9 reads that way.
- Not reachable from the widgets: rule 1 (the side selector offers BUY and SELL
  only, so a HOLD never becomes a proposal here), rule 2 (the price minimum is
  $0.01) and the BUY-versus-cash half of rule 4 (the per-trade ceiling is at most
  25% of cash). The app's "Which guard rules this page exercises" expander lists
  exactly what is and is not covered.

MIT licensed, like the rest of the project.
