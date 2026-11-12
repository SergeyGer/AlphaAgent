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

`canonical_export_json` is the cross-language form used for the export bundle
checksum. It is deliberately *not* the same function as `canonical_json`: the
latter is a Python-internal digest used for stored snapshots and reports, where
no browser ever recomputes it. The export is different - the viewer verifies the
same document in JavaScript - so it needs a form both runtimes can produce
byte-identically. `EXPORT_CANONICALISATION` below is that declared form, and
``docs/spec/fixtures/canonical-json.v1.json`` pins it with vectors exercised by
both `core/tests` and the viewer's Vitest suite.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

__all__ = [
    "EXPORT_CANONICALISATION",
    "SUPPORTED_EXPORT_VERSIONS",
    "CanonicalJSONError",
    "ExportVerificationError",
    "canonical_decimal_text",
    "canonical_export_json",
    "canonical_json",
    "canonical_number_text",
    "content_hash",
    "decimal_string",
    "export_content_hash",
    "export_json_safe",
    "json_safe",
    "sha256_hex",
    "utc_iso",
    "verify_export_bundle",
]

#: The declared canonical form of the export bundle checksum. The browser
#: implementation in ``frontend/viewer/bundle.ts`` names the same rules; the
#: shared vector fixture is what stops the two descriptions from drifting.
EXPORT_CANONICALISATION = (
    "JSON with keys sorted by UTF-16 code unit, compact separators, "
    "no insignificant whitespace, UTF-8; JSON numbers formatted like "
    "ECMAScript Number::toString (so 90.0 is 90); Decimal values as "
    "normalised fixed-point strings without an exponent or trailing "
    "fractional zeros; strings escaped as JSON.stringify escapes them"
)

#: Export formats this build can verify and render.
SUPPORTED_EXPORT_VERSIONS = frozenset({"1.0"})


class CanonicalJSONError(ValueError):
    """A value cannot be represented in the cross-language canonical form."""


class ExportVerificationError(ValueError):
    """An exported bundle is not one this build can import.

    ``code`` is a stable string the caller can report without echoing an
    exception: ``shape``, ``unsupported_kind``, ``unsupported_version`` or
    ``checksum``.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def decimal_string(value: Decimal) -> str:
    """Fixed-point decimal text, with zero always rendered as ``"0"``.

    ``str(Decimal("0E-8"))`` is ``"0E-8"``, which is a valid Decimal but a poor
    wire value for a weight and a poor thing to show a reviewer. Quantising zero
    to eight places should read as zero, not as an exponent.
    """
    if value == 0:
        return "0"
    return format(value, "f")


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


# ---------------------------------------------------------------------------
# Cross-language canonical form (export checksum)
# ---------------------------------------------------------------------------
#: JSON numbers keep full precision; beyond this range a JavaScript consumer
#: parses a different value from the one Python wrote, so it cannot be part of
#: a digest both runtimes must agree on.
_MAX_SAFE_INTEGER = 2**53 - 1


def canonical_number_text(value: float) -> str:
    """ECMAScript ``Number::toString`` for a finite float.

    Python's ``repr`` and ECMAScript both choose the shortest round-tripping
    decimal; they differ only in layout (``90.0`` vs ``90``, ``1e-07`` vs
    ``1e-7``). The layout below is the one ``JSON.stringify`` emits, which is
    what the browser recomputes.
    """
    if not math.isfinite(value):
        raise CanonicalJSONError("JSON numbers must be finite")
    if value == 0:
        return "0"

    sign = "-" if value < 0 else ""
    magnitude = -value if value < 0 else value
    digits_tuple = Decimal(repr(magnitude)).as_tuple()
    digits = list(digits_tuple.digits)
    exponent = digits_tuple.exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    text = "".join(str(digit) for digit in digits)
    count = len(digits)
    # ``point`` is the position of the decimal point: magnitude = 0.text * 10**point.
    point = count + exponent
    if count <= point <= 21:
        return f"{sign}{text}{'0' * (point - count)}"
    if 0 < point <= 21:
        return f"{sign}{text[:point]}.{text[point:]}"
    if -6 < point <= 0:
        return f"{sign}0.{'0' * (-point)}{text}"
    exponent_text = point - 1
    if count == 1:
        return f"{sign}{text}e{'+' if exponent_text >= 0 else '-'}{abs(exponent_text)}"
    return f"{sign}{text[0]}.{text[1:]}e{'+' if exponent_text >= 0 else '-'}{abs(exponent_text)}"


def canonical_decimal_text(value: Decimal) -> str:
    """The declared wire form of a Decimal: fixed-point, no exponent.

    Trailing fractional zeros are removed (``Decimal("90.0")`` becomes
    ``"90"``) because the same quantity must have one spelling in a digest.
    A value too large for a JavaScript number is not converted to a float; it
    stays a string, which is the only representation that round-trips.
    """
    if not value.is_finite():
        raise CanonicalJSONError("Decimal values must be finite")
    if value == 0:
        return "0"
    sign, digits, exponent = value.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        assert isinstance(exponent, int)
        exponent += 1
    text = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        body = f"{text}{'0' * exponent}"
    else:
        point = len(text) + exponent
        if point > 0:
            body = f"{text[:point]}.{text[point:]}"
        else:
            body = f"0.{'0' * (-point)}{text}"
    return f"-{body}" if sign else body


def _string_text(value: str) -> str:
    """A JSON string escaped the way ``JSON.stringify`` escapes it."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalJSONError("Strings must be valid UTF-8 for an export digest") from exc
    return json.dumps(value, ensure_ascii=False)


def _key_bytes(key: str) -> bytes:
    """UTF-16 code-unit ordering, which is what JavaScript's Array.sort uses."""
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise CanonicalJSONError("Object keys must be valid UTF-16 strings") from exc


def _append_canonical(value: Any, parts: list[str]) -> None:
    if value is None:
        parts.append("null")
        return
    if isinstance(value, bool):
        parts.append("true" if value else "false")
        return
    if isinstance(value, Decimal):
        parts.append(json.dumps(canonical_decimal_text(value), ensure_ascii=False))
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise CanonicalJSONError(
                "An integer outside the JavaScript safe range cannot be part of a "
                "cross-language digest"
            )
        parts.append(str(value))
        return
    if isinstance(value, float):
        parts.append(canonical_number_text(value))
        return
    if isinstance(value, str):
        parts.append(_string_text(value))
        return
    if isinstance(value, (list, tuple)):
        parts.append("[")
        for index, item in enumerate(value):
            if index:
                parts.append(",")
            _append_canonical(item, parts)
        parts.append("]")
        return
    if isinstance(value, dict):
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise CanonicalJSONError("Object keys must be strings")
        parts.append("{")
        for index, key in enumerate(sorted(keys, key=_key_bytes)):
            if index:
                parts.append(",")
            parts.append(_string_text(key))
            parts.append(":")
            _append_canonical(value[key], parts)
        parts.append("}")
        return
    raise CanonicalJSONError(f"Cannot canonicalise a {type(value).__name__}")


def canonical_export_json(value: Any) -> str:
    """The canonical text both Python and the viewer's JavaScript produce."""
    parts: list[str] = []
    _append_canonical(value, parts)
    return "".join(parts)


def export_json_safe(value: Any) -> Any:
    """Normalise domain values into the primitives the export is built from.

    The bundle is returned *and* hashed from this normalised form, so the
    digest describes exactly the payload that leaves the server rather than a
    pre-serialisation object the JSON renderer may encode differently. Decimal
    is the important case: DRF would turn it into a float, changing the text
    the viewer reads.
    """
    if isinstance(value, Decimal):
        return canonical_decimal_text(value)
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): export_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [export_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise CanonicalJSONError("JSON numbers must be finite")
    return value


def export_content_hash(value: Any) -> str:
    """SHA-256 of the cross-language canonical form of ``value``."""
    return sha256_hex(canonical_export_json(value))


def _checksum_value(bundle: dict) -> str:
    return export_content_hash({key: item for key, item in bundle.items() if key != "checksum"})


def verify_export_bundle(bundle: Any) -> dict:
    """Verify one ``alphaagent.decision-run`` document as it was stored.

    The digest is recomputed from the document handed in - the parsed file, not
    a freshly-built copy - so a semantic edit anywhere in the payload fails.
    An unsupported version is reported by the field name and the version, not as
    a generic refusal.
    """
    if not isinstance(bundle, dict):
        raise ExportVerificationError("shape", "The decision-run export is not a JSON object.")
    kind = bundle.get("bundle_kind")
    if kind != "alphaagent.decision-run":
        raise ExportVerificationError(
            "unsupported_kind",
            f'Unsupported bundle kind "{kind}"; expected "alphaagent.decision-run".',
        )
    version = bundle.get("bundle_version")
    if version not in SUPPORTED_EXPORT_VERSIONS:
        raise ExportVerificationError(
            "unsupported_version",
            f'Unsupported bundle_version "{version}". This build supports '
            f"{', '.join(sorted(SUPPORTED_EXPORT_VERSIONS))}.",
        )
    schema_version = bundle.get("schema_version", version)
    if schema_version not in SUPPORTED_EXPORT_VERSIONS:
        raise ExportVerificationError(
            "unsupported_version",
            f'Unsupported schema_version "{schema_version}". This build supports '
            f"{', '.join(sorted(SUPPORTED_EXPORT_VERSIONS))}.",
        )
    checksum = bundle.get("checksum")
    if not isinstance(checksum, dict):
        raise ExportVerificationError(
            "checksum", "The decision-run export has no checksum; refusing to import it."
        )
    algorithm = str(checksum.get("algorithm") or "sha256").strip().lower()
    if algorithm != "sha256":
        raise ExportVerificationError(
            "checksum",
            f'Unsupported checksum algorithm "{algorithm}"; only sha256 is accepted.',
        )
    declared = str(checksum.get("value") or "").strip().lower()
    if declared.startswith("sha256:"):
        declared = declared[len("sha256:") :]
    if not declared:
        raise ExportVerificationError("checksum", "The decision-run export has an empty checksum.")
    computed = _checksum_value(bundle)
    if declared != computed:
        raise ExportVerificationError(
            "checksum",
            "Checksum mismatch for the decision-run export - the payload no longer "
            "matches the recorded SHA-256. The file was modified or corrupted.",
        )
    return bundle
