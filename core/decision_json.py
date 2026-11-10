"""JSON/Decimal helpers shared by the Phase 1 decision-run services and API.

Three small guarantees, in one place so the service and the exporter cannot
disagree about them:

* **Decimals become strings.** A float would silently round a price, a quantity
  or a weight, which is exactly what DATA-02 forbids. ``Decimal("0.1")`` is
  serialised as ``"0.1"`` and round-trips without loss.
* **Timestamps are UTC ISO-8601**, with ``Z`` on the wire, so a browser and a
  test read the same instant.
* **The canonical form is stable.** Hashes are computed over sorted keys and a
  fixed separator, so the same snapshot hashes the same in a fixture, a worker
  and a re-run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

__all__ = ["canonical_json", "content_hash", "json_safe", "sha256_hex", "utc_iso"]


def json_safe(value: Any) -> Any:
    """Recursively convert domain values into JSON-safe primitives.

    ``UUID``/``Decimal``/``datetime`` are common in this domain and none of them
    is natively JSON. ``date`` is handled separately from ``datetime`` because
    ``datetime`` is a subclass of ``date``.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Stable JSON text for hashing: sorted keys, no insignificant whitespace."""
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    """SHA-256 of the canonical form of ``value``."""
    return sha256_hex(canonical_json(value))


def utc_iso(value: datetime | None) -> str | None:
    """UTC ISO-8601 with a ``Z`` suffix, the format the rest of the API uses."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
