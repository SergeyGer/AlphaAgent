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
make test               # 139 tests, no network required
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

## Quality gates

CI runs on every push and pull request:

| Job | What it enforces |
| --- | --- |
| `lint` | Ruff lint + format, `manage.py check --deploy` |
| `test` | Full suite against PostgreSQL 16 and Redis 7, migration drift |
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

## Reporting bugs and requesting features

Use the issue templates. For security problems, follow [SECURITY.md](SECURITY.md)
instead of opening a public issue.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
