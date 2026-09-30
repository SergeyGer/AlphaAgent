"""API URL routing for AlphaAgent.

Portfolio
    GET  /api/portfolio/                       portfolio + assets + live metrics
    POST /api/portfolio/toggle-autonomy/       safe autonomous-trading switch
    GET  /api/portfolio/logs/                  AI decision audit trail
    GET  /api/portfolio/transactions/          trade ledger
    POST /api/portfolio/run-agent/             on-demand autonomous sweep
    GET  /api/portfolio/snapshots/             equity time series (dashboard chart)

Human-in-the-loop
    POST /api/portfolio/analyse/               advisory sweep (no execution)
    GET  /api/portfolio/recommendations/       proposals awaiting a decision
    POST /api/portfolio/recommendations/<id>/approve/
    POST /api/portfolio/recommendations/<id>/reject/

Integrations
    POST /api/auth/token/                      obtain an auth token
    GET  /api/telegram/link/                   current chat pairing
    POST /api/telegram/link/                   mint a pairing code
    POST /api/telegram/webhook/                inbound Telegram updates
"""

from __future__ import annotations

from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token

from core.views import (
    AdvisorySweepView,
    AgentDecisionLogListView,
    MarketNewsView,
    PortfolioDetailView,
    PortfolioSnapshotListView,
    RecommendationApproveView,
    RecommendationRejectView,
    RunAgentView,
    TelegramLinkView,
    TelegramWebhookView,
    ToggleAutonomyView,
    TradeRecommendationListView,
    TransactionListView,
)

app_name = "core"

urlpatterns = [
    path("auth/token/", obtain_auth_token, name="obtain-token"),
    # -- portfolio --------------------------------------------------------
    path("portfolio/", PortfolioDetailView.as_view(), name="portfolio-detail"),
    path(
        "portfolio/toggle-autonomy/",
        ToggleAutonomyView.as_view(),
        name="portfolio-toggle-autonomy",
    ),
    path("portfolio/logs/", AgentDecisionLogListView.as_view(), name="portfolio-logs"),
    path(
        "portfolio/transactions/",
        TransactionListView.as_view(),
        name="portfolio-transactions",
    ),
    path("portfolio/snapshots/", PortfolioSnapshotListView.as_view(), name="portfolio-snapshots"),
    path("portfolio/run-agent/", RunAgentView.as_view(), name="portfolio-run-agent"),
    # -- human in the loop ------------------------------------------------
    path("portfolio/analyse/", AdvisorySweepView.as_view(), name="portfolio-analyse"),
    path(
        "portfolio/recommendations/",
        TradeRecommendationListView.as_view(),
        name="portfolio-recommendations",
    ),
    path(
        "portfolio/recommendations/<int:pk>/approve/",
        RecommendationApproveView.as_view(),
        name="recommendation-approve",
    ),
    path(
        "portfolio/recommendations/<int:pk>/reject/",
        RecommendationRejectView.as_view(),
        name="recommendation-reject",
    ),
    # -- market data ------------------------------------------------------
    path("market/news/", MarketNewsView.as_view(), name="market-news"),
    # -- integrations -----------------------------------------------------
    path("telegram/link/", TelegramLinkView.as_view(), name="telegram-link"),
    path("telegram/webhook/", TelegramWebhookView.as_view(), name="telegram-webhook"),
]
