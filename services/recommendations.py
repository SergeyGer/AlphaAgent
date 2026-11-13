"""Human-in-the-loop trade approvals.

Flow
----
1. The AI proposes a trade. In **autonomous** mode it executes immediately
   (unchanged); in **advisory** mode it becomes a ``PENDING`` recommendation.
2. A human approves or rejects it from the dashboard or Telegram.
3. Approval re-validates the trade against **current** prices and limits using
   the same guard, then hands that exact decision to
   :func:`services.execution.execute_trade`, which re-runs it under the portfolio
   lock before writing the ledger.

Step 3 is the important one: approving is not a bypass. A recommendation that
was valid an hour ago may be stale, over-budget or past the stop-loss by the
time someone taps the button, and it will be blocked accordingly. A material
change between the approval evaluation and the lock is a new decision and
requires a new approval.
"""

from __future__ import annotations

import hmac
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from ai_agent import TradeProposal
from core.decision_json import content_hash, json_safe, utc_iso
from core.decision_models import DecisionExecutionState
from core.models import (
    AgentDecisionLog,
    DecidedVia,
    DecisionRun,
    MarketSentiment,
    Portfolio,
    RecommendationApproval,
    RecommendationStatus,
    TradeRecommendation,
)
from services.events import emit_decision, emit_recommendation, emit_trade
from services.execution import (
    ExecutionRejected,
    ExecutionRequest,
    GuardDecision,
    MaterialChangeError,
    ValuationUnavailableError,
    evaluate_proposal,
    execute_trade,
    normalise_marks,
    portfolio_policy_version,
)
from services.execution_audit import (
    ExecutionAudit,
    record_fill_outcome,
    record_terminal_outcome,
)
from services.ledger import PositionState, replay_positions
from services.market_data import get_latest_quote
from services.portfolio_brief import portfolio_state_version
from services.portfolio_metrics import build_exposure
from services.quote_policy import ExecutionFreshnessPolicy, validate_mark
from services.recommendation_terms import terms_reference, terms_reference_from_approval

logger = logging.getLogger("alphaagent.recommendations")

__all__ = [
    "TERMS_REFUSAL_CODES",
    "ApprovalOutcome",
    "approve_recommendation",
    "create_recommendation",
    "expire_stale_recommendations",
    "find_pending_recommendation",
    "recommendation_terms_reference",
    "recommendation_ttl",
    "reject_recommendation",
]

#: Stable refusal codes. A stale screen must fail with a code an operator can
#: act on, not a generic "already decided" that hides which terms were refused.
TERMS_VERSION_REQUIRED = "approval_terms_version_required"
TERMS_VERSION_INVALID = "approval_terms_version_invalid"
TERMS_VERSION_STALE = "approval_terms_version_stale"
TERMS_REFERENCE_STALE = "approval_terms_reference_stale"
IDENTITY_MISMATCH = "approval_identity_mismatch"
IDEMPOTENCY_KEY_INVALID = "approval_idempotency_key_invalid"
NOT_OWNED = "approval_recommendation_not_owned"

TERMS_REFUSAL_CODES = frozenset(
    {
        TERMS_VERSION_REQUIRED,
        TERMS_VERSION_INVALID,
        TERMS_VERSION_STALE,
        TERMS_REFERENCE_STALE,
        IDENTITY_MISMATCH,
        IDEMPOTENCY_KEY_INVALID,
        NOT_OWNED,
    }
)
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_MISSING = object()
_INVALID = object()


def recommendation_terms_reference(recommendation: TradeRecommendation) -> str:
    """The reference the API exposes for the terms currently on the row."""
    return terms_reference(recommendation)


def _coerce_terms_version(value: Any) -> int | object | None:
    if value is None or value == "":
        return _MISSING
    if isinstance(value, bool):
        return _INVALID
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return _INVALID
    return parsed if parsed >= 1 else _INVALID


#: Telegram callback data is capped at 64 bytes, so the bot carries the last
#: ten characters of the reference. That suffix is still a witness: it is not a
#: secret, but a copied reference from another recommendation will not end with
#: it. The service never accepts a suffix shorter than this.
MIN_REFERENCE_SUFFIX = 8


def _reference_matches(submitted: str, expected: str) -> bool:
    """Whether ``submitted`` names ``expected`` (full value or bot suffix)."""
    submitted_bytes = submitted.encode("utf-8")
    expected_bytes = expected.encode("utf-8")
    if hmac.compare_digest(submitted_bytes, expected_bytes):
        return True
    return MIN_REFERENCE_SUFFIX <= len(submitted_bytes) < len(
        expected_bytes
    ) and hmac.compare_digest(submitted_bytes, expected_bytes[-len(submitted_bytes) :])


def recommendation_ttl() -> timedelta:
    from django.conf import settings

    minutes = int(getattr(settings, "RECOMMENDATION_TTL_MINUTES", 60) or 60)
    return timedelta(minutes=minutes)


@dataclass
class ApprovalOutcome:
    """Result of an approve/reject action."""

    recommendation: TradeRecommendation
    executed: bool
    message: str
    transaction_id: int | None = None
    guard_reason: str = ""
    reason_code: str = ""
    requires_new_approval: bool = False
    revised_terms: dict | None = None
    run_id: str | None = None
    execution_attempt_id: str | None = None
    #: The exact reference the reviewer submitted, or the one that was displayed
    #: when the approval was recorded. Kept on the outcome so audit and API
    #: consumers never have to recompute it from mutated row state.
    displayed_terms_ref: str = ""
    #: True when this call returned the original execution for a completed
    #: approval instead of writing a second fill.
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.recommendation.id,
            "status": self.recommendation.status,
            "executed": self.executed,
            "transaction_id": self.transaction_id,
            "message": self.message,
            "guard_reason": self.guard_reason,
            "reason_code": self.reason_code or None,
            "requires_new_approval": self.requires_new_approval,
            "revised_terms": self.revised_terms,
            "run_id": self.run_id,
            "execution_attempt_id": self.execution_attempt_id,
            "terms_version": self.recommendation.terms_version,
            "terms_ref": self.displayed_terms_ref
            or recommendation_terms_reference(self.recommendation),
            "reference_price": str(self.recommendation.price),
            "quote_observed_at": utc_iso(self.recommendation.quote_observed_at),
            "replayed": self.replayed,
        }


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def create_recommendation(
    portfolio: Portfolio,
    proposal: TradeProposal,
    price: Decimal,
    *,
    ticker: str,
    decision_log: AgentDecisionLog | None = None,
    approved_amount: Decimal | None = None,
    decision_run: DecisionRun | None = None,
    quote_observed_at=None,
    quote_retrieved_at=None,
    price_source: str = "",
    price_currency: str = "USD",
    price_tolerance_pct: Decimal | None = None,
) -> TradeRecommendation | None:
    """Persist an advisory proposal for human review.

    Returns ``None`` for HOLD (nothing to approve) or when a recommendation for
    the same ticker is already pending, so a chatty sweep cannot spam the user
    with duplicates.
    """
    if proposal.action == "HOLD":
        return None

    # ``approved_amount`` is the quantity the guard actually permitted, not the
    # raw proposal. Storing the raw number would ask the operator to approve an
    # order the guard had already refused at that size.
    amount = Decimal(str(approved_amount if approved_amount is not None else proposal.amount))
    ticker = (ticker or "").upper()
    if amount <= 0 or price <= 0 or not ticker:
        return None

    if TradeRecommendation.objects.filter(
        portfolio=portfolio, ticker=ticker, status=RecommendationStatus.PENDING
    ).exists():
        logger.info("Suppressing duplicate pending recommendation for %s", ticker)
        return None

    recommendation = TradeRecommendation.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        action=proposal.action,
        amount=amount,
        price=price,
        notional_usd=(amount * price).quantize(Decimal("0.01")),
        sentiment=proposal.sentiment,
        reasoning=proposal.reasoning,
        status=RecommendationStatus.PENDING,
        expires_at=timezone.now() + recommendation_ttl(),
        decision_log=decision_log,
        decision_run=decision_run,
        quote_observed_at=quote_observed_at,
        quote_retrieved_at=quote_retrieved_at,
        price_source=price_source,
        price_currency=price_currency or "USD",
        price_tolerance_pct=price_tolerance_pct,
        terms_version=1,
    )
    emit_recommendation(portfolio.user_id, recommendation, created=True)
    logger.info(
        "Created recommendation %s: %s %s %s",
        recommendation.id,
        recommendation.action,
        amount,
        recommendation.ticker,
    )
    return recommendation


def find_pending_recommendation(portfolio: Portfolio, ticker: str) -> TradeRecommendation | None:
    """The actionable recommendation a suppressed duplicate should link to."""
    return (
        TradeRecommendation.objects.filter(
            portfolio=portfolio,
            ticker=(ticker or "").upper(),
            status=RecommendationStatus.PENDING,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _terms_hash(terms: dict) -> str:
    return f"reco-terms-{content_hash(json_safe(terms))[:24]}"


def _recommendation_terms(recommendation: TradeRecommendation) -> dict:
    return {
        "schema": "recommendation_approval_terms.v1",
        "recommendation_id": recommendation.id,
        "recommendation_version": int(recommendation.terms_version or 1),
        "ticker": recommendation.ticker,
        "side": recommendation.action,
        "quantity": str(recommendation.amount),
        "reference_price": str(recommendation.price),
        "notional_usd": str(recommendation.notional_usd),
        "currency": recommendation.price_currency or "USD",
        "quote_observed_at": utc_iso(recommendation.quote_observed_at),
        "quote_retrieved_at": utc_iso(recommendation.quote_retrieved_at),
        "quote_source": recommendation.price_source or None,
        "expires_at": utc_iso(recommendation.expires_at),
    }


def _record_approval_snapshot(
    recommendation: TradeRecommendation,
    *,
    tolerance: Decimal,
    policy_version: str,
    portfolio_version: str,
    via: str,
    legacy_unknown: bool,
    quote: Any = None,
    idempotency_key: str = "",
) -> RecommendationApproval:
    terms = _recommendation_terms(recommendation)
    displayed_ref = terms_reference(recommendation, tolerance_pct=tolerance)
    return RecommendationApproval.objects.create(
        recommendation=recommendation,
        recommendation_version=int(recommendation.terms_version or 1),
        side=recommendation.action,
        quantity=recommendation.amount,
        reference_price=recommendation.price,
        tolerance_pct=tolerance,
        expires_at=recommendation.expires_at,
        quote_observed_at=recommendation.quote_observed_at,
        quote_retrieved_at=recommendation.quote_retrieved_at,
        quote_source=recommendation.price_source or "",
        policy_version=policy_version,
        portfolio_version=portfolio_version,
        terms_hash=_terms_hash(terms),
        terms_ref=displayed_ref,
        idempotency_key=idempotency_key,
        evaluated_price=getattr(quote, "price", None),
        evaluated_quote_observed_at=getattr(quote, "as_of", None),
        evaluated_quote_retrieved_at=getattr(quote, "retrieved_at", None),
        evaluated_quote_source=str(getattr(quote, "source", "") or ""),
        decided_at=timezone.now(),
        accepted=False,
        legacy_unknown_provenance=legacy_unknown,
        reason_code="pending_evaluation",
        reason=f"Approval requested via {via or 'unknown'}; terms snapshotted before the check.",
    )


def _refresh_recommendation_terms(
    recommendation: TradeRecommendation,
    *,
    price: Decimal,
    quote,
    tolerance: Decimal,
    reason_code: str,
    reason: str,
) -> dict:
    """Show the refreshed terms and require a new approval for them.

    The version is incremented only when a displayed term actually changed. A
    retry that observes the same price must not manufacture a new version: the
    version is the identity the reviewer approved, not an attempt counter.
    """
    new_price = price.quantize(Decimal("0.01"))
    new_notional = (recommendation.amount * new_price).quantize(Decimal("0.01"))
    new_observed = getattr(quote, "as_of", None)
    new_retrieved = getattr(quote, "retrieved_at", None)
    new_source = str(getattr(quote, "source", "") or "")
    new_currency = str(getattr(quote, "currency", "") or "USD")
    material = (
        new_price != recommendation.price
        or new_notional != recommendation.notional_usd
        or new_observed != recommendation.quote_observed_at
        or new_source != recommendation.price_source
        or new_currency != recommendation.price_currency
        or tolerance != recommendation.price_tolerance_pct
    )
    if not material:
        # Nothing the reviewer saw changed. Keep the version and the expiry
        # untouched; a no-change retry is not a new set of terms.
        return {
            **_recommendation_terms(recommendation),
            "terms_ref": terms_reference(recommendation, tolerance_pct=tolerance),
            "reason_code": reason_code,
            "message": reason,
            "requires_new_approval": True,
        }
    recommendation.price = new_price
    recommendation.notional_usd = new_notional
    recommendation.quote_observed_at = new_observed
    recommendation.quote_retrieved_at = new_retrieved
    recommendation.price_source = new_source
    recommendation.price_currency = new_currency
    recommendation.price_tolerance_pct = tolerance
    if material:
        recommendation.terms_version = int(recommendation.terms_version or 1) + 1
        recommendation.expires_at = timezone.now() + recommendation_ttl()
    recommendation.status = RecommendationStatus.PENDING
    recommendation.decided_at = None
    recommendation.decided_via = ""
    recommendation.save(
        update_fields=[
            "price",
            "notional_usd",
            "quote_observed_at",
            "quote_retrieved_at",
            "price_source",
            "price_currency",
            "price_tolerance_pct",
            "terms_version",
            "expires_at",
            "status",
            "decided_at",
            "decided_via",
        ]
    )
    revised = {
        **_recommendation_terms(recommendation),
        "terms_ref": terms_reference(recommendation, tolerance_pct=tolerance),
        "reason_code": reason_code,
        "message": reason,
        "requires_new_approval": True,
    }
    return revised


# ---------------------------------------------------------------------------
# Approval / rejection
# ---------------------------------------------------------------------------
@transaction.atomic
def approve_recommendation(
    recommendation_id: int,
    via: str = DecidedVia.WEB,
    *,
    terms_version: Any = None,
    terms_ref: str = "",
    portfolio_id: int | None = None,
    idempotency_key: str = "",
    recommendation_version: Any = None,
) -> ApprovalOutcome:
    """Approve and attempt execution through the authoritative lock.

    The caller must name the terms it displayed: either the opaque
    ``terms_ref`` returned with the recommendation or its integer
    ``terms_version``. Both are checked under the recommendation lock before any
    quote is read or any audit row is written. A missing, stale or foreign
    reference is refused with a stable code and no ledger write.

    The terms the reviewer saw are frozen first: side, quantity, reference
    price, tolerance, expiry, recommendation version and observation
    provenance. The current quote is then compared with that frozen reference. A
    move beyond the tolerance refreshes the displayed terms and requires a new
    approval; it never substitutes the new price for the one the human approved.

    A repeat of a completed approval reaches this function, presents the same
    terms and idempotency key, and is answered with the original fill and run.
    The fill and its run audit commit together; a failure after the fill but
    before the audit rolls the whole transaction back, and a retry repairs an
    already-committed fill whose attempt row is incomplete.
    """
    if terms_version is None and recommendation_version is not None:
        # The stored term is ``terms_version``; the approval payloads built from
        # the decision contract also call it the recommendation version. Accept
        # either name rather than silently ignoring the caller's identity.
        terms_version = recommendation_version
    recommendation = (
        TradeRecommendation.objects.select_for_update()
        .select_related("portfolio", "portfolio__user")
        .get(pk=recommendation_id)
    )
    portfolio = recommendation.portfolio

    # Ownership is re-checked under the same lock as the decision. The view and
    # the bot also scope their initial lookup, but a caller that reaches the
    # service directly must not be able to decide another portfolio's terms.
    if portfolio_id is not None and int(portfolio_id) != recommendation.portfolio_id:
        return ApprovalOutcome(
            recommendation,
            False,
            "Approval refused: the recommendation belongs to another portfolio.",
            reason_code=NOT_OWNED,
        )

    submitted_key = str(idempotency_key or "").strip()
    if submitted_key and not _IDEMPOTENCY_KEY_RE.match(submitted_key):
        return ApprovalOutcome(
            recommendation,
            False,
            "Approval refused: the idempotency key is not a stable action identity.",
            reason_code=IDEMPOTENCY_KEY_INVALID,
        )

    # A completed approval is replayed against the frozen approval row, not the
    # recommendation's post-execution values (the executed quantity and price
    # may have been clamped). For a pending recommendation the current row is
    # the displayed terms.
    accepted = (
        recommendation.approvals.filter(accepted=True).order_by("-id").first()
        if not recommendation.is_actionable
        else None
    )
    submitted_ref = str(terms_ref or "").strip()
    parsed_version = _coerce_terms_version(terms_version)
    if parsed_version is _INVALID:
        return ApprovalOutcome(
            recommendation,
            False,
            "Approval refused: the submitted terms version is not an integer.",
            reason_code=TERMS_VERSION_INVALID,
        )

    expected_ref = ""
    if accepted is not None:
        expected_ref = accepted.terms_ref or terms_reference_from_approval(accepted, recommendation)
    else:
        expected_ref = terms_reference(recommendation)
    if submitted_ref and len(submitted_ref) > 64:
        return ApprovalOutcome(
            recommendation,
            False,
            "Approval refused: the submitted terms reference is not a valid reference.",
            reason_code=TERMS_REFERENCE_STALE,
            requires_new_approval=True,
            displayed_terms_ref=expected_ref,
        )
    if submitted_ref:
        # A reference from another recommendation or portfolio carries that
        # row's id/portfolio in its hash, so recomputing here rejects it even
        # when the numeric version happens to be equal.
        if expected_ref and not _reference_matches(submitted_ref, expected_ref):
            return ApprovalOutcome(
                recommendation,
                False,
                "Approval refused: these terms are no longer the displayed terms.",
                reason_code=TERMS_REFERENCE_STALE,
                requires_new_approval=True,
                displayed_terms_ref=expected_ref,
            )
    elif parsed_version is _MISSING:
        return ApprovalOutcome(
            recommendation,
            False,
            "Approval refused: a terms version or terms reference is required.",
            reason_code=TERMS_VERSION_REQUIRED,
            requires_new_approval=True,
        )

    if parsed_version is not _MISSING:
        expected_version = (
            accepted.recommendation_version
            if accepted is not None
            else int(recommendation.terms_version or 1)
        )
        if parsed_version != expected_version:
            return ApprovalOutcome(
                recommendation,
                False,
                (
                    f"Approval refused: the submitted terms version "
                    f"v{parsed_version} is not current (v{expected_version})."
                ),
                reason_code=TERMS_VERSION_STALE,
                requires_new_approval=True,
                displayed_terms_ref=expected_ref,
            )

    displayed_version = (
        accepted.recommendation_version
        if accepted is not None
        else int(recommendation.terms_version or 1)
    )
    action_key = submitted_key or f"recommendation:{recommendation.id}:approve:v{displayed_version}"

    if not recommendation.is_actionable:
        if recommendation.status == RecommendationStatus.EXECUTED and recommendation.transaction_id:
            if (
                submitted_key
                and accepted is not None
                and accepted.idempotency_key
                and not hmac.compare_digest(submitted_key, accepted.idempotency_key)
            ):
                return ApprovalOutcome(
                    recommendation,
                    False,
                    "Approval refused: the idempotency key does not match the completed action.",
                    reason_code=IDENTITY_MISMATCH,
                    displayed_terms_ref=expected_ref or action_key,
                )
            # A retried approval must return the original fill and repair an
            # incomplete audit before it is reported as complete.
            from services.execution_audit import resume_execution_audit

            audit = ExecutionAudit(
                run=recommendation.decision_run,
                recommendation=recommendation,
                outcome=DecisionExecutionState.EXECUTED,
                reason="Recovered the audit for an existing recommendation fill.",
                via=via,
                user_id=portfolio.user_id,
                execution_key=f"recommendation:{recommendation.id}",
                approved_terms={
                    "terms_ref": expected_ref or submitted_ref,
                    "recommendation_version": displayed_version,
                    "idempotency_key": action_key,
                    "replayed": True,
                },
            )
            request = ExecutionRequest(
                portfolio_id=portfolio.id,
                ticker=recommendation.ticker,
                side=recommendation.action,
                quantity=recommendation.amount,
                quotes={},
                execution_key=f"recommendation:{recommendation.id}",
                source="advisory",
                approval_ref=f"recommendation:{recommendation.id}",
                approved_price=recommendation.price,
                approved_quantity=recommendation.amount,
            )
            attempt = resume_execution_audit(
                transaction_row=recommendation.transaction, request=request, audit=audit
            )
            run_id = recommendation.decision_run_id or getattr(attempt, "decision_run_id", None)
            return ApprovalOutcome(
                recommendation,
                True,
                "Already executed; returned the original fill.",
                transaction_id=recommendation.transaction_id,
                reason_code="already_executed",
                run_id=str(run_id) if run_id else None,
                execution_attempt_id=str(attempt.id) if attempt is not None else None,
                displayed_terms_ref=expected_ref or submitted_ref,
                replayed=True,
            )
        reason = (
            "Recommendation already decided."
            if not recommendation.is_pending
            else "Recommendation has expired."
        )
        if recommendation.is_pending and recommendation.expires_at:
            recommendation.status = RecommendationStatus.EXPIRED
            recommendation.decided_at = timezone.now()
            recommendation.decided_via = via
            recommendation.save(update_fields=["status", "decided_at", "decided_via"])
            _complete_advisory_run(
                recommendation,
                DecisionExecutionState.EXPIRED,
                reason="Recommendation expired before approval.",
                via=via,
            )
            transaction.on_commit(
                lambda reco=recommendation: emit_recommendation(
                    portfolio.user_id, reco, created=False
                )
            )
        return ApprovalOutcome(
            recommendation,
            False,
            reason,
            reason_code="expired"
            if recommendation.status == RecommendationStatus.EXPIRED
            else "already_decided",
            displayed_terms_ref=expected_ref,
        )

    policy = ExecutionFreshnessPolicy.from_settings()
    quote = get_latest_quote(recommendation.ticker)
    if quote is None:
        return _block_recommendation(
            recommendation,
            reason="No current quote is available; a market estimate cannot authorise a live fill.",
            code="missing_quote",
            via=via,
        )

    try:
        marks = normalise_marks({recommendation.ticker: quote})
    except ValuationUnavailableError as exc:
        return _block_recommendation(
            recommendation, reason=str(exc), code=getattr(exc, "code", "invalid_quote"), via=via
        )
    mark = marks[recommendation.ticker]
    freshness = validate_mark(
        ticker=recommendation.ticker,
        price=mark.price,
        as_of=mark.as_of,
        retrieved_at=mark.retrieved_at,
        source=mark.source,
        now=timezone.now(),
        mode="live",
        require_current=True,
        policy=policy,
    )
    if not freshness.ok:
        return _block_recommendation(
            recommendation,
            reason=freshness.detail,
            code=freshness.code,
            via=via,
        )

    price = mark.price
    tolerance = policy.drift_tolerance(recommendation.price_tolerance_pct)
    policy_version = portfolio_policy_version(portfolio)
    portfolio_version = portfolio_state_version(portfolio)
    legacy_unknown = recommendation.quote_observed_at is None
    approval_row = _record_approval_snapshot(
        recommendation,
        tolerance=tolerance,
        policy_version=policy_version,
        portfolio_version=portfolio_version,
        via=via,
        legacy_unknown=legacy_unknown,
        quote=quote,
        idempotency_key=action_key,
    )
    displayed_ref = approval_row.terms_ref

    # The reference is the price that was *shown*, not the refreshed quote.
    # Read it back from the frozen approval row so the drift and quantity checks
    # cannot accidentally compare the new market price with itself.
    reference_price = approval_row.reference_price
    reference_quantity = approval_row.quantity
    tolerance = approval_row.tolerance_pct
    drift = (abs(price - reference_price) / reference_price) * Decimal("100")
    if drift > tolerance:
        reason = (
            f"The quote moved {drift.quantize(Decimal('0.01'))}% from the approved "
            f"${reference_price} to ${price}; the tolerance is {tolerance}%. A new "
            "approval is required for the refreshed terms."
        )
        revised = _refresh_recommendation_terms(
            recommendation,
            price=price,
            quote=quote,
            tolerance=tolerance,
            reason_code="price_drift_requires_approval",
            reason=reason,
        )
        approval_row.accepted = False
        approval_row.reason_code = "price_drift_requires_approval"
        approval_row.reason = reason
        approval_row.save(update_fields=["accepted", "reason_code", "reason"])
        return ApprovalOutcome(
            recommendation,
            False,
            reason,
            guard_reason=reason,
            reason_code="price_drift_requires_approval",
            requires_new_approval=True,
            revised_terms=revised,
            run_id=str(recommendation.decision_run_id) if recommendation.decision_run_id else None,
            displayed_terms_ref=displayed_ref,
        )

    positions = replay_positions(portfolio)
    position = positions.get(recommendation.ticker, PositionState(ticker=recommendation.ticker))
    realised_today = sum((s.realised_pnl_today for s in positions.values()), Decimal("0")).quantize(
        Decimal("0.01")
    )

    proposal = TradeProposal(
        action=recommendation.action,
        amount=float(reference_quantity),
        sentiment=recommendation.sentiment,
        reasoning=recommendation.reasoning or "Approved by operator.",
    )
    exposure_marks = _current_marks(portfolio, recommendation.ticker, quote)
    decision: GuardDecision = evaluate_proposal(
        portfolio,
        proposal,
        price,
        position,
        realised_today,
        exposure=build_exposure(portfolio, recommendation.ticker, price_map=exposure_marks),
    )
    decision.policy_version = policy_version
    decision.evaluated_portfolio_version = portfolio_version
    decision.quote_time = mark.as_of
    decision.quote_source = mark.source
    decision.quote_retrieved_at = mark.retrieved_at
    decision.approved_price = reference_price
    decision.approved_quantity = reference_quantity
    decision.price_tolerance_pct = tolerance
    decision.quantity_tolerance = Decimal("0.00000001")
    decision.approval_ref = f"recommendation:{recommendation.id}"
    decision.binding_rule = ""

    recommendation.decided_at = timezone.now()
    recommendation.decided_via = via

    if not decision.approved:
        approval_row.accepted = False
        approval_row.reason_code = "guard_rejected"
        approval_row.reason = decision.reason
        approval_row.save(update_fields=["accepted", "reason_code", "reason"])
        return _block_recommendation(
            recommendation,
            reason=decision.reason,
            code="guard_rejected",
            via=via,
            guard_decision=decision,
            displayed_terms_ref=displayed_ref,
        )

    request = ExecutionRequest(
        portfolio_id=portfolio.id,
        ticker=recommendation.ticker,
        side=recommendation.action,
        quantity=decision.amount,
        quotes=exposure_marks,
        requested_quantity=decision.requested_amount or decision.amount,
        execution_key=f"recommendation:{recommendation.id}",
        source="advisory",
        approval_ref=f"recommendation:{recommendation.id}",
        approved_policy_version=policy_version,
        approved_portfolio_version=portfolio_version,
        approved_price=reference_price,
        approved_quantity=reference_quantity,
        price_tolerance_pct=tolerance,
        quote_time=mark.as_of,
        quote_source=mark.source,
        quote_retrieved_at=mark.retrieved_at,
    )
    audit = ExecutionAudit(
        run=recommendation.decision_run,
        recommendation=recommendation,
        outcome=DecisionExecutionState.EXECUTED,
        reason=decision.reason,
        via=via,
        user_id=portfolio.user_id,
        execution_key=f"recommendation:{recommendation.id}",
        approved_terms=_terms_hash_payload(
            recommendation,
            tolerance,
            policy_version,
            portfolio_version,
            terms_ref=displayed_ref,
            idempotency_key=action_key,
        ),
    )
    try:
        execution = execute_trade(request, audit=audit)
    except MaterialChangeError as exc:
        reason = str(exc)
        revised = _refresh_recommendation_terms(
            recommendation,
            price=exc.revised_terms.get("reference_price", price)
            if isinstance(exc.revised_terms, dict)
            else price,
            quote=quote,
            tolerance=tolerance,
            reason_code=getattr(exc, "code", "material_change"),
            reason=reason,
        )
        approval_row.accepted = False
        approval_row.reason_code = getattr(exc, "code", "material_change")
        approval_row.reason = reason
        approval_row.save(update_fields=["accepted", "reason_code", "reason"])
        return ApprovalOutcome(
            recommendation,
            False,
            reason,
            guard_reason=reason,
            reason_code=getattr(exc, "code", "material_change"),
            requires_new_approval=True,
            revised_terms={**revised, **(exc.revised_terms or {})},
            run_id=str(recommendation.decision_run_id) if recommendation.decision_run_id else None,
            displayed_terms_ref=displayed_ref,
        )
    except ExecutionRejected as exc:
        approval_row.accepted = False
        approval_row.reason_code = getattr(exc, "code", "execution_rejected")
        approval_row.reason = str(exc)
        approval_row.save(update_fields=["accepted", "reason_code", "reason"])
        return _block_recommendation(
            recommendation,
            reason=str(exc),
            code=getattr(exc, "code", "execution_rejected"),
            via=via,
            guard_decision=getattr(exc, "decision", None) or decision,
            displayed_terms_ref=displayed_ref,
        )

    if not execution.approved or execution.transaction is None:
        approval_row.accepted = False
        approval_row.reason_code = "guard_rejected"
        approval_row.reason = execution.reason
        approval_row.save(update_fields=["accepted", "reason_code", "reason"])
        return _block_recommendation(
            recommendation,
            reason=execution.reason,
            code="guard_rejected",
            via=via,
            guard_decision=execution.decision or decision,
            displayed_terms_ref=displayed_ref,
        )

    transaction_row = execution.transaction
    executed_decision = execution.decision
    recommendation.status = RecommendationStatus.EXECUTED
    recommendation.transaction = transaction_row
    # Record what was actually executed - the guard may have clamped the size.
    recommendation.amount = executed_decision.amount
    recommendation.price = executed_decision.price or recommendation.price
    recommendation.notional_usd = executed_decision.notional
    recommendation.save(
        update_fields=[
            "status",
            "transaction",
            "amount",
            "price",
            "notional_usd",
            "decided_at",
            "decided_via",
        ]
    )
    approval_row.accepted = True
    approval_row.transaction = transaction_row
    approval_row.reason_code = "executed"
    approval_row.reason = executed_decision.reason
    approval_row.save(update_fields=["accepted", "transaction", "reason_code", "reason"])
    if recommendation.decision_run is not None:
        run = DecisionRun.objects.get(pk=recommendation.decision_run_id)
        if run.execution_state != DecisionExecutionState.EXECUTED:
            record_fill_outcome(
                run=run,
                transaction_row=transaction_row,
                decision=executed_decision,
                request=request,
                recommendation=recommendation,
                reason=executed_decision.reason,
                via=via,
                user_id=portfolio.user_id,
            )

    _log_human_decision(
        portfolio,
        recommendation,
        approved=True,
        executed=True,
        detail=f"Executed {executed_decision.amount.normalize()} {recommendation.ticker} "
        f"@ ${executed_decision.price} (${executed_decision.notional}).",
    )
    transaction.on_commit(lambda tx=transaction_row: emit_trade(portfolio.user_id, tx))
    transaction.on_commit(
        lambda reco=recommendation: emit_recommendation(portfolio.user_id, reco, created=False)
    )

    # A human-approved trade deserves the same Telegram notification as an
    # autonomous one. Dispatched by name to avoid importing tasks.py here
    # (tasks imports this module), and best-effort so a broker hiccup cannot
    # fail an approval that has already been committed.
    def _notify() -> None:
        try:
            from config.celery import app as celery_app

            celery_app.send_task("tasks.notify_trade_task", args=[transaction_row.id])
        except Exception as exc:  # pragma: no cover - notification is best effort
            logger.warning(
                "Could not enqueue trade notification for %s: %s", transaction_row.id, exc
            )

    transaction.on_commit(_notify)

    message = (
        f"Executed {recommendation.action} {executed_decision.amount.normalize()} "
        f"{recommendation.ticker} @ ${executed_decision.price} (attempt "
        f"{getattr(execution.execution_attempt, 'id', 'unrecorded')})."
    )
    logger.info("Recommendation %s executed: %s", recommendation.id, message)
    return ApprovalOutcome(
        recommendation,
        True,
        message,
        transaction_id=transaction_row.id,
        reason_code="executed",
        run_id=str(recommendation.decision_run_id) if recommendation.decision_run_id else None,
        execution_attempt_id=(
            str(execution.execution_attempt.id) if execution.execution_attempt is not None else None
        ),
        displayed_terms_ref=displayed_ref,
    )


def _terms_hash_payload(
    recommendation: TradeRecommendation,
    tolerance: Decimal,
    policy_version: str,
    portfolio_version: str,
    *,
    terms_ref: str = "",
    idempotency_key: str = "",
) -> dict:
    return {
        **_recommendation_terms(recommendation),
        "tolerance_pct": str(tolerance),
        "policy_version": policy_version,
        "portfolio_version": portfolio_version,
        "terms_ref": terms_ref or None,
        "idempotency_key": idempotency_key or None,
    }


def _block_recommendation(
    recommendation: TradeRecommendation,
    *,
    reason: str,
    code: str,
    via: str,
    guard_decision: GuardDecision | None = None,
    displayed_terms_ref: str = "",
) -> ApprovalOutcome:
    """Terminal blocked decision: update the recommendation and its run together."""
    recommendation.status = RecommendationStatus.BLOCKED
    recommendation.decided_at = timezone.now()
    recommendation.decided_via = via
    recommendation.save(update_fields=["status", "decided_at", "decided_via"])
    _log_human_decision(
        recommendation.portfolio,
        recommendation,
        approved=True,
        executed=False,
        detail=f"Approval accepted but execution was blocked: {reason}",
    )
    _complete_advisory_run(
        recommendation,
        DecisionExecutionState.BLOCKED,
        guard_decision=guard_decision,
        reason=reason,
        via=via,
    )
    transaction.on_commit(
        lambda reco=recommendation: emit_recommendation(reco.portfolio.user_id, reco, created=False)
    )
    logger.warning("Recommendation %s blocked: %s", recommendation.id, reason)
    return ApprovalOutcome(
        recommendation,
        False,
        f"Blocked by guardrail: {reason}",
        guard_reason=reason,
        reason_code=code,
        run_id=str(recommendation.decision_run_id) if recommendation.decision_run_id else None,
        displayed_terms_ref=displayed_terms_ref,
    )


@transaction.atomic
def reject_recommendation(
    recommendation_id: int,
    via: str = DecidedVia.WEB,
    *,
    terms_version: Any = None,
    terms_ref: str = "",
    portfolio_id: int | None = None,
    idempotency_key: str = "",
) -> ApprovalOutcome:
    recommendation = (
        TradeRecommendation.objects.select_for_update()
        .select_related("portfolio", "portfolio__user")
        .get(pk=recommendation_id)
    )
    if portfolio_id is not None and int(portfolio_id) != recommendation.portfolio_id:
        return ApprovalOutcome(
            recommendation,
            False,
            "Rejection refused: the recommendation belongs to another portfolio.",
            reason_code=NOT_OWNED,
        )

    if not recommendation.is_actionable:
        if recommendation.status == RecommendationStatus.REJECTED:
            from core.decision_models import ExecutionAttempt

            attempt = ExecutionAttempt.objects.filter(
                execution_key=f"recommendation:{recommendation.id}:rejected"
            ).first()
            return ApprovalOutcome(
                recommendation,
                False,
                "Recommendation already decided: already rejected; the original resolution is unchanged.",
                reason_code="rejected",
                run_id=str(recommendation.decision_run_id)
                if recommendation.decision_run_id
                else None,
                execution_attempt_id=str(attempt.id) if attempt is not None else None,
            )
        return ApprovalOutcome(
            recommendation, False, "Recommendation already decided.", reason_code="already_decided"
        )

    recommendation.status = RecommendationStatus.REJECTED
    recommendation.decided_at = timezone.now()
    recommendation.decided_via = via
    recommendation.save(update_fields=["status", "decided_at", "decided_via"])

    _log_human_decision(
        recommendation.portfolio,
        recommendation,
        approved=False,
        executed=False,
        detail="Operator declined the proposal.",
    )
    emit_recommendation(recommendation.portfolio.user_id, recommendation, created=False)
    _complete_advisory_run(
        recommendation,
        DecisionExecutionState.REJECTED,
        reason="Operator declined the proposal.",
        via=via,
    )
    logger.info("Recommendation %s rejected via %s", recommendation.id, via)
    return ApprovalOutcome(recommendation, False, "Recommendation rejected.")


def _current_marks(portfolio: Portfolio, ticker: str, primary_quote) -> dict:
    """Marks for every material holding, used for authoritative exposure."""
    marks: dict = {}
    if primary_quote is not None:
        marks[ticker.upper()] = primary_quote
    for asset in portfolio.assets.all():
        symbol = asset.ticker.upper()
        if symbol in marks:
            continue
        fallback = asset.avg_purchase_price if asset.avg_purchase_price > 0 else None
        try:
            other = get_latest_quote(symbol, fallback_price=fallback)
        except Exception as exc:  # pragma: no cover - accessor tolerates
            logger.warning("Could not price %s for exposure: %s", symbol, exc)
            other = None
        if other is not None and getattr(other, "source", "") != "fallback":
            marks[symbol] = other
    return marks


def _complete_advisory_run(
    recommendation: TradeRecommendation,
    outcome: str,
    *,
    guard_decision: GuardDecision | None = None,
    transaction_row=None,
    reason: str = "",
    via: str = "",
) -> None:
    """Connect an approval decision back to the run that proposed it.

    The terminal outcome is persisted under a stable identity, so a retried
    rejection/expiry returns the original resolution instead of appending a
    second one. A failure here propagates: the caller's transaction rolls back
    rather than committing a decision the run cannot explain.

    No run is a no-op rather than an error: recommendations created before the
    run link existed still work, and no synthetic history is manufactured for
    them.
    """
    run = recommendation.decision_run
    if run is None:
        return
    record_terminal_outcome(
        run=run,
        outcome=outcome,
        recommendation=recommendation,
        guard_decision=guard_decision,
        transaction_row=transaction_row,
        reason=reason,
        via=via,
        user_id=recommendation.portfolio.user_id,
    )


def _log_human_decision(
    portfolio: Portfolio,
    recommendation: TradeRecommendation,
    *,
    approved: bool,
    executed: bool,
    detail: str,
) -> None:
    """Record the human decision in the AI audit trail.

    Keeping operator actions in the same timeline as agent reasoning means the
    dashboard shows one continuous story: proposal -> decision -> outcome.
    """
    verdict = "APPROVED" if approved else "REJECTED"
    # Stable fields only. This replaces the request-layer log that interpolated
    # the caller's username: an id and two enum values cannot carry a newline or
    # forged record, and the audit row written beside it is the free-form record.
    status = (
        recommendation.status if recommendation.status in RecommendationStatus.values else "unknown"
    )
    via = (
        recommendation.decided_via if recommendation.decided_via in DecidedVia.values else "unknown"
    )
    logger.info(
        "Recommendation decision recorded id=%s status=%s via=%s",
        int(recommendation.pk),
        status,
        via,
    )
    try:
        log = AgentDecisionLog.objects.create(
            portfolio=portfolio,
            transaction=recommendation.transaction,
            reasoning=(
                f"Human decision ({recommendation.decided_via or 'unknown'}): {verdict}.\n\n"
                f"AI proposal: {recommendation.action} {recommendation.amount} "
                f"{recommendation.ticker} @ ${recommendation.price}.\n\n"
                f"Outcome: {detail}\n\n"
                f"Original AI reasoning:\n{recommendation.reasoning or '(none recorded)'}"
            ),
            action_taken=f"{verdict} by operator - {detail}"[:255],
            market_sentiment=recommendation.sentiment or MarketSentiment.NEUTRAL,
        )
        emit_decision(portfolio.user_id, log)
    except Exception as exc:
        logger.exception("Failed to write human-decision audit row: %s", exc)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
def expire_stale_recommendations() -> int:
    """Mark timed-out pending recommendations as EXPIRED."""
    stale = TradeRecommendation.objects.filter(
        status=RecommendationStatus.PENDING, expires_at__lt=timezone.now()
    ).select_related("portfolio")

    expired = 0
    for recommendation in stale:
        recommendation.status = RecommendationStatus.EXPIRED
        recommendation.decided_at = timezone.now()
        recommendation.decided_via = DecidedVia.AUTO
        recommendation.save(update_fields=["status", "decided_at", "decided_via"])
        emit_recommendation(recommendation.portfolio.user_id, recommendation, created=False)
        _complete_advisory_run(
            recommendation,
            DecisionExecutionState.EXPIRED,
            reason="Recommendation expired before a decision.",
            via=DecidedVia.AUTO,
        )
        expired += 1

    if expired:
        logger.info("Expired %s stale recommendation(s)", expired)
    return expired
