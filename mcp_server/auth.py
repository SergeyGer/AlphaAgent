"""Shared-secret authentication for the MCP server's HTTP transports.

Why an ASGI wrapper rather than something inside the tool functions: the secret
has to be checked **before** a request reaches the MCP session, including the
`initialize` handshake. Authenticating inside each tool would still leave the
server capable of enumerating its tools for an anonymous caller, and would mean
repeating the check on every tool added later. One wrapper covers every route the
transport exposes, including ones a future upgrade of the library might add.

The server used to run with no authentication at all, published on host port
8100. The tools it exposes are read-only public market data, so the severity was
low - but an unauthenticated endpoint reachable from outside the compose network
is a surface that exists for no reason, and "the data is public anyway" is an
argument that stops being true the moment a tool is added that is not.

``stdio`` is exempt: it is not a network transport and the caller already has
whatever access the process has.
"""

from __future__ import annotations

import hmac
import logging

from core.log_safety import log_safe

__all__ = ["HEADER_NAME", "SharedSecretGuard", "resolve_secret"]

logger = logging.getLogger(__name__)

#: Header the client must send. Namespaced so it cannot collide with a header an
#: intermediary adds on its own.
HEADER_NAME = b"x-alphaagent-mcp-key"


def resolve_secret(explicit: str | None = None) -> str:
    """The configured shared secret, or ``""`` when none is set."""
    if explicit is not None:
        return explicit.strip()
    import os

    return (os.environ.get("MCP_SHARED_SECRET", "") or "").strip()


class SharedSecretGuard:
    """ASGI middleware rejecting any HTTP request without the shared secret.

    Returns 401 rather than 404 so a misconfigured client fails loudly and
    visibly instead of looking like a routing mistake.
    """

    def __init__(self, app, secret: str, *, header_name: bytes = HEADER_NAME) -> None:
        if not secret:
            # Refusing here rather than at request time: a server that starts
            # successfully while silently accepting anonymous traffic is the
            # exact failure this class exists to prevent.
            raise ValueError(
                "MCP_SHARED_SECRET is not set. The HTTP transports refuse to start "
                "without it. Set MCP_SHARED_SECRET, or use --transport stdio."
            )
        self.app = app
        self._secret = secret.encode()
        self._header_name = header_name

    def _presented(self, scope) -> bytes | None:
        for name, value in scope.get("headers") or ():
            if name.lower() == self._header_name:
                return value
        return None

    async def __call__(self, scope, receive, send):
        # Lifespan and websocket scopes pass through: the streamable HTTP
        # transport is request/response, and refusing lifespan would stop the
        # server booting its own session manager.
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        presented = self._presented(scope)
        # compare_digest, not ==: a length-varying comparison leaks the secret
        # one byte at a time to anyone willing to measure response times.
        if presented is not None and hmac.compare_digest(presented, self._secret):
            return await self.app(scope, receive, send)

        # `log_safe` on both: the method and path come straight off the ASGI
        # scope, so an unauthenticated caller controls them. Without it a request
        # to /mcp%0aInjected%20line forges an extra entry in the log - this is the
        # same py/log-injection sink that was fixed elsewhere in the codebase, and
        # it is on the one endpoint an anonymous caller can reach.
        logger.warning(
            "Rejected unauthenticated MCP request: %s %s",
            log_safe(scope.get("method")),
            log_safe(scope.get("path")),
        )
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    # Tells a caller what to do rather than only that it failed.
                    (b"www-authenticate", HEADER_NAME),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"error":"unauthorized","detail":"Missing or invalid X-AlphaAgent-MCP-Key header."}',
            }
        )
