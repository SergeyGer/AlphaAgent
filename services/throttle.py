"""Sweep throttling: cooldowns and debounce.

The problem
-----------
`max_trade_allocation_pct` is a **per-trade** ceiling. Nothing stopped several
sweeps from stacking up inside one window:

* Celery Beat fires every 15 minutes, and a sweep with a slow LLM can outlive
  its own interval.
* `POST /api/portfolio/run-agent/` had no cooldown, so a manual trigger could
  land seconds after a scheduled one.

Observed in production: two sweeps 3 seconds apart analysed the same ticker
twice and bought AAPL twice, each purchase individually inside the limit.

The fix
-------
A short-lived Redis slot per ``(portfolio, ticker)``. The first run claims it;
any run inside the window is skipped with an explicit status instead of
silently spending tokens and re-trading. A second, coarser debounce prevents
duplicate *fan-outs* so Beat and a manual trigger cannot both dispatch a sweep.

Both are best-effort. A Redis outage must not stop trading — the throttle fails
**open** and logs loudly. Losing a cooldown is a cost problem; refusing to run
the risk engine is a safety problem.
"""

from __future__ import annotations

import logging
import uuid

from django.core.cache import cache

logger = logging.getLogger("alphaagent.throttle")

__all__ = [
    "claim_sweep_slot",
    "claim_ticker_slot",
    "release_ticker_slot",
    "sweep_debounce_seconds",
    "ticker_cooldown_seconds",
    "ticker_slot_key",
]

_COOLDOWN_KEY = "alpha:cooldown:{portfolio_id}:{ticker}"
_SWEEP_KEY = "alpha:sweep:debounce"


def _ai_config() -> dict:
    from django.conf import settings

    return getattr(settings, "AI_CONFIG", {}) or {}


def ticker_cooldown_seconds() -> int:
    """How long one ticker is left alone after a run. Default: one Beat interval."""
    from django.conf import settings

    return int(getattr(settings, "ALPHA_TICKER_COOLDOWN_SECONDS", 900))


def sweep_debounce_seconds() -> int:
    """Minimum gap between two fan-outs. Short: it only prevents double dispatch."""
    from django.conf import settings

    return int(getattr(settings, "ALPHA_SWEEP_DEBOUNCE_SECONDS", 60))


def ticker_slot_key(portfolio_id: int, ticker: str) -> str:
    return _COOLDOWN_KEY.format(portfolio_id=portfolio_id, ticker=ticker.upper())


def claim_ticker_slot(portfolio_id: int, ticker: str, *, window: int | None = None) -> bool:
    """Try to claim the run slot for one ``(portfolio, ticker)`` pair.

    Returns ``True`` when the caller may proceed. ``cache.add`` is an atomic
    ``SET NX``, so two workers racing for the same ticker cannot both win.
    """
    window = window if window is not None else ticker_cooldown_seconds()
    if window <= 0:
        return True  # throttling explicitly disabled

    key = ticker_slot_key(portfolio_id, ticker)
    try:
        claimed = cache.add(key, uuid.uuid4().hex, window)
    except Exception as exc:
        logger.warning("Cooldown check failed for %s (allowing the run): %s", key, exc)
        return True

    if not claimed:
        logger.info(
            "Skipping %s/%s: ran within the last %ss cooldown window",
            portfolio_id,
            ticker,
            window,
        )
    return bool(claimed)


def release_ticker_slot(portfolio_id: int, ticker: str) -> None:
    """Release a slot early, e.g. after a failed run that should be retried."""
    try:
        cache.delete(ticker_slot_key(portfolio_id, ticker))
    except Exception as exc:
        logger.warning("Could not release the cooldown slot: %s", exc)


def claim_sweep_slot(*, window: int | None = None) -> bool:
    """Debounce fan-outs so Beat and a manual trigger cannot both dispatch."""
    window = window if window is not None else sweep_debounce_seconds()
    if window <= 0:
        return True

    try:
        claimed = cache.add(_SWEEP_KEY, uuid.uuid4().hex, window)
    except Exception as exc:
        logger.warning("Sweep debounce check failed (allowing the sweep): %s", exc)
        return True

    if not claimed:
        logger.info("Skipping sweep: another fan-out started within %ss", window)
    return bool(claimed)
