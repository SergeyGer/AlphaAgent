"""Read API for decision runs (API-01).

Every endpoint is authenticated and scoped to the caller's own portfolios. A run
that belongs to somebody else is a **404**, not a 403: the project's existing
policy for another user's object is to behave as if it does not exist, so an id
cannot be probed for ownership (API-04). The response envelope is the same
``{error, status_code, detail, errors}`` shape the rest of ``/api/`` uses.

Routes (mounted under ``/api/`` by ``core/urls.py``)::

    GET /api/decision-runs/                       list, caller's portfolios only
    GET /api/decision-runs/<id>/                  metadata, stages, cost, failure
    GET /api/decision-runs/<id>/detail/           cases, rebuttals, verdict, guard
    GET /api/decision-runs/<id>/events/?after_seq=N  ordered event window
    GET /api/decision-runs/<id>/export/           redacted versioned JSON bundle
"""

from __future__ import annotations

from uuid import UUID

from django.db.models import Max
from django.http import Http404
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.decision_models import DecisionRun
from core.decision_serializers import (
    DecisionRunSerializer,
    RunEventSerializer,
    build_export_bundle,
    build_run_detail,
    build_run_metadata,
)

__all__ = [
    "DecisionRunDetailView",
    "DecisionRunEventsView",
    "DecisionRunExportView",
    "DecisionRunListView",
    "DecisionRunMetadataView",
]

#: Default and maximum window for the events endpoint. The default matches
#: DRF's PAGE_SIZE so the decision endpoints feel like the rest of the API.
EVENTS_PAGE_SIZE = 50
EVENTS_MAX_PAGE_SIZE = 500


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
    """``GET /api/decision-runs/`` - the caller's runs, newest first.

    Filtering is deliberately the same style as the existing decision-log list:
    an unknown value simply matches nothing rather than erroring, so a URL with a
    stale filter still returns a well-formed empty result.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = DecisionRunSerializer

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
        return Response(build_run_metadata(run, events=events), status=status.HTTP_200_OK)


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
        return Response(build_run_detail(run, events=events), status=status.HTTP_200_OK)


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
