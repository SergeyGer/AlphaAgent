"""DRF exception handling - consistent envelope + clean error logging."""

from __future__ import annotations

import logging

from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger("alphaagent.api")

__all__ = ["alphaagent_exception_handler"]


def alphaagent_exception_handler(exc, context):
    """Wrap DRF's default handler with logging and a stable error envelope."""
    response = exception_handler(exc, context)

    if response is not None:
        detail = response.data
        if isinstance(detail, dict) and "detail" in detail and len(detail) == 1:
            message = str(detail["detail"])
        elif isinstance(detail, dict):
            message = "; ".join(
                f"{key}: {', '.join(str(v) for v in value) if isinstance(value, list) else value}"
                for key, value in detail.items()
            )
        else:
            message = str(detail)

        response.data = {
            "error": True,
            "status_code": response.status_code,
            "detail": message,
            "errors": detail if isinstance(detail, dict) else None,
        }
        if response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error(
                "API error %s in %s: %s",
                response.status_code,
                context.get("view"),
                exc,
                exc_info=True,
            )
        else:
            logger.info(
                "API client error %s in %s: %s", response.status_code, context.get("view"), message
            )
        return response

    # Unhandled exceptions -> log cleanly, return a generic 500 (no stack leak).
    if isinstance(exc, (DjangoValidationError, ObjectDoesNotExist, DatabaseError)):
        logger.error("Domain error in %s: %s", context.get("view"), exc, exc_info=True)
        return Response(
            {
                "error": True,
                "status_code": status.HTTP_400_BAD_REQUEST,
                "detail": str(exc),
                "errors": None,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    logger.exception("Unhandled exception in %s", context.get("view"))
    return Response(
        {
            "error": True,
            "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
            "detail": "Internal server error.",
            "errors": None,
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
