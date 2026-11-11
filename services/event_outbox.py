"""Durable event dispatch (FIX-06).

Persistence and notification are deliberately separated:

1. **The domain record is written first.** ``RunEvent``/``Transaction`` rows are
   the authority; a socket frame is only a hint that they changed.
2. **A queued notification is written in the same transaction as the domain
   record.** A crash after commit therefore cannot lose the fact that a
   notification is owed: the outbox row is already committed.
3. **Delivery is retried with backoff.** The channel layer is best-effort; a
   Redis outage moves the row to a later ``next_attempt_at`` instead of dropping
   the event.
4. **Consumers can catch up from the database.** ``catch_up`` returns exactly one
   ordered row per queued event for one user, whether or not a live frame ever
   arrived.

The outbox is per-user so a catch-up query cannot cross account boundaries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from core.decision_json import json_safe, utc_iso
from core.event_models import EventOutbox, OutboxStatus

logger = logging.getLogger("alphaagent.event_outbox")

__all__ = [
    "MAX_ATTEMPTS",
    "catch_up_events",
    "deliver_now",
    "enqueue_event",
    "flush_outbox",
    "outbox_metrics",
]

#: After this many failed deliveries the row is abandoned and its error is kept.
MAX_ATTEMPTS = 10
#: Exponential backoff: 2**attempt seconds, capped at five minutes.
MAX_BACKOFF_SECONDS = 300


def portfolio_group(user_id: int) -> str:
    """The channel-layer group for one user's stream."""
    return f"portfolio.{user_id}"


def _send(user_id: int, event_type: str, payload: dict[str, Any]) -> bool:
    """Best-effort channel-layer send. Returns whether a frame was handed over."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            logger.debug("No channel layer configured; leaving %s queued", event_type)
            return False
        async_to_sync(channel_layer.group_send)(
            portfolio_group(user_id),
            {
                "type": "portfolio_event",
                "event_type": event_type,
                "payload": json_safe(payload),
            },
        )
        return True
    except Exception as exc:
        logger.warning("Failed to deliver %s to user %s: %s", event_type, user_id, exc)
        return False


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(2 ** max(attempts, 0), MAX_BACKOFF_SECONDS))


def enqueue_event(
    user_id: int | None,
    stream: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str = "",
    available_at: datetime | None = None,
) -> EventOutbox | None:
    """Persist a notification that is owed, idempotently.

    Called inside the caller's transaction so the queue row and the domain record
    commit together. ``dedupe_key`` is the identity of the domain event: the
    second enqueue of the same event returns the first row.
    """
    if user_id is None:
        return None
    key = (dedupe_key or "").strip()[:160]
    if key:
        existing = EventOutbox.objects.filter(dedupe_key=key).first()
        if existing is not None:
            return existing
    try:
        # The savepoint matters: a lost race on the dedupe constraint must not
        # abort the caller's transaction. Without it the follow-up query raises
        # TransactionManagementError in PostgreSQL.
        with transaction.atomic():
            return EventOutbox.objects.create(
                user_id=user_id,
                stream=(stream or "")[:64],
                event_type=event_type[:64],
                payload=json_safe(payload or {}),
                dedupe_key=key,
                next_attempt_at=available_at or timezone.now(),
            )
    except IntegrityError:  # pragma: no cover - narrow concurrent enqueue race
        return EventOutbox.objects.filter(dedupe_key=key).first()


def deliver_now(outbox_id: int) -> bool:
    """Attempt one delivery. Never raises.

    The row is locked for the attempt so two workers that race on the same
    notification send one frame, not two. At-least-once would be acceptable for
    an idempotent consumer, but one frame is cheaper to reason about.
    """
    try:
        with transaction.atomic():
            row = EventOutbox.objects.select_for_update().get(pk=outbox_id)
            if row.status == OutboxStatus.DELIVERED:
                return True
            ok = _send(row.user_id, row.event_type, dict(row.payload or {}))
            now = timezone.now()
            if ok:
                EventOutbox.objects.filter(pk=row.pk).update(
                    status=OutboxStatus.DELIVERED,
                    attempts=row.attempts + 1,
                    delivered_at=now,
                    last_error="",
                    updated_at=now,
                )
                return True
            attempts = row.attempts + 1
            status = OutboxStatus.ABANDONED if attempts >= MAX_ATTEMPTS else OutboxStatus.PENDING
            EventOutbox.objects.filter(pk=row.pk).update(
                status=status,
                attempts=attempts,
                next_attempt_at=now + _backoff(attempts),
                last_error="channel layer unavailable",
                updated_at=now,
            )
            return False
    except EventOutbox.DoesNotExist:  # pragma: no cover - deleted queue row
        return False
    except Exception as exc:  # pragma: no cover - delivery must never raise
        logger.warning("Outbox delivery for %s failed unexpectedly: %s", outbox_id, exc)
        return False


def flush_outbox(*, limit: int = 200, now: datetime | None = None) -> dict[str, int]:
    """Deliver due queued notifications. Safe to run concurrently.

    ``skip_locked`` means two workers flushing at once do not block one another
    or deliver the same row twice.
    """
    moment = now or timezone.now()
    delivered = 0
    failed = 0
    processed = 0
    ids = list(
        EventOutbox.objects.filter(status=OutboxStatus.PENDING, next_attempt_at__lte=moment)
        .order_by("id")
        .values_list("id", flat=True)[: max(int(limit), 0)]
    )
    for outbox_id in ids:
        processed += 1
        with transaction.atomic():
            row = (
                EventOutbox.objects.select_for_update(skip_locked=True)
                .filter(pk=outbox_id, status=OutboxStatus.PENDING)
                .first()
            )
            if row is None:
                continue
            ok = _send(row.user_id, row.event_type, dict(row.payload or {}))
            current = timezone.now()
            if ok:
                EventOutbox.objects.filter(pk=row.pk).update(
                    status=OutboxStatus.DELIVERED,
                    attempts=row.attempts + 1,
                    delivered_at=current,
                    last_error="",
                    updated_at=current,
                )
                delivered += 1
            else:
                attempts = row.attempts + 1
                EventOutbox.objects.filter(pk=row.pk).update(
                    status=(
                        OutboxStatus.ABANDONED if attempts >= MAX_ATTEMPTS else OutboxStatus.PENDING
                    ),
                    attempts=attempts,
                    next_attempt_at=current + _backoff(attempts),
                    last_error="channel layer unavailable",
                    updated_at=current,
                )
                failed += 1
    return {"processed": processed, "delivered": delivered, "failed": failed}


def outbox_metrics(*, user_id: int | None = None) -> dict[str, Any]:
    """Backlog, delivery lag and retry counts.

    Lag is the age of the oldest pending row: the worst-case delay a connected
    client would experience without a live frame. Loss is visible here as a
    non-zero ``abandoned`` count, never as silence.
    """
    now = timezone.now()
    rows = EventOutbox.objects.all()
    if user_id is not None:
        rows = rows.filter(user_id=user_id)
    pending = rows.filter(status=OutboxStatus.PENDING)
    oldest = pending.aggregate(oldest=Min("created_at"))["oldest"]
    counts = {
        str(row["status"]): row["count"]
        for row in rows.values("status").annotate(count=Count("id"))
    }
    recent = rows.filter(
        status=OutboxStatus.DELIVERED, delivered_at__gte=now - timedelta(hours=1)
    ).count()
    return {
        "pending": counts.get(OutboxStatus.PENDING, 0),
        "delivered_last_hour": recent,
        "abandoned": counts.get(OutboxStatus.ABANDONED, 0),
        "delivered_total": counts.get(OutboxStatus.DELIVERED, 0),
        "oldest_pending_age_seconds": (
            int(max((now - oldest).total_seconds(), 0)) if oldest is not None else 0
        ),
        "last_error": (pending.order_by("-id").values_list("last_error", flat=True).first() or ""),
        "max_attempts": rows.aggregate(max_attempts=Max("attempts"))["max_attempts"] or 0,
        "generated_at": utc_iso(now),
    }


def catch_up_events(
    user_id: int,
    *,
    after_id: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Reconstruct one user's ordered timeline from persisted queue rows.

    This deliberately returns pending rows as well as delivered ones. A client
    reconnecting after a Redis outage must be able to catch up from the database,
    not from whether a socket frame happened to arrive.
    """
    rows = EventOutbox.objects.filter(user_id=user_id, id__gt=max(int(after_id), 0)).order_by("id")[
        : max(min(int(limit), 500), 1)
    ]
    return [
        {
            "outbox_id": row.id,
            "event_id": str(row.event_id),
            "stream": row.stream,
            "event_type": row.event_type,
            "payload": row.payload,
            "delivery_status": row.status,
            "created_at": utc_iso(row.created_at),
            "delivered_at": utc_iso(row.delivered_at),
        }
        for row in rows
    ]
