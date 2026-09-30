"""Celery application for AlphaAgent.

The task modules live at the project root (``tasks.py``) and are pulled in via
``include`` so that Celery autodiscovers every registered task.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.signals import setup_logging

from runtime_env import ensure_writable_runtime_home

ensure_writable_runtime_home()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("alphaagent")

# Read every CELERY_* key from Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Root-level task modules (spec section 5) plus any app-level tasks.py.
app.autodiscover_tasks()
app.conf.imports = ("tasks",)


@setup_logging.connect
def _configure_celery_logging(**_kwargs: object) -> None:
    """Keep Celery on Django's logging configuration."""
    from logging.config import dictConfig

    from django.conf import settings

    dictConfig(settings.LOGGING)


@app.task(bind=True, name="config.debug_task")
def debug_task(self) -> str:  # pragma: no cover - diagnostic helper
    """Trivial task used to smoke-test broker connectivity."""
    return f"AlphaAgent Celery OK - request id: {self.request.id}"
