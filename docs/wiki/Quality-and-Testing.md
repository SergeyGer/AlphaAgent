# Quality and Testing

AlphaAgent is verified by one Django/unittest suite in `core/tests/`, three CI jobs, and a
small set of static and secret-hygiene checks. There is no pytest, no coverage gate and no
separate integration harness — the suite is run with the Django test runner and CI runs the
same command a developer runs locally.

Measured on this checkout:

| Metric | Value | How it was measured |
| --- | --- | --- |
| Test modules | 14 (`test_*.py`) plus `helpers.py` | `ls core/tests/` |
| Test methods | **382** | `venv/bin/python manage.py test core.tests` |
| Result | `Ran 382 tests` → `OK` | same command, PostgreSQL backend |
| Lint status | `All checks passed!` | `venv/bin/ruff check .` |
| Format status | `68 files already formatted` | `venv/bin/ruff format --check .` |
| Linter version | ruff 0.16.9 | `requirements-dev.txt`, pinned identically in `.pre-commit-config.yaml` |

## Test modules

Counts are `def test_*` methods per module, verified by AST walk and consistent with the
runner's total of 382.

| Module | Classes | Tests | What it actually covers |
| --- | --- | --- | --- |
| `test_ai_agent.py` | 6 | 33 | `TradeProposal` payload contract, JSON extraction from prose/fenced output, heuristic engine sizing, token cost accounting, and real CrewAI crew construction (guardrail annotation contract, tool schemas). |
| `test_api.py` | 5 | 19 | REST auth, portfolio payload + bounded query count, graceful market-data degradation, autonomy toggle confirmation rules, decision-log filtering/scoping, transaction listing, `/healthz`, token issuance. |
| `test_dashboard_api.py` | 7 | 29 | Equity snapshot series and hour filtering, recommendation list/approve/reject (incl. 409 on a second decision and 500-not-crash on failure), advisory sweep dispatch, SPA serving (no build → 404, `index.html` no-cache, path traversal blocked), API-over-SPA route precedence. |
| `test_guardrails.py` | 3 | 17 | The execution guard: allocation ceiling and clamping, daily-loss limit (blocks BUY, allows SELL), HOLD never executes, missing/zero price, zero amount, SELL without or larger than a position, fractional crypto sizing. |
| `test_llm_hooks.py` | 4 (+1 helper) | 19 | LLM transport interceptor: workspace-id and arbitrary extra headers, malformed-JSON tolerance, and an end-to-end test against a mock Anthropic HTTP endpoint that asserts the header arrives **on the wire**. |
| `test_mcp.py` | 5 | 24 | MCP server over the real SDK: handshake, advertised tool set, per-tool JSON payloads, degradation, no-ORM-query isolation, logging forced to stderr, and MCP-vs-local tool routing in the crew. |
| `test_models.py` | 4 | 11 | Schema contract: documented defaults, one-portfolio-per-user, negative balance and non-positive amount rejected at the database, cost-basis/market-value derivations, log/transaction FK behaviour. |
| `test_recommendations.py` | 8 | 36 | Human-in-the-loop: creation and de-duplication, TTL expiry, approval re-validated at current prices (clamping, loss-limit block, SELL-without-position), double-approval refusal, audit rows, snapshots, debate persistence. |
| `test_security.py` | 3 | 9 | `env_required` semantics and the repository-wide secret scan — see below. |
| `test_services.py` | 6 | 31 | Ticker normalisation, quote cache/fallback paths, lexicon sentiment (negation, intensifiers), RSS parsing including billion-laughs and XXE rejection, ledger replay (weighted average cost, realised P&L), portfolio metrics. |
| `test_tasks.py` | 4 | 19 | Celery task bodies: approved BUY/SELL ledger mutation and audit trail, clamping, loss-limit block, rollback on execution failure, crash capture, monitoring fan-out isolation, risk sweep halting, token purge. |
| `test_telegram.py` | 9 (+2 helpers) | 45 | Bot formatting and 64-byte callback budget, link/unlink flow, reports, approve/reject/why callbacks, notification opt-out, webhook secret and always-ACK behaviour, and cross-user portfolio scoping. |
| `test_throttle.py` | 4 | 18 | Sweep-ticker cooldowns and sweep debounce: effectiveness, per-ticker/per-portfolio independence, zero-window disable, explicit release, and **fail-open** when the cache is down. |
| `test_websockets.py` | 3 | 14 | ASGI consumer auth (anonymous closed with policy code), ping/pong, unknown frames ignored, event relay to the owning user only, Decimal serialisation, soft failure without a channel layer. |
| `test_regressions.py` | 15 | 58 | One class per defect found by a review rather than by a failing test: the `portfolio.snapshot` payload shape, per-caller sweep scoping and the truthful `429`, scoped debounce keys, link-code expiry, last-use token purge plus the activity stamp's cost bound, defensive `limit` parsing, the `autonomy.changed` broadcast, webhook fail-closed, trade-notification delivery, scheduled-snapshot emission, execution preconditions surviving `python -O`, ticker validation, log-forging neutralisation, and SPA path containment. Each class is named for the behaviour that was wrong, so a regression reads as a sentence. |

`core/tests/helpers.py` is not a test module. It supplies `TEST_CACHES` (a `LocMemCache`
override replacing the shared Redis cache), the `make_user`/`make_portfolio`/`make_asset`
factories and `auth_client()` for token-authenticated API calls.

## Test strategy

The suite has three tiers, distinguished by what boundary they cross.

| Tier | Modules | Boundary crossed |
| --- | --- | --- |
| Pure logic | `test_guardrails`, `test_services`, `test_throttle`, `test_ai_agent` (parsing/heuristics) | None — plain inputs and outputs. |
| Database / API integration | `test_models`, `test_recommendations`, `test_tasks`, `test_api`, `test_dashboard_api`, `test_telegram` | Real PostgreSQL via `TestCase` transactions, real DRF routing, real ORM constraints. |
| Transport / protocol integration | `test_websockets`, `test_mcp`, `test_llm_hooks` | A live channel layer, the real MCP SDK session, and a real localhost HTTP server respectively. |

### How external services are isolated

Nothing in the suite performs network I/O. Three boundaries are stubbed:

| Dependency | Isolation point | Evidence |
| --- | --- | --- |
| yfinance | `services.market_data._fetch_from_yfinance` is patched, so the *real* cache, retry and stored-price fallback logic still executes | `test_services.MarketDataTests`, `test_api.PortfolioEndpointTests` |
| Consumer call sites | Modules import `get_latest_quote` into their own namespace, so tests patch the call site (`tasks.get_latest_quote`, `services.recommendations.get_latest_quote`, `mcp_server.server.get_latest_quote`) — narrower than patching the provider and independent of its internals | 8, 13 and 4 patch sites respectively |
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
| `test` — "Tests (PostgreSQL 16)" | `lint` | install `requirements-dev.txt` (hash-verified); wait for PostgreSQL via a `psycopg2` retry loop (30 × 2 s); `makemigrations --check --dry-run`; `migrate --noinput`; `test core.tests --verbosity=2` | Model changes without a migration, a failing migration, or any failing test. |
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
`--exit-non-zero-on-fix` and `ruff-format` pinned to `v0.16.9` — the same version as
`requirements-dev.txt`, after commit `ca60782` realigned them — plus `gitleaks v8.28.0`,
`detect-private-key`, `check-added-large-files --maxkb=1024`, and whitespace/EOF/YAML/JSON/TOML
/merge-conflict/mixed-line-ending hooks.

**Dependabot** (`.github/dependabot.yml`) runs monthly (06:00 UTC on the 1st) for three
ecosystems, with a limit of 3 open PRs for pip and Actions and 2 for Docker. The config's
comment records why: the first version opened 11 PRs within minutes, mostly single-package
major bumps. Grouping splits each ecosystem into a major group and a minor/patch group, with
security-relevant surfaces kept individually visible:

| Ecosystem | Groups | Documented ignores |
| --- | --- | --- |
| pip | `python-major`, `python-minor-patch`, `django`, `ai` (`crewai*`, `openai*`, `anthropic*`, `pydantic*`, `yfinance*`), `celery` | `openai >= 3.0.0` and `pydantic >= 2.13.0` (crewai 1.15.23 pins both — the PRs cannot install), `Django >= 6.0.0` (5.2 is the LTS line) |
| github-actions | `actions-major`, `actions-minor-patch` | — |
| docker | `docker-all` | `python >= 3.14` (every crewai release declares `Requires-Python <3.14`) |

## Running everything locally

The Makefile wraps the common paths; the underlying commands are shown beside them.

| Target | Command it runs | Purpose |
| --- | --- | --- |
| `make test` | `venv/bin/python manage.py test core.tests` | The full 382-test suite. Requires PostgreSQL reachable (`make up` starts `db` and `redis` only). |
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

`chromadb` is the one genuine risk acceptance on the project. All four advisories
report *no patched version*, `crewai` 1.15.23 pins `chromadb~=1.1.0`, and 1.15.23
is the newest `crewai` on PyPI — so there is no installable release that fixes
them. They are all server-side findings (code injection against a running Chroma
server, RBAC and tenant isolation), and this project never imports `chromadb`,
runs no Chroma server, publishes no port for one, and builds its Crew with
`memory=False`. The `ignore` entry in `dependabot.yml` records the triage and when
to revisit it; the alerts themselves are dismissed with the same reasoning, so the
decision is auditable rather than silent.

Two things to know before the first run: `config/settings.py` calls
`env_required("POSTGRES_PASSWORD")` at import time, so no management command works without it
(`make env` copies `.env.example` to `.env`); and the suite replaces the shared Redis cache with
`LocMemCache` per module, so a running Redis is not strictly required for the cache-backed
tests, only for the database.

Related pages: [[Execution-Guard]], [[Architecture]], [[Operations]], [[Configuration]],
[[API-Reference]], [[Telegram-Bot]], [[Real-Time-Layer]], [[Incident-Log]].
