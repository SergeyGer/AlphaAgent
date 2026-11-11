"""Durable event catch-up and delivery metrics (FIX-06)."""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from services.event_outbox import catch_up_events, outbox_metrics

__all__ = ["EventCatchUpView", "EventDeliveryMetricsView"]


class EventCatchUpView(APIView):
    """Return one ordered slice of the caller's durable notification timeline.

    Reading the queue rather than a socket is the point: a client that was
    disconnected while Redis was down still reconstructs every stage, and a
    pending row is included because persistence, not delivery, is the record.
    """

    permission_classes = (IsAuthenticated,)

    def get(self, request):
        try:
            after = max(int(request.query_params.get("after", 0) or 0), 0)
            limit = min(max(int(request.query_params.get("limit", 200) or 200), 1), 500)
        except (TypeError, ValueError):
            return Response({"detail": "after and limit must be integers."}, status=400)
        events = catch_up_events(request.user.id, after_id=after, limit=limit)
        return Response(
            {
                "events": events,
                "count": len(events),
                "next_after": events[-1]["outbox_id"] if events else after,
                "authoritative_source": "event_outbox",
            }
        )


class EventDeliveryMetricsView(APIView):
    """Backlog and delivery lag for the caller's own notifications."""

    permission_classes = (IsAuthenticated,)

    def get(self, request):
        return Response(outbox_metrics(user_id=request.user.id))
