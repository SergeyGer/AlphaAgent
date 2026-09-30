"""AlphaAgent MCP server - market and news tools over the Model Context Protocol.

Why this exists
---------------
The news scraper and the market-data client used to be private Python functions
reachable only from inside the CrewAI crew. Wrapping them in an MCP server turns
them into a **protocol-level capability**: any MCP-capable client (Claude Code,
Cursor, a local Ollama agent, another CrewAI crew, a LangGraph workflow) can call
them without a line of backend code changing.

What it exposes (all strictly read-only)
----------------------------------------
``get_market_price``      latest quote for a ticker
``get_news_sentiment``    scraped headlines with per-article polarity
``get_price_history``     daily closes, moving averages, drawdown
``get_financial_health``  leverage, liquidity, valuation, derived red flags
``get_market_snapshot``   the above combined, for one-call clients
``list_watchlist``        tickers this deployment tracks

What it deliberately does NOT expose
------------------------------------
No tool reads or writes portfolios, positions, recommendations or the ledger.
Nothing here can place a trade. Execution stays behind the REST API and its
guard, so a compromised or over-eager agent cannot move money through MCP.

Transports
----------
``stdio``            for clients that spawn the process (Claude Code, Cursor)
``streamable-http``  for the containerised microservice other services call

Run it with::

    python -m mcp_server                      # stdio (default)
    python -m mcp_server --transport http     # streamable HTTP on :8100/mcp
"""

from __future__ import annotations

import json
import logging
import os
import sys
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap Django *before* importing the service layer.
#
# Two ordering constraints:
#   1. runtime_env must run before anything imports CrewAI (see that module).
#   2. The stdio transport uses stdout for the protocol, so every log line must
#      go to stderr or it corrupts the message stream.
# ---------------------------------------------------------------------------
from runtime_env import ensure_writable_runtime_home

ensure_writable_runtime_home()
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def _force_logging_to_stderr() -> None:
    """Send all logs to stderr.

    Django's default console handler writes to stdout. Under the stdio
    transport that would interleave log lines with JSON-RPC frames and break the
    protocol, so this is a correctness requirement, not a preference.
    """
    for handler in logging.root.handlers[:]:
        if (
            isinstance(handler, logging.StreamHandler)
            and getattr(handler, "stream", None) is sys.stdout
        ):
            logging.root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s - %(message)s"))
    logging.root.addHandler(stream)
    logging.root.setLevel(os.environ.get("MCP_LOG_LEVEL", "WARNING"))


_force_logging_to_stderr()

import django  # noqa: E402

django.setup()

# Django's LOGGING config installs its own handlers during setup(); strip the
# stdout one again now that it has been applied.
_force_logging_to_stderr()

from mcp.server.fastmcp import FastMCP  # noqa: E402

# Aliased: the @mcp.tool functions below intentionally share these names,
# and an unaliased import would make them call themselves recursively.
from services.fundamentals import get_financial_health as _financial_health  # noqa: E402
from services.fundamentals import get_price_history as _price_history  # noqa: E402
from services.market_data import get_latest_quote  # noqa: E402
from services.news import build_news_report  # noqa: E402

logger = logging.getLogger("alphaagent.mcp")

DEFAULT_PORT = int(os.environ.get("MCP_PORT", "8100"))
DEFAULT_HOST = os.environ.get("MCP_HOST", "0.0.0.0")

INSTRUCTIONS = """
AlphaAgent market-data tools.

Read-only access to live quotes, financial news with sentiment scoring, price
history and company fundamentals for equities and major crypto pairs.

Tickers are ordinary symbols ("AAPL", "TSLA"); major crypto is accepted either
as the base asset ("BTC") or as a Yahoo pair ("BTC-USD"). Every tool returns a
JSON object and degrades to an explicit `"degraded": true` payload rather than
raising when an upstream provider is unavailable.

These tools cannot read portfolios or place trades.
""".strip()

mcp: FastMCP = FastMCP(
    name="alphaagent-market-data",
    instructions=INSTRUCTIONS,
    host=DEFAULT_HOST,
    port=DEFAULT_PORT,
)


def _json(payload: Any) -> str:
    """Serialise Decimals and dataclasses into a JSON string."""

    def default(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, "as_dict"):
            return value.as_dict()
        return str(value)

    return json.dumps(payload, default=default, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def get_market_price(ticker: str) -> str:
    """Get the latest market price for a ticker.

    Args:
        ticker: Equity or crypto symbol, e.g. "AAPL", "TSLA", "BTC".

    Returns:
        JSON with price, previous_close, change_pct, currency, source and as_of.
        ``source`` is "yfinance" for a live quote, "cache" for a recent cached
        one, or "fallback" when only a stored price was available.
    """
    quote = get_latest_quote(ticker)
    if quote is None:
        return _json({"ticker": ticker, "error": "price unavailable", "degraded": True})
    return _json(quote.as_dict())


@mcp.tool()
def get_news_sentiment(ticker: str, limit: int = 10) -> str:
    """Scrape recent news headlines for a ticker and score their sentiment.

    Headlines come from Google News and Yahoo Finance RSS. Each article carries
    its own polarity, so a caller can present bullish and bearish coverage
    separately instead of relying on one blended score.

    Args:
        ticker: Equity or crypto symbol.
        limit: Maximum number of headlines to return (1-30).

    Returns:
        JSON with the aggregated sentiment label/score/confidence, plus an
        ``articles`` array where each entry has ``polarity`` and ``sentiment``.
    """
    limit = max(1, min(int(limit or 10), 30))
    report = build_news_report(ticker, limit=limit)
    payload = report.as_dict()
    payload["positive_count"] = len(report.positive_articles)
    payload["negative_count"] = len(report.negative_articles)
    payload["neutral_count"] = len(report.neutral_articles)
    return _json(payload)


@mcp.tool()
def get_price_history(ticker: str, period: str = "1y") -> str:
    """Get daily closing prices and trend statistics for a ticker.

    Args:
        ticker: Equity or crypto symbol.
        period: yfinance window - "1mo", "3mo", "6mo", "1y", "2y", "5y".

    Returns:
        JSON with latest, period_high/low, change_pct, sma_50, sma_200,
        drawdown_from_high_pct, boolean trend flags, derived degradation flags
        and a ``points`` array of [date, close] pairs for charting.
    """
    history = _price_history(ticker, period=period or "1y")
    if history is None:
        return _json({"ticker": ticker, "error": "history unavailable", "degraded": True})
    return _json(history.as_dict())


@mcp.tool()
def get_financial_health(ticker: str) -> str:
    """Get balance-sheet and valuation facts for a ticker, with derived red flags.

    Useful for constructing a bearish case: leverage, liquidity, cash burn,
    valuation and analyst downside are surfaced explicitly.

    Args:
        ticker: Equity symbol, e.g. "AAPL". Crypto pairs have no fundamentals
            and return a degraded payload.

    Returns:
        JSON with total_debt, debt_to_equity, current_ratio, profit_margin,
        free_cash_flow, trailing_pe, analyst_target, recommendation_key, sector
        and a ``red_flags`` array of plain-English warnings.
    """
    health = _financial_health(ticker)
    return _json(health.as_dict())


@mcp.tool()
def get_market_snapshot(ticker: str) -> str:
    """Get price, trend, fundamentals and news sentiment in a single call.

    Convenience wrapper for clients that would otherwise make four round trips.

    Args:
        ticker: Equity or crypto symbol.

    Returns:
        JSON with ``price``, ``history``, ``fundamentals`` and ``news`` objects.
        Any section that could not be fetched is marked degraded.
    """
    quote = get_latest_quote(ticker)
    price = quote.price if quote else None

    history = _price_history(ticker)
    health = _financial_health(ticker, price=price)
    news = build_news_report(ticker)

    return _json(
        {
            "ticker": ticker.upper(),
            "price": quote.as_dict() if quote else None,
            "history": history.as_dict() if history else None,
            "fundamentals": health.as_dict(),
            "news": {
                "sentiment": news.sentiment.as_dict(),
                "headline_count": news.headline_count,
                "positive_count": len(news.positive_articles),
                "negative_count": len(news.negative_articles),
                "neutral_count": len(news.neutral_articles),
                "top_headlines": [a.as_dict() for a in news.articles[:5]],
            },
            "degraded": quote is None and history is None and health.degraded,
        }
    )


@mcp.tool()
def list_watchlist() -> str:
    """List the tickers this deployment monitors.

    Returns:
        JSON with a ``tickers`` array from the ALPHA_WATCHLIST setting.
    """
    from django.conf import settings

    watchlist = [str(t).upper() for t in getattr(settings, "ALPHA_WATCHLIST", [])]
    return _json({"tickers": watchlist, "count": len(watchlist)})


# ---------------------------------------------------------------------------
# Entrypoint helpers
# ---------------------------------------------------------------------------
def run(transport: str = "stdio") -> None:
    """Start the server on the requested transport."""
    normalised = {
        "stdio": "stdio",
        "http": "streamable-http",
        "streamable-http": "streamable-http",
        "sse": "sse",
    }.get(transport, "stdio")

    if normalised == "stdio":
        # stdout belongs to the protocol from here on.
        logger.info("Starting AlphaAgent MCP server on stdio")
    else:
        logger.warning(
            "Starting AlphaAgent MCP server on %s http://%s:%s%s",
            normalised,
            DEFAULT_HOST,
            DEFAULT_PORT,
            mcp.settings.streamable_http_path,
        )

    mcp.run(transport=normalised)  # type: ignore[arg-type]


if __name__ == "__main__":
    run(os.environ.get("MCP_TRANSPORT", "stdio"))
