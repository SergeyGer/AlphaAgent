"""Authentication that records when a token was last actually used.

Why this exists
---------------
DRF's authtoken ``Token`` model has exactly three fields: ``key``, ``user`` and
``created``. There is no ``last_used``, no ``AbstractToken`` to subclass and no
swappable-model setting (verified against DRF 3.18.1).

That mattered because ``purge_expired_auth_tokens_task`` documented itself as
deleting "tokens that have not been **used** for N days" while filtering on
``created``. It therefore deleted the token of a client authenticating every
minute, forcing a monthly re-authentication for everyone, and did nothing at all
about genuinely abandoned tokens younger than the cutoff.

Adding a column to a third-party model is not possible, so activity is recorded
on the user instead - which is what Django's own ``update_last_login`` signal
does for session logins, and is the standard notion of "last seen".

Write cost
----------
Stamping on every request would add a write to the hot path of every API call.
Activity is therefore only persisted when the recorded time is already stale
(``ACTIVITY_RESOLUTION``), so a busy client causes at most one write per hour
rather than one per request.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import TokenAuthentication

logger = logging.getLogger("alphaagent.auth")

__all__ = ["ACTIVITY_RESOLUTION", "ActivityTokenAuthentication", "touch_user_activity"]

#: Activity is only re-stamped when the recorded value is older than this.
#: Bounds the write cost: at most one UPDATE per user per hour, regardless of
#: request volume.
ACTIVITY_RESOLUTION = timedelta(hours=1)


def touch_user_activity(user) -> bool:
    """Record that ``user`` was active now. Returns True if a write happened.

    Never raises: an authentication path must not fail because a bookkeeping
    write failed. A missed stamp only delays a purge, which is the safe
    direction.
    """
    try:
        now = timezone.now()
        last = getattr(user, "last_login", None)
        if last is not None and (now - last) < ACTIVITY_RESOLUTION:
            return False
        user.last_login = now
        user.save(update_fields=["last_login"])
        return True
    except Exception as exc:
        logger.warning("Could not record activity for user %s: %s", getattr(user, "pk", "?"), exc)
        return False


class ActivityTokenAuthentication(TokenAuthentication):
    """``TokenAuthentication`` that also records last use.

    A drop-in replacement: authentication semantics are unchanged, and a failure
    to record activity never rejects an otherwise valid request.
    """

    def authenticate_credentials(self, key):
        result = super().authenticate_credentials(key)
        if result is not None:
            user, _token = result
            touch_user_activity(user)
        return result
