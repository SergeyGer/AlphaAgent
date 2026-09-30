"""Portfolio valuation snapshots - the equity time series.

The ledger can reconstruct positions, but not what they were *worth* at a past
moment. This module records that, powering the dashboard's equity chart.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from core.models import Portfolio, PortfolioSnapshot
from services.market_data import PriceQuote
from services.portfolio_metrics import build_price_map, compute_metrics

logger = logging.getLogger("alphaagent.snapshots")

__all__ = ["capture_all_snapshots", "capture_snapshot"]


def capture_snapshot(
    portfolio: Portfolio,
    price_map: dict[str, PriceQuote | None] | None = None,
) -> PortfolioSnapshot:
    """Write one valuation row for ``portfolio``.

    Never raises for market-data problems: an unpriced asset falls back to its
    cost basis inside :func:`compute_metrics`.
    """
    if price_map is None:
        fallbacks = {a.ticker.upper(): a.avg_purchase_price for a in portfolio.assets.all()}
        price_map = build_price_map(fallbacks.keys(), fallbacks=fallbacks)

    metrics = compute_metrics(portfolio, price_map)

    return PortfolioSnapshot.objects.create(
        portfolio=portfolio,
        total_equity_usd=metrics.total_equity_usd,
        cash_balance_usd=metrics.cash_balance_usd,
        positions_value_usd=metrics.positions_value_usd,
        unrealised_pnl_usd=metrics.unrealised_pnl_usd,
        realised_pnl_today_usd=metrics.realised_pnl_today_usd,
    )


def capture_all_snapshots() -> int:
    """Snapshot every portfolio. Returns the number of rows written."""
    captured = 0
    portfolios = Portfolio.objects.select_related("user").prefetch_related("assets")

    for portfolio in portfolios:
        try:
            capture_snapshot(portfolio)
            captured += 1
        except Exception as exc:
            logger.exception("Snapshot failed for portfolio %s: %s", portfolio.id, exc)

    logger.info("Captured %s portfolio snapshot(s)", captured)
    return captured


def prune_snapshots(portfolio: Portfolio, keep_days: int = 90) -> int:
    """Retention guard: drop snapshots older than ``keep_days``."""
    from datetime import timedelta

    from django.utils import timezone

    cutoff = timezone.now() - timedelta(days=keep_days)
    deleted, _ = PortfolioSnapshot.objects.filter(
        portfolio=portfolio, captured_at__lt=cutoff
    ).delete()
    return deleted


def latest_snapshot(portfolio: Portfolio) -> PortfolioSnapshot | None:
    return portfolio.snapshots.order_by("-captured_at").first()


def equity_series(portfolio: Portfolio, hours: int | None = None, limit: int = 2000):
    """Ascending-by-time snapshot queryset, ready for charting."""
    queryset = portfolio.snapshots.all()
    if hours:
        from datetime import timedelta

        from django.utils import timezone

        queryset = queryset.filter(captured_at__gte=timezone.now() - timedelta(hours=hours))
    # Newest-first slice, then reversed, so `limit` keeps the *most recent* rows.
    rows = list(queryset.order_by("-captured_at")[:limit])
    rows.reverse()
    return rows


def baseline_snapshot(portfolio: Portfolio) -> PortfolioSnapshot | None:
    """Oldest retained snapshot - used to compute period change."""
    return portfolio.snapshots.order_by("captured_at").first()


def zero() -> Decimal:
    return Decimal("0.00")
