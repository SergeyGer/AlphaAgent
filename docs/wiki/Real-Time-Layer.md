# Real-Time Layer

The dashboard is server-push: Celery workers publish domain events, a Channels
consumer relays them to one private group per user, and a React hook owns the
socket on the client. The whole layer is deliberately lossy — its failure mode is
a dashboard that goes stale, never a trade that fails.

## Transport

Django Channels runs over ASGI, served by **daphne on the same port as HTTP**
(`daphne -b 0.0.0.0 -p 8000 config.asgi:application` in the `Dockerfile` and in
the Compose `web` service). One process, one port, two protocols.

| Component | File | Responsibility |
| --- | --- | --- |
| ASGI entrypoint | `config/asgi.py` | `ProtocolTypeRouter`: `http` → Django, `websocket` → origin check → token auth → URL router |
| Route | `core/routing.py` | `ws/portfolio/` → `PortfolioConsumer` |
| Consumer | `core/consumers.py` | Group join/leave, greeting frame, event relay, `ping`/`pong` |
| WS auth | `core/ws_auth.py` | Resolve `?token=` to a `User` (or `AnonymousUser`) |
| Event bus | `services/events.py` | `publish()` + typed emitters, called from workers and the request path |
| Client hook | `frontend/src/hooks/usePortfolioStream.ts` | Single owner of the socket: backoff, frame validation, resync on reconnect |

Why not WSGI: WSGI is a request/response contract — one callable in, one response
out, no full-duplex frames and no way to hold a connection open for hours. A
WebSocket consumer is a long-lived ASGI application that receives handshake,
frame and disconnect events, so the platform ships `daphne` (pinned via
`channels[daphne]`) rather than `gunicorn`/`uwsgi`. In development Vite proxies
both `/api` and `/ws` to `127.0.0.1:8000` with `ws: true`.

The client builds the URL from `VITE_WS_BASE`, else an absolute `VITE_API_BASE`,
else the page origin, upgrading `http(s)` to `ws(s)`:

```ts
// frontend/src/api/client.ts
return appendToken(`${origin}${basePath}/ws/portfolio/`, token);  // …?token=<key>
```

## Authentication

A browser cannot set an `Authorization` header on a WebSocket handshake, so the
DRF token travels as a query parameter. `core/ws_auth.py` resolves it against
DRF's own `Token` model — the same credential `TokenAuthentication` checks — so
there is one source of truth for what a valid credential means.

| Step | Behaviour |
| --- | --- |
| Handshake | `TokenAuthMiddleware` parses `?token=`, looks the `Token` up via `database_sync_to_async`, and puts the `User` in `scope["user"]` |
| No/invalid token | Unknown token or `user.is_active is False` ⇒ `AnonymousUser`; a lookup error is logged as `"WebSocket token lookup failed"` and also treated as anonymous |
| Bad token close | The consumer **accepts**, then closes with `4401` (`CLOSE_UNAUTHORIZED`) so the client can distinguish "your token is bad" from "the network dropped" |
| Channel layer down at connect | Accept, then close with `4500` (`CLOSE_SERVER_ERROR`) |
| Application crash mid-stream | daphne closes with `1011` (`daphne/ws_protocol.py`, `handle_exception`) |
| Origin check | `AllowedHostsOriginValidator` wraps the whole WebSocket stack and matches the `Origin` header against `settings.ALLOWED_HOSTS` |
| Origin denied | `WebsocketDenier` rejects before accept ⇒ HTTP **403** handshake refusal |

Observed close codes in `logs/alphaagent.log`:

```
INFO alphaagent.ws:67 - WebSocket handshake rejected: missing or invalid token
INFO alphaagent.ws:75 - WebSocket disconnected (code=4401)
INFO alphaagent.ws:56 - WebSocket connected: user=demo group=portfolio.1
INFO alphaagent.ws:75 - WebSocket disconnected (code=1000)
```

`ALLOWED_HOSTS` comes from `DJANGO_ALLOWED_HOSTS` (default `localhost,127.0.0.1`;
Compose adds `0.0.0.0,web`). A **missing** `Origin` header is refused unless `*`
is allowed, so a browser (which always sends one) is checked on every handshake
and a script must set `Origin` deliberately to pass. The token is in the URL, so
it appears in proxy/access logs: serve over `wss://` in any real deployment and
keep query strings out of logs.

Each consumer joins the group `portfolio.<user_id>` and receives only that user's
events; `test_websockets.py` asserts one user's socket never sees another's
frames. The consumer is read-only: the only client frame it honours is
`{"type": "ping"}` → `{"type": "pong"}`, tested with a `trade.execute` frame that
is silently dropped. Every mutation goes through REST, where the normal
authentication, validation and execution guard apply.

## The channel layer

`CHANNEL_LAYERS` (`config/settings.py`) points at **Redis db 3** — separate from
the Celery broker (db 0), result backend (db 1) and Django cache (db 2).

| Setting | Value | Why |
| --- | --- | --- |
| `address` | `redis://<host>:6379/3` | Isolates pub/sub keys from Celery |
| `socket_timeout` | `30` | Read deadline: 6× the 5 s blocking pop window (see below) |
| `socket_connect_timeout` | `10` | Bounded connect, so a dead Redis cannot stall a worker |
| `socket_keepalive` | `True` | TCP keepalive on the pooled connection |
| `health_check_interval` | `30` | Idle connections are probed with `PING` instead of being discovered dead by a trading-path call |
| `capacity` | `1500` | Per-channel backlog ceiling |
| `expiry` | `10` | Live UI events are worthless once stale, so they are dropped rather than queued |

Defaults on the other side of that boundary, from the pinned libraries:
`channels_redis==4.3.0` sets `brpop_timeout = 5` and runs `BZPOPMIN` with that
timeout, and `redis==8.1.0` defaults to `DEFAULT_SOCKET_TIMEOUT = 5`,
`DEFAULT_SOCKET_CONNECT_TIMEOUT = 5`, `health_check_interval = 0` and a client
retry policy of `Retry(retries=DEFAULT_RETRY_COUNT=10)`. The three numeric socket
options override those defaults; `socket_keepalive` is already `True` in redis-py
8.1.0's TCP `Connection` and is set explicitly here to pin the behaviour.

### The bug: LIVE ↔ RECONNECTING flap

**Symptom.** The dashboard's connection pill flapped between `LIVE` and
`RECONNECTING` roughly every 90 seconds ([[Incident-Log|INC-003]]: 26 occurrences
in 40 minutes), and each flap fired a full REST resync (five endpoints) plus a
"Live stream restored" toast.

**Mechanism.** The consumer's receive loop blocks in `BZPOPMIN` for
`brpop_timeout` (5 s). With no explicit `socket_timeout` in the layer config,
redis-py 8.1.0 applies its own default read deadline of **5 s to the same
operation**, so the socket deadline and the blocking window were identical: any
scheduling or round-trip delay that pushed the reply past the deadline raised
`redis.exceptions.TimeoutError: Timeout reading from socket`. `AsyncConsumer`
only catches `StopConsumer`, so the exception escaped the ASGI application,
daphne called `sendCloseFrame(code=1011)`, and the browser's `onclose` started the
client backoff loop. Redis-py's default retry policy then stretched each failing
read to about a minute, which — with reconnect backoff on top — is the order of
the reported ~90 s cadence.

**Measurement** (this checkout, `redis==8.1.0`, `channels_redis==4.3.0`, Redis on
`127.0.0.1:6379`, `BZPOPMIN` on an unreachable channel, 4 iterations each):

| `socket_timeout` | Block window | Result | Elapsed per call |
| --- | --- | --- | --- |
| `None` (no read deadline) | 5 s | 4/4 clean `nil` | ~5.07 s |
| `5` (redis-py 8 default — pre-fix effective value) | 5 s | **4/4 `TimeoutError`** | ~58–60 s |
| `5`, block shortened to 1 s | 1 s | 1/1 clean `nil` | 1.04 s |
| `5`, retry disabled (`Retry(NoBackoff(), 0)`) | 5 s | 1/1 `TimeoutError` | 5.01 s |
| `30` (shipped config) | 5 s | **4/4 clean `nil`** | ~5.03–5.08 s |

The 5 s versus 1 s pair isolates the blocking window as the trigger; the
retry-disabled row (5.01 s vs 58.89 s) shows the ~60 s stall comes from the
default retry policy, not from Redis.

**Fix.** Explicit socket options in `CHANNEL_LAYERS` — `socket_timeout: 30`,
`socket_connect_timeout: 10`, `socket_keepalive: True`,
`health_check_interval: 30` — documented in place with the reasoning above.

**Effect.** The read deadline is now 6× the blocking window instead of equal to
it, so the race is gone by construction (`4/4` clean pops, no `TimeoutError`), and
idle connections are health-checked every 30 s rather than discovered dead by a
trading-path call. Verified against this checkout: a client authenticated with a
DRF token at `ws://127.0.0.1:8000/ws/portfolio/` completed the handshake in 0.12 s
and **held the socket open for 150 s** — past the window in which the flap used to
appear — receiving the `connection.established` greeting and no `1011` close. What
remains in the log is a rejected bad-token handshake (`4401`) and a normal
navigation close (`1000`).

## Event model

Every frame is `{"type": <event>, "payload": {…}}`. `services/events.serialize()`
converts `Decimal` → string and `datetime` → ISO-8601 UTC before the frame hits
Redis, because JSON has no decimal type. `publish()` targets the group
`portfolio.<user_id>`.

| Event type | Emitted by | Trigger | UI surface |
| --- | --- | --- | --- |
| `connection.established` | `PortfolioConsumer.connect` | Every accepted socket | Greeting frame only — the SPA keys `LIVE` off `onopen`, and `lib/stream.ts` filters this type out |
| `portfolio.snapshot` | `tasks._capture_after_trade` | Immediately after an executed trade (with the post-trade snapshot) | Metrics grid / allocation chart |
| `trade.executed` | `tasks.py`, `services/recommendations.py` | After `apply_trade()` succeeds, autonomous or approved | Trades table (prepended, de-duplicated by id) + success toast |
| `decision.created` | `tasks.py`, `services/recommendations.py` | Every agent run — HOLD, blocked, crash — and every human approve/reject | AI thoughts feed (`ThoughtsFeed`, capped at 100 rows) |
| `agent.thinking` | `tasks.py` | `analyst_started`, `cio_finished`, `failed` | Live "thinking" indicator in the same feed |
| `recommendation.created` | `services.recommendations.create_recommendation` | A new `PENDING` proposal | Recommendations panel (upsert) |
| `recommendation.updated` | approve / reject / block / expire paths | Any status transition | Recommendations panel (upsert) |
| `autonomy.changed` | `telegram_bot.py` only | The Telegram toggle changes `is_autonomous` | Header switch + info toast on other open dashboards |

Two asymmetries worth knowing: the periodic `capture_portfolio_snapshots_task`
writes snapshot rows **without** emitting (only the post-trade path emits), and
the REST toggle-autonomy endpoint does not emit either — it returns the new state
in its HTTP response, so a second open tab learns about the change only when it
refetches.

## Client side

`usePortfolioStream` is the single owner of `/ws/portfolio/`; nothing else in the
app opens a socket.

* **Connect** — one socket per token. `status` is `idle` without a token, then
  `connecting` → `live`; a new token tears the old socket down and reconnects.
* **Reconnect** — `onclose` always schedules a retry: exponential backoff from
  `1 s` to `30 s` with ±20 % jitter and a `500 ms` floor, surfaced as
  `attempt` and `nextRetryAt` so the pill can render "retry in Ns · attempt N".
  A manual `reconnect()` (the pill's Retry button) clears the timer and skips the
  remaining delay.
* **Resubscribe** — the client sends nothing to subscribe. Group membership is
  established server-side by `group_add` on every `connect()`, so a fresh socket
  is automatically re-subscribed; the greeting frame is informational.
* **Frame validation** — `lib/stream.ts` accepts only a JSON object whose `type`
  is in the frozen contract and whose `payload` is an object; anything else is
  dropped without touching state.
* **Resync on reconnect** — `onOpen(isReconnect)` fires `refreshAll()`:
  portfolio, snapshots, transactions, decision logs and recommendations, plus the
  "Live stream restored — portfolio resynced." toast. This is the recovery path
  for everything missed: channel-layer `expiry` is 10 s and there is no replay,
  so REST is the source of truth and the socket is an overlay on it.

## Fire-and-forget semantics

`publish()` never raises. It returns `False` for an unknown user or a missing
channel layer, and wraps the `group_send` in a `try/except` that logs
`"Failed to publish …"` and returns `False`:

```python
except Exception as exc:
    logger.warning("Failed to publish %s to user %s: %s", event_type, user_id, exc)
    return False
```

That is deliberate, and the module docstring states the rule: *a live-update
failure must never fail a trade.* The ordering enforces it — the trade is
committed first, then the event is emitted — so a Redis outage costs a stale
dashboard, never a rolled-back execution or a lost ledger row.
`test_websockets.py` asserts the contract directly
(`test_publish_without_a_channel_layer_fails_softly`). The price of that
guarantee: delivery is at-most-once, there is no acknowledgement, no ordering
guarantee across emitters, and no replay — the client's reconnect resync is what
makes the loss acceptable. Group events are also best-effort inside the consumer:
a failing `send_json` is logged, not retried, and `group_discard` failures on
disconnect are swallowed so a socket teardown can never wedge a worker.

## Defects found in this layer, and their fixes

Four gaps were identified while documenting this layer. All four are now fixed.
They are recorded here because each is a trap worth knowing about before touching
the event path.

| Defect | Fix |
| --- | --- |
| **`portfolio.snapshot` blanked the dashboard.** The client type and handler expect `{metrics, assets, captured_at}`, but `emit_portfolio_snapshot()` published the snapshot-row fields instead. The handler destructured two absent keys and wrote `undefined` over its own state, emptying the metric cards and allocation panel until the next REST resync. | A single `build_portfolio_payload()` in `core/serializers.py` now serves **both** the REST endpoint and the emitter, so the two cannot describe the portfolio differently. The event forwards `metrics` and `assets` from it, and the dashboard additionally merges only keys that are present and non-null — so a partial frame can never blank it again. |
| **`offline` was unreachable.** Declared in `ConnectionStatus` and rendered as `DISCONNECTED`, but the hook never set it, so a dead endpoint showed `RECONNECTING` forever. A rejected token (`4401`) also retried in a loop that could never succeed. | Two terminal rules: `4401` goes offline immediately, and `MAX_RECONNECT_ATTEMPTS` (8) gives up once the backoff is exhausted. The check lives at the single choke point `scheduleReconnect()`, so it also covers a constructor throw. The existing Retry button clears the counter and restarts the loop. |
| **The `connection.established` greeting was discarded.** Absent from `KNOWN_TYPES`, so the frame was parsed and dropped. | Added to the allowlist with a typed `ConnectionEstablishedEvent` variant and consumed as a liveness signal: it confirms the server-side consumer accepted the socket and resets the reconnect backoff. |
| **The REST autonomy toggle emitted nothing.** Only the Telegram path published `autonomy.changed`, so toggling from the dashboard left other open surfaces showing a stale state. | `ToggleAutonomyView` now emits it too, passing the portfolio so the emitter reads id and current state from one source. |

Related: [[Architecture]] for the process topology, [[Configuration]] for
`CHANNEL_LAYERS` and Redis layout, [[Execution-Guard]] for what happens before an
event is ever emitted, and [[Incident-Log]] for the flap write-up.
