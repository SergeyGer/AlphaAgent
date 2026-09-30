"""Runtime bootstrap that keeps third-party storage inside the project tree.

CrewAI (via ``crewai_core``) persists credentials and cache files under
``~/.local/share`` at *import time*.  In sandboxed / containerised runtimes the
process HOME is frequently read-only, which turns a plain ``import crewai`` into
a ``PermissionError`` before any application code runs.

Import this module and call :func:`ensure_writable_runtime_home` *before*
importing ``crewai`` (see ``ai_agent.py``).  When the real HOME is usable the
call is a no-op, so normal developer machines keep standard behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["PROJECT_ROOT", "RUNTIME_DIR", "ensure_writable_runtime_home"]

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_ROOT / ".runtime"

_LOCK_ENV = "ALPHAAGENT_RUNTIME_HOME_APPLIED"


def _candidate_data_dirs() -> list[Path]:
    """Directories CrewAI/other libs may need to write to."""
    home = Path(os.path.expanduser("~"))
    xdg = os.environ.get("XDG_DATA_HOME")
    data_root = Path(xdg) if xdg else home / ".local" / "share"
    return [
        data_root / "crewai" / "credentials",
        data_root / "AlphaAgent",
        home / ".cache",
    ]


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = path / ".alphaagent_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def ensure_writable_runtime_home() -> Path | None:
    """Redirect ``HOME``/``XDG_DATA_HOME`` into the workspace when needed.

    Returns the redirected HOME path when a redirect happened, otherwise
    ``None``.  Idempotent and safe to call from multiple entrypoints.
    """
    if os.environ.get(_LOCK_ENV) == "1":
        return None

    if all(_is_writable(d) for d in _candidate_data_dirs()):
        os.environ[_LOCK_ENV] = "1"
        return None

    fallback_home = RUNTIME_DIR / "home"
    fallback_data = RUNTIME_DIR / "share"
    for directory in (fallback_home, fallback_data):
        directory.mkdir(parents=True, exist_ok=True)
    try:
        for directory in (fallback_home, fallback_data):
            directory.chmod(0o700)
    except OSError:  # best-effort hardening only
        pass

    os.environ["HOME"] = str(fallback_home)
    os.environ["XDG_DATA_HOME"] = str(fallback_data)
    os.environ["XDG_CACHE_HOME"] = str(RUNTIME_DIR / "cache")
    (RUNTIME_DIR / "cache").mkdir(parents=True, exist_ok=True)
    os.environ[_LOCK_ENV] = "1"
    return fallback_home
