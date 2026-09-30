"""ASGI entrypoint: HTTP (Django) + WebSocket (Channels)."""

from __future__ import annotations

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

from runtime_env import ensure_writable_runtime_home

ensure_writable_runtime_home()
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# The HTTP application must be built *before* importing anything that touches
# the ORM (the consumer module imports models), otherwise Django's app registry
# is not ready yet.
django_asgi_app = get_asgi_application()

from core.routing import websocket_urlpatterns  # noqa: E402
from core.ws_auth import TokenAuthMiddleware  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            TokenAuthMiddleware(URLRouter(websocket_urlpatterns))
        ),
    }
)
