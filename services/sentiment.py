"""Deterministic financial sentiment scoring.

The news-analyst agent needs a *reproducible* numeric signal.  An LLM alone is
non-deterministic and may be unavailable, so headlines are scored with a
finance-tuned lexicon (with negation and intensifier handling) and the result is
handed to the LLM as grounding evidence rather than being replaced by it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

__all__ = [
    "BEARISH",
    "BULLISH",
    "NEUTRAL",
    "SentimentResult",
    "aggregate_sentiment",
    "score_text",
]

BULLISH = "BULLISH"
BEARISH = "BEARISH"
NEUTRAL = "NEUTRAL"

# Weighted finance lexicon. Weight 2 == strong signal, 1 == mild.
_POSITIVE: dict[str, float] = {
    "surge": 2.0,
    "surges": 2.0,
    "surged": 2.0,
    "soar": 2.0,
    "soars": 2.0,
    "soared": 2.0,
    "rally": 1.5,
    "rallies": 1.5,
    "rallied": 1.5,
    "jump": 1.5,
    "jumps": 1.5,
    "jumped": 1.5,
    "gain": 1.0,
    "gains": 1.0,
    "gained": 1.0,
    "rise": 1.0,
    "rises": 1.0,
    "rose": 1.0,
    "climb": 1.0,
    "climbs": 1.0,
    "climbed": 1.0,
    "advance": 1.0,
    "advances": 1.0,
    "beat": 1.5,
    "beats": 1.5,
    "outperform": 2.0,
    "outperforms": 2.0,
    "outperformed": 2.0,
    "upgrade": 2.0,
    "upgraded": 2.0,
    "upgrades": 2.0,
    "buy": 1.0,
    "overweight": 1.5,
    "bullish": 2.0,
    "record": 1.5,
    "high": 1.0,
    "highs": 1.0,
    "strong": 1.5,
    "stronger": 1.5,
    "growth": 1.0,
    "grew": 1.0,
    "profit": 1.0,
    "profits": 1.0,
    "profitable": 1.5,
    "revenue": 0.5,
    "expansion": 1.0,
    "expands": 1.0,
    "optimism": 1.5,
    "optimistic": 1.5,
    "confidence": 1.0,
    "breakthrough": 2.0,
    "approval": 1.5,
    "approved": 1.5,
    "partnership": 1.0,
    "launch": 1.0,
    "launches": 1.0,
    "demand": 1.0,
    "boost": 1.5,
    "boosts": 1.5,
    "boosted": 1.5,
    "momentum": 1.0,
    "recovery": 1.0,
    "rebound": 1.5,
    "rebounds": 1.5,
    "dividend": 1.0,
    "buyback": 1.5,
    "upside": 1.5,
    "accelerate": 1.0,
    "accelerating": 1.0,
    "milestone": 1.0,
    "wins": 1.0,
    "win": 1.0,
}

_NEGATIVE: dict[str, float] = {
    "plunge": 2.0,
    "plunges": 2.0,
    "plunged": 2.0,
    "crash": 2.0,
    "crashes": 2.0,
    "crashed": 2.0,
    "slump": 2.0,
    "slumps": 2.0,
    "slumped": 2.0,
    "tumble": 2.0,
    "tumbles": 2.0,
    "tumbled": 2.0,
    "sink": 1.5,
    "sinks": 1.5,
    "sank": 1.5,
    "drop": 1.0,
    "drops": 1.0,
    "dropped": 1.0,
    "fall": 1.0,
    "falls": 1.0,
    "fell": 1.0,
    "decline": 1.0,
    "declines": 1.0,
    "declined": 1.0,
    "slide": 1.0,
    "slides": 1.0,
    "loss": 1.5,
    "losses": 1.5,
    "lose": 1.0,
    "loses": 1.0,
    "lost": 1.0,
    "miss": 1.5,
    "misses": 1.5,
    "missed": 1.5,
    "underperform": 2.0,
    "underperforms": 2.0,
    "downgrade": 2.0,
    "downgraded": 2.0,
    "downgrades": 2.0,
    "sell": 1.0,
    "underweight": 1.5,
    "bearish": 2.0,
    "weak": 1.5,
    "weaker": 1.5,
    "weakness": 1.5,
    "low": 1.0,
    "lows": 1.0,
    "warning": 1.5,
    "warns": 1.5,
    "warned": 1.5,
    "lawsuit": 1.5,
    "investigation": 1.5,
    "probe": 1.5,
    "fine": 1.0,
    "fined": 1.5,
    "fraud": 2.5,
    "bankruptcy": 2.5,
    "default": 2.0,
    "recession": 2.0,
    "inflation": 1.0,
    "layoffs": 1.5,
    "layoff": 1.5,
    "cut": 1.0,
    "cuts": 1.0,
    "slashed": 1.5,
    "slashes": 1.5,
    "recall": 1.5,
    "delay": 1.0,
    "delays": 1.0,
    "delayed": 1.0,
    "halt": 1.5,
    "halts": 1.5,
    "risk": 0.5,
    "risks": 0.5,
    "concern": 1.0,
    "concerns": 1.0,
    "fear": 1.5,
    "fears": 1.5,
    "shortage": 1.5,
    "strike": 1.0,
    "selloff": 2.0,
    "sell-off": 2.0,
    "downside": 1.5,
    "downturn": 1.5,
    "slowdown": 1.5,
    "deficit": 1.0,
    "loss-making": 1.5,
    "writedown": 1.5,
}

_NEGATORS = {
    "no",
    "not",
    "never",
    "none",
    "cannot",
    "cant",
    "can't",
    "without",
    "fails",
    "fail",
    "failed",
    "unlikely",
    "hardly",
    "barely",
    "denies",
    "denied",
}

_INTENSIFIERS = {
    "very": 1.5,
    "sharply": 1.6,
    "surprisingly": 1.4,
    "significantly": 1.5,
    "massively": 1.8,
    "record": 1.4,
    "extremely": 1.8,
    "hugely": 1.6,
    "steeply": 1.5,
}

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z'\-]+")

# Words that flip polarity when they directly precede a lexicon token.
_NEGATION_WINDOW = 3


@dataclass
class SentimentResult:
    """Aggregated sentiment across a batch of headlines/snippets."""

    label: str = NEUTRAL
    score: float = 0.0
    confidence: float = 0.0
    positive_hits: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)
    articles_scored: int = 0
    per_article_scores: list[float] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "positive_hits": self.positive_hits[:15],
            "negative_hits": self.negative_hits[:15],
            "articles_scored": self.articles_scored,
        }


def score_text(text: str) -> tuple[float, list[str], list[str]]:
    """Score a single piece of text.

    Returns ``(raw_polarity, positive_hits, negative_hits)`` where polarity is a
    signed sum of lexicon weights, adjusted for negation and intensifiers.
    """
    if not text:
        return 0.0, [], []

    tokens = _TOKEN_RE.findall(text.lower())
    total = 0.0
    positives: list[str] = []
    negatives: list[str] = []

    for index, token in enumerate(tokens):
        # Negative-lexicon matches contribute a *negative* base polarity.
        weight = _POSITIVE.get(token, 0.0)
        bucket = positives
        polarity = 1.0
        if weight == 0.0:
            weight = _NEGATIVE.get(token, 0.0)
            bucket = negatives
            polarity = -1.0
        if weight == 0.0:
            continue

        backward = tokens[max(0, index - _NEGATION_WINDOW) : index]
        # Intensifiers can precede ("sharply lower") or follow ("fell sharply").
        forward = tokens[index + 1 : index + 2]

        if any(word in _NEGATORS for word in backward):
            polarity = -polarity
        for word in (*backward, *forward):
            if word in _INTENSIFIERS:
                weight *= _INTENSIFIERS[word]

        total += weight * polarity
        bucket.append(token)

    return total, positives, negatives


def aggregate_sentiment(texts: Sequence[str] | Iterable[str]) -> SentimentResult:
    """Aggregate polarity across headlines into a labelled signal.

    The score is normalised into ``[-1, 1]`` using a saturating transform so a
    single very emotive headline cannot dominate a 10-article sample.
    """
    result = SentimentResult()
    items = [t for t in texts if t and t.strip()]
    if not items:
        return result

    raw_scores: list[float] = []
    for text in items:
        raw, pos, neg = score_text(text)
        raw_scores.append(raw)
        result.positive_hits.extend(pos)
        result.negative_hits.extend(neg)

    result.articles_scored = len(items)
    result.per_article_scores = [round(s, 4) for s in raw_scores]

    mean_raw = sum(raw_scores) / len(raw_scores)
    # Saturating normalisation: mean of +4 or better pins to +/-1.
    normalised = max(-1.0, min(1.0, mean_raw / 4.0))
    result.score = normalised

    # Confidence blends agreement (share of articles sharing the mean's sign)
    # with signal strength.
    directional = [s for s in raw_scores if s != 0]
    if directional:
        sign = 1 if mean_raw >= 0 else -1
        agreement = sum(1 for s in directional if s * sign > 0) / len(raw_scores)
    else:
        agreement = 0.0
    result.confidence = round(min(1.0, agreement * 0.7 + abs(normalised) * 0.3), 4)

    if normalised > 0.15:
        result.label = BULLISH
    elif normalised < -0.15:
        result.label = BEARISH
    else:
        result.label = NEUTRAL

    return result
