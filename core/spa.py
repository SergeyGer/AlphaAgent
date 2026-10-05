"""Serve the built React SPA from Django.

In production you would normally put nginx or a CDN in front of the static
assets. Bundling a minimal, path-traversal-safe server here means a fresh
``docker compose up`` yields a working UI at ``http://localhost:8000/`` with no
extra moving parts.

If ``frontend/dist`` does not exist (the SPA was never built) the routes are not
registered at all, and the API remains fully usable on its own.
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404, HttpResponse
from django.utils._os import safe_join
from django.views.static import serve as static_serve

from core.log_safety import log_safe

logger = logging.getLogger("alphaagent.spa")

__all__ = ["dist_dir", "spa_asset", "spa_enabled", "spa_index"]


def dist_dir() -> Path:
    return Path(getattr(settings, "SPA_DIST_DIR", Path(settings.BASE_DIR) / "frontend" / "dist"))


def spa_enabled() -> bool:
    """True when a production build of the SPA is present."""
    return (dist_dir() / "index.html").is_file()


def _index_response() -> HttpResponse:
    index = dist_dir() / "index.html"
    if not index.is_file():
        raise Http404("SPA build not found")
    # Never cache the shell: it references hashed asset filenames that change
    # on every build, so a stale shell would 404 its own bundle.
    # FileResponse closes the handle once the response has been streamed, so
    # it must not be wrapped in a `with` block.
    handle = open(index, "rb")  # noqa: SIM115
    response = FileResponse(handle, content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-cache, must-revalidate"
    return response


def spa_index(request) -> HttpResponse:
    """Serve the SPA shell for the root and any client-side route."""
    return _index_response()


def _resolve_within(root: Path, relative: str) -> None:
    """Reject any path that escapes ``root`` (traversal, absolute paths, symlinks).

    Two layers, deliberately:

    * ``safe_join`` is Django's own containment helper. It rejects absolute
      paths, drive letters and ``..`` segments, and raises
      ``SuspiciousFileOperation`` - which is *not* an ``Http404``, so without
      this guard a crafted request would surface as a 500 rather than a clean
      404.
    * the resolved-path check afterwards additionally covers symlinks inside the
      build directory, which ``safe_join`` cannot see.

    ``safe_join`` is used rather than a hand-rolled ``root / relative`` because
    concatenating first and validating second is what CodeQL flags as
    ``py/path-injection``: the tainted value reaches a path expression before any
    sanitiser runs. Joining through the recognised helper keeps the validation
    ahead of the path construction.
    """
    try:
        joined = safe_join(str(root), relative)
        candidate = Path(joined).resolve()
        root_resolved = root.resolve()
    except (OSError, ValueError, SuspiciousFileOperation) as exc:
        raise Http404("Invalid asset path") from exc

    if not candidate.is_relative_to(root_resolved):
        logger.warning("Blocked path traversal attempt: %r", log_safe(relative))
        raise Http404("Invalid asset path")


def spa_asset(request, path: str) -> HttpResponse:
    """Serve hashed build assets, with traversal explicitly blocked."""
    assets_root = dist_dir() / "assets"
    if not assets_root.is_dir():
        raise Http404("No assets directory")
    _resolve_within(assets_root, path)
    try:
        response = static_serve(request, path, document_root=str(assets_root))
    except Http404:
        raise
    except Exception as exc:
        raise Http404("Asset not found") from exc
    # Vite fingerprints filenames, so these are safe to cache aggressively.
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def spa_file(request, path: str) -> HttpResponse:
    """Serve top-level build artefacts (favicon, manifest, robots.txt)."""
    root = dist_dir()
    if not root.is_dir():
        raise Http404("SPA build not found")
    _resolve_within(root, path)
    try:
        return static_serve(request, path, document_root=str(root))
    except Http404:
        raise
    except Exception as exc:
        raise Http404("File not found") from exc
