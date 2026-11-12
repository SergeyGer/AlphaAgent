"""Advisory portfolio-plan review and approval API (FIX-08).

Routes (mounted under ``/api/`` by ``core/urls.py``)::

    GET  /api/portfolio-plans/                       list, caller's portfolios only
    GET  /api/portfolio-plans/<id>/                  full review payload
    POST /api/portfolio-plans/<id>/approve/          bind version + expiry
    POST /api/portfolio-plans/<id>/reject/           record rejection + audit event
    POST /api/portfolio-plans/<id>/cancel/           withdraw + audit event
    POST /api/portfolio-plans/<id>/revalidate/       re-derive after a material change
    POST /api/portfolio-plans/<id>/apply/            explicit execution through the guard

Submission is ``POST /api/decision-runs/`` (see ``core.decision_views``). The
route is deliberately the one the review proposed; the underlying record is an
``AllocationSubmission`` because a portfolio decision is one plan against one
snapshot, not one ``DecisionRun`` per ticker.

Every endpoint is authenticated and scoped to the caller's portfolios. Another
user's plan is a **404**, not a 403, matching the project's existing ownership
policy. The error envelope is the same ``{error, status_code, detail, errors}``
shape used elsewhere under ``/api/``.

No endpoint here trades except ``apply``, and ``apply`` requires a current,
unexpired approval for the exact plan version. Submission and approval are
advisory: they write no ledger row.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.allocation_models import PortfolioPlan
from services.allocation_submission import collect_quotes
from services.execution import ExecutionError, ExecutionRejected
from services.portfolio_allocation import (
    PlanLegFailed,
    PlanNotApplicableError,
    PlanQuoteUnavailable,
    PlanReapprovalRequired,
    PlanStaleError,
    revalidate_plan,
)
from services.portfolio_approval import (
    PlanApprovalError,
    apply_approved_plan,
    approve_plan,
    cancel_plan,
    reject_plan,
    require_valid_approval,
)
from services.portfolio_review import build_plan_review, plan_summary

__all__ = [
    "PlanApplyView",
    "PlanApproveView",
    "PlanCancelView",
    "PlanDetailView",
    "PlanListView",
    "PlanRejectView",
    "PlanRevalidateView",
]

logger = logging.getLogger("alphaagent.allocation_views")

#: Error codes that are a malformed request rather than a state conflict.
BAD_REQUEST_CODES = frozenset(
    {
        "invalid_expiry",
        "missing_expiry",
        "expiry_not_future",
        "expiry_too_far",
        "invalid_plan_version",
        "reason_required",
        "invalid_ttl",
    }
)


def error_response(status_code: int, detail: str, *, code: str | None = None) -> Response:
    return Response(
        {
            "error": True,
            "status_code": status_code,
            "detail": detail,
            "code": code,
            "errors": None,
        },
        status=status_code,
    )


def _plan_for(request: Request, plan_id: str) -> PortfolioPlan:
    try:
        pk = UUID(str(plan_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise Http404("Portfolio plan not found.") from exc
    plan = (
        PortfolioPlan.objects.select_related("portfolio", "approved_by", "applied_by")
        .filter(pk=pk, portfolio__user=request.user)
        .first()
    )
    if plan is None:
        raise Http404("Portfolio plan not found.")
    return plan


def _submission_status(plan: PortfolioPlan) -> Response:
    return Response(build_plan_review(plan), status=status.HTTP_200_OK)


def _approval_error_response(exc: PlanApprovalError) -> Response:
    """Typed approval failures carry operator-facing text, so the text is served.

    These are declared messages - a plan version is stale, an approval expired -
    not arbitrary exception dumps. The distinction matters: an unexpected exception
    goes through , which quotes nothing.
    """
    code = getattr(exc, "code", "plan_approval_error")
    if code == "plan_not_found":
        return error_response(status.HTTP_404_NOT_FOUND, str(exc), code=code)
    if code in BAD_REQUEST_CODES:
        return error_response(status.HTTP_400_BAD_REQUEST, str(exc), code=code)
    return error_response(status.HTTP_409_CONFLICT, str(exc), code=code)


def _execution_error_response(exc: Exception) -> Response:
    """Map an execution failure to a status without quoting the exception.

    The message of a database or service exception can carry a table name, a file
    path or a fragment of a query. The client gets a correlation id; the traceback
    goes to the log.
    """
    correlation = uuid4().hex[:12]
    logger.exception("Plan execution failed [%s]", correlation)
    detail = (
        "The plan could not be executed. "
        f"The cause was logged on the server with correlation id {correlation}."
    )
    code = getattr(exc, "code", None)
    revised_terms = getattr(exc, "revised_terms", None)
    payload_detail = detail
    if revised_terms:
        payload_detail = {
            "detail": detail,
            "revised_terms": revised_terms,
            "requires_new_approval": True,
        }
    if isinstance(exc, ExecutionRejected):
        return error_response(status.HTTP_409_CONFLICT, payload_detail, code=code)
    if isinstance(exc, ExecutionError):
        return error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, payload_detail, code=code)
    # PlanLegFailed / PlanReapprovalRequired / PlanStaleError carry a stable code
    # and an operator-facing message. Returning the code is what lets a client
    # tell a stale mark from a missing one without parsing prose.
    if isinstance(
        exc, (PlanLegFailed, PlanReapprovalRequired, PlanStaleError, PlanQuoteUnavailable)
    ):
        return error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            str(exc),
            code=getattr(exc, "code", None),
        )
    return error_response(status.HTTP_400_BAD_REQUEST, detail, code=code)


def _plan_tickers(plan: PortfolioPlan) -> list[str]:
    """Every name whose current mark the plan's valuation can depend on."""
    brief = plan.brief or {}
    tickers = {
        str(row.get("ticker") or "").upper()
        for row in brief.get("holdings") or []
        if row.get("ticker")
    }
    for key in ("requested_target_weights", "target_weights", "executed_target_weights"):
        for ticker, weight in (getattr(plan, key) or {}).items():
            symbol = str(ticker).upper()
            if symbol == "CASH":
                continue
            try:
                if float(weight) > 0:
                    tickers.add(symbol)
            except (TypeError, ValueError):
                continue
    tickers |= {leg.ticker.upper() for leg in plan.legs.all()}
    return sorted(ticker for ticker in tickers if ticker)


def _paper_quotes(plan: PortfolioPlan) -> dict[str, dict]:
    """The plan's own marks, labelled, for an explicitly requested paper apply."""
    quotes: dict[str, dict] = {}
    for row in (plan.brief or {}).get("holdings") or []:
        ticker = str(row.get("ticker") or "").upper()
        price = row.get("price")
        if ticker and price not in (None, ""):
            quotes[ticker] = {"price": str(price), "source": "paper_plan_mark"}
    for leg in plan.legs.all():
        quotes.setdefault(
            leg.ticker.upper(), {"price": str(leg.price), "source": "paper_plan_mark"}
        )
    return quotes


class PlanListView(APIView):
    """``GET /api/portfolio-plans/`` - the caller's advisory plans, newest first."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        queryset = PortfolioPlan.objects.filter(portfolio__user=request.user).order_by(
            "-created_at"
        )
        portfolio_param = (request.query_params.get("portfolio_id") or "").strip()
        if portfolio_param:
            try:
                queryset = queryset.filter(portfolio_id=int(portfolio_param))
            except (TypeError, ValueError):
                return error_response(
                    status.HTTP_400_BAD_REQUEST, "portfolio_id must be an integer."
                )
        status_param = (request.query_params.get("status") or "").strip().lower()
        if status_param:
            queryset = queryset.filter(status=status_param)
        approval_param = (request.query_params.get("approval_state") or "").strip().lower()
        rows = [plan_summary(plan) for plan in queryset[:200]]
        if approval_param:
            rows = [row for row in rows if row.get("approval_state") == approval_param]
        return Response({"count": len(rows), "results": rows}, status=status.HTTP_200_OK)


class PlanDetailView(APIView):
    """``GET /api/portfolio-plans/<id>/`` - the full review payload."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, plan_id: str) -> Response:
        plan = _plan_for(request, plan_id)
        try:
            payload = build_plan_review(plan)
        except Exception:
            correlation = uuid4().hex[:12]
            logger.exception("Plan review projection failed [%s]", correlation)
            raise APIException(
                "The plan review could not be produced. The cause was logged with "
                f"correlation id {correlation}."
            ) from None
        return Response(payload, status=status.HTTP_200_OK)


class PlanApproveView(APIView):
    """``POST /api/portfolio-plans/<id>/approve/`` - bind version and expiry.

    Body::

        {"plan_version": 3, "expires_at": "2026-...+00:00",
         "approval_hash": "<optional; must match>", "note": "..."}

    ``expires_in_seconds`` is accepted instead of ``expires_at``. An expiry is
    mandatory: the service refuses an unbounded approval.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, plan_id: str) -> Response:
        plan = _plan_for(request, plan_id)
        data = request.data if isinstance(request.data, dict) else {}
        if data.get("plan_version") in (None, ""):
            return error_response(
                status.HTTP_400_BAD_REQUEST,
                "plan_version is required: approval binds to the exact version reviewed.",
                code="missing_plan_version",
            )
        if data.get("expires_at") in (None, "") and data.get("expires_in_seconds") in (None, ""):
            return error_response(
                status.HTTP_400_BAD_REQUEST,
                "expires_at or expires_in_seconds is required: an approval must expire.",
                code="missing_expiry",
            )
        try:
            approval = approve_plan(
                plan.id,
                user=request.user,
                plan_version=data.get("plan_version"),
                approval_hash=data.get("approval_hash"),
                expires_at=data.get("expires_at"),
                ttl_seconds=data.get("expires_in_seconds"),
                note=str(data.get("note") or ""),
            )
        except PlanApprovalError as exc:
            return _approval_error_response(exc)
        plan.refresh_from_db()
        return Response(
            {
                "approval": {
                    "approval_id": approval.id,
                    "decision": approval.decision,
                    "plan_version": approval.plan_version,
                    "approval_hash": approval.approval_hash,
                    "terms_hash": approval.terms_hash,
                    "approved_legs": approval.approved_legs,
                    "price_tolerance_pct": (
                        str(approval.price_tolerance_pct)
                        if approval.price_tolerance_pct is not None
                        else None
                    ),
                    "policy_version": approval.policy_version,
                    "classification_version": approval.classification_version,
                    "decided_at": approval.decided_at,
                    "expires_at": approval.expires_at,
                    "note": approval.note,
                },
                "review": build_plan_review(plan),
            },
            status=status.HTTP_200_OK,
        )


class PlanRejectView(APIView):
    """``POST /api/portfolio-plans/<id>/reject/`` - record a rejection and event."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, plan_id: str) -> Response:
        plan = _plan_for(request, plan_id)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            approval = reject_plan(
                plan.id,
                user=request.user,
                reason=str(data.get("reason") or ""),
                plan_version=data.get("plan_version"),
            )
        except PlanApprovalError as exc:
            return _approval_error_response(exc)
        plan.refresh_from_db()
        return Response(
            {
                "approval": {
                    "approval_id": approval.id,
                    "decision": approval.decision,
                    "plan_version": approval.plan_version,
                    "decided_at": approval.decided_at,
                    "note": approval.note,
                },
                "review": build_plan_review(plan),
            },
            status=status.HTTP_200_OK,
        )


class PlanCancelView(APIView):
    """``POST /api/portfolio-plans/<id>/cancel/`` - withdraw and audit."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, plan_id: str) -> Response:
        plan = _plan_for(request, plan_id)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            approval, already = cancel_plan(
                plan.id,
                user=request.user,
                reason=str(data.get("reason") or ""),
            )
        except PlanApprovalError as exc:
            return _approval_error_response(exc)
        plan.refresh_from_db()
        return Response(
            {
                "already_cancelled": already,
                "approval": (
                    {
                        "approval_id": approval.id,
                        "decision": approval.decision,
                        "plan_version": approval.plan_version,
                        "decided_at": approval.decided_at,
                        "note": approval.note,
                    }
                    if approval is not None
                    else None
                ),
                "review": build_plan_review(plan),
            },
            status=status.HTTP_200_OK,
        )


class PlanRevalidateView(APIView):
    """``POST /api/portfolio-plans/<id>/revalidate/`` - re-derive before re-approval.

    Current quotes are required: a revalidation against the plan's old marks
    would project today's decision onto yesterday's prices. The service bumps
    ``plan_version`` and clears the previous approval.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, plan_id: str) -> Response:
        plan = _plan_for(request, plan_id)
        tickers = _plan_tickers(plan)
        collection = collect_quotes(plan.portfolio, tickers, allow_cost_fallback=False)
        quotes = collection.observations
        try:
            plan = revalidate_plan(plan.id, quotes=quotes)
        except (PlanStaleError, PlanReapprovalRequired, PlanNotApplicableError) as exc:
            return _execution_error_response(exc)
        except (PlanQuoteUnavailable, PlanLegFailed) as exc:
            return _execution_error_response(exc)
        except Exception as exc:  # pragma: no cover - typed service errors cover the path
            logger.exception("Plan revalidation failed for %s", plan.id)
            return error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
        plan.refresh_from_db()
        return Response(
            {
                "review": build_plan_review(plan),
                "input": collection.as_dict(),
                "note": (
                    "The plan version was incremented and the previous approval cleared. "
                    "Review and approve the current version before applying it."
                ),
            },
            status=status.HTTP_200_OK,
        )


class PlanApplyView(APIView):
    """``POST /api/portfolio-plans/<id>/apply/`` - the explicit execution act.

    The plan must carry a current, unexpired approval for its exact version.
    ``mode=live`` (the default) supplies current marks; ``mode=paper`` uses the
    plan's own marks and is recorded on the plan. Execution goes through
    ``services.execution``; this endpoint never writes a ledger row itself.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, plan_id: str) -> Response:
        plan = _plan_for(request, plan_id)
        data = request.data if isinstance(request.data, dict) else {}
        mode = str(data.get("mode") or "live").strip().lower()
        if mode not in {"live", "paper"}:
            return error_response(
                status.HTTP_400_BAD_REQUEST,
                "mode must be 'live' or 'paper'.",
                code="invalid_mode",
            )

        tickers = _plan_tickers(plan)
        quotes: dict[str, Any] | None
        input_info: dict
        if mode == "paper":
            quotes = _paper_quotes(plan)
            input_info = {
                "mode": "paper",
                "quote_source": "plan_marks",
                "note": (
                    "Paper mode uses the plan's own marks. It is an explicit mode, recorded "
                    "on the plan; it is not a live market fill."
                ),
            }
        else:
            collection = collect_quotes(plan.portfolio, tickers, allow_cost_fallback=False)
            quotes = collection.observations
            input_info = collection.as_dict()

        # Validate the approval before the service so a missing approval is a
        # typed 409 rather than a stale-price 422. The service re-checks it under
        # the plan lock.
        if plan.status == "proposed":
            try:
                require_valid_approval(plan)
            except PlanApprovalError as exc:
                return _approval_error_response(exc)

        try:
            application = apply_approved_plan(
                plan.id,
                user=request.user,
                quotes=quotes,
                mode=mode,
            )
        except PlanApprovalError as exc:
            return _approval_error_response(exc)
        except (PlanStaleError, PlanReapprovalRequired, PlanNotApplicableError) as exc:
            return _execution_error_response(exc)
        except (PlanQuoteUnavailable, PlanLegFailed) as exc:
            return _execution_error_response(exc)
        except Exception as exc:  # pragma: no cover - the service types its failures
            logger.exception("Plan application failed for %s", plan.id)
            return error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

        application.plan.refresh_from_db()
        return Response(
            {
                "application": {
                    "plan_id": str(application.plan.id),
                    "status": application.plan.status,
                    "already_applied": application.already_applied,
                    "mode": application.plan.execution_mode,
                    "transaction_ids": [row.id for row in application.transactions],
                    "legs": [
                        {
                            "sequence": leg.sequence,
                            "ticker": leg.ticker,
                            "side": leg.side,
                            "status": leg.status,
                            "quantity": str(leg.quantity),
                            "price": str(leg.price),
                            "transaction_id": leg.transaction_id,
                        }
                        for leg in application.legs
                    ],
                },
                "input": input_info,
                "review": build_plan_review(application.plan),
            },
            status=status.HTTP_200_OK,
        )
