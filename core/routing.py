"""WebSocket URL routing."""

from __future__ import annotations

from django.urls import path

from core.consumers import PortfolioConsumer

websocket_urlpatterns = [
    path("ws/portfolio/", PortfolioConsumer.as_asgi()),
]
