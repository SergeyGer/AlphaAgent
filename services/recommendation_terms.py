"""The exact recommendation terms a reviewer was shown.

An approval must name *what was displayed*, not only which recommendation row it
belongs to. The recommendation row is mutated in place when a price move
refreshes the terms, so ``id`` alone is ambiguous: a screen opened before the
refresh and a Telegram button sent before it would both resolve to the newer
terms. This module issues a server-side reference over every displayed field and
recomputes it from stored state, so an echoed reference can be checked under the
recommendation lock without trusting the client.

The reference is deliberately a content hash rather than a bare version number:
two recommendations can both be at version 1, and a version copied from another
portfolio must not authorise this one. It binds:

* the recommendation, its portfolio and owner;
* the version, side, quantity, reference price and notional;
* the applicable drift tolerance and the expiry;
* the observation provenance of the displayed price.

``RecommendationApproval.terms_ref`` stores the reference that was displayed at
the moment an approval was attempted. A replay checks the submitted reference
against that stored value, not against the recommendation's current mutable
fields, so a completed approval remains replayable after the executed quantity
and price are written back.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from core.decision_json import content_hash, json_safe, utc_iso

__all__ = [
    "REFERENCE_PREFIX",
    "REFERENCE_SCHEMA",
    "applicable_tolerance",
    "short_reference",
    "terms_payload",
    "terms_reference",
    "terms_reference_from_approval",
]

REFERENCE_SCHEMA = "recommendation_approval_reference.v1"
REFERENCE_PREFIX = "reco-ref-"
#: Callback data and response headers are small; a prefix is enough to make a
#: copied reference from another recommendation fail the comparison.
SHORT_REFERENCE_LENGTH = 10


def applicable_tolerance(recommendation) -> Decimal:
    """The drift tolerance in force for this recommendation's terms."""
    from services.quote_policy import ExecutionFreshnessPolicy

    return ExecutionFreshnessPolicy.from_settings().drift_tolerance(
        recommendation.price_tolerance_pct
    )


def _decimal_text(value: Any) -> str:
    """Canonical decimal text that survives a database reload.

    Django returns ``2.00000000`` for a field that was ``2.0`` in memory. A
    reference must not change just because the row was read back, so the
    numeric scale is normalised while the value is preserved.
    """
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (ArithmeticError, TypeError, ValueError):
        return str(value)


def terms_payload(recommendation, *, tolerance_pct: Decimal | None = None) -> dict[str, Any]:
    """Every field the reviewer sees, in canonical form.

    ``None`` is kept as ``None``: an unknown observation time must not become a
    timestamp inside the reference, because that would make an untimed mark look
    like a known one.
    """
    tolerance = applicable_tolerance(recommendation) if tolerance_pct is None else tolerance_pct
    portfolio = getattr(recommendation, "portfolio", None)
    return {
        "schema": REFERENCE_SCHEMA,
        "recommendation_id": recommendation.id,
        "portfolio_id": recommendation.portfolio_id,
        "user_id": getattr(portfolio, "user_id", None),
        "version": int(recommendation.terms_version or 1),
        "ticker": recommendation.ticker,
        "side": recommendation.action,
        "quantity": _decimal_text(recommendation.amount),
        "reference_price": _decimal_text(recommendation.price),
        "notional_usd": _decimal_text(recommendation.notional_usd),
        "currency": recommendation.price_currency or "USD",
        "quote_observed_at": utc_iso(recommendation.quote_observed_at),
        "quote_retrieved_at": utc_iso(recommendation.quote_retrieved_at),
        "quote_source": recommendation.price_source or None,
        "expires_at": utc_iso(recommendation.expires_at),
        "tolerance_pct": _decimal_text(tolerance),
    }


def _reference(payload: dict[str, Any]) -> str:
    return f"{REFERENCE_PREFIX}{content_hash(json_safe(payload))[:24]}"


def terms_reference(recommendation, *, tolerance_pct: Decimal | None = None) -> str:
    """The stable, server-issued reference for the terms currently displayed."""
    return _reference(terms_payload(recommendation, tolerance_pct=tolerance_pct))


def terms_reference_from_approval(approval, recommendation) -> str:
    """Reconstruct the reference for a legacy approval row.

    Rows written before ``terms_ref`` existed stored every displayed term as
    columns. Rebuilding the payload from those columns lets a replay validate a
    submitted reference instead of accepting any reference for a legacy fill.
    """
    portfolio = getattr(recommendation, "portfolio", None)
    notional = (approval.quantity * approval.reference_price).quantize(Decimal("0.01"))
    return _reference(
        {
            "schema": REFERENCE_SCHEMA,
            "recommendation_id": approval.recommendation_id,
            "portfolio_id": recommendation.portfolio_id,
            "user_id": getattr(portfolio, "user_id", None),
            "version": int(approval.recommendation_version or 1),
            "ticker": recommendation.ticker,
            "side": approval.side,
            "quantity": _decimal_text(approval.quantity),
            "reference_price": _decimal_text(approval.reference_price),
            "notional_usd": _decimal_text(notional),
            "currency": recommendation.price_currency or "USD",
            "quote_observed_at": utc_iso(approval.quote_observed_at),
            "quote_retrieved_at": utc_iso(approval.quote_retrieved_at),
            "quote_source": approval.quote_source or None,
            "expires_at": utc_iso(approval.expires_at),
            "tolerance_pct": _decimal_text(approval.tolerance_pct),
        }
    )


def short_reference(reference: str) -> str:
    """The non-secret suffix placed in Telegram callback data."""
    text = (reference or "").strip()
    if not text:
        return ""
    return text[-SHORT_REFERENCE_LENGTH:]
