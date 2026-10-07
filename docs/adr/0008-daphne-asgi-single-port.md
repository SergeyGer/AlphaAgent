# 0008. Serve HTTP and WebSockets from one Daphne/ASGI process

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** Project maintainer
- **Related:** [ADR-0006](0006-fail-loudly-fail-open.md)

## Context

The dashboard is server-push: Celery workers publish domain events, a Channels
consumer relays them to one private group per user, and a React hook owns the
socket on the client. The feature is only real if the web tier can hold a
connection open and speak full duplex.

WSGI is a request/response contract — one callable in, one response out, no frames
and no way to hold a connection open for hours. This is not hypothetical here: an
earlier iteration deployed gunicorn and the dashboard silently had no real-time
feed at all. The WebSocket upgrade was simply refused, and nothing crashed.

*Date note: 2026-09-30 is the initial-release commit (`f6f3397`) that shipped this
design; the decision itself predates it and no earlier dated record exists.*

## Decision

We run **Daphne** (ASGI, pulled in via `channels[daphne]`) as the web tier, serving
HTTP and WebSocket on the same port: `daphne -b 0.0.0.0 -p 8000
config.asgi:application`. `config/asgi.py` builds a `ProtocolTypeRouter` with an
`http` branch (Django) and a `websocket` branch wrapped in an origin check and a
token-authentication middleware. The WebSocket credential is the DRF token, passed
as `?token=` because a browser cannot set an `Authorization` header on a
handshake, and resolved against DRF's own `Token` model.

## Consequences

### Positive

- One process, one port, two protocols: no separate socket server, no second
  deployment unit, and no cross-origin or auth duplication.
- Agent reasoning reaches the browser as it happens, which is what makes the
  debate reviewable in the UI.
- WebSocket authentication has one source of truth — the same token
  `TokenAuthentication` checks — and the origin validator applies on every
  handshake.
- In development, Vite proxies both `/api` and `/ws` to the same origin, so the
  client code path is identical to production.

### Negative / trade-offs

- Daphne is less operationally familiar than gunicorn, and the ASGI server choice
  is load-bearing: reverting to WSGI does not degrade gracefully, it removes the
  feature silently.
- The token travels as a query parameter, so it appears in proxy and access logs.
  Serving over `wss://` and keeping query strings out of logs is an operational
  requirement, not a nicety.
- HTTP and WebSocket share a process, so a defect in the socket path can affect
  HTTP availability.
- Channels adds Redis db 3 as a dependency of the web tier, with its own
  socket-deadline tuning; getting that wrong produced a real reconnection flap
  (INC-003).

### Neutral

- The real-time layer is deliberately lossy: a live-update failure must never fail
  a trade. Publishing is fire-and-forget and the client resynchronises over REST
  on every reconnect, so event loss costs a stale dashboard, not a lost ledger row.
