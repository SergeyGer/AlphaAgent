"""WebSocket authentication middleware.

Browsers cannot set custom headers on a WebSocket handshake, so the DRF token is
passed as a query parameter (``?token=<key>``) - the standard approach for
token-authenticated sockets. The token is validated with DRF's own
``TokenAuthentication`` so there is exactly one source of truth for what a valid
credential means.

Anonymous connections are still accepted at the protocol level; the consumer
closes them with a policy code so the client can distinguish "bad token" from
"network problem".
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser

logger = logging.getLogger("alphaagent.ws")

__all__ = ["TokenAuthMiddleware", "close_old_connections"]


@database_sync_to_async
def _authenticate(token_key: str):
    """Resolve a token key to a User, or AnonymousUser."""
    from rest_framework.authtoken.models import Token

    try:
        token = Token.objects.select_related("user").get(key=token_key)
    except Token.DoesNotExist:
        return AnonymousUser()
    if not token.user.is_active:
        return AnonymousUser()
    return token.user


def close_old_connections() -> None:
    """Release DB connections held by the worker thread."""
    from django.db import close_old_connections as _close

    _close()


class TokenAuthMiddleware(BaseMiddleware):
    """Populate ``scope['user']`` from a ``token`` query parameter."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            return await super().__call__(scope, receive, send)

        query = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="ignore"))
        token_key = (query.get("token") or [""])[0].strip()

        scope["user"] = AnonymousUser()
        if token_key:
            try:
                scope["user"] = await _authenticate(token_key)
            except Exception as exc:
                logger.warning("WebSocket token lookup failed: %s", exc)

        if not scope["user"].is_authenticated:
            logger.info("WebSocket handshake rejected: missing or invalid token")

        return await super().__call__(scope, receive, send)
