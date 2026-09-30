#!/usr/bin/env python
"""Django management entrypoint for the AlphaAgent platform."""

from __future__ import annotations

import os
import sys

from runtime_env import ensure_writable_runtime_home


def main() -> None:
    ensure_writable_runtime_home()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover - defensive guard
        raise ImportError(
            "Could not import Django. Activate the project virtualenv:\n"
            "    source venv/bin/activate"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
