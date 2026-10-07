# Configuration

AlphaAgent is configured entirely through the environment (12-factor style). `config/settings.py`
loads `BASE_DIR/.env` with `python-dotenv` and then reads every value through the `env_*` helpers.
No credential is hard-coded in the repository; the only secret-bearing file is `.env`, which is
git-ignored.

## How values are loaded

| Helper | Behaviour |
| --- | --- |
| `load_dotenv(BASE_DIR / ".env")` | Loads the file without overriding variables already present in the process environment, so container/host env wins |
| `env_bool(name, default)` | Truthy set is `1`, `true`, `yes`, `on` (case-insensitive); unset or anything else uses `default` / `false` |
| `env_int` / `env_float` | Unset **or unparseable** values fall back to the default instead of raising |
| `env_list` | Comma-separated; whitespace stripped, empty items dropped |
| `env_required(name, hint)` | Strips the value and raises `ImproperlyConfigured` when it is empty — used for `POSTGRES_PASSWORD` |

## Security-critical variables

| Variable | Why it matters |
| --- | --- |
| `POSTGRES_PASSWORD` | **Mandatory, no fallback.** `env_required` aborts startup if empty, and Compose uses `${POSTGRES_PASSWORD:?...}` so the `db` service refuses to start without it |
| `DJANGO_SECRET_KEY` | **Required when `DJANGO_DEBUG=false`**; startup raises `ImproperlyConfigured`. With debug on, an ephemeral `secrets.token_urlsafe(64)` is generated per process — convenient locally, but sessions and tokens die on restart |
| `TELEGRAM_WEBHOOK_SECRET` | Verifies inbound webhook calls via `X-Telegram-Bot-Api-Secret-Token`. If empty while a bot token **is** configured, the endpoint **refuses** calls with `503` rather than accepting unsigned posts — always set it in production |
| `AI_LLM_API_KEY` | Empty means no LLM credential: the deterministic engine runs instead (see fallback table below) |
| `POSTGRES_DB` / `POSTGRES_USER` | Not secret, but they must match the `db` service or the app cannot connect |

## Django core

| Variable | Default | Required? | Purpose |
| --- | --- | --- | --- |
| `DJANGO_SECRET_KEY` | — (ephemeral when `DJANGO_DEBUG=true`) | Yes when debug is off | Django's primary signing key (CSRF tokens, messages, password-reset links) |
| `DJANGO_DEBUG` | `false` | No | Debug pages, verbose errors and the DRF browsable API renderer |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | No | Comma-separated `Host` allow-list; Compose adds `0.0.0.0,web` |
| `DJANGO_LOG_LEVEL` | `INFO` | No | Level for the root, `django`, `alphaagent` and `celery` loggers |
| `DJANGO_MAX_UPLOAD_BYTES` | `5242880` (5 MiB) | No | Caps request body size (`DATA_UPLOAD_MAX_MEMORY_SIZE`) |
| `DJANGO_SECURE_SSL_REDIRECT` | `false` | No | Redirect HTTP to HTTPS; enable behind a TLS terminator |
| `DJANGO_SESSION_COOKIE_SECURE` | `false` | No | Send the session cookie over HTTPS only |
| `DJANGO_CSRF_COOKIE_SECURE` | `false` | No | Send the CSRF cookie over HTTPS only |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` | No | HSTS max-age; `0` disables the header |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | `false` | No | Adds `includeSubDomains` to HSTS |
| `DJANGO_SECURE_HSTS_PRELOAD` | `false` | No | Adds `preload` to HSTS |
| `DJANGO_USE_X_FORWARDED_PROTO` | `false` | No | Trust `X-Forwarded-Proto: https` from a reverse proxy |

The bundled Compose stack serves plain HTTP on `:8000`, so every HTTPS-related flag defaults off.
`SECURE_CONTENT_TYPE_NOSNIFF` (`true`), `SECURE_REFERRER_POLICY` (`same-origin`) and
`X_FRAME_OPTIONS` (`DENY`) are always on and are not configurable.

## Database (PostgreSQL)

| Variable | Default | Required? | Purpose |
| --- | --- | --- | --- |
| `POSTGRES_DB` | `alpha_agent_db` | No | Database name |
| `POSTGRES_USER` | `alpha_user` | No | Database role |
| `POSTGRES_PASSWORD` | — | **Yes** | Password for both Django and the `db` service; startup fails without it |
| `POSTGRES_HOST` | `127.0.0.1` | No | Compose overrides this to `db` |
| `POSTGRES_PORT` | `5432` | No | Compose pins `"5432"` |
| `POSTGRES_CONN_MAX_AGE` | `60` | No | Persistent connection lifetime in seconds |

`connect_timeout` is fixed at 10 seconds in `DATABASES["default"]["OPTIONS"]`; it is not
environment-driven. The engine is always `django.db.backends.postgresql` — there is no SQLite path.

## Redis and Celery

| Variable | Default | Required? | Purpose |
| --- | --- | --- | --- |
| `REDIS_HOST` | `127.0.0.1` | No | Redis host used by cache, broker, result backend and channel layer |
| `REDIS_PORT` | `6379` | No | Redis port |
| `CELERY_BROKER_URL` | `redis://<REDIS_HOST>:<REDIS_PORT>/0` | No | Task broker |
| `CELERY_RESULT_BACKEND` | `redis://<REDIS_HOST>:<REDIS_PORT>/1` | No | Task result store (`CELERY_RESULT_EXPIRES` is 24h) |
| `CELERY_TASK_TIME_LIMIT` | `600` | No | Hard per-task limit in seconds |
| `CELERY_TASK_SOFT_TIME_LIMIT` | `540` | No | Soft limit; raises `SoftTimeLimitExceeded` inside the task |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | No | Run tasks in-process — used by tests and CI to avoid a live worker |

Derived from the same host/port, and **not** configurable by environment:

| Setting | Value | Notes |
| --- | --- | --- |
| Django cache | `redis://<host>:<port>/2`, prefix `alphaagent`, timeout 300s | Market-data and news de-duplication |
| Channel layer | `redis://<host>:<port>/3`, capacity 1500, expiry 10s | WebSocket fan-out on a separate DB index so a cache flush cannot drop live sockets |
| Beat schedule | monitoring and snapshots every 15 min; daily-loss sweep 21:05 Mon–Fri; recommendation expiry every 30 min; token purge 03:30 | Defined in `CELERY_BEAT_SCHEDULE`, not env-driven |

Serializers are JSON, the timezone is UTC, and `CELERY_TASK_ACKS_LATE` with
`CELERY_WORKER_PREFETCH_MULTIPLIER = 1` keep long agent tasks from being double-delivered.

## AI / LLM provider

Every variable here maps to a key in the `AI_CONFIG` dictionary. An empty `AI_LLM_API_KEY`, or a
provider of `none`/`disabled`/empty, marks the crew as unconfigured and the deterministic engine is
used instead.

| Variable | `AI_CONFIG` key | Default | Required? | Purpose |
| --- | --- | --- | --- | --- |
| `AI_LLM_PROVIDER` | `PROVIDER` | `deepseek` | No | Lower-cased selector, e.g. `deepseek`, `openai`, `anthropic`; `none`/`disabled`/empty disables the LLM |
| `AI_LLM_MODEL` | `MODEL` | `deepseek-reasoner` | No | Model id passed to CrewAI/LiteLLM; DeepSeek models get a `deepseek/` prefix |
| `AI_LLM_API_KEY` | `API_KEY` | — | To use an LLM | Credential; empty disables the crew |
| `AI_LLM_BASE_URL` | `BASE_URL` | — | No | Override for gateways; DeepSeek defaults to `https://api.deepseek.com/v1` |
| `AI_LLM_TEMPERATURE` | `TEMPERATURE` | `0.2` | No | Sampling temperature |
| `AI_LLM_MAX_TOKENS` | `MAX_TOKENS` | `2048` | No | Response token ceiling |
| `AI_REQUEST_TIMEOUT` | `REQUEST_TIMEOUT` | `90` | No | Per-request timeout in seconds |
| `AI_MAX_RETRIES` | `MAX_RETRIES` | `2` | No | LLM retries; also drives market-data attempts (`retries + 1` = 3) |
| `AI_ALLOW_HEURISTIC_FALLBACK` | `ALLOW_HEURISTIC_FALLBACK` | `true` | No | See the fallback table below |
| `AI_NEWS_ARTICLE_LIMIT` | `NEWS_ARTICLE_LIMIT` | `10` | No | Headlines fetched per ticker when the caller does not pass `limit` |
| `AI_PRICE_CACHE_TTL` | `PRICE_CACHE_TTL` | `60` | No | Quote cache TTL in seconds |
| `AI_NEWS_CACHE_TTL` | `NEWS_CACHE_TTL` | `900` | No | News cache TTL in seconds |
| `AI_TOOLS_VIA_MCP` | `TOOLS_VIA_MCP` | `false` | No | Route crew tools through the MCP server instead of in-process calls |
| `AI_MCP_SERVER_URL` | `MCP_SERVER_URL` | — | When MCP routing is on | MCP endpoint; Compose sets `http://mcp:8100/mcp` |
| `AI_LLM_WORKSPACE_ID` | `WORKSPACE_ID` | — | For unscoped Anthropic keys | Injects the `anthropic-workspace-id` request header |
| `AI_LLM_EXTRA_HEADERS` | `EXTRA_HEADERS` | — | No | JSON **object** of extra request headers; invalid JSON is logged and ignored |

`AI_MODEL_PRICING` (USD per 1M tokens, used to fill `AgentDecisionLog.api_cost_usd`) is hard-coded in
settings — `deepseek-reasoner`, `deepseek-chat`, `gpt-4o`, `gpt-4o-mini`, with a DeepSeek-shaped
`AI_DEFAULT_PRICING` fallback for unknown models.

### `ALLOW_HEURISTIC_FALLBACK`

The heuristic engine is a deterministic, offline decision engine. This flag decides what happens
when the LLM path fails:

| Situation | `true` (default) | `false` |
| --- | --- | --- |
| No API key / provider disabled | Heuristic result, `source="heuristic_fallback"` | Same — the key check happens before the flag is consulted |
| Crew raises (network, provider, missing SDK) | Log, heuristic result, error recorded on the run | Re-raise: the sweep fails loudly |
| Crew returns an unparseable proposal | Log, heuristic result | `RuntimeError("CrewAI returned an unparseable TradeProposal")` |

Set it to `false` when real trades depend on the model and a silent downgrade to heuristics would be
worse than a failed sweep; keep it `true` for demos and market-data/provider outages. The fallback
still passes through the [[Execution-Guard]], so the flag changes *who decides*, never the limits.

## Trading loop and approvals

| Variable | Default | Required? | Purpose |
| --- | --- | --- | --- |
| `ALPHA_WATCHLIST` | `AAPL,TSLA,BTC` | No | Comma-separated tickers swept in addition to held positions |
| `RECOMMENDATION_TTL_MINUTES` | `60` | No | How long a proposal stays actionable before it is auto-expired |
| `ALPHA_TICKER_COOLDOWN_SECONDS` | `900` | No | Minimum gap before the same `(portfolio, ticker)` pair runs again; `<= 0` disables throttling |
| `ALPHA_SWEEP_DEBOUNCE_SECONDS` | `60` | No | Minimum gap between two fan-out dispatches, so Beat and a manual trigger cannot double-dispatch |

## Telegram

| Variable | Default | Required? | Purpose |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | No | Bot credential from @BotFather; empty disables the integration (API returns `503` on link-code requests) |
| `TELEGRAM_WEBHOOK_SECRET` | — | In production | Shared secret echoed in `X-Telegram-Bot-Api-Secret-Token`. Empty with a bot token configured = `503`; unset entirely = the bot is disabled and the endpoint returns `200` |
| `TELEGRAM_WEBHOOK_URL` | — | No | Public HTTPS webhook URL; leave blank for local development and use `python manage.py telegram_poll` |
| `TELEGRAM_REQUEST_TIMEOUT` | `15` | No | Outbound Telegram API timeout in seconds |

`TELEGRAM_CONFIG["ENABLED"]` is derived from the presence of `TELEGRAM_BOT_TOKEN`.
`MAX_MESSAGE_CHARS` is fixed at 4000 (Telegram's limit) and is not configurable.
`TELEGRAM_WEBHOOK_URL` is read into `TELEGRAM_CONFIG["WEBHOOK_URL"]` but **not consumed anywhere in
this repository** — register the webhook with the Telegram API yourself (`telegram_bot.py` exposes a
`set_webhook` client method, but no command calls it), or run the `telegram_poll` command instead.

## MCP tool server

These are read by `python -m mcp_server`; the web/worker tier only needs `AI_TOOLS_VIA_MCP` and
`AI_MCP_SERVER_URL`. See [[MCP-Tool-Server]].

| Variable | Default | Required? | Purpose |
| --- | --- | --- | --- |
| `MCP_TRANSPORT` | `stdio` | No | `stdio` or `http`; the Compose service runs `http` |
| `MCP_HOST` | `0.0.0.0` | No | Bind address for the HTTP transport |
| `MCP_PORT` | `8100` | No | Bind port for the HTTP transport |
| `MCP_LOG_LEVEL` | `WARNING` | No | Root log level of the MCP process; Compose sets `INFO` |

## Frontend

| Variable | Default | Required? | Purpose |
| --- | --- | --- | --- |
| `SPA_DIST_DIR` | `<BASE_DIR>/frontend/dist` | No | Directory Django serves the compiled SPA from when `index.html` exists; a relative value resolves against the process working directory |
| `VITE_API_BASE` | — (same-origin relative URLs) | No | Build-time Vite variable for the REST base; not read by Django |
| `VITE_WS_BASE` | — (derived from `VITE_API_BASE` or the page origin) | No | Build-time Vite variable for the WebSocket base |

## Set by the application, not by you

| Name | Value | Notes |
| --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | `config.settings` | Defaulted by `manage.py`, `wsgi.py`, `asgi.py` and `celery.py`, so it never needs to be exported |
| `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `PIP_NO_CACHE_DIR`, `PIP_DISABLE_PIP_VERSION_CHECK` | set in the `Dockerfile` | Image-level runtime hygiene; overriding them is not supported |
| `HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` | relocated under `.runtime/` by `runtime_env.py` | Keeps CrewAI's store writable in sandboxes and containers |

## Minimal working .env

Defaults cover hosts, ports, watchlist and the whole AI/Telegram layer, so a local run needs three
values. In Docker the app tier reads `.env` via `env_file`, while `POSTGRES_HOST`, `REDIS_HOST` and
the broker URLs are overridden with the `db`/`redis` service hostnames.

```dotenv
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(64))"
DJANGO_SECRET_KEY=<64-char-random-string>
DJANGO_DEBUG=false
POSTGRES_PASSWORD=<strong-password>

# Optional: enables the LLM crew instead of the deterministic engine.
AI_LLM_API_KEY=<provider-api-key>

# Optional: enables the Telegram bot and webhook verification.
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_WEBHOOK_SECRET=<webhook-secret>
```

## `.env` hygiene

* **Never commit `.env`.** `.gitignore` lists `.env` and `.env.*`, re-including only
  `.env.example`; the template contains placeholders such as `replace-me-with-a-strong-password`.
* Never bake secrets into `docker-compose.yml`, the `Dockerfile` or CI — Compose interpolation
  (`${POSTGRES_PASSWORD:?...}`) is the supported path.
* CI uses its own throwaway secret values, which must never be reused outside CI.
* Rotate `POSTGRES_PASSWORD` in the database and `.env` together; the app reads it at startup only.
* `DJANGO_SECRET_KEY` rotation invalidates signed artefacts such as CSRF tokens and password-reset
  links. DRF tokens are random rows in the database, not derived from the key, so they survive
  rotation — purge them explicitly if they must be revoked.

Related pages: [[Architecture]], [[Operations]], [[API-Reference]], [[Agent-Debate]], [[Telegram-Bot]].
