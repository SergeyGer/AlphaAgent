"""AlphaAgent API views.

Performance notes
-----------------
* The portfolio row, its user and its assets are fetched in **one** query via
  ``select_related`` + ``Prefetch`` (no N+1 on the nested serializer).
* Live prices are resolved once per request into a ``price_map`` and shared by
  the serializer context, so a portfolio with N assets triggers at most N
  (cache-backed) lookups - never N per field.
* The audit-trail endpoint uses ``select_related('transaction')`` and pagination.
"""

from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.log_safety import log_safe
from core.models import (
    AgentDecisionLog,
    Asset,
    DecidedVia,
    Portfolio,
    TelegramLink,
    TradeRecommendation,
    Transaction,
)
from core.serializers import (
    AgentDecisionLogSerializer,
    MarketNewsSerializer,
    PortfolioSnapshotSerializer,
    TelegramLinkSerializer,
    ToggleAutonomySerializer,
    TradeRecommendationSerializer,
    TransactionSerializer,
    build_portfolio_payload,
)
from services.recommendations import approve_recommendation, reject_recommendation
from services.snapshots import equity_series
from telegram_bot import handle_update

logger = logging.getLogger("alphaagent.api")


def telegram_config() -> dict:
    """Read Telegram settings lazily.

    Resolving this at import time would freeze the value and break
    ``override_settings`` in tests (and any runtime reconfiguration).
    """
    return getattr(settings, "TELEGRAM_CONFIG", {}) or {}


def telegram_enabled() -> bool:
    """True when a bot token is configured."""
    return bool(telegram_config().get("BOT_TOKEN"))


__all__ = [
    "AdvisorySweepView",
    "AgentDecisionLogListView",
    "MarketNewsView",
    "PortfolioDetailView",
    "PortfolioSnapshotListView",
    "RecommendationApproveView",
    "RecommendationRejectView",
    "RunAgentView",
    "TelegramLinkView",
    "TelegramWebhookView",
    "ToggleAutonomyView",
    "TradeRecommendationListView",
    "TransactionListView",
]


def get_portfolio(user) -> Portfolio:
    """Return the user's portfolio, provisioning one on first access."""
    portfolio, created = Portfolio.objects.get_or_create(user=user)
    if created:
        logger.info("Provisioned portfolio %s for user %s", portfolio.id, log_safe(user.username))
    return portfolio


def _load_portfolio(user) -> Portfolio:
    """Fetch the portfolio with every relation the response needs, in one query."""
    portfolio = get_portfolio(user)
    return (
        Portfolio.objects.select_related("user")
        .prefetch_related(Prefetch("assets", queryset=Asset.objects.order_by("-amount", "ticker")))
        .get(pk=portfolio.pk)
    )


class PortfolioDetailView(APIView):
    """``GET /api/portfolio/`` - portfolio, nested assets and live metrics."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        portfolio = _load_portfolio(request.user)

        # Shared with the WebSocket snapshot emitter so both surfaces always
        # describe the portfolio identically.
        return Response(
            build_portfolio_payload(portfolio, request=request),
            status=status.HTTP_200_OK,
        )


class ToggleAutonomyView(APIView):
    """``POST /api/portfolio/toggle-autonomy/`` - safe autonomy switch."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        portfolio = get_portfolio(request.user)
        serializer = ToggleAutonomySerializer(
            data=request.data, context={"request": request, "portfolio": portfolio}
        )
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        target = validated["is_autonomous"]
        changed = validated["changed"]
        if changed:
            portfolio.is_autonomous = target
            portfolio.save(update_fields=["is_autonomous"])
            # Only the Telegram path used to emit this, so toggling autonomy from
            # the dashboard left other open surfaces showing a stale state.
            from services.events import emit_autonomy_changed

            # Takes the portfolio: the emitter reads id + current state from it.
            emit_autonomy_changed(request.user.id, portfolio)
            logger.info(
                "User %s set is_autonomous=%s on portfolio %s",
                log_safe(request.user.username),
                target,
                portfolio.id,
            )

        return Response(
            {
                "portfolio_id": portfolio.id,
                "is_autonomous": portfolio.is_autonomous,
                "changed": changed,
                "message": (
                    f"Autonomous trading {'enabled' if portfolio.is_autonomous else 'disabled'}."
                    if changed
                    else "No change - portfolio already in the requested state."
                ),
                "max_trade_budget_usd": str(portfolio.max_trade_budget_usd),
                "daily_loss_limit_usd": str(portfolio.daily_loss_limit_usd),
            },
            status=status.HTTP_200_OK,
        )


class AgentDecisionLogListView(generics.ListAPIView):
    """``GET /api/portfolio/logs/`` - historical AI decision audit trail."""

    permission_classes = [IsAuthenticated]
    serializer_class = AgentDecisionLogSerializer

    def get_queryset(self):
        portfolio = get_portfolio(self.request.user)
        queryset = (
            AgentDecisionLog.objects.filter(portfolio=portfolio)
            .select_related("transaction")
            .order_by("-created_at")
        )

        sentiment = self.request.query_params.get("sentiment")
        if sentiment:
            queryset = queryset.filter(market_sentiment=sentiment.strip().upper())

        ticker = self.request.query_params.get("ticker")
        if ticker:
            queryset = queryset.filter(transaction__ticker=ticker.strip().upper())

        executed = self.request.query_params.get("executed")
        if executed is not None:
            flag = executed.strip().lower() in {"1", "true", "yes"}
            queryset = queryset.filter(transaction__isnull=not flag)

        since = self.request.query_params.get("since")
        if since:
            queryset = queryset.filter(created_at__gte=since)

        return queryset


class TransactionListView(generics.ListAPIView):
    """``GET /api/portfolio/transactions/`` - the immutable trade ledger."""

    permission_classes = [IsAuthenticated]
    serializer_class = TransactionSerializer

    def get_queryset(self):
        portfolio = get_portfolio(self.request.user)
        queryset = Transaction.objects.filter(portfolio=portfolio).order_by("-timestamp")

        ticker = self.request.query_params.get("ticker")
        if ticker:
            queryset = queryset.filter(ticker=ticker.strip().upper())

        tx_type = self.request.query_params.get("tx_type")
        if tx_type:
            queryset = queryset.filter(tx_type=tx_type.strip().upper())

        return queryset


class RunAgentView(APIView):
    """``POST /api/portfolio/run-agent/`` - enqueue an on-demand AI sweep.

    Enqueues the same fan-out task Celery Beat uses; the HTTP request returns
    immediately with the dispatched task count.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        portfolio = get_portfolio(request.user)

        if not portfolio.is_autonomous:
            return Response(
                {
                    "error": True,
                    "status_code": status.HTTP_409_CONFLICT,
                    "detail": "Autonomous trading is disabled for this portfolio.",
                    "errors": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Dispatched inline rather than via .delay(): the fan-out only enqueues
        # subtasks, and doing it here means the response can report the truth.
        # Previously this called the *global* sweep (every autonomous portfolio,
        # spending other users' LLM budget) and returned 202 even when the
        # debounce had discarded the request entirely.
        from tasks import dispatch_market_sweep

        result = dispatch_market_sweep(portfolio.id)

        if result.get("status") == "debounced":
            logger.info(
                "On-demand sweep for portfolio %s debounced (user %s)",
                portfolio.id,
                log_safe(request.user.username),
            )
            return Response(
                {
                    "error": True,
                    "status_code": status.HTTP_429_TOO_MANY_REQUESTS,
                    "detail": (
                        "A sweep for this portfolio was requested moments ago. "
                        "Wait for it to finish before requesting another."
                    ),
                    "errors": None,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": "60"},
            )

        logger.info(
            "User %s triggered a sweep for portfolio %s (%s tasks)",
            log_safe(request.user.username),
            portfolio.id,
            result.get("dispatched", 0),
        )
        return Response(
            {
                "status": "queued",
                "portfolio_id": portfolio.id,
                "dispatched": result.get("dispatched", 0),
                "group_id": result.get("group_id"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


# ---------------------------------------------------------------------------
# Phase 2: dashboard data, human approvals, integrations
# ---------------------------------------------------------------------------
class PortfolioSnapshotListView(generics.ListAPIView):
    """``GET /api/portfolio/snapshots/`` - the equity time series.

    Returned **oldest first** so a charting client can plot it directly.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = PortfolioSnapshotSerializer
    # Deliberately unpaginated: a chart needs the whole series in one response.
    # The {count, results} envelope is kept so the shape matches every other
    # list endpoint (and the SPA's client).
    pagination_class = None

    def list(self, request: Request, *args, **kwargs) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        payload = self.get_serializer(queryset, many=True).data
        return Response({"count": len(payload), "results": payload})

    def get_queryset(self):
        portfolio = get_portfolio(self.request.user)
        hours = self.request.query_params.get("hours")
        # `hours` below is parsed defensively; `limit` was not, so a non-numeric
        # value raised ValueError and surfaced as a 500.
        try:
            limit = int(self.request.query_params.get("limit") or 2000)
        except (TypeError, ValueError):
            limit = 2000
        limit = min(max(limit, 1), 10000)

        try:
            hours_value = int(hours) if hours else None
        except (TypeError, ValueError):
            hours_value = None

        return equity_series(portfolio, hours=hours_value, limit=limit)


class TradeRecommendationListView(generics.ListAPIView):
    """``GET /api/portfolio/recommendations/`` - proposals awaiting a decision."""

    permission_classes = [IsAuthenticated]
    serializer_class = TradeRecommendationSerializer

    def get_queryset(self):
        portfolio = get_portfolio(self.request.user)
        queryset = TradeRecommendation.objects.filter(portfolio=portfolio).order_by("-created_at")
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param.strip().upper())
        return queryset


class RecommendationDecisionView(APIView):
    """``POST /api/portfolio/recommendations/<pk>/{approve,reject}/``.

    Approving does **not** bypass the execution guard: the trade is re-validated
    against live prices and current limits, and is reported as ``BLOCKED`` if it
    no longer fits.
    """

    permission_classes = [IsAuthenticated]
    decision = "approve"

    def post(self, request: Request, pk: int) -> Response:
        portfolio = get_portfolio(request.user)
        recommendation = TradeRecommendation.objects.filter(pk=pk, portfolio=portfolio).first()
        if recommendation is None:
            return Response(
                {
                    "error": True,
                    "status_code": status.HTTP_404_NOT_FOUND,
                    "detail": "Recommendation not found.",
                    "errors": None,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        if not recommendation.is_actionable:
            return Response(
                {
                    "error": True,
                    "status_code": status.HTTP_409_CONFLICT,
                    "detail": f"Recommendation is already {recommendation.status.lower()}.",
                    "errors": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            if self.decision == "approve":
                outcome = approve_recommendation(recommendation.id, via=DecidedVia.WEB)
            else:
                outcome = reject_recommendation(recommendation.id, via=DecidedVia.WEB)
        except Exception as exc:
            logger.exception("Recommendation %s decision failed: %s", pk, log_safe(exc))
            return Response(
                {
                    "error": True,
                    "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "detail": "Could not apply the decision.",
                    "errors": None,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        logger.info(
            "User %s %sd recommendation %s -> %s",
            log_safe(request.user.username),
            self.decision,
            pk,
            outcome.recommendation.status,
        )
        return Response(outcome.as_dict(), status=status.HTTP_200_OK)


class RecommendationApproveView(RecommendationDecisionView):
    decision = "approve"


class RecommendationRejectView(RecommendationDecisionView):
    decision = "reject"


class AdvisorySweepView(APIView):
    """``POST /api/portfolio/analyse/`` - run the AI in advisory mode.

    The AI analyses the watchlist and queues recommendations; nothing executes
    without an explicit approval.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        portfolio = get_portfolio(request.user)
        from tasks import advisory_sweep_task

        result = advisory_sweep_task.delay(portfolio.id)
        logger.info("User %s queued an advisory sweep", log_safe(request.user.username))
        return Response(
            {
                "status": "queued",
                "task_id": result.id,
                "portfolio_id": portfolio.id,
                "mode": "advisory",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class TelegramLinkView(APIView):
    """``GET/POST /api/telegram/link/`` - inspect or create a chat pairing."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        link = getattr(request.user, "telegram_link", None)
        if link is None or not link.is_active:
            return Response({"linked": False}, status=status.HTTP_200_OK)
        return Response(
            {"linked": True, **TelegramLinkSerializer(link).data},
            status=status.HTTP_200_OK,
        )

    def post(self, request: Request) -> Response:
        """Mint a short-lived pairing code the user sends to the bot."""
        if not telegram_enabled():
            return Response(
                {
                    "error": True,
                    "status_code": status.HTTP_503_SERVICE_UNAVAILABLE,
                    "detail": "Telegram integration is not configured on this server.",
                    "errors": None,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        link, _ = TelegramLink.objects.get_or_create(
            user=request.user, defaults={"chat_id": _placeholder_chat_id(request.user)}
        )
        link.link_code = secrets.token_hex(8).upper()
        link.link_code_issued_at = timezone.now()
        link.save(update_fields=["link_code", "link_code_issued_at"])

        # Single source of truth: the TTL the bot enforces at redemption.
        expires_at = link.link_code_issued_at + TelegramLink.LINK_CODE_TTL
        return Response(
            {
                "link_code": link.link_code,
                "expires_at": expires_at,
                "instructions": (
                    "Open Telegram, start a chat with your AlphaAgent bot and send "
                    f"/start {link.link_code}"
                ),
            },
            status=status.HTTP_200_OK,
        )


class TelegramWebhookView(APIView):
    """``POST /api/telegram/webhook/`` - inbound Telegram updates.

    Unauthenticated by design (Telegram cannot send a DRF token) but verified
    with the ``X-Telegram-Bot-Api-Secret-Token`` header, which only Telegram and
    this server know. Always returns 200 so Telegram does not retry forever.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request: Request) -> Response:
        if not telegram_enabled():
            return Response({"ok": True}, status=status.HTTP_200_OK)

        expected = str(telegram_config().get("WEBHOOK_SECRET") or "")
        if not expected:
            # Previously an unset secret skipped verification entirely, leaving
            # the endpoint accepting unsigned POSTs from anyone. An unconfigured
            # deployment should refuse the callback, not accept it blindly.
            logger.error("Telegram webhook rejected: TELEGRAM_WEBHOOK_SECRET is not configured")
            return Response(
                {
                    "error": True,
                    "status_code": status.HTTP_503_SERVICE_UNAVAILABLE,
                    "detail": "Telegram webhook is not configured on this deployment.",
                    "errors": None,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if expected:
            provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not secrets.compare_digest(provided, expected):
                logger.warning("Rejected Telegram webhook with bad secret token")
                return Response({"ok": False}, status=status.HTTP_403_FORBIDDEN)

        try:
            handle_update(request.data if isinstance(request.data, dict) else {})
        except Exception as exc:
            logger.exception("Telegram webhook handler error: %s", exc)

        return Response({"ok": True}, status=status.HTTP_200_OK)


def _placeholder_chat_id(user) -> int:
    """Negative synthetic chat id, replaced when the user pairs for real.

    ``chat_id`` is unique and non-null, so a row needs *some* value before the
    user has actually messaged the bot.
    """
    return -(user.id)


class MarketNewsView(APIView):
    """``GET /api/market/news/?ticker=AAPL`` - coverage split by sentiment.

    Powers the dashboard's positive/negative news columns. This is the same
    evidence the bull and bear agents argue from, surfaced verbatim so a human
    can audit the reasoning rather than trust the verdict.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        ticker = (request.query_params.get("ticker") or "").strip().upper()
        if not ticker:
            return Response(
                {
                    "error": True,
                    "status_code": status.HTTP_400_BAD_REQUEST,
                    "detail": "A 'ticker' query parameter is required.",
                    "errors": None,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            limit = min(max(int(request.query_params.get("limit", 10)), 1), 30)
        except (TypeError, ValueError):
            limit = 10

        from services.news import build_news_report

        report = build_news_report(ticker, limit=limit)
        payload = report.as_dict()
        payload["positive"] = [a.as_dict() for a in report.positive_articles]
        payload["negative"] = [a.as_dict() for a in report.negative_articles]
        payload["neutral"] = [a.as_dict() for a in report.neutral_articles]

        return Response(MarketNewsSerializer(payload).data, status=status.HTTP_200_OK)
