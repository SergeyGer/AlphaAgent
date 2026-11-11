"""Durable event delivery (FIX-06).

The append-only domain records - ``RunEvent`` rows, the trade ledger, the
recommendation table - are the authority. A WebSocket frame is a *hint* that
those records changed. This table is the durable dispatch queue between the two:

* the row is written in the same transaction as the domain record, so a crash
  after commit cannot lose the fact that a notification is owed;
* delivery is attempted after commit and retried with exponential backoff;
* ``dedupe_key`` makes a re-published domain event the same outbox row, so a
  retried handler cannot create two notifications for one event;
* ``event_id`` is the consumer's de-duplication key.

This is deliberately a small outbox, not a second event log. The payload is the
notification payload; the run's event log remains the reconstructable timeline.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

__all__ = ["EventOutbox", "OutboxStatus"]


class OutboxStatus(models.TextChoices):
    PENDING = "pending", "Pending delivery"
    DELIVERED = "delivered", "Delivered"
    ABANDONED = "abandoned", "Abandoned after repeated failure"


class EventOutbox(models.Model):
    """One notification owed to one user, with retry metadata."""

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    #: Numeric user id rather than a FK: the outbox is an operational queue and a
    #: deleted account must not delete the evidence that a delivery was owed.
    user_id = models.BigIntegerField(db_index=True)
    stream = models.CharField(max_length=64, default="")
    event_type = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=OutboxStatus.choices, default=OutboxStatus.PENDING, db_index=True
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(db_index=True, default=timezone.now)
    last_error = models.TextField(blank=True, default="")
    #: Stable identity of the domain event. Empty means no deduplication claim.
    dedupe_key = models.CharField(max_length=160, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Event outbox row"
        verbose_name_plural = "Event outbox rows"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="outbox_status_due_idx"),
            models.Index(fields=["user_id", "id"], name="outbox_user_id_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=~Q(dedupe_key=""),
                name="one_outbox_row_per_dedupe_key",
            ),
            models.CheckConstraint(
                condition=Q(attempts__gte=0), name="outbox_attempts_non_negative"
            ),
        ]

    def __str__(self) -> str:
        return f"outbox {self.id} {self.event_type} [{self.status}]"
