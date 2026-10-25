# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet. See `[1.1.1]` below for what shipped.

## [1.1.1] — 2026-10-25

A security release. No new features and no behaviour changes: the execution guard,
the attribution ledger and the API are untouched.

### Security

- **`twisted` 26.4.0 → 26.10.0rc1** — HTTP request smuggling in `twisted.web` via a
  bare CR or LF in a header value. This is the one advisory against `twisted` that
  this project actually reaches: `daphne` imports `twisted.web.http` and
  `twisted.web.http_headers`, which is the HTTP parsing path the server runs on.
  The other four (IMAP4, IRC, conch Telnet, `twisted.words`) cover subsystems that
  are never loaded.
  Pinned to a **release candidate** deliberately, because `26.10.0rc1` is the only
  published release containing the fix and no stable `26.10.0` exists yet. The pin
  is explicit in `requirements.in` with the reason and a note to replace it, rather
  than reached implicitly through `daphne`'s `twisted[tls]>=22.4`.
- **`filelock` 4.0.12 → 4.2.4** — SQLite reopen of the lock database by name.
- **`virtualenv` 21.14.9 → 21.14.13** — unescaped `--prompt` value in the Bash
  activator.
- **`setuptools` 84.1.0 → 84.2.1** — Windows path escape via `namespace_packages.txt`.
- **`undici` 8.12.x → 8.12.3** (frontend) — denial of service via an unhandled
  error on socket close.

### Known remaining

- **`mcp` 1.28.1** — an unbounded session accumulation in the streamable-HTTP
  transport, patched in 1.30.0. Still blocked: `crewai` declares `mcp~=1.28.1`
  (`>=1.28.1,<1.29.0`), and no released `crewai` widens that range. The vulnerable
  code is in this project's execute path, so it is left visible rather than
  dismissed. Bounded by loopback binding and a required shared secret.

### Verified

528 backend tests and 115 frontend tests pass, and `daphne` serves on the release
candidate: `Listening on TCP address 0.0.0.0:8000`, dashboard returns 200.

## [1.1.0] — 2026-10-23

Three features, a security pass, and the infrastructure that makes the project
reviewable. Roughly 60 commits.

### Added

- **Decision outcome attribution** — every decision is scored against what the
  market did next, at 1, 7 and 30-day horizons. `DecisionOutcome` records forward
  return, the return *the action* implies, and SPY's return over the same window,
  so the scorecard reports **alpha** rather than a bare hit rate. A SELL is scored
  as correct when the price falls; a HOLD is `NULL` rather than `False`, because it
  makes no directional claim. Exposed as `GET /api/portfolio/attribution/` and a
  dashboard panel.
- **Cumulative exposure and concentration ceilings** — `max_portfolio_exposure_pct`
  and `max_ticker_concentration_pct` bound the whole portfolio, not one trade, so
  a sequence of individually-legal buys can no longer reach full investment. Both
  fold into the existing clamp, and every note names the ceiling that bound.
- **Persistent position thesis** — each open position carries a written thesis and
  an invalidation condition, written by the agent that opened it and fed back on
  every later run, so an exit has to argue with the reason for entry. Superseded
  rather than edited, so the history reads as a sequence of beliefs. Exposed as
  `GET /api/portfolio/thesis/`.
- **AI spend ceiling** (`AI_DAILY_SPEND_LIMIT_USD`) — the agent halts when the
  day's recorded cost exceeds the limit, instead of a runaway loop billing silently.
- **MCP authentication** — the tool server requires a shared secret compared with
  `hmac.compare_digest`; it will not construct with an empty one.
- **One-command start/stop** — `make start` / `stop` / `down` / `status`, mirrored
  as VS Code tasks so the workflow is identical on Windows and Linux, plus a
  generated Russian-language operator guide.
- **Dev Containers support** — `.devcontainer/` drives the project's own compose
  file rather than a parallel environment, with `shutdownAction: stopCompose` so
  closing the window releases the ports.
- **Multi-stack isolation** — `COMPOSE_PROJECT_NAME`, `ALPHA_CONTAINER_PREFIX` and
  per-service port variables, so two developers can run separate stacks on one
  host. Every value defaults to the single-developer case.
- **`LLM-Quality-and-Guardrails`** — a wiki page documenting the seven layers that
  contain unreliable model output, and the outcome ledger that measures it.
- **Documentation link tests** — every source link is checked offline against the
  checkout: the file must exist and the line anchor must land inside it.

### Changed

- **Model: Claude Haiku 5.5.** Roughly 90% cheaper per token and materially
  stronger. It rejects `temperature` with a 400, so sampling parameters are now
  omitted for models that do not accept them — a model swap that would otherwise
  have failed every run. Measured on a real run: **$0.161 → $0.0145**.
- **Redis requires authentication** (`--requirepass`), including in the demo file.
- **The data services bind loopback** (`127.0.0.1`) rather than every interface.
- **Tailwind 4 and Vite 8** on the frontend, Vite's Rolldown build replacing
  esbuild.
- **Hash-pinned Python lockfiles** (`requirements.txt`, `requirements-dev.txt`) so
  the dependency graph is visible to Dependabot.
- **The README leads with the result** — six badges instead of seventeen, a
  collapsible table of contents, and the measured hit rate stated as text rather
  than only inside a screenshot.

### Fixed

- **Decision scoring measured from the wrong price.** Forward returns started at
  the day's close while trades executed at the live quote — $166 apart on a real
  BTC decision, biased in the direction that flattered BUYs. Correcting it made the
  published numbers *worse*, which is why it is recorded rather than quietly
  applied.
- **`DecisionOutcome` could not be attributed for HOLD and HALT rows** —
  `AgentDecisionLog` had no ticker of its own, so every non-trading decision had
  none. Now a real indexed column, backfilled.
- **Scheduled snapshots never emitted** even though the task captured them.
- **The guard's refusal named the wrong limit** — every refusal said "per-trade
  ceiling", including refusals caused by the concentration ceiling.
- **Twenty-two CodeQL alerts**, including log injection in two sinks, a missing
  explicit return in the MCP guard, and implicit string concatenation in a list.
- **The paginated thesis envelope** was read as a bare array, so the panel rendered
  nothing at all — no error, no warning, just an absent section.

### Security

- **PostgreSQL and Redis were reachable from the public internet.** Both were
  published on `0.0.0.0` with no host firewall; PostgreSQL completed a handshake
  from outside and had been reachable that way for two weeks. Now bound to
  loopback, and enforced by a test that resolves the Compose configuration.
- **Redis ran unauthenticated** on a published port.
- **Dependency advisories** — Pillow, certifi and Vite patched; the remaining
  `mcp`, `twisted` and `uv` findings are documented with their reachability
  assessed rather than silently dismissed.
- **`main` is protected by a repository ruleset** with five required status
  checks, a pull-request rule, and no force-push.

## [1.0.0] — 2026-09-30

Initial release: a four-tier autonomous investment platform.

### Added

- **Domain model** (`core/models.py`) — `Portfolio`, `Asset`, `Transaction` and
  `AgentDecisionLog`, with database-level `CHECK` constraints, a
  `UNIQUE(portfolio, ticker)` constraint and hot-path indexes.
- **REST API** — token-authenticated endpoints for the portfolio snapshot with
  live metrics, the safety-gated autonomy toggle, the AI decision audit trail,
  the trade ledger and on-demand agent sweeps. Query counts are bounded and
  asserted in tests.
- **AI layer** (`ai_agent.py`) — a sequential three-agent CrewAI crew running
  an adversarial debate (bullish analyst → risk assessor / short seller → CIO
  adjudicator) whose tools are strictly read-only. The output contract is
  enforced twice: a Pydantic `TradeProposal` model and a CrewAI task guardrail
  that forces a retry on malformed JSON. Both arguments are persisted on the
  decision log (`bull_case`, `bear_case`) so every verdict is auditable.
- **Deterministic fallback engine** — when no LLM credential is configured, or
  the provider fails, decisions come from an auditable rule engine instead of
  crashing the pipeline.
- **Async engine** (`tasks.py`) — Celery Beat fans out one independent subtask
  per `(portfolio, ticker)` pair via a single `group` publish, with a per-trade
  execution guard, row-locked execution, a daily stop-loss sweep and
  housekeeping.
- **Ledger accounting** (`services/ledger.py`) — exact weighted-average-cost
  replay deriving positions and realised P&L without a snapshot table.
- **Market and news services** — cached `yfinance` quotes with graceful
  degradation to stored cost basis, and RSS scraping hardened with `defusedxml`.
- **Explainability** — every agent run, including HOLDs, blocked trades and
  crashes, writes an `AgentDecisionLog` row with the full chain of thought,
  token usage and cost.
- **Container stack** — `Dockerfile` plus `web`, `worker` and `beat` Compose
  services with healthchecks and restart policies.
- **Operations** — `seed_demo` to provision a demo account, and `dry_run_agent`
  to exercise the live LLM without writing to the database.
- **Security** — no credential fallbacks in settings, mandatory environment
  secrets, and a CI secret scan over every publishable file.
- **CI** — Ruff lint/format, the full test suite against PostgreSQL 16 and
  Redis 7, migration-drift detection and a Docker build smoke test.

[Unreleased]: https://github.com/SergeyGer/AlphaAgent/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/SergeyGer/AlphaAgent/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/SergeyGer/AlphaAgent/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/SergeyGer/AlphaAgent/releases/tag/v1.0.0
