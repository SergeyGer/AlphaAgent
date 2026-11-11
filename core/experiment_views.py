"""Read the built report for one experiment (EXP-05).

Route (mounted under ``/api/`` by ``core/urls.py``)::

    GET /api/experiments/<id>/report/

The endpoint is authenticated and scoped to the caller's portfolios. An
experiment belonging to another user is a 404, matching the Phase 1 policy that
an id cannot be probed for ownership. A report that has not been built returns a
409 with instructions rather than an empty object: "no report" and "a report
full of nulls" are different states.

The report itself is stored on the experiment. Reading it never calls a model,
fetches a price or rebuilds accounting; ``manage.py build_experiment`` is the
only writer.
"""

from __future__ import annotations

from uuid import UUID

from django.http import Http404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.measurement_models import Experiment

__all__ = ["ExperimentReportView"]


def _error(status_code: int, detail: str) -> Response:
    return Response(
        {
            "error": True,
            "status_code": status_code,
            "detail": detail,
            "errors": None,
        },
        status=status_code,
    )


class ExperimentReportView(APIView):
    """``GET /api/experiments/<id>/report/`` - the versioned experiment report."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, experiment_id: str) -> Response:
        try:
            pk = UUID(str(experiment_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise Http404("Experiment not found.") from exc

        experiment = (
            Experiment.objects.filter(pk=pk, portfolio__user=request.user)
            .select_related("portfolio")
            .first()
        )
        if experiment is None:
            raise Http404("Experiment not found.")

        if not experiment.has_report:
            return _error(
                status.HTTP_409_CONFLICT,
                "This experiment has no built report yet. Run "
                f"`python manage.py build_experiment` with its manifest (experiment "
                f"{experiment.experiment_key!r}) and retry.",
            )

        response = Response(experiment.report, status=status.HTTP_200_OK)
        response["X-Report-Version"] = str(experiment.report_version)
        return response
