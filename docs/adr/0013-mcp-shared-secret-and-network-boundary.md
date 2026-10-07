# 0013. Authenticate the MCP server and keep it off the host network

- **Status:** Accepted
- **Date:** 2026-10-07
- **Deciders:** Project maintainer
- **Related:** [ADR-0005](0005-mcp-tool-boundary.md) (why the MCP boundary exists at all), [ADR-0006](0006-fail-loudly-fail-open.md) (fail loudly on missing configuration)

## Context

The MCP tool server was reachable on host port 8100 with **no authentication of
any kind**. The compose file published `8100:8100`, and every route the transport
exposed — including the `initialize` handshake and tool enumeration — answered
anonymous callers.

The mitigating argument was real but narrow: the three tools are read-only and
serve public market data, so an anonymous caller learned nothing they could not
have fetched from Yahoo Finance themselves. On that reading the exposure was
cosmetic.

Three things made that reasoning insufficient:

1. **"The data is public anyway" expires.** It is a property of the current three
   tools, not of the server. The next tool added — anything touching portfolios,
   positions or the audit trail — inherits an open endpoint by default, and the
   decision to expose it would be made by omission rather than on purpose.
2. **A published port is a listening socket on the host.** Anything that can route
   to that host reaches it. On a machine with a public interface, that is the
   internet, and the project already runs on one.
3. **It was not needed.** The only intended consumer is the Celery worker, which
   lives in the same compose network. Publishing the port served external clients
   that do not exist.

## Decision

**We apply two independent controls: stop publishing the port, and require a
shared secret on every HTTP request.**

- The `mcp` service uses `expose: ["8100"]` instead of `ports:`, so it is
  reachable only from inside the compose network.
- Every HTTP request must carry `X-AlphaAgent-MCP-Key`, compared with
  `hmac.compare_digest` rather than `==` so the comparison does not leak the
  secret one byte at a time to anyone measuring response times.
- The check is an **ASGI middleware wrapping the whole transport**, not a
  per-tool decorator. It therefore covers the handshake, tool enumeration and any
  route a future version of the library adds — none of which a per-tool check
  would see.
- The server **refuses to start** on an HTTP transport when no secret is
  configured. It does not start and warn; it exits with status 2 and an
  explanation.
- The `stdio` transport is exempt. It is not a network transport, and the caller
  already has whatever access the process has.
- Rejections return `401` with a `WWW-Authenticate` header naming the expected
  header, so a misconfigured client fails visibly rather than looking like a
  routing error.
- The worker sends the secret, and refuses to build an MCP tool reference when
  none is configured — falling back to the in-process tools instead of issuing a
  call that would be rejected.

## Consequences

### Positive

- The endpoint is not reachable from outside the compose network at all, so a
  mistake in the authentication code is not immediately exploitable.
- Adding a tool that touches sensitive data does not silently create a public
  endpoint; the network boundary already excludes one, and the secret excludes
  callers inside the network.
- Verification is easy and was performed live: no header → `401`, wrong secret →
  `401`, correct secret → `200`.
- The failure mode is loud. A missing secret stops the service rather than
  producing an open one.

### Negative / trade-offs

- **Two configurations must agree.** `MCP_SHARED_SECRET` is set on both the
  server and the worker from the same compose anchor. Changing it in one place
  and not the other yields a 401 that looks like a bug in the tools.
- **The secret is a static shared secret.** No rotation, no expiry, no per-client
  identity, and no way to revoke one client without rotating for everyone. This is
  appropriate for a single-consumer internal service and would not be for a
  multi-tenant one.
- **The compose default is a known literal**,
  `dev-only-mcp-shared-secret-change-me`. That is deliberate — the demo stack must
  start with no configuration — but it means a deployment that copies the compose
  file verbatim has a published secret. It is named to be obviously unfit for
  production, and `.env.example` says to generate a real one.
- The server can no longer be exercised from the host with a plain `curl` for
  debugging; you need `docker compose exec` or a tunnel. That is the cost of not
  publishing the port, and it is paid every time someone debugs the tools.
- Authenticating at the transport means an unauthenticated caller cannot even
  discover which tools exist. That is intended, but it does make a
  misconfiguration harder to diagnose from the client side, since the failure is
  a bare 401 rather than a tool-not-found.

### Neutral

- `AI_TOOLS_VIA_MCP` still defaults to `false`, so the common path never touches
  the MCP server and is unaffected by any of this.
- The health check is unchanged: a TCP connect to `127.0.0.1:8100` inside the
  container, which needs no secret because it never reaches the MCP session.
