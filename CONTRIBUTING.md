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
python manage.py test core.tests.test_tasks -v 2
```

## Reporting bugs and requesting features

Use the issue templates. For security problems, follow [SECURITY.md](SECURITY.md)
instead of opening a public issue.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
