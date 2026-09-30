"""AlphaAgent project package.

Ensures the Celery application is imported eagerly so that ``@shared_task``
decorators bind to it when Django starts.
"""

from __future__ import annotations

from .celery import app as celery_app

__all__ = ("celery_app",)
