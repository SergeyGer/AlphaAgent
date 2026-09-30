"""Root URL configuration for AlphaAgent.

Routing order matters: the API, admin and health endpoints are matched first,
and only then does the SPA catch-all take over so client-side routes such as
``/dashboard`` resolve to ``index.html``.
"""

from __future__ import annotations

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path, re_path

from core.spa import spa_asset, spa_enabled, spa_file, spa_index


def healthz(_request):
    """Lightweight liveness probe (no auth, no DB hit)."""
    return JsonResponse({"status": "ok", "service": "AlphaAgent"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
    path("api/", include("core.urls")),
]

# Serve the compiled SPA when a build is present. Registered last so it can
# never shadow the API.
if spa_enabled():
    urlpatterns += [
        path("assets/<path:path>", spa_asset, name="spa-asset"),
        re_path(
            r"^(?P<path>favicon\.ico|robots\.txt|manifest\.webmanifest|vite\.svg)$",
            spa_file,
            name="spa-file",
        ),
        re_path(r"^(?!api/|admin/|healthz/|ws/).*$", spa_index, name="spa-index"),
    ]
