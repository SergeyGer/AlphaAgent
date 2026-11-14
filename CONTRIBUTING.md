# Contributing to AlphaAgent

Thanks for your interest. This document covers everything you need to get a
change merged.

## Getting started

```bash
git clone https://github.com/SergeyGer/AlphaAgent.git
cd AlphaAgent

make env                # copy .env.example -> .env
make install-dev        # runtime + lint/test tooling
make up                 # PostgreSQL + Redis via Docker

# Edit .env: set POSTGRES_PASSWORD and DJANGO_SECRET_KEY to real values
make migrate
make seed               # demo user + API token
make test               # full suite (1029 at HEAD 0051067; the run prints the total), no network required
```

Run the API, worker and scheduler in three terminals:

```bash
python manage.py runserver 127.0.0.1:8000
celery -A config worker -l info
celery -A config beat   -l info
```

> Run **either** the local worker **or** the containerised one, never both.
> Celery has no queue ownership: two workers compete for the same Redis queue,
> and two Beats double every scheduled sweep.

## Development workflow

1. Fork the repository and branch from `main`
   (`feat/position-sizing`, `fix/guardrail-clamp`, …).
2. Make your change, adding tests.
3. Run `make check` — lint, format check, Django checks, migration check, tests.
4. Open a pull request against `main` and fill in the template.

Conventional Commits are appreciated (`feat:`, `fix:`, `docs:`, `test:`,
`refactor:`, `chore:`).

## Starting and stopping the stack

One command each way. Pick whichever suits the machine — VS Code tasks work on
Windows and Linux alike, `make` needs a POSIX shell (Linux, macOS, WSL or Git
Bash), and the raw compose command works everywhere:

| | Start | Stop (keep assembled) | Stop and release ports |
| --- | --- | --- | --- |
| VS Code | Run Task → *AlphaAgent: Start* | *AlphaAgent: Stop* | *AlphaAgent: Shut down* |
| Terminal | `make start` | `make stop` | `make down` |
| Any | `docker compose up -d --build` | `docker compose stop` | `docker compose down` |

Use **down**, not stop, when switching to another project for a while: stopped
containers still hold their published ports in some Docker Desktop configurations,
and two projects fighting over 8080 is the problem these commands exist to avoid.
Neither command removes volumes, so your database survives both.

Nothing starts by itself — no service sets a restart policy, so rebooting the
machine does not bring the stack back. Starting it is always deliberate.

Opening the folder in a Dev Container drives the same compose file; see
[`.devcontainer/README.md`](.devcontainer/README.md).

## Quality gates

CI runs on every push and pull request:

| Job | What it enforces |
| --- | --- |
| `lint` | Ruff lint + format, `manage.py check --deploy` |
| `test` | Full suite against PostgreSQL 16 and Redis 7, migration drift, coverage floor |
| `frontend` | Typecheck, production build, Vitest suite with its coverage floors |
| `docker` | The image builds, compose config is valid, the image imports cleanly |

Install the git hooks to catch most of this before you push:

```bash
pre-commit install
pre-commit run --all-files
```

## Code standards

- **Python 3.12**, 4-space indent, 100-column lines, double quotes (Ruff).
- **Type hints** on public functions.
- **No secrets in code, ever.** Credentials come from the environment.
  `config/settings.py` must contain no credential fallback.
- **Never add `from __future__ import annotations` to `ai_agent.py`.**
  PEP 563 turns annotations into strings, and CrewAI introspects them at runtime
  (`get_origin(...) is tuple` for guardrails, `_run` signatures for tool
  schemas). `CrewBuildTests` guards this.
- **Keep the AI layer read-only.** CrewAI tools must not touch the database.
  Persistence happens only in `tasks.py`, after the execution guard approves.

## Testing expectations

Tests must not touch the network. Patch `services.market_data._fetch_from_yfinance`
and `services.news.build_news_report`, and use the `TEST_CACHES` helper so the
suite never reaches Redis.

Any change to trading logic needs a corresponding case in
`core/tests/test_guardrails.py`. That file is the specification for when the AI
is allowed to move money.

```bash
make test                 # everything
make test-guardrails      # safety-critical subset
make coverage             # suite under coverage, with a report
python manage.py test core.tests.test_tasks -v 2
```

CI enforces a coverage floor of 70% (`coverage report --fail-under=70`). It is a
ratchet, not a target: it exists so a change cannot quietly delete tests. Raise it
when coverage rises; do not lower it to get a pull request through.

### Frontend

The dashboard has its own suite, and CI runs it in a separate job:

```bash
cd frontend
npm ci                # exactly the lockfile, as CI installs it
npm test              # vitest run
npm run test:coverage # with the thresholds applied
npm run typecheck
```

Two coverage floors apply rather than one, and the split is deliberate. `src/lib`
— the formatters, the WebSocket frame validator, the feed merge — holds the
frontend's real logic and must stay above 75%. The components and hooks are
covered where a silent failure would blank a panel rather than where the line
count is, so the global floor is a low ratchet that can only rise.

A warning worth heeding: the wire contract in `src/types.ts` and
`src/lib/stream.ts` is validated by tests on **both** sides. A new event type
needs adding to `KNOWN_TYPES` or the validator drops the frame before the
dashboard's `switch` ever sees it — silently, with no error, leaving whatever it
was meant to update quietly stale. `stream.test.ts` asserts every type, so a
failure there means exactly that.

## Documentation is part of the change

**A pull request that changes behaviour updates the page that documents it, in the
same pull request.** This is not a courtesy — it is the rule that keeps the
documentation from diverging from the code, and it is what a reviewer is expected
to check.

There are three places documentation lives, and they have different audiences:

| Where | Audience | What belongs there |
| --- | --- | --- |
| `README.md` | someone deciding whether to care | what the project is, what it demonstrates, how to see it running. No implementation detail. |
| `docs/wiki/` | someone using or operating it | architecture, configuration, the API, runbooks, incident history |
| `docs/adr/` | someone asking *why* | one decision per file, with the alternatives that were rejected |

Concretely, depending on what you changed:

| If you changed | Then update |
| --- | --- |
| A risk limit, or the guard's behaviour | `docs/wiki/Execution-Guard.md`, and `core/tests/test_guardrails.py` |
| A setting or environment variable | `docs/wiki/Configuration.md` and `.env.example` |
| An endpoint, payload or status code | `docs/wiki/API-Reference.md` |
| A model field, or a migration | `docs/wiki/Data-Model.md` |
| How the agents debate, or the prompt shape | `docs/wiki/Agent-Debate.md` |
| Something a runbook covers | `docs/wiki/Operations.md` |
| A new architectural choice, or reversing one | a **new** ADR in `docs/adr/` — never edit an accepted one |

Then publish:

```bash
make wiki-push            # mirrors docs/wiki/ to the GitHub Wiki
```

The repository copy is the source of truth. Editing a wiki page in the GitHub web
UI works until the next `make wiki-push`, which overwrites it — so make the change
here instead.

`Docs` CI checks all of this on every pull request: it link-checks every Markdown
file with lychee, verifies that every image and file referenced actually exists in
the repository, and confirms that wiki pages only cross-reference pages that
exist. A renamed screenshot or a retitled page fails the build.

### Keeping the issue tracker current

Issues are living specifications, not a record of what someone once intended.
The remediation programme showed the failure mode: PRs #60–#64 changed the
behaviour behind #29, #33, #34 and #35, but none of them referenced those issues,
so the issue bodies kept claiming the feature was absent. Use this process:

1. **Every open issue body names the commit it was audited against**, in a
   `## Current state at HEAD <sha>` section, and its acceptance criteria are
   checkboxes with the delivered ones ticked. A body with no commit is stale by
   default.
2. **A pull request that changes an issue's behaviour updates the issue in the
   same pull request.** Use `Refs #N` for a partial delivery, and `Closes #N`
   only when every acceptance criterion is met. The closing comment names the
   merge commit and the test that proves the last criterion — not the whole
   programme.
3. **A partial delivery is not a close.** Tick the criteria that shipped, add a
   "What remains" paragraph with the missing piece, and leave the issue open.
4. **At every release, re-audit the open issues against the release HEAD.** The
   Roadmap wiki links to the issues and must not mark an item done while its
   issue still tracks a missing criterion.
5. **Run the documentation gate after any of this:**
   `venv/bin/python manage.py test core.tests.test_documentation_links --noinput`.
   A source link that points at a moved line fails before the issue does.

## Technical specification: where each phase lives

The five-phase programme is recorded in
[`docs/spec/IMPLEMENTATION-NOTE.md`](docs/spec/IMPLEMENTATION-NOTE.md), with a
definition-of-record for each measurement surface:

| Document | Covers |
| --- | --- |
| [`docs/spec/MEASUREMENT.md`](docs/spec/MEASUREMENT.md) | Signal diagnostics, portfolio accounting, experiments and the report |
| [`docs/spec/ALLOCATION.md`](docs/spec/ALLOCATION.md) | The Phase 4 brief, target contract, projection and plan execution |
| [`docs/spec/FIXTURES.md`](docs/spec/FIXTURES.md) | Decision-run fixture schema, loader and honesty rules |
| [`docs/spec/METHODOLOGY.md`](docs/spec/METHODOLOGY.md) | What the numbers mean, and what they do not |
| [`docs/spec/CASES.md`](docs/spec/CASES.md) | The curated cases a reviewer should inspect |
| [`docs/spec/DEMO.md`](docs/spec/DEMO.md) | Exact commands for every offline demonstration |
| [`docs/spec/CLAIMS.md`](docs/spec/CLAIMS.md) | Every public claim and the file that supports it |

### Phase map

| Phase | Requirements | Where it is implemented | How to test it |
| --- | --- | --- | --- |
| **0 — Baseline** | Inventory, confirmed behaviours, recorded divergences | `docs/spec/IMPLEMENTATION-NOTE.md`; `docs/adr/` | Documentation review; no runtime surface |
| **1 — Explain one decision** | REP-02, REP-03, API-01 | `core/decision_models.py`, `core/decision_serializers.py`, `core/decision_views.py`, `services/evidence.py`, `services/decision_runs.py`, `core/management/commands/load_decision_fixtures.py`, `docs/spec/fixtures/decision-runs.v1.json` | `manage.py test core.tests.test_decision_runs` (includes the detail, events and export contracts) |
| **2 — Real rebuttals** | DEB-01 … DEB-05 | `services/debate_protocol.py`; the public entry point stays `ai_agent.run_alpha_agent`; legacy sequential mode is `AI_DEBATE_PROTOCOL=legacy_sequential` | `manage.py test core.tests.test_debate_protocol` |
| **3 — Measure value** | MET-01 … MET-05, EXP-01 … EXP-05 | `services/signal_diagnostics.py`, `services/portfolio_accounting.py`, `services/experiments.py`, `core/measurement_models.py`, `core/management/commands/build_experiment.py` | `manage.py test core.tests.test_portfolio_accounting core.tests.test_experiments` |
| **4 — Allocate capital** | PORT-01 … PORT-05 | `services/portfolio_brief.py`, `services/portfolio_policy.py`, `services/portfolio_targets.py`, `services/portfolio_allocation.py`, `core/allocation_models.py`, `core/management/commands/run_allocation.py` | `manage.py test core.tests.test_portfolio_allocation` |
| **5 — Publish evidence** | Methodology, curated cases, demo, contributor map, claims index | `docs/spec/METHODOLOGY.md`, `docs/spec/CASES.md`, `docs/spec/DEMO.md`, `docs/spec/CLAIMS.md`, this section | `manage.py test core.tests.test_documentation_links` |

The older dashboard attribution (`services/attribution.py`, ADR-0014) is not the
same engine as Phase 3. Before changing either, read
[`docs/spec/METHODOLOGY.md`](docs/spec/METHODOLOGY.md) §0: the two systems have
different point-in-time guarantees and must not share method versions or claims.

### Tests by layer

The suite is one Django/unittest tree; run the slice that covers your change.

```bash
# Everything (1029 at HEAD 0051067; the run prints the total)
venv/bin/python manage.py test core.tests

# Safety-critical guard and its portfolio-level ceilings
venv/bin/python manage.py test core.tests.test_guardrails \
    core.tests.test_exposure_limits core.tests.test_security

# Decision-run API and fixture loader
venv/bin/python manage.py test core.tests.test_decision_runs

# Phase 2 protocol: independence, rebuttals, adjudication, budgets, reproducibility
venv/bin/python manage.py test core.tests.test_debate_protocol

# Phase 3 measurement: accounting, leakage, fees, TWR, variants, report
venv/bin/python manage.py test core.tests.test_portfolio_accounting \
    core.tests.test_experiments

# Phase 4 allocation: brief, contract, projection, apply, concurrency
venv/bin/python manage.py test core.tests.test_portfolio_allocation

# HTTP and dashboard behaviour
venv/bin/python manage.py test core.tests.test_api core.tests.test_dashboard_api

# Documentation source links and the demo theme contract
venv/bin/python manage.py test core.tests.test_documentation_links \
    core.tests.test_demo_theme_contract

# Frontend (289 tests on this commit)
cd frontend && npm test
```

Counts are snapshots, not constants. The authoritative numbers are whatever the
current run reports; do not copy a count into a new document without naming the
commit. The commit-pinned snapshot at HEAD `0051067` is 1029 backend and 289
frontend tests, reproduced in [`README.md`](README.md),
[`docs/wiki/Quality-and-Testing.md`](docs/wiki/Quality-and-Testing.md) and
[`docs/spec/CLAIMS.md`](docs/spec/CLAIMS.md).

### Migrations are additive only

- New behaviour gets a **new** migration file in `core/migrations/`. Never edit
  or renumber an applied migration; a deployed database has already recorded its
  name and checksum in `django_migrations`.
- Add tables, nullable columns or columns with a safe default. Do not drop or
  rename a column in the same change that adds its replacement; add, backfill in
  a separate reviewed migration, then remove in a later release.
- A column that existing rows cannot answer honestly is nullable rather than
  defaulted. `Transaction.fee` is the reference case: `NULL` means "no fee was
  recorded", and `Decimal("0")` means "a zero fee was recorded". Backfilling a
  zero would turn an unknown into a false measurement.
- New state that is not a trade gets a separate table, not a new `Transaction`
  type. `CashFlow` exists because `services/ledger.replay_positions` treats every
  non-BUY transaction as a SELL, and `tx_type` is `varchar(4)`.
- CI runs `manage.py makemigrations --check --dry-run`; a model change without
  its migration fails the build. Run `make migrate` locally before the suite.

The Phase 1–4 tables arrived as migrations `0010`–`0013`; they are additive, and
no existing column was repurposed.

### Fixture and report commands

All of these are offline: no model key and no market-data request.

```bash
# Decision-run fixtures (idempotent; keyed by fixture_id)
venv/bin/python manage.py load_decision_fixtures
venv/bin/python manage.py load_decision_fixtures --path docs/spec/fixtures/decision-runs.v1.json
venv/bin/python manage.py load_decision_fixtures --username demo
venv/bin/python manage.py load_decision_fixtures --force   # only after a checksum change

# Phase 3 experiment report (rebuilds from stored rows)
venv/bin/python manage.py build_experiment
venv/bin/python manage.py build_experiment --force          # after a manifest checksum change

# Phase 4 paper allocation (synthetic fixture, stub CIO)
venv/bin/python manage.py run_allocation --reset --apply
venv/bin/python manage.py run_allocation --reset --apply --fail-leg AAPL

# Demo account for the browser/API
venv/bin/python manage.py seed_demo --username demo --password demo-pass-123
```

Fixtures are checksummed. If you intentionally edit a manifest, regenerate its
checksum with the script in [`docs/spec/FIXTURES.md`](docs/spec/FIXTURES.md) or
[`docs/spec/MEASUREMENT.md`](docs/spec/MEASUREMENT.md), then reload with
`--force`. Never hand-edit a fixture and leave the checksum stale: the loader
will refuse it, and a report built from mixed rows would be worse than a failed
command.

### A public claim must trace to a committed file

This is the Phase 5 gate and a standing rule, not a one-off review.

1. Every substantive claim in `README.md` and `docs/wiki/` must have a row in
   [`docs/spec/CLAIMS.md`](docs/spec/CLAIMS.md) naming the repository file that
   supports it — a fixture, a report, a test, an ADR, a settings value or a
   workflow.
2. A number taken from a live database, a live run or a local log is **not**
   evidence. Either commit a fixture/report that contains it, or do not publish
   the number.
3. If a claim has no source, fix the claim. Do not invent a source, and do not
   add a test that asserts the claim without asserting the behaviour.
4. When you add or change a public claim, update `CLAIMS.md` in the same pull
   request. A reviewer should be able to go from the sentence to the file in one
   hop.
5. Synthetic content is labelled at the point of use (`mode`, `is_synthetic`,
   `synthetic`, `synthetic_note`, or the report's honesty block). Never describe
   it as market evidence, a track record or a protocol comparison.

## Reporting bugs and requesting features

Use the issue templates. For security problems, follow [SECURITY.md](SECURITY.md)
instead of opening a public issue.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
