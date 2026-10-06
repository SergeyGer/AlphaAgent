"""News scraping layer for the Financial News Analyst agent.

Strategy
--------
1. Query several public RSS endpoints (no API key, no scraping of protected
   pages) for the ticker.
2. Parse with the stdlib XML parser, de-duplicate by normalised title.
3. Score the batch with :mod:`services.sentiment` to obtain a grounded,
   reproducible sentiment label.

Every network failure is contained: the caller receives an empty (but valid)
report and the platform keeps operating.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

import requests
from defusedxml import ElementTree as ET  # hardened parser: RSS is untrusted input
from defusedxml.common import DefusedXmlException
from django.conf import settings
from django.core.cache import cache

from core.log_safety import log_safe
from services.sentiment import SentimentResult, aggregate_sentiment, score_text
from services.tickers import normalise_ticker

logger = logging.getLogger("alphaagent.news")

__all__ = [
    "NewsArticle",
    "NewsReport",
    "build_news_report",
    "fetch_news_articles",
]

_USER_AGENT = "Mozilla/5.0 (compatible; AlphaAgent/1.0; +https://example.invalid)"
_HTTP_TIMEOUT = 15
_CACHE_KEY = "news:report:v2:{ticker}:{limit}"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


@dataclass(frozen=True)
class NewsArticle:
    """A single normalised headline."""

    title: str
    source: str
    url: str = ""
    published_at: str | None = None
    summary: str = ""
    # Per-article scoring, so the dashboard can split coverage into
    # positive and negative columns rather than showing one blended number.
    polarity: float = 0.0
    sentiment: str = "NEUTRAL"

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at,
            "summary": self.summary[:400],
            "polarity": round(self.polarity, 4),
            "sentiment": self.sentiment,
        }

    def as_text(self) -> str:
        return f"{self.title}. {self.summary}".strip()


@dataclass
class NewsReport:
    """The analyst agent's evidence bundle."""

    ticker: str
    articles: list[NewsArticle] = field(default_factory=list)
    sentiment: SentimentResult = field(default_factory=SentimentResult)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sources_used: list[str] = field(default_factory=list)
    degraded: bool = False

    @property
    def headline_count(self) -> int:
        return len(self.articles)

    @property
    def positive_articles(self) -> list[NewsArticle]:
        """Bullish coverage, strongest first."""
        return sorted(
            (a for a in self.articles if a.sentiment == "BULLISH"),
            key=lambda a: a.polarity,
            reverse=True,
        )

    @property
    def negative_articles(self) -> list[NewsArticle]:
        """Bearish coverage, strongest first."""
        return sorted(
            (a for a in self.articles if a.sentiment == "BEARISH"),
            key=lambda a: a.polarity,
        )

    @property
    def neutral_articles(self) -> list[NewsArticle]:
        return [a for a in self.articles if a.sentiment == "NEUTRAL"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "headline_count": self.headline_count,
            "sentiment": self.sentiment.as_dict(),
            "sources_used": self.sources_used,
            "degraded": self.degraded,
            "fetched_at": self.fetched_at.isoformat(),
            "articles": [a.as_dict() for a in self.articles],
        }

    def as_prompt_block(self) -> str:
        """Compact textual digest passed to the LLM agent."""
        if not self.articles:
            return f"No recent headlines available for {self.ticker}."
        lines = [
            f"Ticker: {self.ticker}",
            f"Headlines analysed: {self.headline_count}",
            (
                "Lexicon sentiment: "
                f"{self.sentiment.label} (score={self.sentiment.score:.3f}, "
                f"confidence={self.sentiment.confidence:.2f})"
            ),
            "",
        ]
        positives = self.positive_articles
        negatives = self.negative_articles
        neutral = self.neutral_articles

        if positives:
            lines.append(f"POSITIVE COVERAGE ({len(positives)}):")
            for article in positives:
                lines.append(f"  + [{article.polarity:+.2f}] {article.title} - {article.source}")
            lines.append("")

        if negatives:
            lines.append(f"NEGATIVE COVERAGE ({len(negatives)}):")
            for article in negatives:
                lines.append(f"  - [{article.polarity:+.2f}] {article.title} - {article.source}")
            lines.append("")

        if neutral:
            lines.append(f"NEUTRAL / FACTUAL ({len(neutral)}):")
            for article in neutral:
                lines.append(f"  ~ {article.title} - {article.source}")

        return "\n".join(lines)


# Per-article thresholds. Deliberately tighter than the batch thresholds in
# services.sentiment: one headline carries less evidence than ten.
_ARTICLE_BULLISH = 1.0
_ARTICLE_BEARISH = -1.0


def _score_article(article: NewsArticle) -> NewsArticle:
    """Attach an individual polarity and label to one headline."""
    polarity, _positive, _negative = score_text(article.as_text())
    if polarity >= _ARTICLE_BULLISH:
        label = "BULLISH"
    elif polarity <= _ARTICLE_BEARISH:
        label = "BEARISH"
    else:
        label = "NEUTRAL"
    return replace(article, polarity=polarity, sentiment=label)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _rss_urls(ticker: str) -> list[tuple[str, str]]:
    """Return ``(source_name, url)`` pairs for a ticker.

    The ticker is validated before it reaches the URL. Only the query string was
    ever attacker-influenced (both hosts are hardcoded), but passing unvalidated
    text into an outbound request is what CodeQL flags as ``py/partial-ssrf`` -
    and the same string is logged, which is the log-injection finding. One check
    at the boundary closes both.
    """
    ticker = normalise_ticker(ticker)
    q = quote_plus(f"{ticker} stock")
    return [
        (
            "google_news",
            f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
        ),
        (
            "yahoo_finance",
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(ticker)}"
            "&region=US&lang=en-US",
        ),
    ]


def _parse_rss(payload: bytes, source_name: str) -> list[NewsArticle]:
    """Parse an RSS 2.0 document into articles (never raises).

    Uses ``defusedxml``: RSS is attacker-controllable input, and the stdlib
    parser is vulnerable to entity-expansion ("billion laughs") denial of
    service. ``DefusedXmlException`` covers the blocked-attack cases.
    """
    articles: list[NewsArticle] = []
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        logger.warning("Malformed RSS from %s: %s", source_name, exc)
        return articles
    except DefusedXmlException as exc:
        logger.error("Blocked unsafe XML from %s: %s", source_name, exc)
        return articles

    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip() or None
        summary = _clean(item.findtext("description"))

        # Google News exposes the publisher in a <source> element.
        publisher = _clean(item.findtext("source")) or source_name
        articles.append(
            NewsArticle(
                title=title,
                source=publisher,
                url=link,
                published_at=pub_date,
                summary=summary,
            )
        )
    return articles


def _dedupe(articles: list[NewsArticle]) -> list[NewsArticle]:
    seen: set[str] = set()
    unique: list[NewsArticle] = []
    for article in articles:
        fingerprint = _NON_ALNUM_RE.sub("", article.title.lower())[:110]
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(article)
    return unique


def fetch_news_articles(ticker: str, limit: int = 10) -> tuple[list[NewsArticle], list[str]]:
    """Fetch up to ``limit`` de-duplicated articles for ``ticker``."""
    collected: list[NewsArticle] = []
    used: list[str] = []

    for source_name, url in _rss_urls(ticker):
        try:
            response = requests.get(
                url,
                timeout=_HTTP_TIMEOUT,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/rss+xml, application/xml",
                },
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning(
                "RSS fetch failed (%s) for %s: %s", source_name, log_safe(ticker), log_safe(exc)
            )
            continue

        parsed = _parse_rss(response.content, source_name)
        if parsed:
            used.append(source_name)
            collected.extend(parsed)

    return _dedupe(collected)[:limit], used


def build_news_report(ticker: str, limit: int | None = None, use_cache: bool = True) -> NewsReport:
    """Build the full analyst evidence bundle (cached, never raises)."""
    ai_cfg = getattr(settings, "AI_CONFIG", {})
    effective_limit = int(limit or ai_cfg.get("NEWS_ARTICLE_LIMIT", 10) or 10)
    symbol = (ticker or "").strip().upper()
    cache_key = _CACHE_KEY.format(ticker=symbol, limit=effective_limit)

    if use_cache:
        try:
            cached = cache.get(cache_key)
        except Exception as exc:
            logger.warning("News cache read failed for %s: %s", log_safe(symbol), log_safe(exc))
            cached = None
        if isinstance(cached, dict) and cached.get("articles") is not None:
            articles = [
                NewsArticle(
                    title=a.get("title", ""),
                    source=a.get("source", ""),
                    url=a.get("url", ""),
                    published_at=a.get("published_at"),
                    summary=a.get("summary", ""),
                    polarity=float(a.get("polarity") or 0.0),
                    sentiment=a.get("sentiment") or "NEUTRAL",
                )
                for a in cached["articles"]
            ]
            return NewsReport(
                ticker=symbol,
                articles=articles,
                sentiment=aggregate_sentiment([a.as_text() for a in articles]),
                sources_used=cached.get("sources_used", []),
                degraded=bool(cached.get("degraded", False)),
            )

    articles: list[NewsArticle] = []
    sources: list[str] = []
    try:
        articles, sources = fetch_news_articles(symbol, effective_limit)
    except Exception as exc:
        logger.exception("Unexpected news failure for %s: %s", log_safe(symbol), log_safe(exc))

    articles = [_score_article(article) for article in articles]
    sentiment = aggregate_sentiment([a.as_text() for a in articles])
    report = NewsReport(
        ticker=symbol,
        articles=articles,
        sentiment=sentiment,
        sources_used=sources,
        degraded=not articles,
    )

    ttl = int(ai_cfg.get("NEWS_CACHE_TTL", 900))
    try:
        cache.set(
            cache_key,
            {
                "articles": [a.as_dict() for a in articles],
                "sources_used": sources,
                "degraded": report.degraded,
            },
            ttl,
        )
    except Exception as exc:
        logger.warning("News cache write failed for %s: %s", log_safe(symbol), log_safe(exc))

    return report
