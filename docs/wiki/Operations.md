# Operations

Runbook for the AlphaAgent stack: topology, the Beat schedule, sweep throttling, the
audit-trail purge command, and how to read the system when it misbehaves.

Related pages: [[Architecture]] · [[Execution-Guard]] · [[Incident-Log]] · [[Configuration]] · [[MCP-Tool-Server]]

## Service topology

| Service | Container | Role | Command |
| --- | --- | --- | --- |
| `db` | `alpha_postgres` | Ledger, portfolio state, audit trail | `postgres:16-alpine` |
| `redis` | `alpha_redis` | Celery broker (db 0), result backend (db 1), cache (db 2), channel layer (db 3) | `redis:7-alpine` |
| `web` | `alpha_web` | HTTP API, admin, SPA, WebSocket endpoint | `migrate --noinput && exec daphne -b 0.0.0.0 -p 8000 config.asgi:application` |
| `worker` | `alpha_worker` | Runs the agent fan-out and every subtask | `celery -A config worker --loglevel=info --concurrency=4 --max-tasks-per-child=200 -n alphaagent-worker@%h` |
| `beat` | `alpha_beat` | Cron scheduler for the five Beat entries below | `rm -f /app/celerybeat-schedule && exec celery -A config beat --loglevel=info --schedule /app/celerybeat-schedule` |
| `mcp` | `alpha_mcp` | Read-only market-data tools over MCP | `python -m mcp_server --transport http --host 0.0.0.0 --port 8100` |

`web`, `worker`, `beat` and `mcp` share the `x-app` anchor: image `alphaagent:latest`,
`restart: unless-stopped`, the optional `.env` file, and `depends_on` on healthy
`db`/`redis`. The `environment` block overrides the `.env` 127.0.0.1 defaults with
in-network hostnames (`POSTGRES_HOST=db`, `REDIS_HOST=redis`, `AI_MCP_SERVER_URL=http://mcp:8100/mcp`).

**Why daphne/ASGI, not gunicorn.** The live dashboard consumes
`ws/portfolio/` (`core/routing.py` → `PortfolioConsumer`) through Django Channels.
A WSGI server cannot serve that upgrade, so `config/asgi.py` builds a
`ProtocolTypeRouter` with `http` and `websocket` branches, and the image `CMD` is
Daphne. Two operational details follow from this:

* `daphne` must stay first in `INSTALLED_APPS` — it installs the ASGI-aware
  `runserver` and must precede `django.contrib.staticfiles`.
* The Dockerfile *header comment* still says `web -> gunicorn config.wsgi:application`.
  That comment is stale documentation, not configuration; the `CMD` and the Compose
  `command` both use Daphne. Trust `docker compose config`, not the comment.

Compose defines `web`'s command as a YAML **list** on purpose: a folded scalar (`>`)
once truncated it to `gunicorn config.wsgi:application`, which fell back to
`127.0.0.1:8000`, one worker, and became unreachable from the host. Keep the list form.

## Bring-up and day-2 commands

```bash
cp .env.example .env                 # set DJANGO_SECRET_KEY and POSTGRES_PASSWORD
make docker-up                       # docker compose up -d --build
docker compose exec web python manage.py seed_demo --autonomous --risk high --balance 25000
docker compose ps                     # health column, then tail one service
docker compose logs --tail=100 worker # `make logs` tails the worker continuously
```

| Task | Command |
| --- | --- |
| Apply migrations | `docker compose exec web python manage.py migrate` |
| Restart one service after an env change | `docker compose up -d --force-recreate worker` |
| Stop everything, keep volumes | `make docker-down` |
| Rebuild after a dependency change | `make docker-build` then `docker compose up -d` |
| Run a task synchronously in eager mode | `CELERY_TASK_ALWAYS_EAGER=1 docker compose exec web python manage.py shell` |

`web` runs `migrate --noinput` on every boot, so keep it at one replica; a second
replica would race the same migration. Scale `worker` instead
(`docker compose up -d --scale worker=2`) — the per-ticker cooldown keeps two workers
from duplicating a run.

## Scheduled jobs (Celery Beat)

`CELERY_BEAT_SCHEDULE` in `config/settings.py`, `CELERY_TIMEZONE = UTC`:

| Beat entry | Task | Schedule (UTC) | `expires` | Effect |
| --- | --- | --- | --- | --- |
| `autonomous-market-monitoring` | `tasks.autonomous_market_monitoring_task` | `*/15` min | 900 s | One query for `is_autonomous=True` portfolios, then a single `group` publish of one `run_alpha_agent_task` per `(portfolio, ticker)` — held tickers first, then `ALPHA_WATCHLIST` |
| `capture-portfolio-snapshots` | `tasks.capture_portfolio_snapshots_task` | `*/15` min | 900 s | One `PortfolioSnapshot` row per portfolio; the dashboard equity series |
| `daily-loss-limit-sweep` | `tasks.daily_loss_limit_sweep_task` | `21:05`, Mon–Fri | 3600 s | End-of-session stop-loss: replay realised P&L, set `is_autonomous=False` on breach, write a `HALT - daily loss limit breached` decision log |
| `expire-stale-recommendations` | `tasks.expire_stale_recommendations_task` | `*/30` min | 1800 s | `PENDING` → `EXPIRED` past `RECOMMENDATION_TTL_MINUTES` |
| `purge-expired-auth-tokens` | `tasks.purge_expired_auth_tokens_task` | `03:30` daily | 3600 s | Deletes DRF `Token` rows that are **both** older than 30 days and owned by a user whose `last_login` is older than 30 days or null — last use, not issue date, so an actively authenticating client is never logged out (`core/auth.py`) |

* Run **exactly one** beat container — two copies double-fire every entry. The
  container deletes `/app/celerybeat-schedule` at start, so a stale schedule file
  cannot survive a restart (`celerybeat-schedule` is gitignored; never copy one
  between environments).
* `options.expires` is a deadline, not a timeout: if Beat was down and a 15-minute
  entry is more than 900 s late, the message is discarded instead of running stale.
* Beat is not the only producer. `tasks.notify_trade_task` and
  `tasks.notify_recommendation_task` are enqueued by the pipeline itself after an
  execution or a new proposal (`tasks.py`, `services/recommendations.py`), not on a
  schedule. Both are best-effort: `notify_trade_task` returns
  `{"status": "error", "error": "..."}` on failure and never fails a trade.
* Auth tokens are purged on **last use**, not issue date. `core/auth.py`'s
  `ActivityTokenAuthentication` records activity on the owner's `last_login`, and only
  when the recorded value is older than `ACTIVITY_RESOLUTION` (1 hour) — at most one
  `UPDATE` per user per hour rather than one per request. A `last_used` column cannot be
  added to DRF's `Token` (it has only `key`/`user`/`created`, with no swappable model),
  hence the user-side stamp.
* `CELERY_TASK_TIME_LIMIT=600` / `CELERY_TASK_SOFT_TIME_LIMIT=540` (env-overridable)
  bound one agent run; `acks_late=True` plus `CELERY_TASK_REJECT_ON_WORKER_LOST=True`
  mean a lost worker's task is redelivered. Redelivery is safe only because of the
  cooldown below — remove it and a kill -9 can become a double trade.

## Sweep throttling

`services/throttle.py`. The problem: `max_trade_allocation_pct` is a **per-trade**
ceiling, not a cumulative one. Beat fires every 15 minutes, a slow LLM sweep can
outlive its own interval, and `POST /api/portfolio/run-agent/` had no cooldown.

Two best-effort Redis slots guard the pipeline:

| Guard | Helper | Key | Default window | Env var |
| --- | --- | --- | --- | --- |
| Per-pair cooldown | `claim_ticker_slot(portfolio_id, ticker)` | `alpha:cooldown:{portfolio_id}:{TICKER}` | 900 s (one Beat interval) | `ALPHA_TICKER_COOLDOWN_SECONDS` |
| Fan-out debounce | `claim_sweep_slot(*, window=None, portfolio_id=None)` | `alpha:sweep:debounce` when unscoped, `alpha:sweep:debounce:{portfolio_id}` when scoped | 60 s | `ALPHA_SWEEP_DEBOUNCE_SECONDS` |

* `cache.add` is an atomic `SET NX`, so two workers racing for the same ticker
  cannot both win. The cooldown key is namespaced **per portfolio and ticker**:
  two users holding AAPL never block each other, and AAPL/TSLA/BTC are independent.
* Claim sites: `run_alpha_agent_task` claims the ticker slot as **step 0**, before
  any news, fundamentals, price or LLM work; both fan-outs claim the sweep slot —
  `dispatch_market_sweep`, the plain function that holds the sweep logic (the Celery
  task `autonomous_market_monitoring_task` is now only a Beat wrapper around it, and
  `POST /api/portfolio/run-agent/` calls it inline with the caller's `portfolio_id`),
  and `advisory_sweep_task`, which also passes its portfolio id. Scoping the key means
  a global Beat sweep cannot debounce an unrelated on-demand sweep for one portfolio,
  and two portfolios never debounce each other.
* Observable outcomes: a throttled pair returns
  `{"status": "cooldown", "reason": "ticker_cooldown"}`; a throttled fan-out returns
  `{"status": "debounced", "dispatched": 0}` and logs
  `skipped by the sweep debounce`. Both are normal, not errors — though the on-demand
  endpoint now converts a debounced result into `429` + `Retry-After: 60` instead of
  answering `202` as if it had dispatched ([[API-Reference]]).
* A window of `0` disables that guard entirely — do that only during an incident
  where you deliberately want overlapping sweeps.
* `release_ticker_slot()` exists but is **not** called by the task path (only tests
  use it). A run that claims the slot and then fails holds the window, so an errored
  ticker is not retried by the next Beat tick. To force a retry:

```bash
docker compose exec web python manage.py shell -c \
  "from services.throttle import release_ticker_slot; release_ticker_slot(1, 'AAPL')"
```

**The incident that motivated it.** Two sweeps three seconds apart analysed the same
ticker twice and bought AAPL twice. Each purchase was individually inside its
per-trade allocation limit, so the execution guard approved both — real exposure
compounded to roughly double the intended size. The ceiling was doing exactly what it
promised; the missing control was temporal, not financial. See INC-001 in [[Incident-Log]].

**Fail-open, deliberately.** Both claim helpers catch every cache exception, log a
warning and return `True` (`Cooldown check failed ... allowing the run`). The
trade-off is explicit: *losing a cooldown is a cost problem; refusing to run the risk
engine is a safety problem.* A Redis outage must never stop the stop-loss sweep or
block a de-risking SELL. The price is that a cache outage silently re-enables
duplicate sweeps — watch `alphaagent.throttle` warnings in `logs/alphaagent.log`. Both
keys live in the default cache (Redis db 2, `KEY_PREFIX=alphaagent`), which also
caches market quotes, so `FLUSHDB` on db 2 clears quotes *and* every cooldown.

## Audit-trail maintenance

`AgentDecisionLog` is append-only by design, so it grows without bound, and a
misconfigured provider can flood it with thousands of identical failure rows. Prune
it with `purge_decision_logs`, which refuses to run without an explicit selector.

```bash
docker compose exec web python manage.py purge_decision_logs                        # report only
docker compose exec web python manage.py purge_decision_logs --errors-only --dry-run
docker compose exec web python manage.py purge_decision_logs --errors-only
docker compose exec web python manage.py purge_decision_logs --older-than 90 --prune-snapshots 90
```

| Flag | Selector / effect |
| --- | --- |
| *(no flag)* | Prints current totals and exits — deletes nothing |
| `--errors-only` | Rows where `action_taken` starts with `ERROR` (failed agent runs) |
| `--older-than DAYS` | Rows older than `DAYS` — the retention policy |
| `--all` | Every row; combine deliberately, never by default |
| `--dry-run` | Prints the top 5 `action_taken` groups, the timestamp range, then stops |
| `--prune-snapshots DAYS` | Separate pass: deletes `PortfolioSnapshot` rows older than `DAYS` |

Safety rails, in execution order:

1. No selector at all → report only, exit 0: no accidental deletes from a bare command.
2. Multiple selectors are **ANDed**, so `--errors-only --older-than 30` means "failed
   runs older than 30 days", not the union.
3. Before deleting, the command counts targeted rows linked to a `Transaction` and
   raises `CommandError` (`Refusing to delete records of real executions`) if any
   exist — narrow the selector instead of forcing it.
4. Audit totals are printed before and after the pass, so the delta is on the record.
   Deletion is safe for recommendations: `TradeRecommendation.decision_log` is
   `SET_NULL`, and the recommendation keeps its own copy of the reasoning.

Suggested cadence: weekly `--errors-only --dry-run` (zero-difference is the healthy
state), monthly `--older-than 90`. A flood of new `ERROR` rows usually means an LLM
credential or model problem — fix the cause, then purge.

## Provider troubleshooting

`diagnose_provider_error()` (`ai_agent.py`) restates a provider rejection with the fix
attached; the message lands in the run result's `error` field and in
`AgentDecisionLog.reasoning` under `PIPELINE NOTE:`. Check it against this table first.

| Provider response / log line | Meaning | Configuration fix |
| --- | --- | --- |
| HTTP 400, body mentions workspace scoping / `anthropic-workspace-id` | Key is not scoped to an Anthropic workspace; CrewAI/LiteLLM do not send the header | Set `AI_LLM_WORKSPACE_ID=<workspace-id>` (Console → Settings → Workspaces) or use a workspace-scoped key; `AI_LLM_EXTRA_HEADERS` is the general escape hatch |
| `not_found_error` and `model` in the body | The model ID has been retired | Update `AI_LLM_MODEL` (verify against the provider's model list), then re-run the dry run |
| `authentication`, `401`, `invalid api key` | Credential rejected or rotated | Reissue `AI_LLM_API_KEY`; check for stray whitespace or a key from the wrong environment |
| `LLM provider '<p>' is not installed in this environment` | Provider SDK extra missing (e.g. `claude-*` needs `crewai[anthropic]`) | `pip install "crewai[<provider>]"`, add it to `requirements.txt`, `docker compose build` — or set `AI_LLM_PROVIDER=deepseek`/`openai` |
| `AI_LLM_EXTRA_HEADERS is not valid JSON, ignoring it` / `must be a JSON object` | Malformed header map; it is dropped silently apart from the log line | Fix the JSON object (single line, double quotes) |
| Decision log says `No LLM credential configured (AI_LLM_API_KEY empty)` | `AI_LLM_PROVIDER=none`/`disabled` or an empty key — the deterministic engine is running by design | Set a key if you want live reasoning; the platform is fully functional without one |
| `unparseable_llm_output` | Model returned something outside the JSON contract; the CrewAI guardrail already forced a retry | Lower `AI_LLM_TEMPERATURE`, or use a model that follows the schema |
| Audit trail filling with `ERROR - agent pipeline failed` | Provider failure with `AI_ALLOW_HEURISTIC_FALLBACK=false`, so the exception propagated and the task recorded a failure row | Fix the provider config, then `purge_decision_logs --errors-only` |

Verification loop — read-only, never touches the database:

```bash
docker compose exec web python manage.py dry_run_agent --ticker AAPL
docker compose exec web python manage.py dry_run_agent --portfolio-id 1 --no-news
```

It prints `provider`, `model`, `configured`, then per ticker either
`source : CrewAI (live LLM)` or `source : FALLBACK (<reason>)` — the fastest way to
confirm a config change took effect.

## Healthchecks, restart policies, and reading state

| Service | Healthcheck | Interval / timeout / retries / start period |
| --- | --- | --- |
| `db` | `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` | 5 s / 5 s / 12 / — |
| `redis` | `redis-cli ping` | 5 s / 3 s / 12 / — |
| `web` | `curl -fsS http://127.0.0.1:8000/healthz/` | 15 s / 5 s / 5 / 30 s |
| `worker` | `celery -A config inspect ping -d alphaagent-worker@$(hostname)` | 30 s / 15 s / 3 / 40 s |
| `mcp` | TCP connect to `127.0.0.1:8100` (the endpoint needs a JSON-RPC handshake) | 30 s / 10 s / 3 / 20 s |
| `beat` | none — a scheduler has no request surface | — |

`db` and `redis` use `restart: always`; the four app services inherit
`restart: unless-stopped`, so they survive crashes and host reboots but stay down
after a deliberate `docker compose stop`. A `depends_on` health condition means a slow
Postgres delays the app tier rather than crashing it.

`/healthz/` is a liveness probe only: no auth, no ORM query. It proves Daphne accepts
connections — not that Postgres, Redis or the worker are usable.

```bash
docker compose ps                                        # (healthy) / (unhealthy) / (health: starting)
docker inspect --format '{{json .State.Health}}' alpha_worker | python3 -m json.tool
docker compose logs --tail=100 --timestamps worker
docker compose exec web curl -fsS http://127.0.0.1:8000/healthz/
docker compose exec web tail -n 50 /app/logs/alphaagent.log
```

Logs go to stdout *and* to rotating files (`logs/alphaagent.log`, `logs/errors.log`,
10 MB × 5 backups each). `/app/logs` is not a named volume, so those files vanish when
the container is recreated — use `docker compose logs` for history.

## Common failures and what they mean

| Symptom | Cause | Action |
| --- | --- | --- |
| `docker compose up` aborts: `POSTGRES_PASSWORD must be set in .env` | Compose `${POSTGRES_PASSWORD:?...}` guard; `.env` missing or empty | `cp .env.example .env` and set the password (it must match the app's) |
| `web` exits: `ImproperlyConfigured: DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false` | No secret, debug off — there is deliberately no fallback | `python -c "import secrets; print(secrets.token_urlsafe(64))"` into `.env` |
| App tier restart-loops, `depends_on` never satisfied | `db` or `redis` unhealthy | `docker compose ps`, then `docker compose logs db redis` |
| `worker` unhealthy, `inspect ping` fails | Broker unreachable, or the node name no longer matches `-n alphaagent-worker@%h` | Check `CELERY_BROKER_URL`, keep the `-n`/healthcheck pair in sync |
| Sweeps report `debounced`, nothing analyses | Another fan-out for the **same** portfolio (Beat or a manual trigger) started inside the 60 s debounce | Expected. The key is scoped per portfolio, so an unrelated sweep no longer blocks a manual trigger; `POST /api/portfolio/run-agent/` surfaces this as `429` + `Retry-After: 60`. Tune `ALPHA_SWEEP_DEBOUNCE_SECONDS` only if manual triggers feel blocked |
| One ticker reports `cooldown` on every sweep | Its slot was claimed by a run that then failed and never released it | Wait out `ALPHA_TICKER_COOLDOWN_SECONDS`, or `release_ticker_slot` from a shell |
| Trades blocked: `Rejected: no reliable market price available.` | Cache and upstream quote both missed and no fallback price existed | Verify outbound network and the ticker symbol; news/fundamental failures alone only degrade to `None` |
| Two purchases for one signal | Cooldown set to `0`, Redis db 2 flushed, or an unthrottled path added | Restore a non-zero window; see [[Incident-Log]] |
| Dashboard flaps `LIVE` / `RECONNECTING` | Channel-layer socket deadline below the consumer's `BZPOPMIN` block | Keep `socket_timeout: 30` in `CHANNEL_LAYERS`; check Redis latency before blaming the client |
| `logs/` empty after a redeploy | `/app/logs` lives inside the container and has no volume | Read `docker compose logs`, or mount/ship the directory |

Backups: `pg_dump` from the `db` container is the only state you must keep — Postgres
holds the ledger, portfolios and audit trail. Redis holds the broker, results, quote
cache and cooldowns: losing it delays work and briefly disables throttling, so assume
duplicate sweeps are possible until the next window is claimed.
