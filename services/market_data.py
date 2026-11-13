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
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache

from core.log_safety import log_safe

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
    """Normalised price observation.

    ``as_of`` is the instant the market observation was made. It is deliberately
    nullable: a provider or an old cache row that does not say when the price
    was observed has *unknown* provenance, and the execution policy treats that
    as missing data. Filling it with ``now`` would turn a cache hit into a fresh
    observation that never happened.

    ``retrieved_at`` is when this process read the value. It is a different fact
    from the observation time and is carried separately for exactly that reason.
    """

    ticker: str
    symbol: str
    price: Decimal
    previous_close: Decimal | None = None
    currency: str = "USD"
    source: str = "yfinance"  # yfinance_intraday | yfinance_daily_close | cache | fallback
    as_of: datetime | None = None
    #: The observation time of ``previous_close``, which is a different market
    #: fact from ``as_of``. A fast intraday price must never be stamped with the
    #: daily candle's closing time, and a previous close must carry its own
    #: session close rather than borrow the current price's instant.
    previous_close_as_of: datetime | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

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
            "previous_close_as_of": (
                self.previous_close_as_of.isoformat() if self.previous_close_as_of else None
            ),
            "change_pct": str(self.change_pct) if self.change_pct is not None else None,
            "currency": self.currency,
            "source": self.source,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "retrieved_at": self.retrieved_at.isoformat(),
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


def _as_utc(value: Any) -> datetime | None:
    """Convert a provider timestamp (including a pandas Timestamp) to UTC."""
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        try:
            value = value.to_pydatetime()
        except (TypeError, ValueError):
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _frame_observation(frame: Any) -> tuple[Decimal, datetime] | None:
    """The last close in a frame and the timestamp that belongs to it."""
    if frame is None or getattr(frame, "empty", True) or "Close" not in frame:
        return None
    closes = frame["Close"].dropna()
    if closes.empty:
        return None
    price = _to_decimal(closes.iloc[-1])
    observed_at = _as_utc(closes.index[-1])
    if price is None or observed_at is None:
        return None
    return price, observed_at


def _intraday_observation(ticker_obj, symbol: str) -> tuple[Decimal, datetime] | None:
    """A timestamped intraday trade, preferred over an untimed fast price.

    ``fast_info`` reports a last price but no instant. An intraday bar does
    carry one, so it is the only fast source that can authorise an execution
    without inheriting a daily candle's close.
    """
    try:
        frame = ticker_obj.history(period="1d", interval="1m", auto_adjust=False, prepost=False)
    except Exception as exc:
        logger.warning(
            "yfinance intraday history failed for %s: %s", log_safe(symbol), log_safe(exc)
        )
        return None
    return _frame_observation(frame)


def _daily_observations(ticker_obj, symbol: str) -> list[tuple[datetime, Decimal]]:
    """Completed daily closes, each with the session close that observed it.

    A daily candle for an unfinished session is not a close. Its session-close
    instant is in the future, so it is dropped rather than used to stamp a fast
    price or to become the current mark. The previous close keeps its own
    timestamp, which is why the result is a list of pairs rather than a value.
    """
    from services.quote_policy import FUTURE_SKEW_SECONDS, session_close_utc

    try:
        frame = ticker_obj.history(period="5d", interval="1d", auto_adjust=False)
    except Exception as exc:
        logger.warning("yfinance history failed for %s: %s", log_safe(symbol), log_safe(exc))
        return []
    if frame is None or getattr(frame, "empty", True) or "Close" not in frame:
        return []
    closes = frame["Close"].dropna()
    if closes.empty:
        return []
    now = datetime.now(UTC)
    observations: list[tuple[datetime, Decimal]] = []
    for index, raw_close in closes.items():
        observed_at = session_close_utc(index)
        price = _to_decimal(raw_close)
        if observed_at is None or price is None:
            continue
        if observed_at > now + timedelta(seconds=FUTURE_SKEW_SECONDS):
            # The candle's session has not closed yet; it is not a close.
            continue
        observations.append((observed_at, price))
    observations.sort(key=lambda item: item[0])
    return observations


def _previous_close_before(
    daily: list[tuple[datetime, Decimal]], session_date
) -> tuple[Decimal, datetime] | None:
    """The last completed close strictly before ``session_date``."""
    candidates = [
        (price, observed_at)
        for observed_at, price in daily
        if observed_at.astimezone(ZoneInfo("America/New_York")).date() < session_date
    ]
    return candidates[-1] if candidates else None


def _fetch_from_yfinance(symbol: str) -> PriceQuote | None:
    """Blocking upstream fetch. Imported lazily to keep Django boot fast.

    Observation times are attached per price. A timestamped intraday bar is
    preferred and keeps its own instant; a daily close keeps the real session
    close of its candle, and an unfinished candle is never used as a close. A
    ``fast_info`` price without an intraday timestamp stays untimed (``as_of``
    ``None``) instead of inheriting the daily candle's closing time: an unknown
    observation instant is missing data, not a fresh mark.
    """
    import yfinance as yf  # local import: heavy, optional at import time

    ticker_obj = yf.Ticker(symbol)
    fast_price: Decimal | None = None
    fast_previous: Decimal | None = None
    try:
        fast = ticker_obj.fast_info
        fast_price = _to_decimal(fast.last_price)
        fast_previous = _to_decimal(fast.previous_close)
    except Exception as exc:
        logger.warning("yfinance fast_info failed for %s: %s", symbol, exc)

    intraday = _intraday_observation(ticker_obj, symbol)
    daily = _daily_observations(ticker_obj, symbol)

    price: Decimal | None = None
    previous_close: Decimal | None = None
    as_of: datetime | None = None
    previous_close_as_of: datetime | None = None
    source = "yfinance"

    if intraday is not None:
        price, as_of = intraday
        source = "yfinance_intraday"
        session_date = as_of.astimezone(ZoneInfo("America/New_York")).date()
        previous = _previous_close_before(daily, session_date)
        if previous is not None:
            previous_close, previous_close_as_of = previous
        elif fast_previous is not None:
            previous_close = fast_previous
    elif fast_price is not None:
        price = fast_price
        source = "yfinance_untimed"
        today = datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).date()
        previous = _previous_close_before(daily, today)
        if previous is not None:
            previous_close, previous_close_as_of = previous
        elif fast_previous is not None:
            previous_close = fast_previous
    elif daily:
        as_of, price = daily[-1]
        source = "yfinance_daily_close"
        if len(daily) >= 2:
            previous_close_as_of, previous_close = daily[-2]

    if price is None:
        return None

    return PriceQuote(
        ticker=symbol,
        symbol=symbol,
        price=price,
        previous_close=previous_close,
        source=source,
        as_of=as_of,
        previous_close_as_of=previous_close_as_of,
        retrieved_at=datetime.now(UTC),
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
                        datetime.fromisoformat(cached["as_of"]) if cached.get("as_of") else None
                    ),
                    previous_close_as_of=(
                        datetime.fromisoformat(cached["previous_close_as_of"])
                        if cached.get("previous_close_as_of")
                        else None
                    ),
                    retrieved_at=datetime.now(UTC),
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
                    "source": quote.source,
                    "as_of": quote.as_of.isoformat() if quote.as_of else None,
                    "previous_close_as_of": (
                        quote.previous_close_as_of.isoformat()
                        if quote.previous_close_as_of
                        else None
                    ),
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
            previous_close_as_of=quote.previous_close_as_of,
            retrieved_at=quote.retrieved_at,
        )

    if fallback_price is not None and fallback_price > 0:
        logger.warning("Falling back to stored price for %s: %s", symbol, fallback_price)
        return PriceQuote(
            ticker=ticker.upper(),
            symbol=symbol,
            price=Decimal(fallback_price).quantize(Decimal("0.01")),
            previous_close=None,
            source="fallback",
            # A stored price has no market observation instant. Recording "now"
            # here would make a planning crutch indistinguishable from a quote.
            as_of=None,
            retrieved_at=datetime.now(UTC),
        )

    logger.error("No market data available for %s", symbol)
    return None


def get_latest_price(ticker: str, *, fallback_price: Decimal | None = None) -> Decimal | None:
    """Convenience accessor returning just the price."""
    quote = get_latest_quote(ticker, fallback_price=fallback_price)
    return quote.price if quote else None
