"""API URL routing for AlphaAgent.

Portfolio
    GET  /api/portfolio/                       portfolio + assets + live metrics
    POST /api/portfolio/toggle-autonomy/       safe autonomous-trading switch
    GET  /api/portfolio/logs/                  AI decision audit trail
    GET  /api/portfolio/transactions/          trade ledger
    POST /api/portfolio/run-agent/             on-demand autonomous sweep
    GET  /api/portfolio/snapshots/             equity time series (dashboard chart)

Decision runs
    GET  /api/decision-runs/                   the caller's decision runs
    POST /api/decision-runs/                   idempotent portfolio analysis submission
    GET  /api/decision-runs/<id>/status/        submission status + plan link
    GET  /api/decision-runs/<id>/              metadata, stages, cost, failure
    GET  /api/decision-runs/<id>/detail/       cases, rebuttals, verdict, guard
    GET  /api/decision-runs/<id>/events/       ordered event window (after_seq)
    GET  /api/decision-runs/<id>/export/       redacted versioned JSON bundle

Portfolio plans (FIX-08)
    GET  /api/portfolio-plans/                 advisory plans for review
    GET  /api/portfolio-plans/<id>/            requested vs constrained review payload
    POST /api/portfolio-plans/<id>/approve/    approve an exact version + expiry
    POST /api/portfolio-plans/<id>/reject/     reject with a reason
    POST /api/portfolio-plans/<id>/cancel/     cancel with an audit event
    POST /api/portfolio-plans/<id>/revalidate/ re-derive after a material change
    POST /api/portfolio-plans/<id>/apply/      explicit execution through the guard

Experiments
    GET  /api/experiments/<id>/report/         versioned report, built from stored output

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

from core.allocation_views import (
    PlanApplyView,
    PlanApproveView,
    PlanCancelView,
    PlanDetailView,
    PlanListView,
    PlanRejectView,
    PlanRevalidateView,
)
from core.decision_views import (
    DecisionRunDetailView,
    DecisionRunEventsView,
    DecisionRunExportView,
    DecisionRunListView,
    DecisionRunMetadataView,
    DecisionRunStatusView,
)
from core.event_views import EventCatchUpView, EventDeliveryMetricsView
from core.experiment_views import ExperimentReportView
from core.views import (
    AdvisorySweepView,
    AgentDecisionLogListView,
    DecisionScorecardView,
    MarketNewsView,
    PortfolioDetailView,
    PortfolioSnapshotListView,
    PositionThesisView,
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
        "portfolio/thesis/",
        PositionThesisView.as_view(),
        name="portfolio-thesis",
    ),
    path(
        "portfolio/attribution/",
        DecisionScorecardView.as_view(),
        name="portfolio-attribution",
    ),
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
    # -- decision runs (Phase 1) -----------------------------------------
    path("decision-runs/", DecisionRunListView.as_view(), name="decision-run-list"),
    path(
        "decision-runs/<str:run_id>/",
        DecisionRunMetadataView.as_view(),
        name="decision-run-detail",
    ),
    path(
        "decision-runs/<str:run_id>/status/",
        DecisionRunStatusView.as_view(),
        name="decision-run-status",
    ),
    path(
        "decision-runs/<str:run_id>/detail/",
        DecisionRunDetailView.as_view(),
        name="decision-run-detail-rich",
    ),
    path(
        "decision-runs/<str:run_id>/events/",
        DecisionRunEventsView.as_view(),
        name="decision-run-events",
    ),
    path(
        "decision-runs/<str:run_id>/export/",
        DecisionRunExportView.as_view(),
        name="decision-run-export",
    ),
    # -- advisory portfolio plans (FIX-08) -------------------------------
    path("portfolio-plans/", PlanListView.as_view(), name="portfolio-plan-list"),
    path(
        "portfolio-plans/<str:plan_id>/",
        PlanDetailView.as_view(),
        name="portfolio-plan-detail",
    ),
    path(
        "portfolio-plans/<str:plan_id>/approve/",
        PlanApproveView.as_view(),
        name="portfolio-plan-approve",
    ),
    path(
        "portfolio-plans/<str:plan_id>/reject/",
        PlanRejectView.as_view(),
        name="portfolio-plan-reject",
    ),
    path(
        "portfolio-plans/<str:plan_id>/cancel/",
        PlanCancelView.as_view(),
        name="portfolio-plan-cancel",
    ),
    path(
        "portfolio-plans/<str:plan_id>/revalidate/",
        PlanRevalidateView.as_view(),
        name="portfolio-plan-revalidate",
    ),
    path(
        "portfolio-plans/<str:plan_id>/apply/",
        PlanApplyView.as_view(),
        name="portfolio-plan-apply",
    ),
    # -- durable event delivery (FIX-06) ----------------------------------
    path("events/catch-up/", EventCatchUpView.as_view(), name="event-catch-up"),
    path("events/metrics/", EventDeliveryMetricsView.as_view(), name="event-metrics"),
    # -- experiments (Phase 3) -------------------------------------------
    path(
        "experiments/<str:experiment_id>/report/",
        ExperimentReportView.as_view(),
        name="experiment-report",
    ),
    # -- market data ------------------------------------------------------
    path("market/news/", MarketNewsView.as_view(), name="market-news"),
    # -- integrations -----------------------------------------------------
    path("telegram/link/", TelegramLinkView.as_view(), name="telegram-link"),
    path("telegram/webhook/", TelegramWebhookView.as_view(), name="telegram-webhook"),
]
