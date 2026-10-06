"""Validation for the market identifiers a language model supplies.

Two things an LLM passes into this codebase reach external systems verbatim: a
**ticker**, which is interpolated into outbound request URLs and written to log
lines, and a **history window**, which is handed to yfinance. Both are validated
here, once, at the boundary.

Fixing the input rather than escaping every downstream use is deliberate: a
sanitiser applied at each sink is a rule someone has to remember, whereas a value
that is rejected on entry cannot reach any sink at all.

Tickers — without this, CodeQL correctly flagged two things:

* ``py/partial-ssrf`` — the raw string reached ``requests.get`` via the RSS URL
  builders. The hosts are hardcoded, so the impact was limited to the query
  string, but nothing stopped a crafted value from being passed through.
* ``py/log-injection`` — the same raw string was logged, so a value containing
  newlines could forge additional log entries.

Accepted ticker form: 1-15 characters of ``A-Z a-z 0-9 . - ^ =``. That covers
equities (``AAPL``, ``BRK.B``), indices (``^GSPC``), crypto pairs (``BTC-USD``)
and Yahoo's ``=X`` / ``=F`` suffixes, while excluding whitespace, path
separators, control characters and scheme syntax.

Windows — yfinance accepts exactly ``1d 5d 1mo 3mo 6mo 1y 2y 5y 10y ytd max``.
A model asked for six months writes ``6m``, which yfinance rejects outright with
``Period '6m' is invalid``, so the tool returned nothing and both debating agents
silently lost their price evidence. The aliases below map what a model naturally
writes onto what the API accepts.
"""

from __future__ import annotations

import re

__all__ = [
    "DEFAULT_PERIOD",
    "TICKER_PATTERN",
    "VALID_PERIODS",
    "InvalidTicker",
    "is_valid_ticker",
    "normalise_period",
    "normalise_ticker",
]

#: Anchored, so a partial match cannot smuggle a suffix such as "\nInjected".
TICKER_PATTERN = re.compile(r"\A[A-Za-z0-9.\-^=]{1,15}\Z")

MAX_TICKER_LENGTH = 15


class InvalidTicker(ValueError):
    """Raised when a string cannot be a ticker."""


def is_valid_ticker(raw: object) -> bool:
    """True when ``raw`` is a well-formed ticker."""
    return isinstance(raw, str) and TICKER_PATTERN.match(raw.strip()) is not None


def normalise_ticker(raw: object) -> str:
    """Return the canonical upper-case ticker, or raise :class:`InvalidTicker`.

    Raises rather than returning ``None`` or an empty string so a caller cannot
    silently continue with a value that failed validation.
    """
    if not isinstance(raw, str):
        raise InvalidTicker(f"ticker must be a string, got {type(raw).__name__}")

    candidate = raw.strip().upper()
    if not candidate:
        raise InvalidTicker("ticker must not be empty")
    if len(candidate) > MAX_TICKER_LENGTH:
        raise InvalidTicker(f"ticker exceeds {MAX_TICKER_LENGTH} characters")
    if not TICKER_PATTERN.match(candidate):
        raise InvalidTicker("ticker contains characters that are not permitted")

    return candidate


# ---------------------------------------------------------------------------
# History windows
# ---------------------------------------------------------------------------
#: Exactly what yfinance accepts. Anything else raises inside yfinance.
VALID_PERIODS = frozenset({"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"})

#: Used when a window cannot be understood. One year is the tool's own default
#: and the shortest span for which a 200-day average is computable.
DEFAULT_PERIOD = "1y"

#: What a model writes -> what yfinance accepts. Only windows yfinance actually
#: offers are targets: a request for 2 months rounds up to 3mo rather than
#: falling all the way back to a year, which would quietly change the evidence
#: the agents reason from.
_PERIOD_ALIASES = {
    "1w": "5d",
    "1wk": "5d",
    "1week": "5d",
    "1m": "1mo",
    "2m": "3mo",
    "3m": "3mo",
    "4m": "3mo",
    "6m": "6mo",
    "9m": "1y",
    "12m": "1y",
    "1yr": "1y",
    "2yr": "2y",
    "5yr": "5y",
    "10yr": "10y",
    "year": "1y",
    "month": "1mo",
    "day": "1d",
    "all": "max",
}


def normalise_period(raw: object, *, default: str = DEFAULT_PERIOD) -> str:
    """Return a window yfinance will accept, or ``default``.

    Never raises. A model that asks for an odd window should get sensible data
    back, not a failed tool call that silently removes price evidence from both
    sides of the debate - which is exactly what happened with ``"6m"``.
    """
    if not isinstance(raw, str):
        return default

    candidate = raw.strip().lower().replace(" ", "")
    if not candidate:
        return default
    if candidate in VALID_PERIODS:
        return candidate
    if candidate in _PERIOD_ALIASES:
        return _PERIOD_ALIASES[candidate]
    return default
