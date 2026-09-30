"""Portfolio valuation and risk metrics.

Kept out of the serializers so the same numbers can be reused by tasks,
management commands and tests. Positions come from the ``Asset`` table (the
system of record for the API) while realised P&L comes from the ledger replay
in :mod:`services.ledger`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from services.ledger import realised_pnl_today, replay_positions
from services.market_data import PriceQuote, get_latest_quote

if TYPE_CHECKING:  # pragma: no cover
    from core.models import Portfolio

__all__ = ["PortfolioMetrics", "build_price_map", "compute_metrics"]

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


@dataclass
class PortfolioMetrics:
    """Aggregated financial state of a portfolio."""

    cash_balance_usd: Decimal = ZERO
    positions_value_usd: Decimal = ZERO
    total_equity_usd: Decimal = ZERO
    invested_cost_usd: Decimal = ZERO
    unrealised_pnl_usd: Decimal = ZERO
    unrealised_pnl_pct: Decimal = ZERO
    realised_pnl_today_usd: Decimal = ZERO
    realised_pnl_total_usd: Decimal = ZERO
    daily_loss_limit_usd: Decimal = ZERO
    daily_loss_used_usd: Decimal = ZERO
    daily_loss_remaining_usd: Decimal = ZERO
    max_trade_budget_usd: Decimal = ZERO
    is_autonomy_blocked: bool = False
    block_reason: str | None = None
    unpriced_tickers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "cash_balance_usd": str(self.cash_balance_usd),
            "positions_value_usd": str(self.positions_value_usd),
            "total_equity_usd": str(self.total_equity_usd),
            "invested_cost_usd": str(self.invested_cost_usd),
            "unrealised_pnl_usd": str(self.unrealised_pnl_usd),
            "unrealised_pnl_pct": str(self.unrealised_pnl_pct),
            "realised_pnl_today_usd": str(self.realised_pnl_today_usd),
            "realised_pnl_total_usd": str(self.realised_pnl_total_usd),
            "daily_loss_limit_usd": str(self.daily_loss_limit_usd),
            "daily_loss_used_usd": str(self.daily_loss_used_usd),
            "daily_loss_remaining_usd": str(self.daily_loss_remaining_usd),
            "max_trade_budget_usd": str(self.max_trade_budget_usd),
            "is_autonomy_blocked": self.is_autonomy_blocked,
            "block_reason": self.block_reason,
            "unpriced_tickers": self.unpriced_tickers,
        }


def build_price_map(
    tickers: Iterable[str],
    fallbacks: dict[str, Decimal] | None = None,
    use_cache: bool = True,
) -> dict[str, PriceQuote | None]:
    """Resolve a live quote per ticker (cache-first, never raises)."""
    fallbacks = fallbacks or {}
    price_map: dict[str, PriceQuote | None] = {}
    for ticker in {t.upper() for t in tickers if t}:
        try:
            price_map[ticker] = get_latest_quote(
                ticker,
                fallback_price=fallbacks.get(ticker),
                use_cache=use_cache,
            )
        except Exception:
            price_map[ticker] = None
    return price_map


def compute_metrics(
    portfolio: Portfolio,
    price_map: dict[str, PriceQuote | None] | None = None,
) -> PortfolioMetrics:
    """Compute every headline metric for ``portfolio``."""
    price_map = price_map or {}
    metrics = PortfolioMetrics(
        cash_balance_usd=portfolio.balance_usd.quantize(CENT),
        daily_loss_limit_usd=portfolio.daily_loss_limit_usd.quantize(CENT),
        max_trade_budget_usd=portfolio.max_trade_budget_usd,
    )

    positions_value = ZERO
    invested_cost = ZERO
    for asset in portfolio.assets.all():
        quote = price_map.get(asset.ticker.upper())
        if quote is not None:
            price = quote.price
        elif asset.avg_purchase_price > 0:
            price = asset.avg_purchase_price
            metrics.unpriced_tickers.append(asset.ticker)
        else:
            price = ZERO
            metrics.unpriced_tickers.append(asset.ticker)

        positions_value += (asset.amount * price).quantize(CENT)
        invested_cost += (asset.amount * asset.avg_purchase_price).quantize(CENT)

    metrics.positions_value_usd = positions_value.quantize(CENT)
    metrics.invested_cost_usd = invested_cost.quantize(CENT)
    metrics.total_equity_usd = (metrics.cash_balance_usd + positions_value).quantize(CENT)
    metrics.unrealised_pnl_usd = (positions_value - invested_cost).quantize(CENT)
    if invested_cost > 0:
        metrics.unrealised_pnl_pct = (
            metrics.unrealised_pnl_usd / invested_cost * Decimal("100")
        ).quantize(CENT)

    try:
        states = replay_positions(portfolio)
        metrics.realised_pnl_today_usd = sum(
            (s.realised_pnl_today for s in states.values()), Decimal("0")
        ).quantize(CENT)
        metrics.realised_pnl_total_usd = sum(
            (s.realised_pnl_total for s in states.values()), Decimal("0")
        ).quantize(CENT)
    except Exception:
        metrics.realised_pnl_today_usd = realised_pnl_today(portfolio)

    limit = metrics.daily_loss_limit_usd
    if limit > 0:
        used = max(-metrics.realised_pnl_today_usd, Decimal("0")).quantize(CENT)
        metrics.daily_loss_used_usd = used
        metrics.daily_loss_remaining_usd = max(limit - used, Decimal("0")).quantize(CENT)
        if used >= limit:
            metrics.is_autonomy_blocked = True
            metrics.block_reason = (
                f"Daily loss limit breached: ${used} lost against a ${limit} limit."
            )
    else:
        metrics.daily_loss_remaining_usd = ZERO

    if portfolio.balance_usd <= 0 and metrics.positions_value_usd <= 0:
        metrics.is_autonomy_blocked = True
        metrics.block_reason = metrics.block_reason or "Portfolio has no buying power."

    return metrics
