"""Dashboard API tests: equity series, approvals, advisory sweeps and SPA serving."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from ai_agent import TradeProposal
from core.models import (
    PortfolioSnapshot,
    RecommendationStatus,
    TelegramLink,
    TradeRecommendation,
    Transaction,
)
from core.spa import spa_asset, spa_enabled, spa_index
from core.tests.helpers import TEST_CACHES, auth_client, make_asset, make_portfolio, make_user
from services.market_data import PriceQuote
from services.recommendations import create_recommendation


@override_settings(CACHES=TEST_CACHES)
class SnapshotEndpointTests(APITestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="5000.00")
        make_asset(self.portfolio, "AAPL", "10", "100.00")
        self.client = auth_client(self.user)
        self.url = reverse("core:portfolio-snapshots")

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_an_empty_series_before_any_snapshot(self):
        body = self.client.get(self.url).json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["results"], [])

    def test_series_is_chronological_and_unpaginated(self):
        from services.snapshots import capture_snapshot

        for _ in range(3):
            capture_snapshot(self.portfolio, price_map={})

        body = self.client.get(self.url).json()
        self.assertEqual(body["count"], 3)
        self.assertNotIn("next", body)  # charting clients want a plain list
        timestamps = [row["captured_at"] for row in body["results"]]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_hours_filter_excludes_older_points(self):
        from datetime import timedelta

        from django.utils import timezone

        from services.snapshots import capture_snapshot

        old = capture_snapshot(self.portfolio, price_map={})
        PortfolioSnapshot.objects.filter(pk=old.pk).update(
            captured_at=timezone.now() - timedelta(days=10)
        )
        capture_snapshot(self.portfolio, price_map={})

        self.assertEqual(self.client.get(self.url).json()["count"], 2)
        self.assertEqual(self.client.get(self.url, {"hours": 24}).json()["count"], 1)

    def test_snapshots_are_scoped_to_the_caller(self):
        from services.snapshots import capture_snapshot

        capture_snapshot(self.portfolio, price_map={})
        other = make_portfolio(make_user("stranger"), balance="1.00")
        capture_snapshot(other, price_map={})

        self.assertEqual(self.client.get(self.url).json()["count"], 1)


@override_settings(CACHES=TEST_CACHES)
class RecommendationEndpointTests(APITestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="10000.00")
        self.client = auth_client(self.user)
        self.list_url = reverse("core:portfolio-recommendations")
        self.reco = create_recommendation(
            self.portfolio,
            TradeProposal(action="BUY", amount=2.0, sentiment="BULLISH", reasoning="Momentum."),
            Decimal("100.00"),
            ticker="AAPL",
        )

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get(self.list_url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_lists_recommendations_with_the_expected_shape(self):
        body = self.client.get(self.list_url).json()
        self.assertEqual(body["count"], 1)
        row = body["results"][0]
        self.assertEqual(row["ticker"], "AAPL")
        self.assertEqual(row["status"], RecommendationStatus.PENDING)
        self.assertEqual(row["notional_usd"], "200.00")
        self.assertTrue(row["is_actionable"])

    def test_filter_by_status(self):
        self.assertEqual(
            len(self.client.get(self.list_url, {"status": "PENDING"}).json()["results"]), 1
        )
        self.assertEqual(
            len(self.client.get(self.list_url, {"status": "EXECUTED"}).json()["results"]), 0
        )

    def test_approve_endpoint_executes(self):
        quote = PriceQuote(ticker="AAPL", symbol="AAPL", price=Decimal("100.00"))
        with patch("services.recommendations.get_latest_quote", return_value=quote):
            response = self.client.post(reverse("core:recommendation-approve", args=[self.reco.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body["executed"])
        self.assertEqual(body["status"], RecommendationStatus.EXECUTED)
        self.assertIsNotNone(body["transaction_id"])
        self.assertEqual(Transaction.objects.count(), 1)

    def test_reject_endpoint_does_not_execute(self):
        response = self.client.post(reverse("core:recommendation-reject", args=[self.reco.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["executed"])
        self.assertEqual(Transaction.objects.count(), 0)

    def test_second_decision_returns_409(self):
        self.client.post(reverse("core:recommendation-reject", args=[self.reco.id]))
        response = self.client.post(reverse("core:recommendation-approve", args=[self.reco.id]))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(response.json()["error"])

    def test_cannot_touch_another_users_recommendation(self):
        intruder = make_user("intruder")
        make_portfolio(intruder, balance="1000.00")
        intruder_client = auth_client(intruder)

        response = intruder_client.post(reverse("core:recommendation-approve", args=[self.reco.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.reco.refresh_from_db()
        self.assertEqual(self.reco.status, RecommendationStatus.PENDING)

    def test_unknown_recommendation_returns_404(self):
        response = self.client.post(reverse("core:recommendation-approve", args=[999999]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_approval_failure_is_reported_as_500_not_a_crash(self):
        with patch("core.views.approve_recommendation", side_effect=RuntimeError("boom")):
            response = self.client.post(reverse("core:recommendation-approve", args=[self.reco.id]))
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertTrue(response.json()["error"])


@override_settings(CACHES=TEST_CACHES)
class AdvisorySweepEndpointTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.portfolio = make_portfolio(self.user)
        self.client = auth_client(self.user)

    def test_requires_authentication(self):
        self.assertEqual(
            APIClient().post(reverse("core:portfolio-analyse")).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_queues_an_advisory_sweep(self):
        with patch("tasks.advisory_sweep_task.delay") as delay:
            delay.return_value.id = "task-123"
            response = self.client.post(reverse("core:portfolio-analyse"))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        body = response.json()
        self.assertEqual(body["mode"], "advisory")
        self.assertEqual(body["portfolio_id"], self.portfolio.id)
        delay.assert_called_once_with(self.portfolio.id)


@override_settings(CACHES=TEST_CACHES)
class AdvisorySweepTaskTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        from config.celery import app as celery_app

        cache.clear()  # the sweep debounce is process-global
        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", False)

    def test_dispatches_one_advisory_subtask_per_ticker(self):
        from tasks import advisory_sweep_task

        portfolio = make_portfolio(balance="1000.00")
        make_asset(portfolio, "AAPL", "1", "100.00")

        with patch("tasks.group") as group_mock:
            group_mock.return_value.apply_async.return_value.id = "gid"
            result = advisory_sweep_task.apply(args=[portfolio.id]).get()

        signatures = group_mock.call_args[0][0]
        self.assertEqual([sig.args[1] for sig in signatures], ["AAPL", "TSLA", "BTC"])
        # Every subtask must be dispatched in advisory mode.
        self.assertTrue(all(sig.args[2] is True for sig in signatures))
        self.assertEqual(result["dispatched"], 3)

    def test_missing_portfolio_is_handled(self):
        from tasks import advisory_sweep_task

        result = advisory_sweep_task.apply(args=[999999]).get()
        self.assertEqual(result["status"], "skipped")


class SpaServingTests(TestCase):
    """Unit tests for the SPA server (routes are registered at import time)."""

    def test_disabled_when_there_is_no_build(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            override_settings(SPA_DIST_DIR=Path(tmp) / "dist"),
        ):
            self.assertFalse(spa_enabled())

    def test_enabled_when_index_html_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<html>ok</html>")
            with override_settings(SPA_DIST_DIR=dist):
                self.assertTrue(spa_enabled())

    def test_index_is_served_with_no_cache(self):
        from django.test import RequestFactory

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<html>shell</html>")
            with override_settings(SPA_DIST_DIR=dist):
                response = spa_index(RequestFactory().get("/"))
                self.assertEqual(response.status_code, 200)
                # A cached shell would reference a stale hashed bundle.
                self.assertIn("no-cache", response["Cache-Control"])

    def test_missing_build_raises_404(self):
        from django.http import Http404
        from django.test import RequestFactory

        with (
            tempfile.TemporaryDirectory() as tmp,
            override_settings(SPA_DIST_DIR=Path(tmp) / "nope"),
            self.assertRaises(Http404),
        ):
            spa_index(RequestFactory().get("/"))

    def test_path_traversal_is_blocked(self):
        from django.http import Http404
        from django.test import RequestFactory

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            (dist / "assets").mkdir(parents=True)
            (dist / "assets" / "app.js").write_text("console.log(1)")
            secret = Path(tmp) / "secret.txt"
            secret.write_text("top secret")

            with override_settings(SPA_DIST_DIR=dist):
                self.assertEqual(spa_asset(RequestFactory().get("/"), "app.js").status_code, 200)
                with self.assertRaises(Http404):
                    spa_asset(RequestFactory().get("/"), "../secret.txt")


@override_settings(CACHES=TEST_CACHES)
class RealApiRoutePrecedenceTests(APITestCase):
    """The SPA catch-all must never shadow the API."""

    def test_api_returns_401_not_the_spa_shell(self):
        response = self.client.get("/api/portfolio/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unknown_api_route_is_not_the_spa(self):
        response = self.client.get("/api/does-not-exist/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_healthz_is_unaffected(self):
        self.assertEqual(self.client.get("/healthz/").json()["status"], "ok")


@override_settings(CACHES=TEST_CACHES)
class TelegramModelTests(TestCase):
    def test_link_is_one_per_user(self):
        from django.db import IntegrityError, transaction

        user = make_user()
        TelegramLink.objects.create(user=user, chat_id=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TelegramLink.objects.create(user=user, chat_id=2)

    def test_chat_id_is_unique(self):
        from django.db import IntegrityError, transaction

        TelegramLink.objects.create(user=make_user("a"), chat_id=500)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TelegramLink.objects.create(user=make_user("b"), chat_id=500)

    def test_recommendation_status_defaults_to_pending(self):
        portfolio = make_portfolio(balance="100.00")
        reco = TradeRecommendation.objects.create(
            portfolio=portfolio,
            ticker="AAPL",
            action="BUY",
            amount=Decimal("1"),
            price=Decimal("10.00"),
            notional_usd=Decimal("10.00"),
        )
        self.assertEqual(reco.status, RecommendationStatus.PENDING)
        self.assertIsNone(reco.transaction)
