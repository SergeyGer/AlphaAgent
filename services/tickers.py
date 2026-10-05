"""Ticker normalisation and validation.

A ticker is the one piece of user-supplied text that reaches the most dangerous
places in this codebase: it is interpolated into outbound request URLs and
written into log lines. Validating it once, at the boundary, is what keeps both
of those safe.

Without this, CodeQL correctly flagged two things:

* ``py/partial-ssrf`` — the raw string reached ``requests.get`` via the RSS URL
  builders. The hosts are hardcoded, so the impact was limited to the query
  string, but nothing stopped a crafted value from being passed through.
* ``py/log-injection`` — the same raw string was logged, so a value containing
  newlines could forge additional log entries.

Fixing the input rather than escaping every downstream use is deliberate: a
sanitiser applied at each sink is a rule someone has to remember, whereas an
invalid ticker that never enters the system cannot reach any sink at all.

Accepted form: 1-15 characters of ``A-Z a-z 0-9 . - ^ =``. That covers equities
(``AAPL``, ``BRK.B``), indices (``^GSPC``), crypto pairs (``BTC-USD``) and
Yahoo's ``=X`` / ``=F`` suffixes, while excluding whitespace, path separators,
control characters and scheme syntax.
"""

from __future__ import annotations

import re

__all__ = ["TICKER_PATTERN", "InvalidTicker", "is_valid_ticker", "normalise_ticker"]

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
