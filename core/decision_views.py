"""Read API for decision runs (API-01).

Every endpoint is authenticated and scoped to the caller's own portfolios. A run
that belongs to somebody else is a **404**, not a 403: the project's existing
policy for another user's object is to behave as if it does not exist, so an id
cannot be probed for ownership (API-04). The response envelope is the same
``{error, status_code, detail, errors}`` shape the rest of ``/api/`` uses.

Routes (mounted under ``/api/`` by ``core/urls.py``)::

    GET /api/decision-runs/                       list, caller's portfolios only
    POST /api/decision-runs/                      idempotent portfolio analysis submission
    GET /api/decision-runs/<id>/status/           submission status + plan link
    GET /api/decision-runs/<id>/                  metadata, stages, cost, failure
    GET /api/decision-runs/<id>/detail/           cases, rebuttals, verdict, guard
    GET /api/decision-runs/<id>/events/?after_seq=N  ordered event window
    GET /api/decision-runs/<id>/export/           redacted versioned JSON bundle
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from django.db.models import Max
from django.http import Http404
from django.urls import reverse
from rest_framework import generics, status
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.allocation_models import AllocationSubmission
from core.decision_models import DecisionRun
from core.decision_serializers import (
    DecisionRunSerializer,
    RunEventSerializer,
    build_export_bundle,
    build_run_detail,
    build_run_metadata,
)
from core.models import Portfolio
from services.allocation_submission import (
    AllocationSubmissionConflict,
    AllocationSubmissionError,
    submission_as_dict,
    submit_analysis,
)

__all__ = [
    "DecisionRunDetailView",
    "DecisionRunEventsView",
    "DecisionRunExportView",
    "DecisionRunListView",
    "DecisionRunMetadataView",
    "DecisionRunStatusView",
]

#: Default and maximum window for the events endpoint. The default matches
#: DRF's PAGE_SIZE so the decision endpoints feel like the rest of the API.
EVENTS_PAGE_SIZE = 50
EVENTS_MAX_PAGE_SIZE = 500

logger = logging.getLogger("alphaagent.decision_views")


def _error(status_code: int, detail: str) -> Response:
    """The project's error envelope, for hand-built error responses."""
    return Response(
        {
            "error": True,
            "status_code": status_code,
            "detail": detail,
            "errors": None,
        },
        status=status_code,
    )


def _get_run(request: Request, run_id: str, *, with_detail: bool = False) -> DecisionRun:
    """Load a run owned by the caller, or raise an indistinguishable 404."""
    try:
        pk = UUID(str(run_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise Http404("Decision run not found.") from exc

    queryset = DecisionRun.objects.filter(pk=pk, portfolio__user=request.user)
    if with_detail:
        queryset = queryset.select_related("snapshot", "decision", "portfolio")
    else:
        queryset = queryset.select_related("snapshot", "portfolio")

    run = queryset.first()
    if run is None:
        raise Http404("Decision run not found.")
    return run


class DecisionRunListView(generics.ListAPIView):
    """``GET``/``POST /api/decision-runs/``.

    ``GET`` lists the caller's decision runs, newest first. Filtering is
    deliberately the same style as the existing decision-log list: an unknown
    value simply matches nothing rather than erroring, so a URL with a stale
    filter still returns a well-formed empty result.

    ``POST`` is the authenticated, idempotent portfolio-analysis submission
    (FIX-08). It is not a decision-log write: it records an
    ``AllocationSubmission`` and, when analysis completes, one
    ``PortfolioPlan`` for the whole portfolio. The body carries
    ``portfolio_id``, ``tickers``, ``mode``, ``protocol_version`` and an
    ``idempotency_key``. The same key with the same body returns the first run;
    the same key with a different body is **409**. The response is **202** with
    a ``run_id`` and a status URL, and no trade is placed.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = DecisionRunSerializer

    def post(self, request: Request) -> Response:
        raw_portfolio = request.data.get("portfolio_id") if isinstance(request.data, dict) else None
        try:
            portfolio_id = int(raw_portfolio)
        except (TypeError, ValueError):
            return _error(
                status.HTTP_400_BAD_REQUEST,
                "portfolio_id is required and must be an integer.",
            )
        portfolio = Portfolio.objects.filter(pk=portfolio_id, user=request.user).first()
        if portfolio is None:
            # Ownership is not probed: another user's portfolio is a 404.
            raise Http404("Portfolio not found.")
        try:
            submission, created = submit_analysis(portfolio, data=request.data, user=request.user)
        except AllocationSubmissionError as exc:
            return _error(status.HTTP_400_BAD_REQUEST, exc.detail)
        except AllocationSubmissionConflict as exc:
            return _error(status.HTTP_409_CONFLICT, str(exc))

        payload = submission_as_dict(submission)
        status_url = reverse("core:decision-run-status", args=[submission.id])
        payload["status_url"] = request.build_absolute_uri(status_url)
        payload["status_path"] = status_url
        payload["plan_url"] = (
            request.build_absolute_uri(
                reverse("core:portfolio-plan-detail", args=[submission.plan_id])
            )
            if submission.plan_id
            else None
        )
        payload["idempotent_replay"] = not created
        response = Response(payload, status=status.HTTP_202_ACCEPTED)
        response["Location"] = status_url
        response["Idempotency-Key"] = submission.idempotency_key
        return response

    def get_queryset(self):
        queryset = DecisionRun.objects.filter(portfolio__user=self.request.user).order_by(
            "-started_at", "-created_at"
        )

        status_param = (self.request.query_params.get("status") or "").strip().lower()
        if status_param:
            queryset = queryset.filter(status=status_param)

        mode = (self.request.query_params.get("mode") or "").strip().lower()
        if mode:
            queryset = queryset.filter(mode=mode)

        source = (self.request.query_params.get("source") or "").strip().lower()
        if source:
            queryset = queryset.filter(source=source)

        ticker = (self.request.query_params.get("ticker") or "").strip().upper()
        if ticker:
            queryset = queryset.filter(ticker=ticker)

        return queryset


class DecisionRunMetadataView(APIView):
    """``GET /api/decision-runs/<id>/`` - metadata, stage states, cost, failure."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, run_id: str) -> Response:
        run = _get_run(request, run_id)
        events = list(run.events.order_by("sequence"))
        try:
            payload = build_run_metadata(run, events=events)
        except Exception:
            correlation = uuid4().hex[:12]
            logger.exception("Decision run metadata projection failed [%s]", correlation)
            raise _projection_failed(correlation) from None
        return Response(payload, status=status.HTTP_200_OK)


class DecisionRunStatusView(APIView):
    """``GET /api/decision-runs/<run_id>/status/`` - submission status.

    The id in this URL is the ``run_id`` returned by ``POST /api/decision-runs/``
    (an ``AllocationSubmission``), not a ``DecisionRun`` id. It is served under
    the same prefix because that is where the submission was accepted; the
    separate ``.../status/`` segment keeps the two id spaces from colliding.

    The status is ``accepted``, ``running``, ``completed`` or ``failed``. A
    completed submission always names the plan it produced; a failed one always
    names its typed error. Neither case is a trade.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, run_id: str) -> Response:
        try:
            pk = UUID(str(run_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise Http404("Allocation submission not found.") from exc
        submission = (
            AllocationSubmission.objects.select_related("plan")
            .filter(pk=pk, portfolio__user=request.user)
            .first()
        )
        if submission is None:
            raise Http404("Allocation submission not found.")
        payload = submission_as_dict(submission)
        payload["status_url"] = request.build_absolute_uri(
            reverse("core:decision-run-status", args=[submission.id])
        )
        payload["plan_url"] = (
            request.build_absolute_uri(
                reverse("core:portfolio-plan-detail", args=[submission.plan_id])
            )
            if submission.plan_id
            else None
        )
        payload["terminal"] = submission.is_terminal
        response = Response(payload, status=status.HTTP_200_OK)
        if not submission.is_terminal:
            response["Retry-After"] = "1"
        return response


class DecisionRunDetailView(APIView):
    """``GET /api/decision-runs/<id>/detail/`` - the evidence-backed decision.

    Serves the two cases, the recorded rebuttals (empty when the run predates the
    rebuttal protocol - which is every live Phase 1 run), the verdict with its
    decisive claims resolved, the guard's requested-vs-allowed comparison, the
    measured outcomes, and one aggregated limitations list.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, run_id: str) -> Response:
        run = _get_run(request, run_id, with_detail=True)
        events = list(run.events.order_by("sequence"))
        try:
            payload = build_run_detail(run, events=events)
        except Exception:
            correlation = uuid4().hex[:12]
            logger.exception("Decision run detail projection failed [%s]", correlation)
            raise _projection_failed(correlation) from None
        return Response(payload, status=status.HTTP_200_OK)


class DecisionRunEventsView(APIView):
    """``GET /api/decision-runs/<id>/events/?after_seq=N`` - an event window.

    Cursor pagination, not page numbers: sequences are contiguous per run and a
    consumer reconnecting wants "everything after 41", not "page 3". The response
    reports ``next`` (the URL to fetch the following window, or null) and both
    ``last_sequence`` (last event in this window) and ``latest_sequence`` (the
    run's newest persisted sequence), so a client can distinguish "no more in
    this page" from "nothing has happened since".
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, run_id: str) -> Response:
        run = _get_run(request, run_id)

        raw_after = request.query_params.get("after_seq")
        try:
            after_seq = int(raw_after) if raw_after not in (None, "") else 0
        except (TypeError, ValueError):
            return _error(
                status.HTTP_400_BAD_REQUEST,
                "after_seq must be a non-negative integer.",
            )
        if after_seq < 0:
            return _error(
                status.HTTP_400_BAD_REQUEST,
                "after_seq must be a non-negative integer.",
            )

        raw_limit = request.query_params.get("limit")
        try:
            limit = int(raw_limit) if raw_limit not in (None, "") else EVENTS_PAGE_SIZE
        except (TypeError, ValueError):
            return _error(status.HTTP_400_BAD_REQUEST, "limit must be an integer.")
        limit = max(1, min(limit, EVENTS_MAX_PAGE_SIZE))

        query = run.events.filter(sequence__gt=after_seq).order_by("sequence")
        window = list(query[: limit + 1])
        has_more = len(window) > limit
        events = window[:limit]

        latest_sequence = run.events.aggregate(last=Max("sequence"))["last"] or 0
        last_sequence = events[-1].sequence if events else after_seq
        next_url = None
        if has_more and events:
            next_url = f"{request.path}?after_seq={last_sequence}&limit={limit}"

        return Response(
            {
                "run_id": str(run.id),
                "after_seq": after_seq,
                "limit": limit,
                "count": len(events),
                "last_sequence": last_sequence,
                "latest_sequence": latest_sequence,
                "next": next_url,
                "events": RunEventSerializer(events, many=True).data,
            },
            status=status.HTTP_200_OK,
        )


class DecisionRunExportView(APIView):
    """``GET /api/decision-runs/<id>/export/`` - redacted, checksummed bundle.

    The bundle is a document: it can be filed, attached to a review or diffed
    between runs. It contains no credentials, no account identifiers and no
    unlicensed source bodies; see ``core.decision_serializers`` for how that is
    enforced structurally rather than by deleting fields one at a time.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, run_id: str) -> Response:
        run = _get_run(request, run_id, with_detail=True)
        events = list(run.events.order_by("sequence"))
        bundle = build_export_bundle(run, events=events)
        response = Response(bundle, status=status.HTTP_200_OK)
        response["Content-Disposition"] = f'attachment; filename="decision-run-{run.id}.json"'
        return response


def _projection_failed(correlation: str) -> APIException:
    """Build a typed 500 without an exception object anywhere near it.

    A serializer bug must not hand a client an exception class, a message or a file
    path. The cause is logged by the caller with ``exc_info`` under a correlation id,
    and only that id reaches the response - so there is nothing for a future edit to
    accidentally start serialising.
    """
    return APIException(
        "The decision run could not be produced. "
        f"The cause was logged on the server with correlation id {correlation}."
    )
