"""WebSocket consumers for the live dashboard.

Each authenticated user joins a private group (``portfolio.<user_id>``) and
receives only their own events. The consumer is read-only: clients may send
``{"type": "ping"}`` for liveness, but no client message can mutate state. All
mutations go through the REST API, where the normal authentication, validation
and execution guard apply.
"""

from __future__ import annotations

import logging
from typing import Any

from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger("alphaagent.ws")

__all__ = ["PortfolioConsumer", "portfolio_group_name"]

# Custom close codes (4000-4999 are application-defined).
CLOSE_UNAUTHORIZED = 4401
CLOSE_SERVER_ERROR = 4500


def portfolio_group_name(user_id: int) -> str:
    """Group name for one user's live stream."""
    return f"portfolio.{user_id}"


class PortfolioConsumer(AsyncJsonWebsocketConsumer):
    """Streams portfolio, trade, decision and recommendation events."""

    group_name: str | None = None

    async def connect(self) -> None:
        user = self.scope.get("user")

        if user is None or not user.is_authenticated:
            # Accept then close, so the client receives a meaningful code
            # instead of an opaque handshake failure.
            await self.accept()
            await self.close(code=CLOSE_UNAUTHORIZED)
            return

        self.group_name = portfolio_group_name(user.id)
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        except Exception as exc:
            logger.exception("Channel layer unavailable during connect: %s", exc)
            await self.accept()
            await self.close(code=CLOSE_SERVER_ERROR)
            return

        await self.accept()
        logger.info("WebSocket connected: user=%s group=%s", user.username, self.group_name)

        # Send an immediate snapshot so the UI has data before the first event.
        try:
            await self.send_json(
                {
                    "type": "connection.established",
                    "payload": {"username": user.username, "group": self.group_name},
                }
            )
        except Exception as exc:
            logger.warning("Failed to send greeting frame: %s", exc)

    async def disconnect(self, code: int) -> None:
        if self.group_name:
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception as exc:
                logger.warning("group_discard failed: %s", exc)
        logger.info("WebSocket disconnected (code=%s)", code)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        """Handle client frames. Only a no-op ping is supported."""
        message_type = (content or {}).get("type") if isinstance(content, dict) else None
        if message_type == "ping":
            await self.send_json({"type": "pong", "payload": {}})

    # -- group event handlers ---------------------------------------------
    async def portfolio_event(self, event: dict) -> None:
        """Relay a bus event published by ``services.events``."""
        await self.send_json(
            {
                "type": event.get("event_type", "unknown"),
                "payload": event.get("payload", {}),
            }
        )
