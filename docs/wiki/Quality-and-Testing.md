# Quality and Testing

AlphaAgent is verified by one Django/unittest suite in `core/tests/`, a separate Vitest
suite in `frontend/`, four CI jobs in `ci.yml`, and a small set of static and
secret-hygiene checks. There is no pytest and no separate integration harness — the
backend suite is run with the Django test runner, and CI runs the same command a
developer runs locally. A backend coverage floor of 70% is enforced.

Measured on this checkout:

| Metric | Value | How it was measured |
| --- | --- | --- |
| Backend test modules | 52 (`test_*.py`) plus `helpers.py` | `ls core/tests/` |
| Backend test methods | **1029** at HEAD `0051067` (2026-11-14; the runner prints the current total) | `venv/bin/python manage.py test core.tests` |
| Frontend test files / tests | 27 / **289** | `cd frontend && npm test` |
| Result | `OK` | both commands, PostgreSQL backend for the Django suite |
| Lint status | `All checks passed!` | `venv/bin/ruff check .` |
| Format status | clean | `venv/bin/ruff format --check .` |
| Linter version | pinned in `requirements-dev.txt` | `.pre-commit-config.yaml` pins the hook's own version |

## Test modules

Counts are `def test_*` methods per module at HEAD `0051067`, verified by AST
walk and consistent with the runner's total of 1029. The authoritative number is
the total the runner prints on the commit being reviewed; the commit is named
because a count without one is a claim that will drift.

| Module | Tests | What it actually covers |
| --- | --- | --- |
| `test_ai_agent.py` | 36 | `TradeProposal` payload contract, JSON extraction from prose/fenced output, heuristic engine sizing, token cost accounting, and real CrewAI crew construction (guardrail annotation contract, tool schemas). |
| `test_api.py` | 19 | REST auth, portfolio payload + bounded query count, graceful market-data degradation, autonomy toggle confirmation rules, decision-log filtering/scoping, transaction listing, `/healthz`, token issuance. |
| `test_attribution.py` | 24 | Outcome scoring: SELL is right when the price falls, HOLD never enters the hit rate, empty scorecard reports no rate, start-price choice, idempotent evaluation. |
| `test_dashboard_api.py` | 29 | Equity snapshot series and hour filtering, recommendation list/approve/reject, advisory sweep dispatch, SPA serving, API-over-SPA route precedence. |
| `test_debate_protocol.py` | 41 | Independent first round, one rebuttal round, adjudication, stage budgets, typed failures, fallback separation, reproducibility metadata. |
| `test_decision_runs.py` | 25 | Decision-run list/metadata/detail/events/export contracts, fixture label propagation, ownership scoping and redaction. |
| `test_decision_lifecycle.py` | 14 | One run from analysis to validation, approval, execution, rejection and expiry; the guard record is persisted from the under-lock evaluation. |
| `test_event_delivery.py` | 8 | Durable outbox delivery, retry/backoff, catch-up ordering and delivery metrics. |
| `test_execution_integrity.py` | 12 | Execution-key idempotency, under-lock revalidation, audit pairing and the repair path. |
| `test_demo_theme_contract.py` | 3 | The Streamlit demo's theme contract against the pinned Streamlit version. |
| `test_documentation_links.py` | 6 | Every absolute source link resolves to a file and line in the checkout; the wiki-breaking relative form is rejected. |
| `test_experiments.py` | 47 | Experiment manifest validation, variant publication, report assembly, fairness, sample gates, evidence coverage, Sharpe absence and counterfactual reporting. |
| `test_experiment_execution.py` | 21 | Arm execution, one frozen opportunity set, execution namespaces, the pre-registered metric, cost scope and report build. |
| `test_exposure_limits.py` | 26 | Portfolio-level ceilings: exact clamp, binding limit named, SELL never blocked, production callers pass `Exposure`. |
| `test_guardrails.py` | 17 | The execution guard: allocation ceiling and clamping, daily-loss limit, HOLD never executes, missing/zero price, zero amount, SELL sizing, fractional crypto. |
| `test_llm_hooks.py` | 19 | LLM transport interceptor: headers, malformed-JSON tolerance, and an end-to-end mock Anthropic HTTP endpoint. |
| `test_mcp.py` | 24 | MCP handshake, advertised tools, per-tool payloads, degradation, no-ORM-query isolation, logging to stderr, MCP-vs-local routing. |
| `test_model_compatibility.py` | 11 | Sampling-parameter omission for models that reject `temperature`, and pricing/estimate behaviour. |
| `test_models.py` | 11 | Schema contract: documented defaults, one portfolio per user, negative balance and non-positive amount rejected at the database, derivations. |
| `test_portfolio_accounting.py` | 35 | Equity, fees, flows, TWR approximations, missing prices and benchmarks, weekend maturities, leakage, overlapping decisions, LLM cost, counterfactuals. |
| `test_portfolio_allocation.py` | 53 | Brief completeness, target contract, deterministic projection, leg sizing, atomic apply, revalidation, concurrency and the paper command. |
| `test_allocation_workflow.py` | 36 | Submission, review, approval, apply, cancellation and provider recording through the advisory workflow. |
| `test_policy_application.py` | 15 | Portfolio policy rechecked at application time: limits, classifications, cost model and version binding. |
| `test_recommendations.py` | 36 | Human-in-the-loop creation, de-duplication, TTL expiry, approval re-validated at current prices, double approval, audit rows, snapshots. |
| `test_regressions.py` | 104 | One class per defect found by review rather than a failing test: payload shapes, sweep scoping, truthful 429, debounce keys, link-code expiry, token purge, limit parsing, autonomy broadcast, webhook fail-closed, SPA containment and more. |
| `test_security.py` | 9 | `env_required` semantics and the repository-wide secret scan. |
| `test_service_exposure.py` | 3 | Wiring: the production callers actually pass an `Exposure` to the guard. |
| `test_services.py` | 31 | Ticker normalisation, quote cache/fallback, lexicon sentiment, RSS parsing including entity-expansion and XXE rejection, ledger replay, portfolio metrics. |
| `test_tasks.py` | 19 | Celery task bodies: approved BUY/SELL mutation, clamping, loss-limit block, rollback, crash capture, monitoring isolation, risk sweep, token purge. |
| `test_telegram.py` | 45 | Bot formatting, 64-byte callback budget, link/unlink, reports, approve/reject callbacks, opt-out, webhook secret and always-ACK, cross-user scoping. |
| `test_thesis.py` | 30 | One live thesis per instrument, superseded not overwritten, empty position closes it, absence stated in the prompt, wiring. |
| `test_thinking_budget.py` | 2 | Adaptive-thinking token budget guard for models that expose one. |
| `test_throttle.py` | 18 | Sweep-ticker cooldowns and debounce: effectiveness, independence, zero-window disable, release, fail-open. |
| `test_p0_execution_remediation.py` | 20 | Quote freshness and provenance, approval binding, price-drift refusal, audit/fill atomicity, duplicate recommendations, plan failure races and the repair command. |
| `test_p1_attribution_remediation.py` | 9 | Fill-based measurement, HOLD proposal basis, independent horizons, idempotent retries, backfill provenance. |
| `test_p1_replay_remediation.py` | 8 | Stable guard-evaluation ids, cursor resolution, late-outcome events and outcome atomicity. |
| `test_p1_export_portability.py` | 16 | Shared canonical JSON vectors, export sample verification, tamper detection, endpoint shape and the full guard collection. |
| `test_p1_allocation_jobs.py` | 11 | 202-before-provider, crash resume without double spend, cancellation, timeout, repair and ownership. |
| `test_p1_experiment_isolation_metric.py` | 9 | Execution isolation, the registered metric, denominator and dispersion, and the committed archive recompute. |
| `test_p1_simulation_rebuild.py` | 12 | Monotonic simulated time, byte-identical replays and archived report rebuild. |
| `test_p1_cost_limiter.py` | 9 | Reservation before the provider call, tokens-without-dollars reconciliation, unknown cost, durable replay and state shape. |
| `test_p1_acceptance_verification.py` | 18 | Verifier stages and exit codes, acceptance record digest, dirty-tree refusal, recording pairing and committed recordings. |
| `test_p0_terms_binding_and_redelivery.py` | 9 | Terms-reference binding for web and Telegram approvals, observation provenance per mark, the wire format the clients carry, and redelivery returning the original fill. |
| `test_p1_step3_fair_scheduling.py` | 4 | Due-horizon work selected oldest-first, ineligible pairs not consuming slots, and idempotent overlapping ticks plus backfill. |
| `test_p1_step4_replay_and_unavailable.py` | 4 | Event-revision replay, the unavailable outcome event and its rollback, and the server-tail contract. |
| `test_p1_step5_provenance.py` | 11 | The authored export labelled as authored, `run.origin` as a separate claim, the superseded sample, and provider-backed limitations. |
| `test_p1_step6_recovery.py` | 14 | Broker-failure recovery without a repost, lease and attempt rules under lock, the recorded input snapshot, and the beat-scheduled sweep. |
| `test_p1_step7_identity_benchmarks.py` | 5 | Resolved attempt identity, retained reruns, matched benchmark windows, observation states and the missing-benchmark path. |
| `test_p1_step8_fill_validation.py` | 5 | Cash, holding and daily-loss validation at the executable price, deferred fills, and the v3 archive rebuild digest. |
| `test_p1_step9_spend_limits.py` | 8 | Per-identity reservations for allocation attempts, one paid attempt per experiment allowance, token-only settlement and confirmed charges. |
| `test_p1_step10_release_gate.py` | 14 | A developer run is not release acceptance, an incomplete stage set is unusable, and a release record must pass all eight stages. |
| `test_websockets.py` | 14 | ASGI consumer auth, ping/pong, unknown frames, event relay to the owning user only, Decimal serialisation, soft failure without a channel layer. |

`core/tests/helpers.py` is not a test module. It supplies `TEST_CACHES` (a `LocMemCache`
override replacing the shared Redis cache), the `make_user`/`make_portfolio`/`make_asset`
factories and `auth_client()` for token-authenticated API calls.

## Test strategy

The suite has three tiers, distinguished by what boundary they cross.

| Tier | Modules | Boundary crossed |
| --- | --- | --- |
| Pure logic | `test_guardrails`, `test_services`, `test_throttle`, `test_ai_agent` (parsing/heuristics), `test_portfolio_accounting` | None — plain inputs and outputs. |
| Database / API integration | `test_models`, `test_recommendations`, `test_tasks`, `test_api`, `test_dashboard_api`, `test_telegram`, `test_decision_runs`, `test_experiments`, `test_portfolio_allocation` | Real PostgreSQL via `TestCase` transactions, real DRF routing, real ORM constraints. |
| Transport / protocol integration | `test_websockets`, `test_mcp`, `test_llm_hooks`, `test_debate_protocol` | A live channel layer, the real MCP SDK session, a localhost HTTP server, and the protocol's model seam. |

### How external services are isolated

Nothing in the suite performs network I/O. Three boundaries are stubbed:

| Dependency | Isolation point | Evidence |
| --- | --- | --- |
| yfinance | `services.market_data._fetch_from_yfinance` is patched, so the *real* cache, retry and stored-price fallback logic still executes | `test_services.MarketDataTests`, `test_api.PortfolioEndpointTests` |
| Consumer call sites | Modules import `get_latest_quote` into their own namespace, so tests patch the call site (`tasks.get_latest_quote`, `services.recommendations.get_latest_quote`, `mcp_server.server.get_latest_quote`) — narrower than patching the provider and independent of its internals | each caller patches its own import |
| RSS feeds | `_parse_rss` is fed literal XML bytes; malformed documents, entity-expansion bombs and external entities are asserted to yield `[]` rather than reaching `requests` | `test_services.NewsParsingTests` |
| LLM provider | CI sets `AI_LLM_PROVIDER=none` and an empty `AI_LLM_API_KEY`. `test_ai_agent` builds **real** CrewAI `Agent`/`Crew`/`Task` objects — so CrewAI's own validators run — and stubs only `crewai.LLM` and `Crew.kickoff`; `test_llm_hooks` runs a mock Anthropic endpoint on localhost | `test_ai_agent.CrewBuildTests`, `test_llm_hooks` |
| Redis cache | `helpers.TEST_CACHES` swaps the default cache for `LocMemCache` via `@override_settings` | `core/tests/helpers.py` |
| Celery | Tasks are invoked as plain callables (`.delay`/`group` patched where dispatch is the behaviour under test) | `test_tasks.MonitoringFanOutTests` |

`test_ai_agent.CrewBuildTests` is worth reading as a case study: the docstring records that a
PEP 563 `from __future__ import annotations` import turned the guardrail's return annotation
into the string `'tuple[bool, Any]'`, which made CrewAI's `Task` validator abort crew
construction with *"If return type is annotated, it must be Tuple[bool, Any]"*. Nothing in the
suite had ever built a crew, so the entire configured-LLM path was untested. The test now
asserts `get_origin(annotation) is tuple` and `get_args(...) == (bool, Any)` — pinning the
framework contract, not the implementation detail.

### The execution guard is a pure function by design

`services/execution.py` documents itself as "deliberately free of Celery imports so the REST
API, the Telegram bot and the tests can all reach the *same* guard implementation. There is
exactly one way for an AI proposal to become a ledger row."

```python
def evaluate_proposal(
    portfolio: Portfolio,
    proposal: TradeProposal,
    price: Decimal | None,
    position: PositionState,
    realised_pnl_today: Decimal,
) -> GuardDecision:
    """Apply every pre-trade guardrail. Pure function - performs no I/O."""
```

Everything the guard needs arrives as an argument; it returns a `GuardDecision` dataclass
(`approved`, `reason`, `amount`, `notional`, `clamped`, `notes`). It reads no clock, opens no
connection and writes nothing — only `apply_trade`, the separate `@transaction.atomic`
function, mutates the ledger.

That separation is what makes the safety layer cheaply testable. `test_guardrails.py` contains
17 tests in three classes and **zero mocks**: each case constructs a `Portfolio` via
`make_portfolio()` (the only database use — reading the guardrail columns), builds a
`TradeProposal` and a `PositionState`, and asserts on the returned decision.

| Rule | Enforced in | Representative test |
| --- | --- | --- |
| `HOLD` never reaches the database | early return | `test_hold_is_never_executed` |
| A live, positive price is mandatory | price check | `test_missing_price_blocks_execution`, `test_zero_price_blocks_execution` |
| `notional` must fit `max_trade_allocation_pct` of cash (auto-clamp, reject if nothing fits) | BUY branch | `test_buy_over_budget_is_clamped_to_ceiling`, `test_buy_blocked_when_allocation_pct_is_zero` |
| BUY ≤ cash; SELL ≤ held position (clamped) | both branches | `test_buy_blocked_when_cash_is_zero`, `test_sell_larger_than_position_is_clamped` |
| Once `daily_loss_limit_usd` is breached, BUY is blocked while SELL stays available | BUY branch | `test_buy_blocked_when_loss_limit_breached`, `test_sell_still_allowed_when_loss_limit_breached` |

See [[Execution-Guard]] for the guardrail semantics themselves.

## CI pipeline

`.github/workflows/ci.yml` defines three jobs. Triggers: push and pull request to `main`,
`master` and `develop`, plus `workflow_dispatch`. A `concurrency` group cancels superseded runs
on the same ref, and the workflow holds `contents: read` at the top level.

| Job | `needs` | Steps | Fails the build when |
| --- | --- | --- | --- |
| `lint` — "Lint & format" | — | install `requirements-dev.txt`; `ruff check --output-format=github .`; `ruff format --check --diff .`; `python manage.py check --deploy` | Any ruff lint finding, any formatting diff, or any Django system-check **error** (with `DJANGO_DEBUG=false`). |
| `test` — "Tests (PostgreSQL 16)" | `lint` | install `requirements-dev.txt` (hash-verified); wait for PostgreSQL; `makemigrations --check --dry-run`; `migrate --noinput`; `coverage run manage.py test core.tests`; `coverage report --fail-under=70`; regenerate the coverage badge | Model changes without a migration, a failing migration, a failing test, or coverage below 70%. |
| `frontend` — "Frontend tests" | `lint` | `npm ci`; `npm run typecheck`; `npm run build`; `npm run test:coverage` | A type error, a failed production build, a failing Vitest test, or a coverage threshold breach. |
| `docker` — "Docker image builds" | `lint` | buildx; `docker/build-push-action@v7` (`push: false`, `load: true`, GHA cache); `docker compose config --quiet`; import smoke test (`django, celery, crewai, yfinance`); real boot `manage.py check` with `DJANGO_SECRET_KEY` + `POSTGRES_PASSWORD` | Image build failure, invalid compose file, missing runtime import, or a settings/config error on boot. |

Both service containers (`postgres:16-alpine`, `redis:7-alpine`) are declared with health
checks, so a flaky startup surfaces as a job timeout rather than an unexplained connection
error. The `test` matrix is currently one entry (`postgres: ["16"]`, `python: ["3.12"]`) with
`fail-fast: false`, kept as a matrix so widening it is a one-line change.

Credentials in the workflow are explicitly throwaway — `DJANGO_SECRET_KEY:
ci-only-secret-key-not-used-anywhere-else`, `POSTGRES_PASSWORD: ci-only-postgres-password` —
and `AI_LLM_PROVIDER: none` with `AI_LLM_API_KEY: ""` keeps the suite offline and
deterministic.

### CI runs against a clean checkout with no `.env`

CI never creates a `.env`; every variable arrives through the workflow-level `env:` block.
That is deliberate, because it reproduces the state every new contributor is in before
`cp .env.example .env` — and it caught a real bug.

`docker-compose.yml` previously declared `env_file: .env`, which Compose treats as **required**
by default. `docker compose config` therefore aborted with *"env file .env not found"* on a
fresh clone, failing the `docker` job before anything was built. Commit `252ddf6`
("fix(ci): make .env optional for compose, strengthen the image smoke test") fixed it:

```yaml
env_file:
  - path: .env
    required: false
```

The `${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env}` guard still aborts when the
password is genuinely unset, so the safety property survives. The same commit replaced the
image smoke test's `manage.py --help` — which exits 0 *without loading settings* and therefore
could not catch a broken configuration — with a real `manage.py check` under the required
secrets.

`test_security.RepositorySecretScanTests.test_env_file_is_gitignored` asserts `.env` is listed
in `.gitignore`, so the file the CI job omits cannot be committed by accident either.

## Static analysis and security

**Ruff** (`pyproject.toml`) is the only linter and formatter: `line-length = 100`,
`target-version = "py312"`, excluding `.git`, `.runtime`, virtualenvs, `__pycache__`,
`**/migrations/*`, `staticfiles` and `logs`. Selected families are `E`, `W`, `F`, `I`, `UP`,
`B`, `C4`, `DJ`, `SIM`, `S`, `RUF` — note `S` (flake8-bandit), and `DJ` for Django-specific
hazards. Global ignores are `E501` (the formatter owns line length), `B008` (Django/Celery call
in argument default), `S101` (asserts belong in tests) and `SIM108`. Per-file ignores record
the reasoning rather than hiding it: `core/tests/*` may use asserts, dummy secrets and
`S311`; `RUF012` is suppressed for `models.py`/`serializers.py`/`views.py`/`admin.py` as a
false positive on Django's class-level `Meta` lists; `mcp_server/*` ignores `S104` because a
network service has to bind an interface.

**CodeQL** (`.github/workflows/codeql.yml`) analyses Python on push and pull request to `main`
and `master`, plus a weekly schedule (`cron: "23 4 * * 1"`) so newly disclosed query patterns
are caught without new commits. It runs the `security-and-quality` query suite, has a
30-minute timeout, and needs `security-events: write` to upload results. Unlike `ci.yml` it
does not cover `develop`.

The first full run reported **22 findings**, all now resolved — 22 fixed and 14 dismissed as
false positives with a recorded reason. They fell into five groups, and each was fixed at its
root rather than suppressed:

| Finding | Count | Fix |
| --- | --- | --- |
| `py/log-injection` | 12 | Log values routed through `core.log_safety.log_safe()`, which strips CR, LF, the C0/C1 range and U+2028/U+2029, and bounds the length. Ticker-derived values additionally pass `services.tickers.normalise_ticker()`, an anchored allowlist. |
| `py/partial-ssrf` | 1 | The same ticker validator runs before the RSS URL builders. Both hosts were already hardcoded, so the blast radius was the query string. |
| `py/path-injection` | 1 | `core/spa.py` resolves through Django's `safe_join` *before* constructing a path, with the resolved-path check retained to cover symlinks. |
| `py/cyclic-import` | 3 | `telegram_bot` dispatched a task by importing `tasks` inside a function, which hides a cycle rather than removing it. It now uses `celery_app.send_task`, so the edge is gone; verified by AST. |
| Dead code / naming | 3 | A redundant `model = model`, an unused logger in a pure module, and a mock parameter whose rename had shadowed the enclosing `TestCase`. |

> **The 14 dismissals are honest false positives, not suppressed risk.** `log_safe` is a real
> sanitiser with five tests, but CodeQL cannot see an unmodelled helper — taint flows straight
> through it. `.github/codeql/model-pack` declares the barrier using the `barrierModel`
> predicate; `codeql-action/init`'s `packs` input accepts only *published* pack references, so
> it takes effect after one `codeql pack publish`. The pack's README records the command and
> the workflow edit. Dismissing was chosen over excluding the rule from configuration, so that
> any future `py/log-injection` finding is still surfaced.

**Secret scanning** is a *test*, not a separate job — `core/tests/test_security.py` (9 tests)
runs inside the `test` job, so a leaked credential fails the same suite as a regression. Three
classes:

| Class | Tests | Property |
| --- | --- | --- |
| `EnvRequiredTests` | 3 | `env_required` raises `ImproperlyConfigured` naming the variable and pointing at `.env.example`; whitespace-only counts as missing; valid values are returned stripped. |
| `SettingsSourceTests` | 3 | `config/settings.py` contains no hard-coded database password, no `SECRET_KEY` fallback (`dev-only-insecure`, `change-me`), and reads `POSTGRES_PASSWORD` through `env_required`. |
| `RepositorySecretScanTests` | 3 | Walks every publishable file and asserts no hard-coded DB password, Anthropic key (`sk-ant-api…`), OpenAI-style key, AWS access key ID or PEM private-key block is present; `.env` is in `.gitignore`; `.env.example` still uses `replace-me` placeholders. |

Two details make the scan trustworthy. The forbidden literals and regexes are **assembled from
concatenated fragments at runtime** (a leaked database password split across two string
literals, an API-key prefix split from its pattern), so the test file never contains the strings
it searches for and cannot match itself. And the walk prunes the
directories that are never published (`venv`, `.runtime`, `.git`, `logs`, `staticfiles`,
`media`, `node_modules`, caches) plus the files that are *expected* to hold real secrets
(`.env`, `celerybeat-schedule`) and binary/log suffixes, so the scan reports on the repository
rather than on a developer's working tree.

**Pre-commit** (`.pre-commit-config.yaml`) mirrors the CI gates locally: `ruff` with
`--exit-non-zero-on-fix` and `ruff-format` pinned in `.pre-commit-config.yaml` (the
lockfile pins the version CI installs; keep the two in step when upgrading) — plus `gitleaks v8.28.0`,
`detect-private-key`, `check-added-large-files --maxkb=1024`, and whitespace/EOF/YAML/JSON/TOML
/merge-conflict/mixed-line-ending hooks.

**Dependabot** (`.github/dependabot.yml`) runs monthly (06:00 UTC on the 1st) for three
ecosystems, with a limit of 3 open PRs for pip and Actions and 2 for Docker. The config's
comment records why: the first version opened 11 PRs within minutes, mostly single-package
major bumps. Grouping splits each ecosystem into a major group and a minor/patch group, with
security-relevant surfaces kept individually visible:

| Ecosystem | Groups | Documented ignores |
| --- | --- | --- |
| pip | `python-major`, `python-minor-patch`, `django`, `ai` (`crewai*`, `openai*`, `anthropic*`, `pydantic*`, `yfinance*`), `celery` | `openai >= 3.0.0` and `pydantic >= 2.13.0` (crewai 1.15.33 pins both below those majors — the PRs cannot install), `Django >= 6.0.0` (5.2 is the LTS line) |
| github-actions | `actions-major`, `actions-minor-patch` | — |
| docker | `docker-all` | `python >= 3.14` (every crewai release declares `Requires-Python <3.14`) |

## Running everything locally

The Makefile wraps the common paths; the underlying commands are shown beside them.

| Target | Command it runs | Purpose |
| --- | --- | --- |
| `make test` | `venv/bin/python manage.py test core.tests` | The full suite (1029 at HEAD `0051067`). Requires PostgreSQL reachable (`make up` starts `db` and `redis` only). |
| `make test-guardrails` | `venv/bin/python manage.py test core.tests.test_guardrails core.tests.test_security` | The safety-critical subset: execution guardrails plus secret hygiene. |
| `make lint` | `venv/bin/ruff check .` | Lint only. |
| `make format` | `venv/bin/ruff check . --fix && venv/bin/ruff format .` | Auto-fix and format. |
| `make check` | `lint`, then `ruff format --check .`, `manage.py check`, `makemigrations --check --dry-run`, `manage.py test core.tests` | Closest local equivalent of the `lint` + `test` CI jobs in one command. |
| `make up` / `make down` | `docker compose up -d db redis` / `down` | Data tier for a local test run. |
| `make dry-run` | `docker compose exec web python manage.py dry_run_agent --portfolio-id 1` | Exercise the real crew against the live LLM with no database writes. |
| `make install-dev` | `pip install --require-hashes -r requirements-dev.txt` | Runtime plus tooling; both files are hash-verified lockfiles. |
| `make lock` / `make lock-dev` | `pip-compile --generate-hashes …` | Re-resolve a lockfile from its `.in` file. |

### Dependency locking, and why Dependabot could not see the Python tree

`requirements.txt` and `requirements-dev.txt` are **generated lockfiles**, compiled
from `requirements.in` and `requirements-dev.in` by `pip-tools`. Edit the `.in`
files; `make lock` and `make lock-dev` rewrite the `.txt` files.

Two things this buys, and one trap worth knowing:

- **Full hash verification.** Every artefact is pinned by `==` *and* by sha256, and
  the image build, CI and `make install` all pass `--require-hashes`. A tampered
  or substituted package fails the install rather than executing.
- **Dependabot can finally see the Python supply chain.** GitHub builds its
  dependency graph from manifest files, so a hand-written list of 16 direct pins
  left everything resolved beneath them untracked: **34 of 201 installed packages
  were visible, and 183 were not** — including `chromadb`, which carries four
  advisories. The lockfile lists all 180 Python packages explicitly, so they are
  all tracked now, exactly as `frontend/package-lock.json` already made the 219
  npm packages visible.

> **The trap.** pip switches to hash-checking mode automatically as soon as *any*
> requirement in a file carries a hash — and then demands hashes for **all** of
> them. `requirements-dev.txt` therefore had to be compiled too, not left as a
> thin `-r requirements.txt` plus two tool pins, or the CI install fails with
> *"Hashes are required in --require-hashes mode"*. The dev lockfile also needs
> `--allow-unsafe`, without which `pip` and `setuptools` are left unpinned and pip
> rejects the file: *"all requirements must have their versions pinned with =="*.
> That second failure does **not** reproduce on a developer machine where
> setuptools is already present — only in a clean container.

#### What enabling alerts actually revealed

Turning on `dependabot_security_updates` did not fix anything by itself; it
revealed how much was invisible. The count moved 0 → 6 → 14 → 0 as each layer
became visible, and every step is instructive:

| Step | Open | What changed |
| --- | --- | --- |
| Alerts enabled | 6 | Six npm advisories appeared immediately — always detectable, never reported, because the feature was off |
| Lockfile pushed | 14 | `chromadb` surfaced for the first time: **4 advisories × 2 manifests**. It appears twice because the dev lockfile duplicates the runtime tree, which is inherent to pip-tools |
| Frontend patched | 8 | All six npm advisories cleared |
| `chromadb` triaged | 0 | Dismissed with the reasoning recorded — see below |

The six npm ones were genuinely fixed, not dismissed: `vite` 5.4.21 → 8.3.2 (three
advisories; its rolldown bundler also removes `esbuild` from the tree entirely),
`postcss-selector-parser` 6.1.4 → 7.1.6 via an `overrides` entry because
`tailwindcss` 3 pins `^6`, and `source-map-js` 1.2.1 → 1.2.2. The emitted
stylesheet is hash-named `index-BGYHA9pb.css` before and after and the files are
**byte-identical**, which is the evidence that a major bundler upgrade changed
nothing observable.

`chromadb` is the one genuine risk acceptance on the project. At the time of the
triage, all four advisories reported *no patched version* and the then-pinned
`crewai` required `chromadb~=1.1.0`, so there was no installable release that fixed
them. The lockfile has since moved on (`crewai==1.15.33` pulls `chromadb==1.5.9`);
the ignore entry and the advisory database should be re-checked rather than assumed
to still say the same thing. The findings are all server-side (code injection
against a running Chroma server, RBAC and tenant isolation), and this project never
imports `chromadb`,
runs no Chroma server, publishes no port for one, and builds its Crew with
`memory=False`. The `ignore` entry in `dependabot.yml` records the triage and when
to revisit it; the alerts themselves are dismissed with the same reasoning, so the
decision is auditable rather than silent.

Two things to know before the first run: `config/settings.py` calls
`env_required("POSTGRES_PASSWORD")` at import time, so no management command works without it
(`make env` copies `.env.example` to `.env`); and the suite replaces the shared Redis cache with
`LocMemCache` per module, so a running Redis is not strictly required for the cache-backed
tests, only for the database.

## Test modules for attribution, exposure and theses

Three modules cover the features that score decisions, cap portfolio risk and give
positions a memory. Each is written around the mistake that would be easy to make
rather than around the happy path.

| Module | What it pins down |
| --- | --- |
| `test_attribution.py` | A SELL counts as right when the price **falls**; a HOLD never enters the hit rate; an empty scorecard reports no rate rather than 0%; the evaluator is idempotent, since it runs hourly on Beat |
| `test_exposure_limits.py` | The clamp stops at exactly the ceiling; the binding limit is the one named in the note; a SELL is never blocked; and the production callers actually pass an `Exposure` |
| `test_thesis.py` | One live thesis per instrument, enforced by the schema; a superseded thesis is closed not overwritten; an empty position closes its thesis; the absence of a thesis is stated in the prompt rather than left blank |

Two of these assert **wiring** rather than behaviour, which is unusual and
deliberate. Both the exposure limits and the thesis depend on a caller passing
something the guard cannot fetch for itself, and a caller that forgets degrades
silently — the limits simply do not apply, with no error. `ExposureWiringTests` and
`ThesisWiringTests` read the task source and assert the call is on the path. That
is a test standing in for a type the guard's purity will not allow.

## Frontend tests

The dashboard has a Vitest suite that runs as its own CI job, because a failure
there has nothing to do with the Python tier and should not be reported as one.

```bash
cd frontend && npm test
```

What it covers, and why those parts:

| Module | Why it is tested |
| --- | --- |
| `lib/format.ts` | Every money value crosses the wire as a decimal *string*, so parsing is the frontend's most load-bearing code. The contract is that unparseable input renders `—`, never `NaN` — asserted for seven hostile inputs across three formatters |
| `lib/stream.ts` | The only thing between an arbitrary WebSocket frame and dashboard state. A frame that gets through with a missing field writes `undefined` into state and blanks a panel |
| `lib/feed.ts` | De-duplication between the live socket and the REST log. A row arriving over both must collapse to one, or the feed reads as the agent deciding twice |
| `components/MetricsGrid.tsx` | The one component whose failure is silent: a missing metric used to render `NaN` across the cards and look like a crash |

Coverage is enforced at **two** levels. `src/lib` must stay above 75%; the whole
`src/` tree is held to a low ratchet set just below its current figure. A single
global number would mislead in both directions — high enough for `lib` to be
meaningless, low enough for the components to be theatre.

Two defects this suite found on its first run, both silent in production:

- **Budget frames were dropped.** `budget.updated` and `budget.exhausted` had been
  added to the TypeScript union but not to the validator's `KNOWN_TYPES`, so the
  live AI spend meter would never have updated over the socket. Nothing errored;
  the number would simply have stayed stale until a REST resync.
- **Halts were rendered as ordinary activity.** `deriveAction` matched `halted`
  but not `HALT`, and the backend writes rows beginning `HALT - `. Every
  daily-loss freeze and spend-ceiling halt therefore carried a neutral `ACTION`
  badge instead of `BLOCKED`, understating the two most serious states in the
  system.

Related pages: [[Execution-Guard]], [[Architecture]], [[Operations]], [[Configuration]],
[[API-Reference]], [[Telegram-Bot]], [[Real-Time-Layer]], [[Incident-Log]].

## Quality signals

Numbers here are deliberately kept in as few places as possible. A count quoted in
several files drifts apart — this project once had three different test counts in
three files at once. The README now carries one **commit-pinned** snapshot (1029
backend / 289 frontend at `0051067`), and this page plus `CONTRIBUTING.md` carry
the same snapshot. A number without the commit is still a claim that will drift;
the authoritative number is whatever CI reports on the commit being reviewed.

| | |
| --- | --- |
| **Two test suites** | 1029 Python tests at HEAD `0051067` — risk guard, ledger, API, task pipeline, WebSockets, Telegram bot, tool server, decision runs, debate protocol, measurement, allocation, the P0/P1 remediation modules, documentation links and the demo's theme contract — plus 289 frontend tests in 27 files (Vitest + Testing Library) covering formatters, the WebSocket frame validator, feed merging, the metric grid, portfolio plans, replay and the offline viewer. The authoritative number is whatever CI reports on the current commit; treat any figure written down, including this one, as a snapshot |
| **Coverage enforced** | CI fails below a 70% floor on the backend |
| **Continuous integration** | Lint, format, the full backend suite against PostgreSQL 16 with a 70% coverage floor, the frontend typecheck/build/Vitest suite, and a Docker image build — on every push |
| **Security scanning** | CodeQL analysis, Dependabot, secret scanning, and a test that walks every publishable file looking for committed credentials |
| **Docs are checked too** | Every Markdown and wiki link is validated on each push, and every referenced screenshot is confirmed to exist — a renamed image fails the build rather than rotting silently |
| **Supply chain** | `make audit` scans the pinned dependencies from an isolated environment, because `pip-audit` and `crewai` have incompatible `tomli` requirements and cannot share a virtualenv |

