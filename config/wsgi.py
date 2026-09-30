"""WSGI entrypoint."""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

from runtime_env import ensure_writable_runtime_home

ensure_writable_runtime_home()
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
