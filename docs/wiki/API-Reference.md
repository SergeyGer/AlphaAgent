# API Reference

Django REST Framework API mounted at `/api/` by `config/urls.py` (routes in `core/urls.py`). Paths
are unversioned, payloads are JSON, and the browsable HTML renderer exists only when
`DJANGO_DEBUG=true` ([[Configuration]]). Verified against `core/urls.py`, `core/views.py`,
`core/serializers.py`, `core/exceptions.py` and `core/tests/`.

## Authentication

| Item | Value |
| --- | --- |
| Scheme | `core.auth.ActivityTokenAuthentication` (a DRF `TokenAuthentication` subclass that stamps the owner's `last_login`), then `SessionAuthentication` (browsable API / `/admin/`) |
| Header | `Authorization: Token <key>` — the literal keyword is `Token`, not `Bearer` |
| Anonymous result | `401` with `WWW-Authenticate`; default permission is `IsAuthenticated` |
| Token lifetime | No protocol expiry; a nightly task purges tokens abandoned for 30 days, judged by the owner's `last_login` |

`POST /api/auth/token/` exchanges credentials for a token. Repeat logins return the **same** row
(`Token.objects.get_or_create`) — there is no rotation on re-login.

```bash
API=http://127.0.0.1:8000
TOKEN=$(curl -sS -X POST "$API/api/auth/token/" -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"<your-password>"}' | jq -r .token)
curl -sS "$API/api/portfolio/" -H "Authorization: Token $TOKEN"
```

An unauthenticated call returns `{"error": true, "status_code": 401, "detail": "Authentication credentials were not provided.", "errors": {...}}`.

* **Ownership is enforced on every endpoint.** Querysets are filtered by `request.user`; no endpoint
  accepts a user or portfolio id from the client. Touching another user's recommendation returns
  `404`, never `403` (`RecommendationDecisionView` filters `pk` and `portfolio` together).
* All endpoints require `Authorization: Token <key>` except `POST /api/auth/token/` (public), `POST /api/telegram/webhook/` (shared secret), `GET /healthz/` (public) and `/admin/` (session auth + staff user).
* A `Portfolio` is auto-provisioned on first access with $10,000.00 cash, `medium` risk,
  `max_trade_allocation_pct=5.00` and `daily_loss_limit_usd=500.00`.
* `tasks.purge_expired_auth_tokens_task` (daily 03:30 UTC) deletes a token only when **both** signals
  are stale: the owner's `last_login` is older than 30 days (or null) *and* the token row was created
  before the cutoff. `core/auth.ActivityTokenAuthentication` stamps activity on authenticated
  requests at most once per user per hour (`ACTIVITY_RESOLUTION`), so a client that keeps
  authenticating keeps its token — the filter previously looked at `created` while documenting
  itself as last-use based. Session auth needs a CSRF token for unsafe methods.

## Conventions

**Error envelope.** `core.exceptions.alphaagent_exception_handler` rewrites every DRF error as
`{"error": true, "status_code": <int>, "detail": "<message>", "errors": <field errors|null>}`.
Unhandled exceptions become `500 {"detail": "Internal server error."}` — no stack trace is leaked.

**Pagination.** `PageNumberPagination`, `PAGE_SIZE = 50`, on every list endpoint except snapshots.
`?page=<n>` selects the page (`page=last` for the final one); `page_size` is **not** enabled.
Envelope: `{"count": 128, "next": ".../logs/?page=3", "previous": null, "results": []}`.
`GET /api/portfolio/snapshots/` is deliberately unpaginated (`{"count", "results"}` only) so a chart
client receives the whole series in one response.

**Rate limiting.** No DRF throttling class is configured; HTTP endpoints are not rate limited, and
`ALPHA_TICKER_COOLDOWN_SECONDS` / `ALPHA_SWEEP_DEBOUNCE_SECONDS` gate the agent sweep — see [[Configuration]].

## Portfolio

### `GET /api/portfolio/` — `200`, `401`, `405`

Portfolio with nested assets and computed metrics. Live prices resolve once per request into a shared
`price_map`; if market data is unavailable the API degrades to the stored cost basis instead of failing.

```json
{"id": 1, "username": "demo", "balance_usd": "10000.00", "risk_profile": "medium",
 "is_autonomous": false, "max_trade_allocation_pct": "5.00", "max_trade_budget_usd": "500.00",
 "daily_loss_limit_usd": "500.00", "metrics": {"cash_balance_usd": "10000.00", "positions_value_usd": "1500.00",
   "total_equity_usd": "11500.00", "unrealised_pnl_usd": "500.00", "is_autonomy_blocked": false,
   "block_reason": null, "unpriced_tickers": []},
 "assets": [{"id": 7, "ticker": "AAPL", "amount": "10.00000000", "avg_purchase_price": "100.00",
   "market_price": "150.00", "price_source": "yfinance", "market_value_usd": "1500.00"}]}
```

`metrics` also carries `invested_cost_usd`, `unrealised_pnl_pct`, `realised_pnl_today_usd`,
`realised_pnl_total_usd`, `daily_loss_limit_usd`, `daily_loss_used_usd` and
`daily_loss_remaining_usd`; each asset also carries `cost_basis_usd`, `unrealised_pnl_usd`,
`unrealised_pnl_pct` and `allocation_pct`. `is_autonomy_blocked` is `true` when the daily loss limit
is exhausted or buying power is zero; `unpriced_tickers` lists positions valued at cost.

### `GET /api/portfolio/snapshots/` — `200`, `401`

Oldest-first equity series for the dashboard chart (`pagination_class = None`). Each row: `id`,
`captured_at`, `total_equity_usd`, `cash_balance_usd`, `positions_value_usd`, `unrealised_pnl_usd`,
`realised_pnl_today_usd`.

| Query | Type | Default | Notes |
| --- | --- | --- | --- |
| `hours` | int | — | Keep points newer than `now - hours`; unparseable values are ignored |
| `limit` | int | `2000` | Parsed defensively: a non-numeric or blank value falls back to `2000`, then the result is clamped to `1..10000` (previously a non-integer value surfaced as `500`) |

### `POST /api/portfolio/toggle-autonomy/` — `200`, `400`, `401`, `405`

The only way to hand trading control to the AI. Enabling requires `confirm=true` and a portfolio
with positive cash and a non-zero allocation ceiling.

| Body | Behaviour |
| --- | --- |
| `{"is_autonomous": true, "confirm": true}` | Enable autonomy |
| `{"is_autonomous": false}` | Disable autonomy (no confirmation needed) |
| `{}` | Flip the current state; a flip **to** `true` still requires `confirm` |

```bash
curl -sS -X POST "$API/api/portfolio/toggle-autonomy/" \
  -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' \
  -d '{"is_autonomous": true, "confirm": true}'
# 200 {"portfolio_id": 1, "is_autonomous": true, "changed": true, "max_trade_budget_usd": "500.00",
#      "daily_loss_limit_usd": "500.00", "message": "Autonomous trading enabled."}
```

`400` means `confirm` is missing, the balance is `$0.00`, or the allocation ceiling is `0%`; a no-op
returns `"changed": false` with `200`.

## Trading

### `GET /api/portfolio/transactions/` — `200`, `401`

Immutable ledger, newest first, paginated (50 per page), scoped to the caller's portfolio. Row shape:
`id`, `ticker`, `tx_type` (`BUY`/`SELL`), `amount`, `price`, `gross_value_usd`, `executed_by`, `timestamp`.

| Query | Values | Notes |
| --- | --- | --- |
| `ticker` | e.g. `AAPL` | Upper-cased before filtering |
| `tx_type` | `BUY`, `SELL` | Upper-cased; an unknown value returns an empty page, not an error |

### `POST /api/portfolio/run-agent/` — `202`, `401`, `405`, `409`, `429`

Dispatches `tasks.dispatch_market_sweep(portfolio.id)` for the **caller's portfolio only** — the sweep
is scoped by portfolio id, so one user's click cannot spend every other autonomous user's LLM budget
(it previously dispatched the global Beat fan-out). The call is made inline rather than via
`.delay()`, so the response reports what actually happened: `202` with
`{"status": "queued", "portfolio_id": 1, "dispatched": <int>, "group_id": "b0c1..."}`. `dispatched` is
the number of `run_alpha_agent_task` subtasks published (`0`, with `group_id: null`, when there is
nothing to scan); `409` means the caller's portfolio has `is_autonomous=false`
(`{"error": true, "status_code": 409, "detail": "Autonomous trading is disabled for this portfolio.", "errors": null}`).
`429` means the per-portfolio 60 s debounce (`ALPHA_SWEEP_DEBOUNCE_SECONDS`) discarded the request:
the error envelope carries `"A sweep for this portfolio was requested moments ago. Wait for it to
finish before requesting another."` and the response sets `Retry-After: 60`. Previously the endpoint
answered `202` even when the debounce had discarded the request. The same sweep function backs the
Beat entry point, `tasks.autonomous_market_monitoring_task`.

## Agent

### `GET /api/portfolio/logs/` — `200`, `400`, `401`

AI decision audit trail (chain-of-thought, debate cases, token spend), newest first, paginated.
Row shape: `id`, `action_taken`, `market_sentiment`, `ticker`, `transaction_id`, `tokens_used`,
`api_cost_usd`, `reasoning`, `bull_case`, `bear_case`, `created_at`.

| Query | Values | Notes |
| --- | --- | --- |
| `sentiment` | `BULLISH`, `BEARISH`, `NEUTRAL` | Upper-cased; unknown values yield an empty page |
| `ticker` | e.g. `AAPL` | Matches the linked transaction; HOLD rows have `ticker: null` |
| `executed` | `1`/`true`/`yes` | Truthy set only; any other value selects rows **without** a transaction |
| `since` | ISO-8601 | `created_at >= since`; an unparseable value returns `400` |

### `POST /api/portfolio/analyse/` — `202`, `401`, `405`

Runs the AI in **advisory** mode: proposals are queued for approval, nothing executes, and autonomy
is not required. Returns `{"status": "queued", "task_id": "...", "mode": "advisory"}`.

## Recommendations

### `GET /api/portfolio/recommendations/` — `200`, `401`

Proposals awaiting (or having received) a human decision, newest first, paginated. Optional
`?status=` accepts `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `EXECUTED`, `BLOCKED`. Row shape:
`id`, `ticker`, `action` (`BUY`/`SELL`), `amount`, `price`, `notional_usd`, `sentiment`, `reasoning`,
`status`, `decided_via` (`WEB`/`TELEGRAM`/`API`/`AUTO`), `decided_at`, `expires_at`, `created_at`,
`is_actionable`, `transaction_id`; proposals expire after `RECOMMENDATION_TTL_MINUTES` (default 60).

### `POST /api/portfolio/recommendations/<id>/approve/`

Approving is **not** a bypass of the [[Execution-Guard]]: the trade is re-validated against current
prices, budget and the loss limit. A stale proposal is marked `BLOCKED`, still with HTTP `200`.

```bash
curl -sS -X POST "$API/api/portfolio/recommendations/12/approve/" -H "Authorization: Token $TOKEN"
# 200 executed: {"id": 12, "status": "EXECUTED", "executed": true, "transaction_id": 44,
#      "message": "Executed BUY 2 AAPL @ $100.00", "guard_reason": ""}
# 200 blocked:  {"status": "BLOCKED", "executed": false, "transaction_id": null, "guard_reason": "..."}
```

| Status | Condition |
| --- | --- |
| `200` | Decision applied (executed, or blocked with `status: "BLOCKED"` and `guard_reason`) |
| `404` | Unknown `id` **or** the recommendation belongs to another user |
| `409` | Already decided, or past `expires_at` (the message reads "already pending") |
| `500` | Unexpected failure while applying the decision |
| `401` | Missing or invalid token |

### `POST /api/portfolio/recommendations/<id>/reject/` — same codes

Marks the proposal `REJECTED` (`decided_via: "WEB"`), writes a matching audit row and starts no
trade. The body has `"executed": false` and `message: "Recommendation rejected."`.

## Market data

### `GET /api/market/news/?ticker=<TICKER>` — `200`, `400`, `401`

RSS headlines scored by the same lexicon the bull/bear agents argue from, split into
positive/negative/neutral lists. `ticker` is **required** (trimmed, upper-cased); `limit` defaults to
`10`, is clamped to `1..30`, and `400` means `ticker` was missing or blank.

```json
{"ticker": "AAPL", "headline_count": 5, "sources_used": ["Google News"], "degraded": false,
 "fetched_at": "2025-09-30T12:00:00Z",
 "sentiment": {"label": "BULLISH", "score": 0.42, "confidence": 0.61, "positive_hits": ["beat"],
               "negative_hits": [], "articles_scored": 5},
 "positive": [{"title": "...", "source": "...", "url": "...", "published_at": "...", "summary": "...",
               "polarity": 0.42, "sentiment": "BULLISH"}], "negative": [], "neutral": []}
```

`degraded` is `true` when no article could be fetched; the internal `articles` key is not part of
the serializer output — use the three split lists.

## Telegram

The integration is optional and degrades cleanly when `TELEGRAM_BOT_TOKEN` is empty. See
[[Telegram-Bot]] for the command surface.

### `GET /api/telegram/link/` — `200`, `401`

Returns `{"linked": false}`, or `{"linked": true, ...}` with `chat_id`, `telegram_username`,
`is_active`, `notify_trades`, `notify_recommendations`, `linked_at`, `last_seen_at` — only the
caller's own binding is ever returned.

### `POST /api/telegram/link/` — `200`, `401`, `503`

Mints a pairing code (16 hex, upper-cased) sent to the bot as `/start <code>`; `503` means no bot
token is configured. Until pairing, the row keeps a negative synthetic `chat_id`.

```bash
curl -sS -X POST "$API/api/telegram/link/" -H "Authorization: Token $TOKEN"
# 200 {"link_code": "A1B2C3D4E5F60718", "expires_at": "2025-09-30T12:15:00Z", "instructions":
#      "Open Telegram, start a chat with your AlphaAgent bot and send /start A1B2C3D4E5F60718"}
```

`expires_at` is `link_code_issued_at + TelegramLink.LINK_CODE_TTL` — the same 15-minute window the bot
enforces at redemption (`TelegramLink.redeemable`), so the advertised and enforced windows cannot
diverge. Redeeming clears both `link_code` and `link_code_issued_at`, so a consumed code is not
reusable; a code with no issue timestamp, or one older than the TTL, is rejected at redemption.

### `POST /api/telegram/webhook/`

Called by Telegram, not by clients. Unauthenticated by design (Telegram cannot send a DRF token) and
CSRF-exempt, but verified with the `X-Telegram-Bot-Api-Secret-Token` header. It **fails closed**: when
a bot token is configured but `TELEGRAM_WEBHOOK_SECRET` is empty, unsigned POSTs are refused rather
than skipped — previously an empty value disabled the check entirely and the endpoint accepted
unsigned posts from anyone.

| Status | Body | Condition |
| --- | --- | --- |
| `200` | `{"ok": true}` | Handled, or no bot token configured (the integration is simply off), or the handler raised — always acked so Telegram stops retrying |
| `403` | `{"ok": false}` | Secret configured and the header does not match |
| `503` | error envelope | Bot token configured but `TELEGRAM_WEBHOOK_SECRET` is empty (`"Telegram webhook is not configured on this deployment."`) |

## Health

### `GET /healthz/` — `200`

Public liveness probe: no auth, no database access; used by the Compose healthcheck. Returns
`{"status": "ok", "service": "AlphaAgent"}`.

## Static SPA and real-time

When `frontend/dist/index.html` exists (`SPA_DIST_DIR`), Django also serves the compiled dashboard;
these routes are registered **after** the API, admin and health endpoints, so they cannot shadow
`/api/`, `/admin/`, `/healthz/` or `/ws/`.

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/assets/<path>`, `/favicon.ico`, `/robots.txt`, `/manifest.webmanifest`, `/vite.svg` | Build assets; path traversal returns `404` |
| `GET` | `/<any client route>` | SPA shell (`index.html`) with `Cache-Control: no-cache` |

Unknown API routes return `404` — they are never rewritten to the SPA shell. The live dashboard streams
over `ws://<host>/ws/portfolio/?token=<key>` (token in the query string — browsers cannot set WebSocket
headers); see [[Real-Time-Layer]] and [[Quality-and-Testing]].
