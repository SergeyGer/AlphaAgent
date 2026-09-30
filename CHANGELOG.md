# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `make` targets for the common development workflows.

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

[Unreleased]: https://github.com/SergeyGer/AlphaAgent/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/SergeyGer/AlphaAgent/releases/tag/v1.0.0
