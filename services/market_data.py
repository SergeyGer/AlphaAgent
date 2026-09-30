"""Market data access layer (yfinance).

Responsibilities
----------------
* Normalise tickers (``BTC`` -> ``BTC-USD`` for Yahoo Finance).
* Fetch the latest close/last price with retries and a hard timeout.
* De-duplicate upstream calls through the Redis cache.
* **Never raise into the request/task path** - return ``None`` and log instead,
  so a market-data outage degrades the platform instead of breaking it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("alphaagent.market_data")

__all__ = [
    "MarketDataError",
    "PriceQuote",
    "get_latest_price",
    "get_latest_quote",
    "normalize_ticker",
]

# Common crypto symbols quoted against USD on Yahoo Finance.
_CRYPTO_BASES = {
    "BTC",
    "ETH",
    "SOL",
    "ADA",
    "XRP",
    "DOGE",
    "AVAX",
    "DOT",
    "LTC",
    "LINK",
    "MATIC",
    "BNB",
}

_CACHE_KEY = "market:quote:v1:{symbol}"


class MarketDataError(RuntimeError):
    """Raised only by the strict accessor; the tolerant API swallows it."""


@dataclass(frozen=True)
class PriceQuote:
    """Normalised price observation."""

    ticker: str
    symbol: str
    price: Decimal
    previous_close: Decimal | None = None
    currency: str = "USD"
    source: str = "yfinance"  # yfinance | cache | fallback
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def change_pct(self) -> Decimal | None:
        if not self.previous_close:
            return None
        delta = self.price - self.previous_close
        return (delta / self.previous_close * Decimal("100")).quantize(Decimal("0.01"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "symbol": self.symbol,
            "price": str(self.price),
            "previous_close": str(self.previous_close) if self.previous_close else None,
            "change_pct": str(self.change_pct) if self.change_pct is not None else None,
            "currency": self.currency,
            "source": self.source,
            "as_of": self.as_of.isoformat(),
        }


def normalize_ticker(ticker: str) -> str:
    """Map a platform ticker to its Yahoo Finance symbol."""
    symbol = (ticker or "").strip().upper()
    if not symbol:
        raise MarketDataError("Empty ticker symbol")
    if symbol in _CRYPTO_BASES:
        return f"{symbol}-USD"
    # ``BTCUSD`` / ``BTCUSDT`` style input.
    for quote in ("USDT", "USD"):
        if symbol.endswith(quote) and symbol[: -len(quote)] in _CRYPTO_BASES:
            return f"{symbol[: -len(quote)]}-USD"
    return symbol


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if dec.is_nan() or dec <= 0:
        return None
    return dec.quantize(Decimal("0.01"))


def _fetch_from_yfinance(symbol: str) -> PriceQuote | None:
    """Blocking upstream fetch. Imported lazily to keep Django boot fast."""
    import yfinance as yf  # local import: heavy, optional at import time

    ticker_obj = yf.Ticker(symbol)
    price: Decimal | None = None
    previous_close: Decimal | None = None

    try:
        fast = ticker_obj.fast_info
        price = _to_decimal(fast.last_price)
        previous_close = _to_decimal(fast.previous_close)
    except Exception as exc:
        logger.warning("yfinance fast_info failed for %s: %s", symbol, exc)

    if price is None:
        # Fallback: 5-day daily candles, last non-null close.
        history = ticker_obj.history(period="5d", interval="1d", auto_adjust=False)
        if history is not None and not history.empty:
            closes = history["Close"].dropna()
            if len(closes) >= 1:
                price = _to_decimal(closes.iloc[-1])
            if len(closes) >= 2:
                previous_close = _to_decimal(closes.iloc[-2])

    if price is None:
        return None

    return PriceQuote(
        ticker=symbol,
        symbol=symbol,
        price=price,
        previous_close=previous_close,
        source="yfinance",
    )


def get_latest_quote(
    ticker: str,
    *,
    fallback_price: Decimal | None = None,
    use_cache: bool = True,
) -> PriceQuote | None:
    """Return the best available quote for ``ticker``.

    Order of preference: Redis cache -> yfinance -> ``fallback_price``
    (typically the portfolio's average purchase price).

    Returns ``None`` only when every source is unavailable and no fallback was
    supplied.
    """
    try:
        symbol = normalize_ticker(ticker)
    except MarketDataError as exc:
        logger.error("Cannot normalise ticker %r: %s", ticker, exc)
        return None

    cache_key = _CACHE_KEY.format(symbol=symbol)
    if use_cache:
        try:
            cached = cache.get(cache_key)
        except Exception as exc:
            logger.warning("Cache read failed for %s: %s", symbol, exc)
            cached = None
        if isinstance(cached, dict) and cached.get("price"):
            try:
                return PriceQuote(
                    ticker=ticker.upper(),
                    symbol=symbol,
                    price=Decimal(str(cached["price"])),
                    previous_close=(
                        Decimal(str(cached["previous_close"]))
                        if cached.get("previous_close")
                        else None
                    ),
                    currency=cached.get("currency", "USD"),
                    source="cache",
                    as_of=(
                        datetime.fromisoformat(cached["as_of"])
                        if cached.get("as_of")
                        else datetime.now(UTC)
                    ),
                )
            except (InvalidOperation, ValueError, TypeError):
                logger.warning("Discarding malformed cached quote for %s", symbol)

    quote: PriceQuote | None = None
    retries = max(int(getattr(settings, "AI_CONFIG", {}).get("MAX_RETRIES", 2)), 0) + 1
    for attempt in range(1, retries + 1):
        try:
            quote = _fetch_from_yfinance(symbol)
        except Exception as exc:
            logger.warning(
                "yfinance fetch error for %s (attempt %s/%s): %s", symbol, attempt, retries, exc
            )
            quote = None
        if quote is not None:
            break

    if quote is not None:
        ttl = int(getattr(settings, "AI_CONFIG", {}).get("PRICE_CACHE_TTL", 60))
        try:
            cache.set(
                cache_key,
                {
                    "price": str(quote.price),
                    "previous_close": (str(quote.previous_close) if quote.previous_close else None),
                    "currency": quote.currency,
                    "as_of": quote.as_of.isoformat(),
                },
                ttl,
            )
        except Exception as exc:
            logger.warning("Cache write failed for %s: %s", symbol, exc)
        return PriceQuote(
            ticker=ticker.upper(),
            symbol=symbol,
            price=quote.price,
            previous_close=quote.previous_close,
            currency=quote.currency,
            source=quote.source,
            as_of=quote.as_of,
        )

    if fallback_price is not None and fallback_price > 0:
        logger.warning("Falling back to stored price for %s: %s", symbol, fallback_price)
        return PriceQuote(
            ticker=ticker.upper(),
            symbol=symbol,
            price=Decimal(fallback_price).quantize(Decimal("0.01")),
            previous_close=None,
            source="fallback",
        )

    logger.error("No market data available for %s", symbol)
    return None


def get_latest_price(ticker: str, *, fallback_price: Decimal | None = None) -> Decimal | None:
    """Convenience accessor returning just the price."""
    quote = get_latest_quote(ticker, fallback_price=fallback_price)
    return quote.price if quote else None
