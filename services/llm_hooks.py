"""LLM transport hooks.

Some providers require headers that the LLM libraries do not send by default.
The motivating case is Anthropic's workspace scoping:

    This API key is not scoped to a workspace, so this request must include the
    anthropic-workspace-id header with the ID of the workspace to use.

Neither CrewAI nor LiteLLM sends ``anthropic-workspace-id``, so an account that
uses Workspaces gets an HTTP 400 on every call.

CrewAI exposes a documented transport hook for exactly this:
``LLM(interceptor=...)`` receives an :class:`~crewai.llms.hooks.base.BaseInterceptor`
whose ``on_outbound`` runs against the real ``httpx.Request`` before it leaves
the process. That is a public API, so this module needs no monkeypatching and
survives library upgrades.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("alphaagent.llm_hooks")

__all__ = [
    "WORKSPACE_HEADER",
    "build_llm_interceptor",
]

#: Header Anthropic requires when an API key is not workspace-scoped.
WORKSPACE_HEADER = "anthropic-workspace-id"

#: Headers whose values must never reach the logs.
_SENSITIVE = ("authorization", "x-api-key", "api-key", "proxy-authorization")


def _redact(headers: dict[str, str]) -> dict[str, str]:
    return {key: ("***" if key.lower() in _SENSITIVE else value) for key, value in headers.items()}


class _HeaderInjectionMixin:
    """Header-injection behaviour, independent of CrewAI.

    Kept separate from the CrewAI base class so this module stays importable
    (and unit-testable) without loading the AI stack.
    """

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = {k: v for k, v in headers.items() if v}
        logger.info("LLM requests will carry extra header(s): %s", sorted(_redact(self.headers)))

    def on_outbound(self, request: Any) -> Any:
        """Attach the configured headers to an outgoing request."""
        for key, value in self.headers.items():
            request.headers[key] = value
        return request

    def on_inbound(self, response: Any) -> Any:
        """Surface provider-side rejections with actionable context.

        A workspace/auth problem otherwise surfaces as an opaque 400 deep inside
        the agent executor, which is expensive to debug.
        """
        status = getattr(response, "status_code", None)
        if status in (400, 401, 403):
            try:
                body = response.text or ""
            except Exception:
                body = ""
            if "workspace" in body.lower():
                logger.error(
                    "The LLM provider rejected the request (HTTP %s) over workspace "
                    "scoping. Set AI_LLM_WORKSPACE_ID to the workspace ID, or use a "
                    "workspace-scoped API key. Response: %s",
                    status,
                    body[:300],
                )
        return response


#: Built once on first use; subclassing BaseInterceptor requires importing
#: CrewAI, which is far too heavy to do at module import time.
_INTERCEPTOR_CLASS: type | None = None


def _interceptor_class() -> type | None:
    """Return a CrewAI-compatible interceptor class, or ``None`` if unavailable.

    CrewAI validates ``isinstance(interceptor, BaseInterceptor)``, so the object
    passed to ``LLM(interceptor=...)`` must genuinely subclass it - structural
    typing is rejected.
    """
    global _INTERCEPTOR_CLASS
    if _INTERCEPTOR_CLASS is not None:
        return _INTERCEPTOR_CLASS

    try:
        import httpx
        from crewai.llms.hooks.base import BaseInterceptor
    except ImportError as exc:  # pragma: no cover - CrewAI is a hard dependency
        logger.warning("Cannot build an LLM interceptor: %s", exc)
        return None

    class HeaderInjectionInterceptor(  # type: ignore[misc, valid-type]
        _HeaderInjectionMixin, BaseInterceptor[httpx.Request, httpx.Response]
    ):
        """Inject configured headers into every outbound LLM request.

        Example:
            >>> LLM(model="claude-...", interceptor=HeaderInjectionInterceptor(
            ...     {"anthropic-workspace-id": "ws_123"}))
        """

    _INTERCEPTOR_CLASS = HeaderInjectionInterceptor
    return _INTERCEPTOR_CLASS


def build_llm_interceptor(config: dict[str, Any]) -> Any | None:
    """Build an interceptor from ``AI_CONFIG``, or ``None`` when nothing is set.

    Two sources, merged (explicit extras win):

    * ``WORKSPACE_ID`` - shorthand for the Anthropic workspace header, which is
      the common case and awkward to express as raw JSON.
    * ``EXTRA_HEADERS`` - an arbitrary mapping, for gateways and proxies.
    """
    headers: dict[str, str] = {}

    workspace_id = str(config.get("WORKSPACE_ID") or "").strip()
    if workspace_id:
        headers[WORKSPACE_HEADER] = workspace_id

    raw_extra = config.get("EXTRA_HEADERS")
    if isinstance(raw_extra, str) and raw_extra.strip():
        try:
            parsed = json.loads(raw_extra)
        except json.JSONDecodeError as exc:
            logger.error("AI_LLM_EXTRA_HEADERS is not valid JSON, ignoring it: %s", exc)
            parsed = None
        if isinstance(parsed, dict):
            headers.update({str(k): str(v) for k, v in parsed.items()})
        elif parsed is not None:
            logger.error("AI_LLM_EXTRA_HEADERS must be a JSON object, ignoring it")
    elif isinstance(raw_extra, dict):
        headers.update({str(k): str(v) for k, v in raw_extra.items()})

    if not headers:
        return None

    cls = _interceptor_class()
    if cls is None:
        logger.error(
            "Header injection was requested (%s) but no interceptor could be built; "
            "the provider will reject the requests.",
            sorted(headers),
        )
        return None
    return cls(headers)
