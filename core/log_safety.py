"""Log-output sanitisation.

CodeQL's ``py/log-injection`` rule flags any user-supplied value written to a
log. The risk is log forging rather than memory corruption: a value containing
``\\n`` can fabricate a whole extra log entry, which is how an attacker hides an
action or plants a misleading one. In a system whose audit trail is a selling
point, a forgeable log is not a cosmetic problem.

``log_safe`` replaces every control character (including CR and LF) with ``?``
and truncates long values, so a single call site cannot produce more than one
line of output.

Prefer validating input at the boundary where a validator exists - see
``services/tickers.py`` for the pattern. This helper is for the cases where the
value is legitimately free-form (usernames, URL paths, exception text) and still
must not be able to break the line structure of a log.
"""

from __future__ import annotations

import re

__all__ = ["MAX_LOGGED_LENGTH", "log_safe"]

#: C0 and C1 control characters, DEL, and the Unicode line/paragraph separators.
#: The last two are not control characters in the C0/C1 sense but are treated as
#: line breaks by some log consumers, so they are forging vectors too.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

#: Anything longer is truncated; a log line is not a data dump.
MAX_LOGGED_LENGTH = 200


def log_safe(value: object) -> str:
    """Return ``value`` as a single-line, bounded string safe to log.

    Never raises: a logging helper that can throw would turn a diagnostic into
    an outage, so any failure to stringify falls back to a placeholder.
    """
    try:
        text = str(value)
    except Exception:
        return "<unprintable>"

    text = _CONTROL_CHARS.sub("?", text)
    if len(text) > MAX_LOGGED_LENGTH:
        text = text[:MAX_LOGGED_LENGTH] + "..."
    return text
