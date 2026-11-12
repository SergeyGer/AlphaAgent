"""Human approval, expiry and explicit execution for advisory plans (FIX-08).

An advisory plan is not a trade. It becomes executable only through two distinct
acts:

1. **Approve** — a person names the exact ``plan_version`` and the permission
   hash they reviewed, and supplies an expiry. The approval is stored twice: as
   mutable "current approval" fields on the plan (for fast checks) and as an
   immutable :class:`PlanApproval` row (for audit). Approving writes no trade.
2. **Apply** — an explicit request with a mode. ``apply_plan`` re-checks the
   approval under the plan's lock and then writes every leg through
   ``services.execution``. This module does not have its own ledger path.

A materially changed plan is re-derived by ``revalidate_plan``, which bumps
``plan_version`` and clears the current approval. The old :class:`PlanApproval`
row remains readable; it simply no longer matches. An expired approval is never
silently renewed: it must be approved again, and the plan is still subject to
the same version and permission checks.

Rejection and cancellation write audit rows and durable events. They are
different decisions (``rejected`` ends a proposed plan; ``cancelled`` withdraws
one) and are never collapsed into a mutable status column alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.allocation_models import (
    PlanApproval,
    PlanApprovalDecision,
    PlanStatus,
    PortfolioPlan,
)
from core.decision_json import content_hash, utc_iso
from core.models import InstrumentClassification
from services.events import EventType, emit_portfolio_plan
from services.execution import portfolio_policy_version
from services.portfolio_allocation import (
    ApprovalReference,
    PlanApplication,
    PlanNotApplicableError,
    apply_plan,
)
from services.portfolio_brief import portfolio_state_version

logger = logging.getLogger("alphaagent.portfolio_approval")

__all__ = [
    "PlanApprovalError",
    "PlanApprovalExpired",
    "PlanApprovalRequired",
    "apply_approved_plan",
    "approval_reference",
    "approval_state",
    "approve_plan",
    "cancel_plan",
    "reject_plan",
    "require_valid_approval",
]

MIN_APPROVAL_TTL_SECONDS = 60
DEFAULT_APPROVAL_TTL_SECONDS = 86_400
MAX_APPROVAL_TTL_SECONDS = 604_800
DEFAULT_PLAN_PRICE_DRIFT_PCT = Decimal("0")
DEFAULT_PLAN_QUANTITY_TOLERANCE = Decimal("0.00000001")


class PlanApprovalError(Exception):
    """A review or execution request is invalid for this plan right now."""

    code = "plan_approval_error"

    def __init__(self, detail: str, *, code: str | None = None) -> None:
        if code:
            self.code = code
        self.detail = detail
        super().__init__(detail)


class PlanApprovalRequired(PlanApprovalError):
    """No current, unexpired approval covers this exact plan version."""

    code = "approval_required"

    def __init__(self, detail: str, *, code: str = "approval_required") -> None:
        super().__init__(detail, code=code)


class PlanApprovalExpired(PlanApprovalRequired):
    code = "approval_expired"


def _config_int(key: str, default: int) -> int:
    try:
        return max(0, int(getattr(settings, "ALLOCATION_CONFIG", {}).get(key, default)))
    except (TypeError, ValueError):
        return default


def _max_ttl() -> int:
    return max(
        MIN_APPROVAL_TTL_SECONDS, _config_int("MAX_APPROVAL_TTL_SECONDS", MAX_APPROVAL_TTL_SECONDS)
    )


def _default_ttl() -> int:
    return min(
        _max_ttl(),
        max(
            MIN_APPROVAL_TTL_SECONDS,
            _config_int("APPROVAL_TTL_SECONDS", DEFAULT_APPROVAL_TTL_SECONDS),
        ),
    )


def _parse_expiry(value) -> datetime:
    if value is None or value == "":
        raise PlanApprovalError(
            "An approval must name an expiry time; an unbounded approval is not accepted.",
            code="missing_expiry",
        )
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise PlanApprovalError(
                "expires_at must be an ISO-8601 timestamp.", code="invalid_expiry"
            ) from None
    if timezone.is_naive(parsed):
        raise PlanApprovalError(
            "expires_at must include a timezone; a naive local time is ambiguous.",
            code="invalid_expiry",
        )
    return parsed


def _resolve_expiry(*, expires_at, ttl_seconds, now: datetime) -> datetime:
    if expires_at not in (None, ""):
        expiry = _parse_expiry(expires_at)
    else:
        try:
            ttl = int(ttl_seconds) if ttl_seconds not in (None, "") else _default_ttl()
        except (TypeError, ValueError):
            raise PlanApprovalError(
                "expires_in_seconds must be an integer.", code="invalid_expiry"
            ) from None
        if ttl < MIN_APPROVAL_TTL_SECONDS:
            raise PlanApprovalError(
                f"expires_in_seconds must be at least {MIN_APPROVAL_TTL_SECONDS}.",
                code="invalid_expiry",
            )
        if ttl > _max_ttl():
            raise PlanApprovalError(
                f"expires_in_seconds is more than the {_max_ttl()}-second ceiling.",
                code="expiry_too_far",
            )
        expiry = now + timedelta(seconds=ttl)
    if expiry <= now + timedelta(seconds=MIN_APPROVAL_TTL_SECONDS - 1):
        raise PlanApprovalError("expires_at must be in the future.", code="expiry_not_future")
    if expiry > now + timedelta(seconds=_max_ttl()):
        raise PlanApprovalError(
            f"expires_at is more than the {_max_ttl()}-second approval ceiling away.",
            code="expiry_too_far",
        )
    return expiry


def _latest_approval(plan: PortfolioPlan, decision: str | None = None) -> PlanApproval | None:
    queryset = plan.approvals.all()
    if decision is not None:
        queryset = queryset.filter(decision=decision)
    return queryset.order_by("-decided_at", "-id").first()


def _decimal_setting(key: str, default: Decimal) -> Decimal:
    try:
        value = Decimal(str(getattr(settings, "ALLOCATION_CONFIG", {}).get(key, default)))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return value if value >= 0 else default


def plan_price_tolerance_pct() -> Decimal:
    """The plan-level drift tolerance.

    It defaults to zero: a plan leg's quantity was sized at its reference price,
    so a changed price is a changed order unless an operator declares otherwise.
    A recommendation uses the separate, narrower execution tolerance.
    """
    return _decimal_setting("PLAN_PRICE_DRIFT_PCT", DEFAULT_PLAN_PRICE_DRIFT_PCT)


def plan_quantity_tolerance() -> Decimal:
    return _decimal_setting("PLAN_QUANTITY_TOLERANCE", DEFAULT_PLAN_QUANTITY_TOLERANCE)


def classification_version(portfolio, tickers) -> str:
    """Stable version of the explicit classifications that decide risk."""
    symbols = sorted({str(ticker).upper() for ticker in tickers})
    rows = (
        InstrumentClassification.objects.filter(portfolio=portfolio, ticker__in=symbols)
        .order_by("ticker")
        .values_list("ticker", "sector", "asset_class", "source")
    )
    payload = [
        [str(ticker).upper(), sector, asset_class, source]
        for ticker, sector, asset_class, source in rows
    ]
    return f"classifications-{content_hash(payload)[:24]}" if payload else ""


def plan_approval_terms(plan: PortfolioPlan) -> dict:
    """The exact side, quantity, reference price and observation provenance.

    The terms are built from the plan that was reviewed. A missing observation
    time is preserved as null with the source label; it is never replaced by the
    approval's own clock.
    """
    from services.portfolio_allocation import _material_tickers

    legs = list(plan.legs.order_by("sequence"))
    observations = plan.quote_observations if isinstance(plan.quote_observations, dict) else {}
    material = _material_tickers(plan, legs)
    approved_legs = []
    for leg in legs:
        observation = observations.get(leg.ticker.upper()) or {}
        approved_legs.append(
            {
                "sequence": int(leg.sequence),
                "ticker": leg.ticker.upper(),
                "side": leg.side,
                "quantity": str(leg.quantity),
                "reference_price": str(leg.price),
                "notional_usd": str(leg.notional_usd),
                "quote_observed_at": observation.get("as_of") or None,
                "quote_retrieved_at": observation.get("retrieved_at") or None,
                "quote_source": observation.get("source") or "",
                "quote_currency": observation.get("currency") or "USD",
            }
        )
    tolerance = plan_price_tolerance_pct()
    quantity_tolerance = plan_quantity_tolerance()
    max_age = int(
        getattr(
            settings,
            "EXECUTION_MAX_QUOTE_AGE_SECONDS",
            900,
        )
    )
    terms = {
        "schema": "plan_approval_terms.v1",
        "plan_id": str(plan.id),
        "plan_version": int(plan.plan_version),
        "approval_hash": plan.approval_hash,
        "execution_mode": plan.execution_mode,
        "legs": approved_legs,
        "price_tolerance_pct": str(tolerance),
        "quantity_tolerance": str(quantity_tolerance),
        "max_quote_age_seconds": max_age,
        "policy_version": portfolio_policy_version(plan.portfolio),
        "classification_version": classification_version(plan.portfolio, material),
        "allocation_policy_version": plan.policy_version,
    }
    terms["terms_hash"] = f"terms-{content_hash(terms)[:24]}"
    return terms


def approval_state(plan: PortfolioPlan, *, now: datetime | None = None) -> dict:
    """The current permission, as a review surface should display it.

    ``approved`` is the only state in which :func:`apply_approved_plan` will
    write a leg. Every other state names *why* the plan is not executable.
    """
    now = now or timezone.now()
    latest = _latest_approval(plan, PlanApprovalDecision.APPROVED)
    latest_rejection = _latest_approval(plan, PlanApprovalDecision.REJECTED)
    latest_cancel = _latest_approval(plan, PlanApprovalDecision.CANCELLED)

    if plan.status == PlanStatus.REJECTED:
        state = "rejected"
    elif plan.status == PlanStatus.CANCELLED:
        state = "cancelled"
    elif plan.status in {PlanStatus.APPLIED, PlanStatus.NO_CHANGE}:
        state = "consumed" if plan.approval_version else "not_required"
    elif plan.revalidation_required:
        state = "invalidated"
    elif not plan.approval_version or plan.approval_version != plan.plan_version:
        # A cleared approval on a plan that *had* one is an invalidation, not a
        # fresh plan awaiting its first review. The distinction is what tells a
        # reviewer the earlier decision no longer covers the current version.
        state = "invalidated" if (plan.approval_version or latest is not None) else "pending"
    elif plan.approval_expires_at is None:
        state = "invalidated"
    elif plan.approval_expires_at <= now:
        state = "expired"
    else:
        state = "approved"

    expires_in = None
    if plan.approval_expires_at is not None:
        expires_in = max(int((plan.approval_expires_at - now).total_seconds()), 0)
    decision = latest
    if plan.status == PlanStatus.REJECTED:
        decision = latest_rejection
    elif plan.status == PlanStatus.CANCELLED:
        decision = latest_cancel
    return {
        "state": state,
        "executable": state in {"approved"},
        "required": plan.status in {PlanStatus.PROPOSED},
        "plan_version": plan.plan_version,
        "approved_version": plan.approval_version or None,
        "approval_hash": plan.approval_hash or None,
        "terms_hash": (getattr(latest, "terms_hash", None) if latest is not None else None),
        "approved_legs": (
            list(getattr(latest, "approved_legs", []) or []) if latest is not None else []
        ),
        "price_tolerance_pct": (
            str(latest.price_tolerance_pct)
            if latest is not None and latest.price_tolerance_pct is not None
            else None
        ),
        "policy_version": getattr(latest, "policy_version", None) if latest else None,
        "classification_version": (
            getattr(latest, "classification_version", None) if latest else None
        ),
        "approved_at": utc_iso(plan.approved_at),
        "approved_by": plan.approved_by.username if plan.approved_by_id else None,
        "approved_by_id": plan.approved_by_id,
        "expires_at": utc_iso(plan.approval_expires_at),
        "seconds_remaining": expires_in,
        "decided_at": utc_iso(decision.decided_at) if decision is not None else None,
        "note": decision.note if decision is not None else "",
        "reason": (
            "The plan is approved for this version."
            if state == "approved"
            else {
                "pending": "No approval has been given for this plan version.",
                "invalidated": (
                    "The plan changed (or was flagged for revalidation) after approval; "
                    "the previous approval no longer covers it."
                ),
                "expired": "The approval's expiry has passed; approve the current version again.",
                "rejected": "A reviewer rejected this plan.",
                "cancelled": "This plan was cancelled before execution.",
                "consumed": "This plan has already been applied or recorded as no-change.",
                "not_required": "This plan is terminal and needs no approval.",
            }.get(state, "The plan is not executable.")
        ),
    }


def require_valid_approval(
    plan: PortfolioPlan, *, now: datetime | None = None
) -> ApprovalReference:
    """Return the approval reference, or raise with the exact reason."""
    state = approval_state(plan, now=now)
    if state["state"] == "expired":
        raise PlanApprovalExpired(
            "The approval has expired; approve the current plan version again before applying it.",
            code="approval_expired",
        )
    if state["state"] != "approved":
        raise PlanApprovalRequired(
            str(state["reason"]),
            code=f"approval_{state['state']}"
            if state["state"] not in {"pending"}
            else "approval_required",
        )
    if plan.approval_expires_at is None:  # pragma: no cover - guarded by approval_state
        raise PlanApprovalRequired(
            "The plan's approval has no expiry and is not executable.",
            code="approval_missing_expiry",
        )
    approved_row = (
        plan.approvals.filter(
            decision=PlanApprovalDecision.APPROVED,
            plan_version=int(plan.approval_version),
        )
        .order_by("-decided_at", "-id")
        .first()
    )
    if approved_row is None or not approved_row.terms_hash:
        raise PlanApprovalRequired(
            "This plan's approval has no recorded leg terms or risk versions. It cannot "
            "be used to execute; approve the current plan version again.",
            code="approval_terms_unknown",
        )
    if approved_row.expires_at is not None and approved_row.expires_at != plan.approval_expires_at:
        raise PlanApprovalRequired(
            "The stored approval's expiry does not match the plan's current permission; "
            "approve the current plan version again.",
            code="approval_expiry_changed",
        )
    if approved_row.expires_at is not None and approved_row.expires_at <= timezone.now():
        raise PlanApprovalExpired(
            "The approval has expired; approve the current plan version again before applying it.",
            code="approval_expired",
        )
    return ApprovalReference(
        plan_version=int(plan.plan_version),
        approval_hash=str(plan.approval_hash),
        approval_version=int(plan.approval_version),
        expires_at=plan.approval_expires_at,
        approval_id=approved_row.id,
        terms_hash=approved_row.terms_hash,
        approved_legs=tuple(approved_row.approved_legs or ()),
        price_tolerance_pct=approved_row.price_tolerance_pct,
        quantity_tolerance=approved_row.quantity_tolerance,
        max_quote_age_seconds=approved_row.max_quote_age_seconds,
        policy_version=approved_row.policy_version,
        classification_version=approved_row.classification_version,
    )


def approval_reference(
    plan: PortfolioPlan, *, now: datetime | None = None
) -> ApprovalReference | None:
    """The executable reference, or ``None`` when the plan is not currently approved."""
    state = approval_state(plan, now=now)
    if state["state"] != "approved":
        return None
    return require_valid_approval(plan, now=now)


def _locked_plan(plan_id) -> PortfolioPlan:
    try:
        # No select_related here: ``approved_by`` is nullable, and PostgreSQL
        # refuses FOR UPDATE over the nullable side of an outer join. The
        # portfolio is read separately where it is needed.
        plan = PortfolioPlan.objects.select_for_update().get(pk=plan_id)
    except PortfolioPlan.DoesNotExist:
        raise PlanApprovalError(
            f"Portfolio plan {plan_id} does not exist.", code="plan_not_found"
        ) from None
    _ = plan.portfolio  # cached on the instance; no lock is needed for a version read
    return plan


def _check_plan_version(plan: PortfolioPlan, plan_version) -> None:
    if plan_version in (None, ""):
        return
    try:
        supplied = int(plan_version)
    except (TypeError, ValueError):
        raise PlanApprovalError(
            "plan_version must be an integer.", code="invalid_plan_version"
        ) from None
    if supplied != int(plan.plan_version):
        raise PlanApprovalError(
            (
                f"This review names plan version {supplied}, but the plan is now version "
                f"{plan.plan_version}. Reload the plan and review the current version."
            ),
            code="plan_version_changed",
        )


def approve_plan(
    plan_id,
    *,
    user,
    plan_version=None,
    approval_hash: str | None = None,
    expires_at=None,
    ttl_seconds=None,
    note: str = "",
) -> PlanApproval:
    """Bind one human approval to one exact plan version and expiry.

    Refuses a stale plan version, a changed permission hash, a plan already
    approved, and a plan whose portfolio moved since it was evaluated. It does
    **not** apply the plan.
    """
    now = timezone.now()
    with transaction.atomic():
        plan = _locked_plan(plan_id)
        if plan.status != PlanStatus.PROPOSED:
            raise PlanApprovalError(
                f"Plan {plan.id} is {plan.status!r}; only a proposed plan can be approved.",
                code="plan_not_reviewable",
            )
        if plan.revalidation_required:
            raise PlanApprovalRequired(
                "The plan is marked for revalidation. Revalidate and review the current "
                "version before approving it.",
                code="revalidation_required",
            )
        _check_plan_version(plan, plan_version)
        if approval_hash not in (None, "") and str(approval_hash) != plan.approval_hash:
            raise PlanApprovalError(
                (
                    "The supplied approval hash does not match the plan's current permission "
                    f"binding ({plan.approval_hash}). Reload the review payload."
                ),
                code="approval_hash_changed",
            )
        current_version = portfolio_state_version(plan.portfolio)
        if current_version != plan.evaluated_portfolio_version:
            PortfolioPlan.objects.filter(pk=plan.id).update(revalidation_required=True)
            raise PlanApprovalRequired(
                (
                    "The portfolio changed after this plan was evaluated "
                    f"({plan.evaluated_portfolio_version} -> {current_version}). The plan "
                    "must be revalidated before it can be approved."
                ),
                code="plan_stale",
            )
        expiry = _resolve_expiry(expires_at=expires_at, ttl_seconds=ttl_seconds, now=now)
        terms = plan_approval_terms(plan)
        existing = approval_state(plan, now=now)
        if existing["state"] == "approved":
            raise PlanApprovalError(
                (
                    f"Plan version {plan.plan_version} is already approved until "
                    f"{existing['expires_at']}. No second approval was recorded."
                ),
                code="already_approved",
            )
        approval = PlanApproval.objects.create(
            plan=plan,
            plan_version=int(plan.plan_version),
            approval_hash=plan.approval_hash,
            decision=PlanApprovalDecision.APPROVED,
            decided_by=user if getattr(user, "pk", None) else None,
            decided_at=now,
            expires_at=expiry,
            note=str(note or "")[:4000],
            evaluated_portfolio_version=plan.evaluated_portfolio_version,
            approved_legs=terms["legs"],
            terms_hash=terms["terms_hash"],
            price_tolerance_pct=Decimal(terms["price_tolerance_pct"]),
            quantity_tolerance=Decimal(terms["quantity_tolerance"]),
            max_quote_age_seconds=int(terms["max_quote_age_seconds"]),
            policy_version=terms["policy_version"],
            classification_version=terms["classification_version"],
            allocation_policy_version=terms["allocation_policy_version"],
        )
        plan.approval_version = int(plan.plan_version)
        plan.approved_by = user if getattr(user, "pk", None) else None
        plan.approved_at = now
        plan.approval_expires_at = expiry
        plan.save(
            update_fields=[
                "approval_version",
                "approved_by",
                "approved_at",
                "approval_expires_at",
                "updated_at",
            ]
        )
    emit_portfolio_plan(
        getattr(user, "id", None),
        plan,
        EventType.PORTFOLIO_PLAN_APPROVED,
        extra={"approval_id": approval.id, "expires_at": utc_iso(expiry)},
        dedupe_key=f"plan-approval:{approval.id}",
    )
    logger.info(
        "Plan %s version %s approved by %s until %s",
        plan.id,
        plan.plan_version,
        getattr(user, "pk", "system"),
        expiry,
    )
    return approval


def reject_plan(
    plan_id,
    *,
    user,
    reason: str,
    plan_version=None,
) -> PlanApproval:
    """Reject a proposed plan. The audit row and status are both durable."""
    text = str(reason or "").strip()
    if len(text) < 3:
        raise PlanApprovalError(
            "A rejection needs a reason of at least 3 characters.", code="reason_required"
        )
    now = timezone.now()
    with transaction.atomic():
        plan = _locked_plan(plan_id)
        if plan.status != PlanStatus.PROPOSED:
            raise PlanApprovalError(
                f"Plan {plan.id} is {plan.status!r}; only a proposed plan can be rejected.",
                code="plan_not_reviewable",
            )
        _check_plan_version(plan, plan_version)
        approval = PlanApproval.objects.create(
            plan=plan,
            plan_version=int(plan.plan_version),
            approval_hash=plan.approval_hash,
            decision=PlanApprovalDecision.REJECTED,
            decided_by=user if getattr(user, "pk", None) else None,
            decided_at=now,
            expires_at=None,
            note=text[:4000],
            evaluated_portfolio_version=plan.evaluated_portfolio_version,
        )
        plan.status = PlanStatus.REJECTED
        plan.approval_version = 0
        plan.approved_by = None
        plan.approved_at = None
        plan.approval_expires_at = None
        plan.save(
            update_fields=[
                "status",
                "approval_version",
                "approved_by",
                "approved_at",
                "approval_expires_at",
                "updated_at",
            ]
        )
    emit_portfolio_plan(
        getattr(user, "id", None),
        plan,
        EventType.PORTFOLIO_PLAN_REJECTED,
        extra={"approval_id": approval.id, "reason": text[:2000]},
        dedupe_key=f"plan-rejection:{approval.id}",
    )
    logger.info("Plan %s rejected by %s", plan.id, getattr(user, "pk", "system"))
    return approval


def cancel_plan(plan_id, *, user, reason: str = "") -> tuple[PlanApproval | None, bool]:
    """Cancel a proposed plan. Returns ``(approval, already_cancelled)``."""
    now = timezone.now()
    with transaction.atomic():
        plan = _locked_plan(plan_id)
        if plan.status == PlanStatus.CANCELLED:
            latest = _latest_approval(plan, PlanApprovalDecision.CANCELLED)
            return latest, True
        if plan.status in {PlanStatus.APPLIED, PlanStatus.NO_CHANGE}:
            raise PlanApprovalError(
                f"Plan {plan.id} is {plan.status!r} and cannot be cancelled.",
                code="plan_not_reviewable",
            )
        if plan.status != PlanStatus.PROPOSED:
            raise PlanApprovalError(
                f"Plan {plan.id} is {plan.status!r} and cannot be cancelled.",
                code="plan_not_reviewable",
            )
        text = str(reason or "").strip()
        approval = PlanApproval.objects.create(
            plan=plan,
            plan_version=int(plan.plan_version),
            approval_hash=plan.approval_hash,
            decision=PlanApprovalDecision.CANCELLED,
            decided_by=user if getattr(user, "pk", None) else None,
            decided_at=now,
            expires_at=None,
            note=text[:4000],
            evaluated_portfolio_version=plan.evaluated_portfolio_version,
        )
        plan.status = PlanStatus.CANCELLED
        plan.approval_version = 0
        plan.approved_by = None
        plan.approved_at = None
        plan.approval_expires_at = None
        plan.save(
            update_fields=[
                "status",
                "approval_version",
                "approved_by",
                "approved_at",
                "approval_expires_at",
                "updated_at",
            ]
        )
    emit_portfolio_plan(
        getattr(user, "id", None),
        plan,
        EventType.PORTFOLIO_PLAN_CANCELLED,
        extra={"approval_id": approval.id, "reason": text[:2000]},
        dedupe_key=f"plan-cancellation:{approval.id}",
    )
    logger.info("Plan %s cancelled by %s", plan.id, getattr(user, "pk", "system"))
    return approval, False


def apply_approved_plan(
    plan_id,
    *,
    user,
    quotes=None,
    mode: str,
    max_quote_age_seconds: int | None = None,
) -> PlanApplication:
    """Execute a plan under its current approval, through ``apply_plan``.

    ``apply_plan`` is the only ledger path and re-checks the approval under the
    lock. ``applied_by`` is recorded here after a successful application; a
    no-change result is also an execution and is recorded the same way.
    """
    plan = (
        PortfolioPlan.objects.select_related("portfolio", "approved_by").filter(pk=plan_id).first()
    )
    if plan is None:
        raise PlanApprovalError(f"Portfolio plan {plan_id} does not exist.", code="plan_not_found")
    if plan.status in {PlanStatus.APPLIED, PlanStatus.NO_CHANGE}:
        # A retried apply is idempotent and needs no new approval.
        return apply_plan(
            plan_id, quotes=quotes, mode=mode, max_quote_age_seconds=max_quote_age_seconds
        )
    if plan.status != PlanStatus.PROPOSED:
        raise PlanNotApplicableError(f"Plan {plan.id} is {plan.status!r} and cannot be applied.")
    reference = require_valid_approval(plan)
    application = apply_plan(
        plan_id,
        quotes=quotes,
        mode=mode,
        max_quote_age_seconds=max_quote_age_seconds,
        approval=reference,
    )
    if user is not None and getattr(user, "pk", None):
        PortfolioPlan.objects.filter(pk=plan.id).update(applied_by=user)
        application.plan.applied_by = user
    emit_portfolio_plan(
        getattr(user, "id", None),
        application.plan,
        EventType.PORTFOLIO_PLAN_APPLIED,
        extra={
            "mode": mode,
            "already_applied": application.already_applied,
            "transaction_ids": [row.id for row in application.transactions],
            "applied_approval_version": application.plan.applied_approval_version,
        },
        dedupe_key=f"plan-applied:{plan.id}:{reference.approval_version}",
    )
    return application
