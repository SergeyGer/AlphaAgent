"""Fundamentals and price history - the evidence base for the bearish case.

A debate only works if both sides argue from facts. The bull analyst reads
headlines; the Risk Assessor needs harder material: leverage, liquidity, cash
generation, valuation, and where the price sits relative to its own trend.

Everything here degrades gracefully. A missing field becomes ``None`` rather
than an exception, and derived red flags are only asserted when the underlying
data actually exists - an unknown is not a bearish signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("alphaagent.fundamentals")

__all__ = [
    "FinancialHealth",
    "PriceHistory",
    "get_financial_health",
    "get_price_history",
]


def _to_decimal(value: Any, places: str = "0.01") -> Decimal | None:
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if dec.is_nan() or dec.is_infinite():
        return None
    return dec.quantize(Decimal(places))


def _cache_ttl() -> int:
    return int(getattr(settings, "AI_CONFIG", {}).get("PRICE_CACHE_TTL", 60)) * 15


@dataclass
class PriceHistory:
    """Price action over a window, with the trend context a short seller cites."""

    ticker: str
    period: str = "1y"
    points: list[tuple[str, Decimal]] = field(default_factory=list)
    latest: Decimal | None = None
    period_high: Decimal | None = None
    period_low: Decimal | None = None
    change_pct: Decimal | None = None
    sma_50: Decimal | None = None
    sma_200: Decimal | None = None
    drawdown_from_high_pct: Decimal | None = None
    degradation_flags: list[str] = field(default_factory=list)

    @property
    def below_sma_50(self) -> bool | None:
        if self.latest is None or self.sma_50 is None:
            return None
        return self.latest < self.sma_50

    @property
    def below_sma_200(self) -> bool | None:
        if self.latest is None or self.sma_200 is None:
            return None
        return self.latest < self.sma_200

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "period": self.period,
            "latest": str(self.latest) if self.latest is not None else None,
            "period_high": str(self.period_high) if self.period_high is not None else None,
            "period_low": str(self.period_low) if self.period_low is not None else None,
            "change_pct": str(self.change_pct) if self.change_pct is not None else None,
            "sma_50": str(self.sma_50) if self.sma_50 is not None else None,
            "sma_200": str(self.sma_200) if self.sma_200 is not None else None,
            "drawdown_from_high_pct": (
                str(self.drawdown_from_high_pct)
                if self.drawdown_from_high_pct is not None
                else None
            ),
            "below_sma_50": self.below_sma_50,
            "below_sma_200": self.below_sma_200,
            "degradation_flags": self.degradation_flags,
            # The chart payload stays small: at most ~130 points.
            "points": [[stamp, str(price)] for stamp, price in self.points],
        }


@dataclass
class FinancialHealth:
    """Balance-sheet and valuation facts, plus derived red flags."""

    ticker: str = ""
    total_debt: Decimal | None = None
    total_cash: Decimal | None = None
    debt_to_equity: Decimal | None = None
    current_ratio: Decimal | None = None
    profit_margin: Decimal | None = None
    free_cash_flow: Decimal | None = None
    trailing_pe: Decimal | None = None
    analyst_target: Decimal | None = None
    recommendation_key: str = ""
    sector: str = ""
    red_flags: list[str] = field(default_factory=list)
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "ticker": self.ticker,
            "total_debt": s(self.total_debt),
            "total_cash": s(self.total_cash),
            "debt_to_equity": s(self.debt_to_equity),
            "current_ratio": s(self.current_ratio),
            "profit_margin": s(self.profit_margin),
            "free_cash_flow": s(self.free_cash_flow),
            "trailing_pe": s(self.trailing_pe),
            "analyst_target": s(self.analyst_target),
            "recommendation_key": self.recommendation_key,
            "sector": self.sector,
            "red_flags": self.red_flags,
            "degraded": self.degraded,
        }

    def as_prompt_block(self) -> str:
        """Compact evidence block handed to the Risk Assessor."""
        if self.degraded:
            return f"No fundamental data available for {self.ticker}."

        lines = [f"Fundamentals for {self.ticker}:"]
        if self.sector:
            lines.append(f"- Sector: {self.sector}")
        if self.total_debt is not None:
            lines.append(f"- Total debt: ${self.total_debt:,.0f}")
        if self.total_cash is not None:
            lines.append(f"- Total cash: ${self.total_cash:,.0f}")
        if self.debt_to_equity is not None:
            lines.append(f"- Debt/equity: {self.debt_to_equity}")
        if self.current_ratio is not None:
            lines.append(f"- Current ratio: {self.current_ratio}")
        if self.profit_margin is not None:
            lines.append(f"- Profit margin: {self.profit_margin * 100:.2f}%")
        if self.free_cash_flow is not None:
            lines.append(f"- Free cash flow: ${self.free_cash_flow:,.0f}")
        if self.trailing_pe is not None:
            lines.append(f"- Trailing P/E: {self.trailing_pe}")
        if self.analyst_target is not None:
            lines.append(f"- Analyst mean target: ${self.analyst_target}")
        if self.recommendation_key:
            lines.append(f"- Street consensus: {self.recommendation_key}")

        if self.red_flags:
            lines.append("Derived red flags:")
            lines.extend(f"- {flag}" for flag in self.red_flags)
        else:
            lines.append("Derived red flags: none detected in the available data.")
        return "\n".join(lines)


def _derive_price_flags(history: PriceHistory) -> list[str]:
    flags: list[str] = []
    if history.below_sma_200:
        flags.append(
            f"Price {history.latest} is below the 200-day average {history.sma_200} "
            "(long-term downtrend)."
        )
    if history.below_sma_50:
        flags.append(
            f"Price {history.latest} is below the 50-day average {history.sma_50} "
            "(short-term weakness)."
        )
    if history.drawdown_from_high_pct is not None and history.drawdown_from_high_pct <= Decimal(
        "-20"
    ):
        flags.append(
            f"Down {abs(history.drawdown_from_high_pct)}% from the period high "
            f"{history.period_high} - bear-market territory."
        )
    if history.change_pct is not None and history.change_pct <= Decimal("-10"):
        flags.append(f"Lost {abs(history.change_pct)}% over the last {history.period}.")
    return flags


def get_price_history(ticker: str, period: str = "1y") -> PriceHistory | None:
    """Default window is 1y so the 200-day average is computable."""
    """Daily closes plus moving averages. Returns ``None`` if unavailable."""
    from services.market_data import normalize_ticker

    try:
        symbol = normalize_ticker(ticker)
    except Exception:
        return None

    key = f"market:history:v1:{symbol}:{period}"
    try:
        cached = cache.get(key)
    except Exception:
        cached = None

    if isinstance(cached, dict) and cached.get("points"):
        return PriceHistory(
            ticker=symbol,
            period=period,
            points=[(stamp, Decimal(price)) for stamp, price in cached["points"]],
            latest=_to_decimal(cached.get("latest")),
            period_high=_to_decimal(cached.get("period_high")),
            period_low=_to_decimal(cached.get("period_low")),
            change_pct=_to_decimal(cached.get("change_pct")),
            sma_50=_to_decimal(cached.get("sma_50")),
            sma_200=_to_decimal(cached.get("sma_200")),
            drawdown_from_high_pct=_to_decimal(cached.get("drawdown_from_high_pct")),
            degradation_flags=list(cached.get("degradation_flags") or []),
        )

    try:
        import yfinance as yf

        frame = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
    except Exception as exc:
        logger.warning("Price history failed for %s: %s", symbol, exc)
        return None

    if frame is None or frame.empty or "Close" not in frame:
        logger.warning("No price history returned for %s", symbol)
        return None

    closes = frame["Close"].dropna()
    if closes.empty:
        return None

    def sma(window: int) -> Decimal | None:
        if len(closes) < window:
            return None
        return _to_decimal(closes.tail(window).mean())

    latest = _to_decimal(closes.iloc[-1])
    period_high = _to_decimal(closes.max())
    period_low = _to_decimal(closes.min())
    first = _to_decimal(closes.iloc[0])
    change_pct = (
        ((latest - first) / first * Decimal("100")).quantize(Decimal("0.01"))
        if latest is not None and first
        else None
    )
    drawdown = (
        ((latest - period_high) / period_high * Decimal("100")).quantize(Decimal("0.01"))
        if latest is not None and period_high
        else None
    )

    history = PriceHistory(
        ticker=symbol,
        period=period,
        points=[
            (stamp.date().isoformat() if hasattr(stamp, "date") else str(stamp), _to_decimal(price))
            for stamp, price in closes.tail(130).items()
            if _to_decimal(price) is not None
        ],
        latest=latest,
        period_high=period_high,
        period_low=period_low,
        change_pct=change_pct,
        sma_50=sma(50),
        sma_200=sma(200),
        drawdown_from_high_pct=drawdown,
    )
    history.degradation_flags = _derive_price_flags(history)

    try:
        cache.set(
            key,
            {
                "points": [[stamp, str(price)] for stamp, price in history.points],
                "latest": str(history.latest) if history.latest is not None else None,
                "period_high": str(history.period_high) if history.period_high else None,
                "period_low": str(history.period_low) if history.period_low else None,
                "change_pct": str(history.change_pct) if history.change_pct is not None else None,
                "sma_50": str(history.sma_50) if history.sma_50 is not None else None,
                "sma_200": str(history.sma_200) if history.sma_200 is not None else None,
                "drawdown_from_high_pct": (
                    str(history.drawdown_from_high_pct)
                    if history.drawdown_from_high_pct is not None
                    else None
                ),
                "degradation_flags": history.degradation_flags,
            },
            _cache_ttl(),
        )
    except Exception as exc:
        logger.warning("Could not cache price history for %s: %s", symbol, exc)

    return history


def _derive_fundamental_flags(health: FinancialHealth, price: Decimal | None) -> list[str]:
    flags: list[str] = []

    if health.debt_to_equity is not None and health.debt_to_equity > Decimal("150"):
        flags.append(
            f"High leverage: debt/equity of {health.debt_to_equity} "
            "(above 150 is heavily indebted)."
        )
    if health.current_ratio is not None and health.current_ratio < Decimal("1"):
        flags.append(
            f"Liquidity risk: current ratio {health.current_ratio} is below 1.0, "
            "so short-term liabilities exceed short-term assets."
        )
    if health.free_cash_flow is not None and health.free_cash_flow < 0:
        flags.append(f"Burning cash: free cash flow is ${health.free_cash_flow:,.0f}.")
    if health.profit_margin is not None and health.profit_margin < 0:
        flags.append(f"Unprofitable: net margin {health.profit_margin * 100:.2f}%.")
    if health.trailing_pe is not None and health.trailing_pe > Decimal("45"):
        flags.append(
            f"Rich valuation: trailing P/E of {health.trailing_pe} leaves little room "
            "for disappointment."
        )
    if price is not None and health.analyst_target is not None and health.analyst_target < price:
        downside = ((health.analyst_target - price) / price * Decimal("100")).quantize(
            Decimal("0.01")
        )
        flags.append(
            f"Street target ${health.analyst_target} sits {abs(downside)}% below the "
            f"current ${price}."
        )
    return flags


def get_financial_health(ticker: str, price: Decimal | None = None) -> FinancialHealth:
    """Balance-sheet and valuation facts with derived red flags.

    Never returns ``None``: an unavailable ticker yields a ``degraded`` object so
    the Risk Assessor can state that it found no evidence rather than crashing.
    """
    from services.market_data import normalize_ticker

    try:
        symbol = normalize_ticker(ticker)
    except Exception:
        return FinancialHealth(ticker=str(ticker), degraded=True)

    key = f"market:fundamentals:v1:{symbol}"
    try:
        cached = cache.get(key)
    except Exception:
        cached = None

    if isinstance(cached, dict):
        health = FinancialHealth(
            ticker=symbol,
            total_debt=_to_decimal(cached.get("total_debt")),
            total_cash=_to_decimal(cached.get("total_cash")),
            debt_to_equity=_to_decimal(cached.get("debt_to_equity")),
            current_ratio=_to_decimal(cached.get("current_ratio")),
            profit_margin=_to_decimal(cached.get("profit_margin"), "0.0001"),
            free_cash_flow=_to_decimal(cached.get("free_cash_flow")),
            trailing_pe=_to_decimal(cached.get("trailing_pe")),
            analyst_target=_to_decimal(cached.get("analyst_target")),
            recommendation_key=cached.get("recommendation_key", ""),
            sector=cached.get("sector", ""),
            degraded=bool(cached.get("degraded", False)),
        )
        health.red_flags = _derive_fundamental_flags(health, price)
        return health

    payload: dict[str, Any] = {"degraded": False}
    health = FinancialHealth(ticker=symbol)

    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info or {}
        health.total_debt = _to_decimal(info.get("totalDebt"))
        health.total_cash = _to_decimal(info.get("totalCash"))
        health.debt_to_equity = _to_decimal(info.get("debtToEquity"))
        health.current_ratio = _to_decimal(info.get("currentRatio"))
        health.profit_margin = _to_decimal(info.get("profitMargins"), "0.0001")
        health.free_cash_flow = _to_decimal(info.get("freeCashflow"))
        health.trailing_pe = _to_decimal(info.get("trailingPE"))
        health.analyst_target = _to_decimal(info.get("targetMeanPrice"))
        health.recommendation_key = str(info.get("recommendationKey") or "")
        health.sector = str(info.get("sector") or "")

        payload = {
            "total_debt": health.total_debt,
            "total_cash": health.total_cash,
            "debt_to_equity": health.debt_to_equity,
            "current_ratio": health.current_ratio,
            "profit_margin": health.profit_margin,
            "free_cash_flow": health.free_cash_flow,
            "trailing_pe": health.trailing_pe,
            "analyst_target": health.analyst_target,
            "recommendation_key": health.recommendation_key,
            "sector": health.sector,
            "degraded": False,
        }
    except Exception as exc:
        logger.warning("Fundamentals failed for %s: %s", symbol, exc)
        health.degraded = True
        payload["degraded"] = True
        return health

    # An object with every field empty carries no information.
    if not any(
        value is not None
        for value in (
            health.total_debt,
            health.debt_to_equity,
            health.current_ratio,
            health.profit_margin,
            health.trailing_pe,
        )
    ):
        health.degraded = True
        payload["degraded"] = True

    health.red_flags = _derive_fundamental_flags(health, price)

    try:
        cache.set(
            key,
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in payload.items()},
            _cache_ttl(),
        )
    except Exception as exc:
        logger.warning("Could not cache fundamentals for %s: %s", symbol, exc)

    return health


def today() -> date:
    from django.utils import timezone

    return timezone.now().date()
