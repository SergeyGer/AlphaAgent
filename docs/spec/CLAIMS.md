# Claims index — every public claim and its evidence

Phase 5's gate is: **all public claims trace to reviewed evidence.** This file is
that gate in practice. It lists the substantive, checkable statements the project
makes in `README.md` and `docs/wiki/`, names the repository file that supports
each one, and records the claims that had to be corrected or removed.

The rule for adding a claim is in
[`CONTRIBUTING.md`](../../CONTRIBUTING.md#a-public-claim-must-trace-to-a-committed-file):
add a row here, cite a committed file, and update the index in the same pull
request. If there is no source, fix the claim.

## How to read the evidence column

| Class | Meaning | Can a reviewer reproduce it? |
| --- | --- | --- |
| **A — fixture/report** | A committed fixture or report contains the claim, including its numbers | Yes, with the commands in [`DEMO.md`](DEMO.md) |
| **B — code/config/test** | The behaviour is implemented and pinned by a named test, setting or workflow | By reading the file and running the test |
| **C — recorded narrative** | A committed changelog, ADR or incident entry records a past observation whose raw inputs are not committed | No. It is context, not measurement |
| **D — no source** | The claim could not be supported | It was corrected or removed in this change |

Class C is used sparingly and is never the basis of a performance claim. Where a
number mattered to a public conclusion, it was either moved into a class A
fixture or removed.

## 1. README claims

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| R-01 | The project is an AI investment platform with deterministic risk governance and verifiable decision attribution | `README.md` — title | `services/execution.py`; `services/debate_protocol.py`; `docs/spec/METHODOLOGY.md` §1–§2 | B |
| R-02 | It answers what the agent decided, why, and whether it was right | `README.md` — introduction | `core/models.py` (`AgentDecisionLog`, `DecisionOutcome`); `docs/wiki/Decision-Attribution.md` | B |
| R-03 | Decisions that name an instrument are evaluated at 1, 7 and 30 days when the horizon matures and a permitted price exists; otherwise the outcome is absent | `README.md` — pillar 1; attribution section | `services/attribution.py` (`evaluate_pending`); `core/models.py` (`DecisionOutcome.HORIZONS`) | B |
| R-04 | A HOLD has a zero decision return by convention but no directional hit | `README.md` — pillar 1 | `services/attribution.py` (`directional_hit`, `signed_return`); `core/tests/test_attribution.py` | B |
| R-05 | Missing prices and benchmarks are null, never zero | `README.md` — pillar 1 | `services/attribution.py`; `core/tests/test_attribution.py`; `docs/wiki/Decision-Attribution.md` | B |
| R-06 | A debate-produced decision records both cases, tokens and API cost, and links to its transaction when executed; a heuristic fallback is a separate run with no claims | `README.md` — pillar 1 | `core/models.py` (`AgentDecisionLog`); `tasks.py`; `core/decision_models.py` (`DecisionRun`); `docs/wiki/Data-Model.md` | B |
| R-07 | The execution guard is a pure function with no I/O, and a malformed proposal is clamped or rejected | `README.md` — pillar 2 | `services/execution.py` (`evaluate_proposal`); `core/tests/test_guardrails.py` | B |
| R-08 | A refusal names the limit that bound it | `README.md` — pillar 2 | `services/execution.py`; `core/tests/test_exposure_limits.py` | B |
| R-09 | Bull and bear cases are independent and a separate CIO adjudicates | `README.md` — pillar 3 | `services/debate_protocol.py`; `core/tests/test_debate_protocol.py` | B |
| R-10 | Both sides are instructed to concede when their case is weak | `README.md` — pillar 3 | `services/debate_protocol.py` prompts; `docs/spec/fixtures/decision-runs.v1.json` (`disagreement-tsla-2026-02-04`, a `qualify` rebuttal) | A/B |
| R-11 | The transcript is stored with the decision | `README.md` — pillar 3 | `core/decision_models.py` (`Claim`, `Rebuttal`); `core/models.py` (`bull_case`, `bear_case`) | B |
| R-12 | The walkthrough video is a recording of the running system | `README.md` — See it running | `docs/demo-walkthrough.mp4` (media file; its contents are not machine-checkable) | media |
| R-13 | The Streamlit page drives the real `services/execution.py` guard, with no installation or key | `README.md` — See it running | `demo/app.py`; `core/tests/test_demo_theme_contract.py` | B |
| R-14 | The GIF was recorded from the live React application, not the Streamlit demo | `README.md` — See it running | `docs/demo.gif` (media file; the caption is a provenance statement, not a code claim) | media |
| R-15 | The dashboard shows the decision scorecard to a signed-in visitor | `README.md` — attribution section | `frontend/src/components/DecisionQualityPanel.tsx`; `core/views.py` (`DecisionScorecardView`) | B |
| R-16 | The scorecard reports directional hit rate, average decision return and alpha vs SPY per horizon | `README.md` — attribution table | `services/attribution.py` (`Scorecard.as_dict`); `core/views.py`; `frontend/src/types.ts` | B |
| R-17 | A HOLD is excluded from the hit-rate denominator | `README.md` — attribution table | `services/attribution.py`; `frontend/src/components/DecisionQualityPanel.tsx` | B |
| R-18 | The scorecard is published as measured, not curated | `README.md` — attribution section | Design statement supported by the panel's null/zero handling and `docs/wiki/Decision-Attribution.md`; no number is claimed | B |
| R-19 | A start-price flaw in the scoring biased toward flattering BUY calls | `README.md` — end of attribution section | `services/attribution.py` (`_start_price` docstring); `CHANGELOG.md` (1.1.0, "Decision scoring measured from the wrong price") | B/C |
| R-20 | The dashboard screenshot shows equity, allocation, reasoning, approvals, trades and news | `README.md` — screenshots | `docs/screenshots/dashboard.png` (media file) | media |
| R-21 | The risk-ceiling panel closes the INC-001 over-concentration gap | `README.md` — screenshots | `docs/wiki/Incident-Log.md` INC-001; `services/execution.py` (`_portfolio_headroom`); `docs/adr/0015-cumulative-exposure-and-concentration-limits.md` | B |
| R-22 | Each position has a thesis and an invalidation condition, fed back to the agents | `README.md` — screenshots | `core/models.py` (`PositionThesis`); `services/thesis.py`; `core/tests/test_thesis.py` | B |
| R-23 | Expanding a decision shows bull, bear and CIO | `README.md` — screenshots | `frontend/src/components/ThoughtsFeed.tsx` (as shipped); Phase 1/2 detail API; `docs/wiki/Data-Model.md` | B |
| R-24 | The stack runs with `docker compose -f docker-compose.ghcr.yml up`, on host port 8080 | `README.md` — Run it yourself | `docker-compose.ghcr.yml` (`${ALPHA_WEB_PORT:-8080}:8000`) | B |
| R-25 | No service restarts itself; no `restart: always` or `unless-stopped` | `README.md` — Run it yourself | `docker-compose.yml` and `docker-compose.ghcr.yml` (`restart: "no"`) | B |
| R-26 | The demo login is `demo` / `demo-pass-123` | `README.md` — Run it yourself | `core/management/commands/seed_demo.py` (defaults) | B |
| R-27 | No API key is needed; a deterministic engine keeps the pipeline running | `README.md` — Run it yourself | `ai_agent.py` (`HeuristicDecisionEngine`); `core/tests/test_ai_agent.py`; `services/debate_protocol.py` fallback contract | B |
| R-28 | The request path is bull/bear → CIO → guard → ledger or audit trail | `README.md` — How it works | `services/debate_protocol.py`; `tasks.py`; `services/execution.py` | B |
| R-29 | The guard has no knowledge of language models | `README.md` — Why a guard | `services/execution.py` imports no LLM module; `core/tests/test_guardrails.py` | B |
| R-30 | The wiki links under "Under the hood" exist | `README.md` — Under the hood | `docs/wiki/*.md`; `docs/adr/README.md` | B |
| R-31 | There are seventeen ADRs, each with rejected alternatives | `README.md` — Choose your path; Under the hood | `docs/adr/README.md` index rows 0001–0017; `docs/adr/*.md` | B |
| R-32 | The incident log contains eleven real defects | `README.md` — Choose your path | `docs/wiki/Incident-Log.md` INC-001–INC-011 | C |
| R-33 | The stack is Python 3.12+, Django 5.2, PostgreSQL 16, Redis 7, Celery 5, React 18, TypeScript 5, Tailwind 4 | `README.md` — Under the hood badges | `pyproject.toml` (`requires-python = ">=3.12"`); `requirements.txt`; `frontend/package.json`; `docker-compose.yml`; `Dockerfile` (image uses Python 3.13) | B |
| R-34 | The roadmap items are tracked as GitHub issues #27–#35 | `README.md` — Roadmap | External issue links; the repository cannot verify issue state offline | external |
| R-35 | The project is MIT licensed | `README.md` — footer | `LICENSE` | B |
| R-36 | The README has nine sections | `README.md` — Contents | The nine `##` headings in the file | B |
| R-37 | The quality-and-testing link points at a real page | `README.md` — Choose your path | `docs/wiki/Quality-and-Testing.md` | B |

## 2. Wiki claims

### Home

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-H-01 | AI agents propose and deterministic code decides | `Home.md` — introduction | `services/execution.py`; `services/debate_protocol.py` | B |
| W-H-02 | The walkthrough is recorded from the running system | `Home.md` — Watch first | `docs/demo-walkthrough.mp4` (media) | media |
| W-H-03 | Current version is 1.1.7 (2026-11-11); 1.1.5/1.1.6/1.1.7 correspond to Phases 1/2/3 | `Home.md` — Current version | `CHANGELOG.md` entries 1.1.5–1.1.7 | C |
| W-H-04 | Engineering-Decisions has eight long-form records; the ADR index has seventeen | `Home.md` — If you are evaluating | `docs/wiki/Engineering-Decisions.md` (ADR-001–008 sections); `docs/adr/README.md` | B |
| W-H-05 | Celery Beat wakes every fifteen minutes and fans out one subtask per portfolio and instrument | `Home.md` — The system; `Architecture.md`; `Operations.md` | `config/settings.py` (`autonomous-market-monitoring`, `*/15`); `tasks.py` (`group`) | B |
| W-H-06 | A pure guard checks allocation, cash, concentration and daily loss before the ledger | `Home.md` — The system | `services/execution.py`; `core/tests/test_guardrails.py` | B |
| W-H-07 | Missing market data does not abort the run; the guard refuses any execution without a positive price | `Home.md` — Design principles | `tasks.py` (price may be null); `services/execution.py`; `ai_agent.py` heuristic engine returns HOLD when it cannot size | B |
| W-H-08 | No LLM credential still exercises the full pipeline with the deterministic engine | `Home.md` — Design principles | `ai_agent.py`; `core/tests/test_ai_agent.py`; `core/tests/test_tasks.py` | B |

### Architecture

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-A-01 | Six containers, four logical tiers; the web tier is the only one published on all interfaces | `Architecture.md` — System overview | `docker-compose.yml`: `db` and `redis` bind `127.0.0.1`, `web` publishes `${ALPHA_WEB_PORT}`, `mcp` is `expose` only | B |
| W-A-02 | Daphne serves HTTP and WebSockets on one port; gunicorn cannot hold a WebSocket | `Architecture.md` — Why ASGI | `Dockerfile` (`daphne`); `config/asgi.py`; `docs/wiki/Incident-Log.md` INC-002 | B |
| W-A-03 | The AI layer produces a proposal; it never writes to the database | `Architecture.md` — Capabilities; step 5 | `tasks.py` persistence; `services/execution.py`; ADR-0001 | B |
| W-A-04 | The pipeline is autonomy/cooldown → context → debate → guard → atomic write → audit → event → snapshot | `Architecture.md` — decision pipeline | `tasks.py`; `services/debate_protocol.py`; `services/events.py`; `services/snapshots.py` | B |
| W-A-05 | Fan-out uses a Celery group, one task per `(portfolio, ticker)` | `Architecture.md` — Fan-out | `tasks.py` (`autonomous_market_monitoring_task`) | B |
| W-A-06 | The Phase 2 producer is `services/debate_protocol.py`; the legacy sequential crew remains behind a flag | `Architecture.md` — Capabilities; step 3 | `ai_agent.py` (`run_protocol`); `services/debate_protocol.py` | B |
| W-A-07 | Project layout lists the actual modules and management commands | `Architecture.md` — Project layout | `core/`, `services/`, `core/management/commands/` | B |

### Agent-Debate

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-D-01 | The default producer is the bounded Phase 2 protocol; the public entry point is `ai_agent.run_alpha_agent` | `Agent-Debate.md` — introduction | `ai_agent.py`; `services/debate_protocol.py` | B |
| W-D-02 | First cases are independent: neither prompt contains the other side's output | `Agent-Debate.md` — Why adversarial | `services/debate_protocol.py` (prompt construction); `core/tests/test_debate_protocol.py` (`IndependentFirstRoundTests`) | B |
| W-D-03 | Both agents receive the same evidence catalog, not different tools | `Agent-Debate.md` — evidence table | `services/debate_protocol.py`; the pre-Phase-2 tool split is still visible in `ai_agent.py` for the legacy path | B |
| W-D-04 | One rebuttal round; a concession is a valid output | `Agent-Debate.md` — Prompting | `services/debate_protocol.py`; `core/tests/test_debate_protocol.py` (`RebuttalRoundTests`) | B |
| W-D-05 | Stage output is schema-validated with at most one bounded repair | `Agent-Debate.md` — Output contract | `services/debate_protocol.py`; `core/tests/test_debate_protocol.py` | B |
| W-D-06 | Claims and rebuttals are persisted as `DecisionRun`/`Claim`/`Rebuttal`; legacy text columns are rendered from them | `Agent-Debate.md` — What the debate produces | `core/decision_models.py`; `ai_agent.py` (`_case_text`); `services/evidence.py` | B |
| W-D-07 | A heuristic fallback is a separate run with no claims or rebuttals | `Agent-Debate.md` — Graceful degradation | `services/debate_protocol.py`; `ai_agent.py`; `core/tests/test_debate_protocol.py` (`FailurePathTests`) | B |
| W-D-08 | Provider-specific request requirements are applied by an interceptor hook | `Agent-Debate.md` — Provider abstraction | `services/llm_hooks.py`; `core/tests/test_llm_hooks.py` | B |

### Position-Thesis

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-T-01 | Exactly one open thesis per `(portfolio, ticker)`, enforced by a unique constraint | `Position-Thesis.md` — The model | `core/models.py` (`one_open_thesis_per_portfolio_ticker`); `core/tests/test_thesis.py` | B |
| W-T-02 | A superseded thesis is closed and kept, not overwritten | `Position-Thesis.md` — Superseded | `services/thesis.py`; `core/tests/test_thesis.py` | B |
| W-T-03 | A thesis is recorded only after an executed trade, and a blank thesis is not recorded | `Position-Thesis.md` — Lifecycle | `tasks.py`; `services/thesis.py`; `core/tests/test_thesis.py` | B |
| W-T-04 | The thesis is injected into the CIO brief; absence is stated explicitly | `Position-Thesis.md` — What the agents see | `ai_agent.py` (`_thesis_block`); `core/tests/test_thesis.py` | B |
| W-T-05 | Stale, model-written and unverified theses are known limitations | `Position-Thesis.md` — Limitations | Stated as a limitation; no contrary claim | B |

### Execution-Guard

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-E-01 | The guard is a pure function with no I/O and no model | `Execution-Guard.md` — Why it exists | `services/execution.py`; `core/tests/test_guardrails.py` | B |
| W-E-02 | The rule table: HOLD, price, amount, budget, daily loss, per-trade clamp, cash, SELL position | `Execution-Guard.md` — The rules | `services/execution.py` (docstring groups seven rules; the table splits BUY/SELL branches); `core/tests/test_guardrails.py` | B |
| W-E-03 | A breached daily loss limit blocks BUY but permits SELL | `Execution-Guard.md` — Rule 5 | `services/execution.py`; `core/tests/test_guardrails.py` | B |
| W-E-04 | Oversized trades are clamped; the note names the binding ceiling | `Execution-Guard.md` — Rules 6 and 9 | `services/execution.py`; `core/tests/test_exposure_limits.py` | B |
| W-E-05 | `apply_trade` is the only mutating path and revalidates under a row lock | `Execution-Guard.md` — The only mutating path | `services/execution.py` (`apply_trade`); ADR-0003; `core/tests/test_recommendations.py` | B |
| W-E-06 | Approving a recommendation re-runs the guard at a fresh price | `Execution-Guard.md` — Human approval | `services/recommendations.py`; `core/tests/test_recommendations.py` | B |
| W-E-07 | Two portfolio-level ceilings are percentages of equity, default 90% and 30% | `Execution-Guard.md` — Portfolio-level limits | `core/models.py`; `services/execution.py` (`_portfolio_headroom`); ADR-0015 | B |
| W-E-08 | Omitting `exposure` skips both ceilings; the scheduled and approval callers supply it, and Phase 4 `apply_plan` does too | `Execution-Guard.md` — The exposure parameter | `services/execution.py`; `core/tests/test_exposure_limits.py` (`ExposureWiringTests`); `services/portfolio_allocation.py` | B |
| W-E-09 | The AI-spend ceiling is separate from the trading guard | `Execution-Guard.md` — What this guard does not bound | `services/budget.py`; `config/settings.py` (`AI_DAILY_SPEND_LIMIT_USD`); ADR-0012 | B |

### Decision-Attribution

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-DA-01 | A SELL scores positively when the price falls; a HOLD has a null directional hit | `Decision-Attribution.md` — Two framing choices | `services/attribution.py`; `core/tests/test_attribution.py` | B |
| W-DA-02 | A missing benchmark is null, never zero | `Decision-Attribution.md` — Two framing choices | `services/attribution.py`; `core/tests/test_attribution.py` | B |
| W-DA-03 | Horizons are separate rows (1, 7, 30) | `Decision-Attribution.md` — Horizons | `core/models.py` (`DecisionOutcome.HORIZONS`) | B |
| W-DA-04 | The live scorecard is computed at read time and is not committed; no figure is quoted | `Decision-Attribution.md` — Horizons; The published scorecard | `services/attribution.py` (`build_scorecard`); `core/views.py`; the page was corrected to remove the untraceable figures | B |
| W-DA-05 | A migration backfilled 62 ticker rows; 1,247 pre-migration HOLD/HALT rows stay blank and unscorable | `Decision-Attribution.md` — The ticker column | `docs/adr/0014-decision-outcome-attribution.md`; `core/migrations/0006_agentdecisionlog_ticker.py`; the counts are recorded, not reproducible | C |
| W-DA-06 | The outcome evaluator runs hourly on Beat | `Decision-Attribution.md` — Running it | `config/settings.py` (`evaluate-decision-outcomes`, minute 25) | B |
| W-DA-07 | The measure is per-decision, not portfolio-level; the benchmark is naive; the sample is small | `Decision-Attribution.md` — What it does not measure | Design limitations; `docs/spec/METHODOLOGY.md` §1, §7 | B |
| W-DA-08 | The start-price flaw and its correction are documented | `Decision-Attribution.md` — The published scorecard | `services/attribution.py`; `CHANGELOG.md` 1.1.0 | B/C |

### LLM-Quality-and-Guardrails

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-L-01 | The only structure the AI emits is `TradeProposal`; tools are read-only | `LLM-Quality-and-Guardrails.md` §1 | `ai_agent.py`; `services/execution.py`; ADR-0001 | B |
| W-L-02 | Pydantic constraints reject invented verbs, negative quantities and empty reasoning | `LLM-Quality-and-Guardrails.md` §2 | `ai_agent.py`; `core/tests/test_ai_agent.py` | B |
| W-L-03 | `parse_proposal` returns `None` rather than defaulting to HOLD | `LLM-Quality-and-Guardrails.md` §3 | `ai_agent.py` (`parse_proposal`); `core/tests/test_ai_agent.py` | B |
| W-L-04 | The heuristic fallback is deterministic and offline; the switch is recorded on the decision run (`mode`/`source`) and the task result, not on the legacy log | `LLM-Quality-and-Guardrails.md` §4 | `ai_agent.py`; `core/decision_models.py` (`DecisionRun.mode`, `.source`); `tasks.py` (`agent_source`); `core/tests/test_ai_agent.py` | B |
| W-L-05 | The guard can approve, clamp, reject or halt; the message names the constraint | `LLM-Quality-and-Guardrails.md` §5 | `services/execution.py`; `core/tests/test_guardrails.py` | B |
| W-L-06 | Sentiment is computed from a finance lexicon with a stated confidence formula | `LLM-Quality-and-Guardrails.md` §6 | `services/sentiment.py`; `core/tests/test_services.py` | B |
| W-L-07 | The debate is bull, bear and CIO; the bear is permitted to report no credible case | `LLM-Quality-and-Guardrails.md` §7 | `services/debate_protocol.py`; `core/tests/test_debate_protocol.py`; `docs/spec/fixtures/decision-runs.v1.json` (synthetic rebuttals) | A/B |
| W-L-08 | A thesis is prior belief and is superseded, not edited | `LLM-Quality-and-Guardrails.md` §8 | `core/models.py`; `services/thesis.py`; `core/tests/test_thesis.py` | B |
| W-L-09 | Decision outcomes are scored at 1, 7 and 30 days; the dashboard scorecard is live and not committed | `LLM-Quality-and-Guardrails.md` §9 | `services/attribution.py`; `core/models.py`; the page was corrected to remove untraceable figures | B |
| W-L-10 | No live scorecard figure is quoted; a committed measurement report exists but is synthetic | `LLM-Quality-and-Guardrails.md` §9 | `docs/spec/fixtures/experiments/phase3-engineering.v1.report.json`; `docs/spec/METHODOLOGY.md` §0 | A |

### Quality-and-Testing

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-Q-01 | The backend suite has 716 tests in 28 modules at the Phase 0-4 baseline; the frontend suite has 178 tests in 16 files; the runner's printed total is authoritative | `Quality-and-Testing.md` — Measured | `core/tests/`; `frontend/src/**/*.test.*`; reproducible by `manage.py test core.tests` and `npm test` (not stored in a fixture) | B (snapshot) |
| W-Q-02 | CI runs four jobs in `ci.yml`: lint, test, frontend, docker | `Quality-and-Testing.md` — CI pipeline | `.github/workflows/ci.yml` job names | B |
| W-Q-03 | The backend coverage floor is 70% | `Quality-and-Testing.md` — CI pipeline; Quality signals | `.github/workflows/ci.yml` (`coverage report --fail-under=70`) | B |
| W-Q-04 | The frontend has two coverage floors: `src/lib` 75% and a global ratchet | `Quality-and-Testing.md` — Frontend tests | `frontend/vitest.config.ts` thresholds | B |
| W-Q-05 | Tests make no external network calls; yfinance, RSS, LLM and Redis are stubbed | `Quality-and-Testing.md` — How external services are isolated | `core/tests/helpers.py`; `core/tests/test_services.py`; `core/tests/test_llm_hooks.py` | B |
| W-Q-06 | CodeQL, Dependabot and the secret scan are configured | `Quality-and-Testing.md` — Static analysis | `.github/workflows/codeql.yml`; `.github/dependabot.yml`; `core/tests/test_security.py` | B |
| W-Q-07 | The dependency files are hash-pinned lockfiles | `Quality-and-Testing.md` — Dependency locking | `requirements.txt`, `requirements-dev.txt`, `requirements.in`, `requirements-dev.in` | B |
| W-Q-08 | A count quoted in several files drifts; the README states none and this page plus CONTRIBUTING carry the snapshot | `Quality-and-Testing.md` — Quality signals | `README.md` (no test count); `CONTRIBUTING.md` (716); this index | B |
| W-Q-09 | The project had 22 CodeQL findings, all resolved | `Quality-and-Testing.md` — Static analysis | `CHANGELOG.md` 1.1.0 ("Twenty-two CodeQL alerts"); historical, not reproducible | C |
| W-Q-10 | The `chromadb` advisory triage was recorded when no patched version existed; the lockfile has moved on | `Quality-and-Testing.md` — What enabling alerts revealed | `requirements.txt` (`chromadb==1.5.9`); `CHANGELOG.md`; the page now says to re-check the advisory database | C/B |

### Operations

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-O-01 | `main` is protected by a ruleset with five required checks | `Operations.md` — Branch protection | External repository ruleset; the five job names exist in `.github/workflows/`; the rule itself is not in the repository | external |
| W-O-02 | The MCP streamable-HTTP advisory is closed: `mcp` 1.30.0 is pinned and the `crewai` release that unblocked it declares `mcp<2,>=1.30.0` | `Operations.md` — Closed advisory | `requirements.txt` (`mcp==1.30.0`, `crewai==1.15.33`); `CHANGELOG.md` 1.1.2 (records `crewai` 1.15.32); `docs/wiki/MCP-Tool-Server.md` | B |
| W-O-03 | `db` and `redis` bind 127.0.0.1; the MCP port is not published; web publishes `ALPHA_WEB_PORT` | `Operations.md` — Network exposure | `docker-compose.yml` | B |
| W-O-04 | The Beat schedule has six entries, including the hourly outcome evaluator | `Operations.md` — Scheduled jobs | `config/settings.py` (`CELERY_BEAT_SCHEDULE`) | B |
| W-O-05 | Per-ticker cooldown is 900 s and sweep debounce is 60 s | `Operations.md` — Sweep throttling | `config/settings.py`; `services/throttle.py` | B |
| W-O-06 | A throttle failure fails open, because the guard is the authoritative control | `Operations.md` — Sweep throttling | `services/throttle.py`; ADR-0006; `core/tests/test_throttle.py` | B |
| W-O-07 | Two stacks can run on one host through five isolation variables | `Operations.md` — Two stacks | `.env.example`; `docker-compose.yml` | B |
| W-O-08 | No service sets a restart policy | `Operations.md` — Automatic start | `docker-compose.yml`; `docker-compose.ghcr.yml` | B |

### Configuration

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-C-01 | `.env` is loaded without overriding the process environment; typed helpers have the stated fallbacks | `Configuration.md` — How values are loaded | `config/settings.py` (`load_dotenv`, `env_*`) | B |
| W-C-02 | `POSTGRES_PASSWORD` is mandatory; `DJANGO_SECRET_KEY` is required when debug is off | `Configuration.md` — Security-critical | `config/settings.py`; `core/tests/test_security.py` | B |
| W-C-03 | The four portfolio limits default to 5.00/90.00/30.00/500.00 | `Configuration.md` — Per-portfolio limits | `core/models.py`; `core/tests/test_models.py` | B |
| W-C-04 | The host port defaults to 8080 (container 8000) | `Configuration.md` — Host port | `docker-compose.yml` (`${ALPHA_WEB_PORT:-8080}:8000`) | B |
| W-C-05 | Haiku 5.5 rejects `temperature`, so sampling parameters are omitted for it | `Configuration.md` — Model compatibility | `ai_agent.py` (`_NO_SAMPLING_PARAM_MODELS`); `core/tests/test_model_compatibility.py` | B |
| W-C-06 | Model prices are held in `AI_MODEL_PRICING`; the page's table matches the configured rates | `Configuration.md` — Model compatibility | `config/settings.py` (`AI_MODEL_PRICING`); `core/tests/test_model_compatibility.py` | B |
| W-C-07 | A real run measured $0.161 → $0.0145 after the model swap | `Configuration.md` — Model compatibility | `CHANGELOG.md` 1.1.0; the run is not committed, and the page says so | C |
| W-C-08 | Redis uses `--requirepass`, and the four consumers use databases 0–3 | `Configuration.md` — Redis authentication | `docker-compose.yml`; `config/settings.py` (`_redis_url`); tests | B |
| W-C-09 | `AI_DAILY_SPEND_LIMIT_USD` defaults to $5.00 | `Configuration.md` — AI spend | `config/settings.py`; `.env.example`; ADR-0012 | B |

### Data-Model

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-DM-01 | The original seven tables plus the additive Phase 1–4 tables hold the state | `Data-Model.md` — introduction | `core/models.py`; `core/decision_models.py`; `core/measurement_models.py`; `core/allocation_models.py` | B |
| W-DM-02 | `Transaction` is append-only; positions, cost basis and realised P&L are derived | `Data-Model.md` — introduction | `services/ledger.py`; ADR-0003 | B |
| W-DM-03 | `apply_trade` is the only trading-path writer of `Transaction`; demo seeders (`seed_demo`, `run_allocation`) create setup accounts/holdings directly | `Data-Model.md` — introduction | `services/execution.py`; `core/management/commands/seed_demo.py`; `core/management/commands/run_allocation.py` | B |
| W-DM-04 | `Transaction.fee` is nullable; `NULL` is unrecorded, `0` is a measured zero | `Data-Model.md` — Transaction | `core/models.py`; `core/migrations/0012_*` (Decimal(12,8)); `docs/spec/MEASUREMENT.md` §2.3 | B |
| W-DM-05 | Migrations 0001–0013 are additive and CI rejects model drift | `Data-Model.md` — introduction | `core/migrations/`; `.github/workflows/ci.yml` (`makemigrations --check --dry-run`) | B |
| W-DM-06 | One live thesis per instrument is a schema constraint | `Data-Model.md` — PositionThesis | `core/models.py`; `core/tests/test_thesis.py` | B |
| W-DM-07 | The ticker column backfilled 62 rows and left 1,247 blank | `Data-Model.md` — Why the ticker is denormalised | `docs/adr/0014-decision-outcome-attribution.md`; `core/migrations/0006_*`; recorded, not reproducible | C |

### API-Reference

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-API-01 | The document covers every endpoint under `/api/` plus health and SPA routes | `API-Reference.md` — whole page | `core/urls.py`; `config/urls.py`; the decision-run and experiment sections were added in this change | B |
| W-API-02 | Authentication is a DRF token in `Authorization: Token <key>`; re-login returns the same row | `API-Reference.md` — Authentication | `core/auth.py`; `core/tests/test_api.py` | B |
| W-API-03 | Another user's object returns 404, never 403 | `API-Reference.md` — Authentication | `core/views.py`; `core/decision_views.py`; `core/tests/test_decision_runs.py` | B |
| W-API-04 | Errors use the `{error, status_code, detail, errors}` envelope | `API-Reference.md` — Conventions | `core/exceptions.py`; tests | B |
| W-API-05 | Pagination is page-number based, size 50, and snapshots are unpaginated | `API-Reference.md` — Conventions | `config/settings.py` (`PAGE_SIZE`); `core/views.py` | B |
| W-API-06 | Decision-run routes: list, metadata, detail, events, export | `API-Reference.md` — Decision runs | `core/urls.py`; `core/decision_views.py`; `core/tests/test_decision_runs.py` | B |
| W-API-07 | The experiment report route returns 409 before a report is built | `API-Reference.md` — Experiments | `core/experiment_views.py`; `core/tests/test_experiments.py` (`ReportApiTests`) | B |

### Real-Time-Layer

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-RT-01 | Daphne serves HTTP and WebSocket on one port | `Real-Time-Layer.md` — transport | `Dockerfile`; `config/asgi.py`; ADR-0008 | B |
| W-RT-02 | A bad token is accepted then closed with 4401; a channel-layer failure closes with 4500 | `Real-Time-Layer.md` — connection lifecycle | `core/consumers.py`; `core/tests/test_websockets.py` | B |
| W-RT-03 | The channel layer uses Redis DB 3 with bounded socket timeouts | `Real-Time-Layer.md` — Channel layer | `config/settings.py` | B |
| W-RT-04 | Event types map to named dashboard updates | `Real-Time-Layer.md` — Event model | `services/events.py`; `frontend/src/types.ts`; both test suites | B |
| W-RT-05 | There is no replay; reconnect resyncs over REST | `Real-Time-Layer.md` — Reconnect | `frontend/src/hooks`; `core/consumers.py` | B |
| W-RT-06 | The 1011 close was fixed by explicit Redis socket options | `Real-Time-Layer.md` — The fix | `config/settings.py`; `docs/wiki/Incident-Log.md` INC-003 | B |

### MCP-Tool-Server

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-M-01 | Six read-only tools are advertised | `MCP-Tool-Server.md` — table; Tools | `mcp_server/server.py` (`@mcp.tool()` declarations); `core/tests/test_mcp.py` | B |
| W-M-02 | `X-AlphaAgent-MCP-Key` is required on every HTTP request, compared with `hmac.compare_digest` | `MCP-Tool-Server.md` — Authentication | `mcp_server/auth.py`; `core/tests/test_mcp.py` | B |
| W-M-03 | The server cannot be started over HTTP without a secret | `MCP-Tool-Server.md` — Authentication | `mcp_server/auth.py`; `mcp_server/__main__.py` | B |
| W-M-04 | The service is exposed on the compose network but not published to the host | `MCP-Tool-Server.md` — Network boundary | `docker-compose.yml` (`expose: ["8100"]`) | B |
| W-M-05 | No tool queries the database or can place a trade | `MCP-Tool-Server.md` — read-only boundary | `core/tests/test_mcp.py` (`McpIsolationTests`); `mcp_server/server.py` | B |
| W-M-06 | The SDK is `mcp` 1.30.0; the release that unblocked the pin (`crewai` 1.15.32) declares `mcp<2,>=1.30.0`, and the lockfile pins 1.15.33 | `MCP-Tool-Server.md` — table | `requirements.txt`; `CHANGELOG.md` 1.1.2; installed `crewai-1.15.32.dist-info/METADATA` | B |

### Telegram-Bot

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-TG-01 | The webhook always returns 200 so Telegram does not retry forever | `Telegram-Bot.md` — webhook | `core/views.py` (`TelegramWebhookView`); `core/tests/test_telegram.py` | B |
| W-TG-02 | An unconfigured webhook secret returns 503 rather than accepting unsigned posts | `Telegram-Bot.md` — webhook | `core/views.py`; `core/tests/test_telegram.py` | B |
| W-TG-03 | Callback data stays within Telegram's 64-byte limit | `Telegram-Bot.md` — labels | `telegram_bot.py`; `core/tests/test_telegram.py` | B |
| W-TG-04 | A callback is treated as untrusted input and ownership is re-derived | `Telegram-Bot.md` — callback security | `telegram_bot.py`; `core/tests/test_telegram.py` | B |
| W-TG-05 | Link codes are 16 hex characters and expire after 15 minutes | `Telegram-Bot.md` — linking | `core/models.py` (`TelegramLink`); `core/tests/test_telegram.py` | B |
| W-TG-06 | Long messages are truncated at 3900 characters; reasoning at 600 | `Telegram-Bot.md` — formatting | `telegram_bot.py`; `core/tests/test_telegram.py` | B |

### Incident-Log

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-I-01 | Eleven incidents are recorded, each with symptom, impact, root cause and fix | `Incident-Log.md` — index | `docs/wiki/Incident-Log.md` INC-001–INC-011 | C |
| W-I-02 | Several incidents changed the design, not just the code | `Incident-Log.md` — index | Design columns in the incident table; ADRs 0004/0006/0015/0016 | C |
| W-I-03 | Counts quoted in incident entries come from production logs that are not committed | `Incident-Log.md` — evidence note | The page's evidence note; no repository log file contains them | C |

### Roadmap

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-RM-01 | Phases 0–4 are complete; Phase 5 is in progress | `Roadmap.md` — Implementation programme | `docs/spec/IMPLEMENTATION-NOTE.md`; `CHANGELOG.md`; this change | B |
| W-RM-02 | A cumulative daily deployment ceiling, backtesting, deterministic guard replay, per-user strategies and more asset classes are not built | `Roadmap.md` — Next/Later | `services/execution.py` has no daily deployment total; no backtest module; no guard-replay command | B |
| W-RM-03 | Leverage, margin and short selling are explicitly not planned | `Roadmap.md` — Explicitly not planned | `services/execution.py` long-only; `services/portfolio_targets.py` long-only contract; `docs/spec/ALLOCATION.md` §3 | B |

### Engineering-Decisions

| # | Claim | Where stated | Supporting evidence | Class |
| --- | --- | --- | --- | --- |
| W-ED-01 | Eight long-form decision records are on the page; the full set is seventeen ADRs | `Engineering-Decisions.md` — introduction | The page's ADR-001–008 sections; `docs/adr/README.md` | B |
| W-ED-02 | ADR-002's debate uses independent first cases from one evidence catalog | `Engineering-Decisions.md` — ADR-002 | `services/debate_protocol.py`; the page was corrected from "different evidence" | B |
| W-ED-03 | The heuristic fallback is production code, and the pipeline is exercisable offline | `Engineering-Decisions.md` — ADR-008 | `ai_agent.py`; `core/tests/test_tasks.py`; `core/tests/test_ai_agent.py` | B |

## 3. Claims corrected in this change

Every correction below changed a public document; no application code was changed.
The "was" column quotes the claim as it stood before Phase 5.

| # | Where | Was | Now | Why |
| --- | --- | --- | --- | --- |
| C-01 | `README.md` — title | "An enterprise-grade AI investment platform" | "An AI investment platform" | "Enterprise-grade" is unfalsifiable positioning, not evidence |
| C-02 | `README.md` — See it running | "Try the live demo — the real UI in a browser" | "Try the offline Streamlit demo — a browser page that drives the real risk guard" | The Streamlit page is a risk-guard demonstration, not the live React application |
| C-03 | `README.md` — GIF caption | "Recorded from the live system" | "Recorded from the live React application (not the Streamlit risk-guard demo above)" | The caption sat under the Streamlit link and implied the demo was the application |
| C-04 | `README.md` — pillar 1 | "Every decision — including every deliberate HOLD — is scored retroactively against real market data at 1, 7 and 30 days" | Decisions that name an instrument are evaluated when a permitted price exists; a HOLD has no directional hit; missing data is null | `evaluate_pending` skips rows with no ticker and leaves missing prices null; HOLD is not scored directionally |
| C-05 | `README.md` — pillar 1 | "absolute return" | "average decision return" | The scorecard stores a signed decision return; "absolute return" is a different metric |
| C-06 | `README.md` — attribution section | "counts them separately for the long side so a good average cannot hide bad BUY calls" | The dashboard has no long/short split; the Phase 3 report has a per-action breakdown | `build_scorecard` has no per-action split; only `SignalDiagnosticsReport` does |
| C-07 | `README.md` — pillar 1 | "every execution links back to the debate" | A debate-produced decision records both cases and links to its transaction when executed; a human approval creates its own row and a seeded trade has no debate | A human approval creates its own decision row; a seeded trade has no debate behind it |
| C-08 | `README.md` — choose your path | Incident log has "ten real defects" | "eleven real defects" | `Incident-Log.md` contains INC-001–INC-011 |
| C-09 | `README.md` — choose your path; Under the hood | "sixteen decisions" (twice) | "seventeen decisions" | `docs/adr/` contains 0001–0017 |
| C-10 | `README.md` — choose your path | Link to `#quality-signals` | Link to the Quality and Testing wiki page | The README has no `#quality-signals` heading; the anchor was dead |
| C-11 | `README.md` — contents | "8 sections" | "9 sections" | The file has nine `##` sections |
| C-12 | `README.md` — contents | "the live demo" | "the offline risk-guard demo" | Same conflation as C-02 |
| C-13 | `README.md` — why three agents | "a short seller ... work from different evidence"; a live Apple quote | The two first cases are independent from the same evidence catalog; no live transcript is quoted | Phase 2 builds both prompts from one catalog; the Apple quote had no committed source |
| C-14 | `README.md` — pillar 3 | "live prices, news and filings" | "recorded prices, news and fundamentals" | The system fetches fundamentals; it does not retrieve filings |
| C-15 | `README.md` — choose your path | "the six limits" | "the guard rules" | The guard page documents nine rule rows (seven documented rules split by branch); "six limits" was an unnecessary count |
| C-16 | `README.md` — screenshots | "caught a real over-concentration in this demo portfolio" | "closes the over-concentration gap recorded as INC-001" | INC-001 records the defect; no committed evidence ties the screenshot to a specific demo account |
| C-17 | `README.md` — attribution section | "the flaw ... one that made the figures worse" | "a start-price bias that flattered BUY calls" | The direction of the bias is in `services/attribution.py`; the "worse figures" comparison is not committed |
| C-18 | `README.md` — stack badge | "Python 3.12" | "Python 3.12+" | `requires-python >=3.12`, CI uses 3.12, the Docker image uses 3.13 |
| C-19 | `Decision-Attribution.md` | A 57-decision scorecard with 10.5% hit rate, −1.78%, −1.54%, 7.0%, −2.08% | Removed; the scorecard is live and not committed, and the page says so | The figures are in no committed fixture or report and contradicted the LLM page's sample |
| C-20 | `Decision-Attribution.md` | A sample dump of 15/2/0 evaluated calls | Removed; method explained instead | Not a committed artefact, and it contradicted the 57-call claim |
| C-21 | `LLM-Quality-and-Guardrails.md` | 12.3% → 10.5%, 15.9%, −1.20% → −1.54%, −1.88% → −2.27%, 7.0%, −2.08%, 57 calls | Removed; method and limitation stated, no figure quoted | Same unsourced live scorecard, with a different sample from the other page |
| C-22 | `LLM-Quality-and-Guardrails.md` | "a $166 difference on a real BTC decision" | Removed the number; the bias direction is retained | The run is not committed; `CHANGELOG.md` records the same anecdote, but it is not reproducible |
| C-23 | `LLM-Quality-and-Guardrails.md` | "an instrument allow-list" in the guard | "the position-size check" | The guard has no instrument allow-list |
| C-24 | `LLM-Quality-and-Guardrails.md` | Stale line anchors (`ai_agent.py#L87`, `#L78`, `#L79`, `#L97`, `#L103`, `#L129`, `#L132`, `#L141`, `#L160`, `#L403`; `core/models.py#L420`, `#L335`) | Corrected to the current lines (97, 106, 112, 107, 113, 139, 144, 153, 170, 421; 483, 398) | The Phase 2 additions moved the symbols; the old anchors pointed at different code |
| C-25 | `Agent-Debate.md` | Two sides use different tools; only the short seller sees fundamentals | Both first-round prompts use the same evidence catalog | Phase 2 independence is a prompt-context property, not a tool split |
| C-26 | `Agent-Debate.md` | A live Apple quote ("cannot build a credible bear case") | Removed; prompt permission and the synthetic disagreement fixture described | No live transcript is committed |
| C-27 | `Agent-Debate.md` | The payload is "enforced twice" by Pydantic and a CrewAI guardrail | Phase 2 stage schemas with one bounded repair; Pydantic/guardrail described as the legacy path | The default producer is no longer the sequential CrewAI crew |
| C-28 | `Agent-Debate.md` | The fallback produces "the same two recorded cases" | The fallback is a separate run with no claims and no rebuttals | Phase 2 records a separate heuristic run and a `protocol.fallback` block |
| C-29 | `Architecture.md` | "CrewAI debate bull → bear → CIO"; "Every decision scored" | Recorded debate protocol; audited decisions with an instrument are evaluated | Phase 2 producer and attribution eligibility |
| C-30 | `Home.md` | Current version 1.1.0 (2026-10-23); "81-second walkthrough" | 1.1.7 (2026-11-11); "Watch the walkthrough" | `CHANGELOG.md` has 1.1.7; the duration was unverifiable offline |
| C-31 | `Home.md` | "Eight decision records" with no context | "Eight long-form records; the full set of seventeen is in the ADR index" | Both counts are true of different pages; the old text invited confusion |
| C-32 | `Roadmap.md` | Phase 1 "In progress", Phases 2–4 "Planned" | Phases 0–4 "Done", Phase 5 "In progress" | Phases 1–4 are merged (`IMPLEMENTATION-NOTE.md`, `CHANGELOG.md`) |
| C-33 | `Quality-and-Testing.md` | 14 modules, 382 tests; later 533 Python / 115 frontend; "no coverage gate"; "three CI jobs" | 28 modules, 716 tests; 178 frontend; 70% backend floor; four jobs | The suite and CI grew; the page contradicted itself and the actual workflow |
| C-34 | `Quality-and-Testing.md` | Ruff pinned "identically" at 0.16.9 in two files | Lockfile pins the CI version; the pre-commit hook pins its own | `requirements-dev.txt` pins 0.17.3, the hook 0.16.9 |
| C-35 | `Quality-and-Testing.md` | `crewai` 1.15.23 pins `chromadb~=1.1.0` and is the newest release | Historical triage wording; current pins are `crewai==1.15.33`, `chromadb==1.5.9` | The lockfile moved; the old claim was false |
| C-36 | `Operations.md` | "Known open advisory ... cannot be closed here": `mcp` 1.28.1 blocked by `crewai~=1.28.1` | Closed: `mcp==1.30.0`; `crewai` 1.15.32 (recorded in `CHANGELOG.md` 1.1.2) declares `mcp<2,>=1.30.0`, and the lockfile pins 1.15.33 | The upgrade is recorded in `CHANGELOG.md` 1.1.2; the old page understated the fix and was a false security claim |
| C-37 | `Operations.md` | Beat schedule lists five jobs | Six, including `evaluate-decision-outcomes` | `config/settings.py` has six entries |
| C-38 | `Configuration.md` | "serves plain HTTP on `:8000`" | Host 8080, container 8000 | The compose mapping is `${ALPHA_WEB_PORT:-8080}:8000` |
| C-39 | `Configuration.md` | The $0.161 → $0.0145 figures with no provenance | Same figures, now cited to `CHANGELOG.md` and labelled a recorded observation | The run is not committed |
| C-40 | `Data-Model.md` | "Seven tables ... hold the platform's complete state" | Original seven plus the additive Phase 1–4 tables, with a table summary | Nine Phase 1 tables, four measurement tables and three allocation tables exist |
| C-41 | `Data-Model.md` | No `Transaction.fee` field | Added, with the `NULL` vs `0` contract | The Phase 3 column is part of the schema |
| C-42 | `Data-Model.md` | Migration history stopped at 0003 | 0004–0013 summarised | The migrations exist and are additive |
| C-43 | `API-Reference.md` | No decision-run or experiment endpoints | Added both sections | `core/urls.py` exposes them; the page claimed to cover every endpoint |
| C-44 | `MCP-Tool-Server.md` | SDK `mcp` 1.28.1, `crewai==1.15.23`; "publishing the port is deliberate" | `mcp` 1.30.0, `crewai==1.15.33`; `expose: ["8100"]`, not published | The compose file has no host mapping; the old text contradicted its own table |
| C-45 | `Real-Time-Layer.md` | "26 occurrences in 40 minutes"; "0.12 s" / "150 s" | Removed the exact counts; the incident and the fix remain | Live log measurements, not committed |
| C-46 | `Incident-Log.md` | INC-011 "one run in a hundred" and "16 of 1,532"; "35 tests" for eight defects | "Runs failed on a token cap"; qualitative impact; regression tests named without a count | The production log is not committed; the test count was stale |
| C-47 | `Execution-Guard.md` | "twenty 5%-of-cash buys reach fully invested" | "a sequence of individually-permitted 5%-of-cash buys compounds exposure" | The arithmetic is wrong: 5% of a shrinking cash balance never reaches 100% |
| C-48 | `Execution-Guard.md` | "two production callers" supply `Exposure` | Scheduled and approval callers are pinned; Phase 4 `apply_plan` also supplies one | The Phase 4 caller exists and builds an `Exposure` |
| C-49 | `Execution-Guard.md` | The overspend "around $0.15" | Removed the number | Not a committed measurement |
| C-50 | `Engineering-Decisions.md` | ADR-002: cases built from different evidence; "~100k tokens and about $0.10 per run" | Independent first cases from one catalog; per-run usage is recorded, no dollar figure asserted | Phase 2 changed the evidence supply; no committed run supports the cost figure |
| C-51 | `Home.md`; `Architecture.md` | "Missing market data becomes HOLD" / "degrades to HOLD" | Missing data does not abort; the gap is recorded and the guard refuses an execution without a price | Only the heuristic engine is guaranteed to return HOLD when it cannot size; an LLM proposal can be rejected by the guard instead |

## 4. Claims that could not be traced, and what happened to them

These are the cases where the honest move was to remove or soften the claim rather
than invent a source.

| Claim | Outcome |
| --- | --- |
| The 57-decision scorecard (10.5%, −1.78%, −1.54%, 7.0%, −2.08%) | Removed from `Decision-Attribution.md` and `LLM-Quality-and-Guardrails.md`; the pages now explain that the live scorecard is not committed and that the only committed report is synthetic. The two pages quoted different samples, which is itself evidence that neither was a stable artefact. |
| The 12.3/10.5/15.9/−1.20/−1.54/−1.88/−2.27 before/after table | Removed. No committed file contains it; it came from a live database. |
| The "cannot build a credible bear case for Apple" live quote | Removed from `README.md` and `Agent-Debate.md`. No live transcript is committed. The prompt permission remains, and the synthetic disagreement fixture shows the recordable shape. |
| The `$166` BTC start-price difference | Removed. `CHANGELOG.md` records the anecdote, but the run is not in the repository. The bias direction remains in `services/attribution.py`. |
| The 16/1,532 failed-run count and "one in a hundred" | Removed from `Incident-Log.md`; the incident is now described qualitatively, with an evidence note. |
| The 26-occurrences-in-40-minutes and 0.12 s/150 s WebSocket measurements | Removed from `Real-Time-Layer.md`; the incident and the fix remain. |
| "Twenty 5%-of-cash buys reach fully invested" | Removed; it was false arithmetic. |
| The "~$0.15" AI-spend overshoot | Removed; no committed measurement. |
| "Sixteen decisions" for the ADR index and "ten real defects" for the incident log | Corrected to seventeen and eleven, which match the committed indexes. |
| "The real UI in a browser" for the Streamlit link | Corrected to "offline risk-guard demo", which is what `demo/app.py` is. |
| The open MCP advisory that "cannot be closed here" | Corrected: the lockfile pins the patched release and `crewai` allows it. This was the most consequential stale claim because it understated a security fix. |
| `62` backfilled rows and `1,247` blank HOLD/HALT rows | Kept, but labelled class C and cited to `docs/adr/0014-decision-outcome-attribution.md`; the raw production rows are not committed. |
| `$0.161 → $0.0145` model-cost measurement | Kept in `Configuration.md`, cited to `CHANGELOG.md`, and explicitly labelled a recorded observation rather than a reproducible measurement. |
| The 22 CodeQL findings | Kept as historical context, cited to `CHANGELOG.md`; not a current measurement. |

## 5. What the gate now covers

- **README:** every substantive statement is either in the table above, a link to
  a page that is, or explicitly media/positioning. The corrections in §3 are the
  delta from the pre-Phase-5 README.
- **Wiki:** every page is represented. Pages that are primarily narrative
  (Incident Log, Engineering Decisions) are represented by their checkable
  structural claims and carry an evidence note where their numbers are not
  reproducible.
- **Numbers:** no public performance number is quoted from a live database. The
  only performance-shaped numbers in public documentation come from the synthetic
  Phase 3 fixture report, and they are labelled engineering evidence.
- **Synthetic content:** every case and demo that uses a fixture states the
  `mode`, `synthetic` flag or `synthetic_note` at the point of use. See
  [`CASES.md`](CASES.md) and [`DEMO.md`](DEMO.md).
- **Future changes:** the rule in `CONTRIBUTING.md` makes the index part of the
  pull request, not a later audit.
