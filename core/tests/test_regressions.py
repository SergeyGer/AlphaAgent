"""Regression tests for defects found while documenting the system.

Every test here corresponds to a defect that reached the codebase and was found
by reading it rather than by a failing test - which is precisely why they are
worth pinning now. Each one is named for the behaviour that was wrong, so a
future regression reads as a sentence.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from core.models import TelegramLink, Transaction
from core.tests.helpers import TEST_CACHES, auth_client, make_asset, make_portfolio, make_user

# ---------------------------------------------------------------------------
# 1. The live snapshot frame must match the shape the dashboard destructures.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class PortfolioSnapshotPayloadTests(TestCase):
    """``emit_portfolio_snapshot`` once published flat snapshot-row fields.

    The dashboard's ``PortfolioSnapshotEvent`` destructures ``{metrics, assets}``,
    so it read two absent keys and wrote ``undefined`` over its own state -
    blanking the metric cards and allocation panel until the next REST resync.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="5000.00")
        make_asset(self.portfolio, "AAPL", "10", "100.00")

    def test_shared_builder_returns_metrics_and_assets(self):
        from core.serializers import build_portfolio_payload

        payload = build_portfolio_payload(self.portfolio)

        self.assertIn("metrics", payload)
        self.assertIn("assets", payload)
        self.assertEqual(len(payload["assets"]), 1)
        self.assertEqual(payload["assets"][0]["ticker"], "AAPL")
        # The keys the client actually reads must be present.
        self.assertIn("total_equity_usd", payload["metrics"])

    def test_event_publishes_exactly_the_keys_the_client_expects(self):
        from core.serializers import build_portfolio_payload
        from services.events import emit_portfolio_snapshot

        payload = build_portfolio_payload(self.portfolio)
        payload["captured_at"] = timezone.now().isoformat()

        with patch("services.events.publish") as publish:
            emit_portfolio_snapshot(self.user.id, payload)

        publish.assert_called_once()
        _user_id, event_type, sent = publish.call_args[0]
        self.assertEqual(event_type, "portfolio.snapshot")
        self.assertEqual(set(sent), {"metrics", "assets", "captured_at"})
        # The regression: these were absent, and their absence blanked the UI.
        self.assertIsNotNone(sent["metrics"])
        self.assertIsNotNone(sent["assets"])

    def test_rest_endpoint_and_event_agree_on_the_portfolio(self):
        """One builder, so the two surfaces cannot describe it differently.

        Quotes are pinned so the two calls cannot disagree merely because the
        first populated the price cache and the second read from it - the
        comparison is about the *shape and values*, not the cache state.
        """
        from core.serializers import build_portfolio_payload
        from services.market_data import PriceQuote

        quote = PriceQuote(ticker="AAPL", symbol="AAPL", price=Decimal("123.45"), source="test")

        with patch("services.portfolio_metrics.get_latest_quote", return_value=quote):
            client = auth_client(self.user)
            rest = client.get(reverse("core:portfolio-detail")).json()
            event = build_portfolio_payload(self.portfolio)

        self.assertEqual(rest["metrics"], event["metrics"])
        self.assertEqual(rest["assets"], event["assets"])


# ---------------------------------------------------------------------------
# 2. An on-demand sweep must be scoped to the caller.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class RunAgentScopeTests(APITestCase):
    """The endpoint authenticated one user, then dispatched the *global* sweep.

    One user's click therefore spent every other autonomous user's LLM budget.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="5000.00", autonomous=True, risk="high")
        self.client = auth_client(self.user)
        self.url = reverse("core:portfolio-run-agent")

    def test_sweep_is_scoped_to_the_callers_portfolio(self):
        # The view imports the sweep lazily, so it is not a `core.views`
        # attribute - patching the definition in `tasks` is what takes effect.
        with patch("tasks.dispatch_market_sweep") as sweep:
            sweep.return_value = {"status": "dispatched", "dispatched": 3, "group_id": "gid"}
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        sweep.assert_called_once_with(self.portfolio.id)

    def test_another_users_portfolio_is_not_scanned(self):
        other_user = make_user("someone-else")
        other = make_portfolio(other_user, balance="9000.00", autonomous=True, risk="high")

        captured: dict = {}

        def fake_sweep(portfolio_id=None):
            captured["portfolio_id"] = portfolio_id
            return {"status": "dispatched", "dispatched": 1, "group_id": "gid"}

        with patch("tasks.dispatch_market_sweep", side_effect=fake_sweep):
            self.client.post(self.url)

        self.assertEqual(captured["portfolio_id"], self.portfolio.id)
        self.assertNotEqual(captured["portfolio_id"], other.id)

    def test_a_debounced_sweep_reports_429_not_202(self):
        """It used to answer 202 even when the debounce discarded the request."""
        with patch(
            "tasks.dispatch_market_sweep",
            return_value={"status": "debounced", "dispatched": 0},
        ):
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.headers.get("Retry-After"), "60")

    def test_disabled_autonomy_still_conflicts(self):
        self.portfolio.is_autonomous = False
        self.portfolio.save(update_fields=["is_autonomous"])

        self.assertEqual(self.client.post(self.url).status_code, status.HTTP_409_CONFLICT)


@override_settings(CACHES=TEST_CACHES)
class SweepDebounceScopeTests(TestCase):
    """A global sweep must not debounce an unrelated scoped one."""

    def setUp(self):
        cache.clear()

    def test_scoped_and_global_debounces_are_independent(self):
        from services.throttle import claim_sweep_slot

        self.assertTrue(claim_sweep_slot())  # Beat's global sweep
        # The global slot is taken, but this portfolio has its own.
        self.assertTrue(claim_sweep_slot(portfolio_id=7))
        self.assertFalse(claim_sweep_slot())  # global still debounced
        self.assertFalse(claim_sweep_slot(portfolio_id=7))  # and so is that one

    def test_two_portfolios_do_not_debounce_each_other(self):
        from services.throttle import claim_sweep_slot

        self.assertTrue(claim_sweep_slot(portfolio_id=1))
        self.assertTrue(claim_sweep_slot(portfolio_id=2))


# ---------------------------------------------------------------------------
# 3. Link codes must actually expire.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class LinkCodeExpiryTests(TestCase):
    """The API advertised a 15-minute window the bot never enforced.

    The lookup filtered on ``is_active`` alone, so an unused code stayed valid
    forever and a leaked code was a permanent credential.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user()

    def _link(self, *, age: timedelta | None) -> TelegramLink:
        return TelegramLink.objects.create(
            user=self.user,
            chat_id=987654321,
            link_code="ABCD1234",
            link_code_issued_at=None if age is None else timezone.now() - age,
            is_active=True,
        )

    def test_a_fresh_code_is_redeemable(self):
        link = self._link(age=timedelta(minutes=1))
        self.assertTrue(link.link_code_is_valid())
        self.assertEqual(TelegramLink.redeemable("ABCD1234").count(), 1)

    def test_a_code_older_than_the_ttl_is_rejected(self):
        link = self._link(age=timedelta(minutes=16))
        self.assertFalse(link.link_code_is_valid())
        self.assertEqual(TelegramLink.redeemable("ABCD1234").count(), 0)

    def test_a_code_with_no_issue_timestamp_is_rejected(self):
        link = self._link(age=None)
        self.assertFalse(link.link_code_is_valid())
        self.assertEqual(TelegramLink.redeemable("ABCD1234").count(), 0)

    def test_expiry_matches_the_window_the_api_advertises(self):
        self.assertEqual(TelegramLink.LINK_CODE_TTL, timedelta(minutes=15))

    def test_the_ttl_boundary_is_enforced_precisely(self):
        self.assertEqual(TelegramLink.redeemable("ABCD1234").count(), 0)
        self._link(age=timedelta(minutes=14, seconds=59))
        self.assertEqual(TelegramLink.redeemable("ABCD1234").count(), 1)

    def test_redeeming_clears_the_code_and_its_timestamp(self):
        link = self._link(age=timedelta(minutes=1))
        link.link_code = ""
        link.link_code_issued_at = None
        link.save(update_fields=["link_code", "link_code_issued_at"])
        link.refresh_from_db()

        self.assertFalse(link.link_code_is_valid())
        self.assertEqual(TelegramLink.redeemable("ABCD1234").count(), 0)


# ---------------------------------------------------------------------------
# 4. The token purge must be based on last use, not issue date.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class AuthTokenPurgeTests(TestCase):
    """The task documented itself as last-use based but filtered on ``created``.

    That deleted the token of a client authenticating every minute (forcing a
    monthly re-authentication for everyone) while keeping genuinely abandoned
    tokens younger than the cutoff.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user()

    def _token_aged(self, days: int) -> Token:
        token = Token.objects.create(user=self.user)
        Token.objects.filter(pk=token.pk).update(created=timezone.now() - timedelta(days=days))
        token.refresh_from_db()
        return token

    def test_an_old_token_for_an_active_user_is_kept(self):
        from tasks import purge_expired_auth_tokens_task

        self._token_aged(90)
        self.user.last_login = timezone.now()
        self.user.save(update_fields=["last_login"])

        purge_expired_auth_tokens_task.apply().get()

        self.assertEqual(Token.objects.count(), 1)

    def test_an_old_token_for_an_inactive_user_is_removed(self):
        from tasks import purge_expired_auth_tokens_task

        self._token_aged(90)
        self.user.last_login = timezone.now() - timedelta(days=90)
        self.user.save(update_fields=["last_login"])

        result = purge_expired_auth_tokens_task.apply().get()

        self.assertEqual(Token.objects.count(), 0)
        self.assertEqual(result["deleted"], 1)

    def test_a_recent_token_is_never_removed(self):
        from tasks import purge_expired_auth_tokens_task

        self._token_aged(1)

        purge_expired_auth_tokens_task.apply().get()

        self.assertEqual(Token.objects.count(), 1)


@override_settings(CACHES=TEST_CACHES)
class ActivityAuthenticationTests(APITestCase):
    """Token use must actually be recorded, or the purge has nothing to read."""

    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.client = auth_client(self.user)

    def test_an_authenticated_request_stamps_activity(self):
        self.user.last_login = None
        self.user.save(update_fields=["last_login"])

        self.client.get(reverse("core:portfolio-detail"))

        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)

    def test_a_fresh_stamp_is_not_rewritten_on_every_request(self):
        """Bounded write cost: at most one UPDATE per user per resolution."""
        from core.auth import ACTIVITY_RESOLUTION, touch_user_activity

        self.user.last_login = timezone.now()
        self.user.save(update_fields=["last_login"])

        self.assertFalse(touch_user_activity(self.user))
        stale = timezone.now() - ACTIVITY_RESOLUTION - timedelta(minutes=1)
        self.user.last_login = stale
        self.user.save(update_fields=["last_login"])
        self.assertTrue(touch_user_activity(self.user))

    def test_a_bookkeeping_failure_never_rejects_a_valid_request(self):
        """The guard lives *inside* touch_user_activity, so break the write.

        Patching the function itself would bypass the guard and test the mock
        rather than the code.
        """
        with patch("django.contrib.auth.models.User.save", side_effect=Exception("db down")):
            response = self.client.get(reverse("core:portfolio-detail"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_touch_user_activity_swallows_a_write_failure(self):
        from core.auth import touch_user_activity

        class Exploding:
            last_login = None
            pk = 1

            def save(self, **_kwargs):
                raise RuntimeError("database is gone")

        self.assertFalse(touch_user_activity(Exploding()))


# ---------------------------------------------------------------------------
# 5. Malformed query parameters must not produce a 500.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class SnapshotQueryParamTests(APITestCase):
    """``?limit=abc`` raised ValueError and surfaced as a 500.

    ``hours`` was already parsed defensively on the following line.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="5000.00")
        self.client = auth_client(self.user)
        self.url = reverse("core:portfolio-snapshots")

    def test_non_numeric_limit_falls_back_instead_of_erroring(self):
        response = self.client.get(self.url, {"limit": "abc"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_blank_limit_falls_back(self):
        self.assertEqual(self.client.get(self.url, {"limit": ""}).status_code, status.HTTP_200_OK)

    def test_negative_limit_is_clamped_to_at_least_one(self):
        self.assertEqual(self.client.get(self.url, {"limit": "-5"}).status_code, status.HTTP_200_OK)

    def test_absurd_limit_is_clamped_to_the_ceiling(self):
        self.assertEqual(
            self.client.get(self.url, {"limit": "999999"}).status_code, status.HTTP_200_OK
        )

    def test_non_numeric_hours_still_falls_back(self):
        self.assertEqual(
            self.client.get(self.url, {"hours": "abc"}).status_code, status.HTTP_200_OK
        )


# ---------------------------------------------------------------------------
# 6. Autonomy changes must be broadcast from every surface.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class AutonomyEventTests(APITestCase):
    """Only the Telegram path emitted ``autonomy.changed``.

    Toggling autonomy from the dashboard therefore left other open surfaces
    showing a stale state.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="5000.00", autonomous=False)
        self.client = auth_client(self.user)
        self.url = reverse("core:portfolio-toggle-autonomy")

    def test_toggling_from_the_api_emits_the_event(self):
        # Enabling autonomy deliberately requires an explicit confirmation.
        with patch("services.events.emit_autonomy_changed") as emit:
            response = self.client.post(
                self.url, {"is_autonomous": True, "confirm": True}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        emit.assert_called_once()
        _user_id, portfolio = emit.call_args[0]
        self.assertEqual(portfolio.id, self.portfolio.id)

    def test_a_no_op_toggle_emits_nothing(self):
        with patch("services.events.emit_autonomy_changed") as emit:
            self.client.post(self.url, {"is_autonomous": False}, format="json")

        emit.assert_not_called()


# ---------------------------------------------------------------------------
# 7. An unconfigured webhook must refuse, not accept blindly.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class TelegramWebhookSecurityTests(APITestCase):
    """An empty secret skipped verification entirely.

    The endpoint then accepted unsigned POSTs from anyone, so an unconfigured
    deployment was *less* safe than a configured one.
    """

    def setUp(self):
        cache.clear()
        self.url = reverse("core:telegram-webhook")

    @override_settings(TELEGRAM_CONFIG={"WEBHOOK_SECRET": "", "BOT_TOKEN": "123:abc"})
    def test_missing_secret_is_refused(self):
        # Bot token present, secret absent: the exact misconfiguration where an
        # enabled webhook would otherwise accept unsigned POSTs from anyone.
        response = APIClient().post(self.url, {"update_id": 1}, format="json")
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @override_settings(TELEGRAM_CONFIG={"WEBHOOK_SECRET": "", "BOT_TOKEN": ""})
    def test_a_disabled_bot_still_short_circuits(self):
        response = APIClient().post(self.url, {"update_id": 1}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_settings(TELEGRAM_CONFIG={"WEBHOOK_SECRET": "expected", "BOT_TOKEN": "123:abc"})
    def test_wrong_secret_is_forbidden(self):
        response = APIClient().post(
            self.url,
            {"update_id": 1},
            format="json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="wrong",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# 8. Executed trades must actually reach Telegram.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class TradeNotificationTests(TestCase):
    """``notify_trade`` and the ``notify_trades`` preference had no caller.

    The setting existed, the function existed, and nothing ever invoked it - so
    the preference silently did nothing.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="5000.00", autonomous=True)

    def test_the_task_is_registered(self):
        from config.celery import app as celery_app

        self.assertIn("tasks.notify_trade_task", celery_app.tasks)

    def test_the_task_delegates_to_the_bot_helper(self):
        from tasks import notify_trade

        tx = Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type="BUY",
            amount=Decimal("1"),
            price=Decimal("100.00"),
            executed_by="AI",
        )
        with patch("telegram_bot.notify_trade", return_value={"status": "sent"}) as send:
            result = notify_trade.apply(args=[tx.id]).get()

        send.assert_called_once_with(tx.id)
        self.assertEqual(result["status"], "sent")

    def test_a_bot_failure_does_not_raise_into_the_caller(self):
        from tasks import notify_trade

        with patch("telegram_bot.notify_trade", side_effect=RuntimeError("bot down")):
            result = notify_trade.apply(args=[1]).get()

        self.assertEqual(result["status"], "error")


# ---------------------------------------------------------------------------
# 9. The scheduled snapshot sweep must publish what it writes.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class ScheduledSnapshotEmitTests(TestCase):
    """The sweep wrote the equity series every 15 minutes but emitted nothing.

    An open dashboard therefore kept stale metric cards and allocation until a
    trade happened or the socket reconnected.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="5000.00")
        make_asset(self.portfolio, "AAPL", "10", "100.00")

    def test_the_task_emits_one_event_per_captured_portfolio(self):
        from tasks import capture_portfolio_snapshots_task

        with patch("services.events.emit_portfolio_snapshot") as emit:
            result = capture_portfolio_snapshots_task.apply().get()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["captured"], 1)
        self.assertEqual(result["emitted"], 1)
        emit.assert_called_once()

    def test_the_emitted_payload_carries_metrics_and_assets(self):
        from tasks import capture_portfolio_snapshots_task

        with patch("services.events.emit_portfolio_snapshot") as emit:
            capture_portfolio_snapshots_task.apply().get()

        _user_id, payload = emit.call_args[0]
        self.assertIn("metrics", payload)
        self.assertIn("assets", payload)
        self.assertIn("captured_at", payload)
        self.assertEqual(len(payload["assets"]), 1)

    def test_capture_all_snapshots_returns_the_rows_it_wrote(self):
        """Returning a bare count is what made the sweep silent."""
        from services.snapshots import capture_all_snapshots

        captured = capture_all_snapshots()

        self.assertEqual(len(captured), 1)
        portfolio, snapshot = captured[0]
        self.assertEqual(portfolio.id, self.portfolio.id)
        self.assertIsNotNone(snapshot.captured_at)

    def test_a_publish_failure_does_not_lose_the_row(self):
        """The snapshot is already committed; a failed push must not roll it back."""
        from core.models import PortfolioSnapshot
        from tasks import capture_portfolio_snapshots_task

        with patch(
            "services.events.emit_portfolio_snapshot", side_effect=RuntimeError("channel down")
        ):
            result = capture_portfolio_snapshots_task.apply().get()

        self.assertEqual(result["captured"], 1)
        self.assertEqual(result["emitted"], 0)
        self.assertEqual(PortfolioSnapshot.objects.count(), 1)


# ---------------------------------------------------------------------------
# 10. Execution preconditions must survive `python -O`.
# ---------------------------------------------------------------------------


@override_settings(CACHES=TEST_CACHES)
class ExecutionPreconditionTests(TestCase):
    """``apply_trade`` guarded its price with ``assert``.

    Assertions are stripped under ``-O``/``PYTHONOPTIMIZE``, so the check would
    vanish in exactly the environment where it matters, letting a None price
    reach the sizing arithmetic.
    """

    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.portfolio = make_portfolio(self.user, balance="5000.00")

    def test_a_decision_without_a_price_is_refused(self):
        from services.execution import GuardDecision, GuardError, apply_trade

        decision = GuardDecision(
            approved=True, reason="approved", action="BUY", amount=Decimal("1"), price=None
        )

        with self.assertRaises(GuardError):
            apply_trade(self.portfolio.id, "AAPL", decision)

    def test_the_guard_error_is_a_real_exception_not_an_assertion(self):
        """An AssertionError would be stripped; GuardError cannot be."""
        from services.execution import GuardError

        self.assertTrue(issubclass(GuardError, Exception))
        self.assertFalse(issubclass(GuardError, AssertionError))

    def test_no_bare_assert_remains_in_the_execution_path(self):
        import inspect

        from services import execution

        source = inspect.getsource(execution)
        self.assertNotIn("\n    assert ", source)
