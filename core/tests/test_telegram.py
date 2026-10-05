"""Telegram bot tests.

The HTTP layer is replaced with a recording fake, so these tests exercise the
real routing, formatting and authorisation logic without touching Telegram.

The security property under test: **a chat can only ever act on its own
portfolio.** Callback data is attacker-controllable (anyone can forge a
``callback_data`` payload), so ownership is re-checked server-side on every
action rather than being trusted from the button.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from ai_agent import TradeProposal
from core.models import (
    DecidedVia,
    Portfolio,
    RecommendationStatus,
    TelegramLink,
    TradeRecommendation,
    Transaction,
)
from core.tests.helpers import TEST_CACHES, make_portfolio, make_user
from services.recommendations import create_recommendation
from telegram_bot import (
    TelegramError,
    build_balance_message,
    handle_update,
    main_menu_keyboard,
    money,
    notify_recommendation,
    recommendation_keyboard,
    signed_money,
)

TELEGRAM_TEST_CONFIG = {
    "BOT_TOKEN": "123456:TEST-TOKEN",
    "WEBHOOK_SECRET": "test-webhook-secret",
    "WEBHOOK_URL": "",
    "REQUEST_TIMEOUT": 5,
    "MAX_MESSAGE_CHARS": 4000,
    "ENABLED": True,
}


class FakeTelegramClient:
    """Records outbound calls instead of performing them."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.answers: list[dict] = []
        self.edits: list[dict] = []
        self.fail_next: str | None = None

    def send_message(self, chat_id, text, *, reply_markup=None, disable_notification=False):
        if self.fail_next == "send":
            raise TelegramError("simulated send failure")
        record = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        self.messages.append(record)
        return {"message_id": len(self.messages)}

    def answer_callback_query(self, callback_query_id, text="", *, show_alert=False):
        self.answers.append({"id": callback_query_id, "text": text, "alert": show_alert})
        return {}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {}

    @property
    def last_text(self) -> str:
        return self.messages[-1]["text"] if self.messages else ""


def message_update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "username": "tester"},
            "text": text,
        },
    }


def callback_update(chat_id: int, data: str) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": chat_id, "username": "tester"},
            "data": data,
            "message": {"message_id": 42, "chat": {"id": chat_id, "type": "private"}},
        },
    }


@override_settings(TELEGRAM_CONFIG=TELEGRAM_TEST_CONFIG, CACHES=TEST_CACHES)
class TelegramTestCase(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.client_fake = FakeTelegramClient()
        patcher = patch("telegram_bot.get_client", return_value=self.client_fake)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.chat_id = 555001
        self.user = make_user("tg_user")
        self.portfolio = make_portfolio(self.user, balance="10000.00", autonomous=False)


class FormattingTests(TestCase):
    def test_money_and_signed_money(self):
        self.assertEqual(money(Decimal("1234.5")), "$1,234.50")
        self.assertEqual(signed_money(Decimal("10")), "+$10.00")
        self.assertEqual(signed_money(Decimal("-10")), "-$10.00")
        self.assertEqual(money(None), "n/a")

    def test_keyboards_are_english_and_reference_the_recommendation(self):
        keyboard = recommendation_keyboard(7)
        labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertIn("✅ Approve Trade", labels)
        self.assertIn("❌ Reject Trade", labels)
        data = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertIn("ap:7", data)
        self.assertIn("rj:7", data)

    def test_main_menu_has_report_and_control_buttons(self):
        labels = [b["text"] for row in main_menu_keyboard()["inline_keyboard"] for b in row]
        self.assertIn("📊 Balance Report", labels)
        self.assertIn("📈 Open Positions", labels)
        self.assertIn("🧠 Latest AI Reasoning", labels)
        self.assertIn("🗂 Pending Approvals", labels)

    def test_callback_data_stays_within_the_64_byte_limit(self):
        for keyboard in (recommendation_keyboard(999999), main_menu_keyboard()):
            for row in keyboard["inline_keyboard"]:
                for button in row:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)


class LinkingTests(TelegramTestCase):
    def test_start_without_code_explains_how_to_link(self):
        handle_update(message_update(self.chat_id, "/start"))
        text = self.client_fake.last_text
        self.assertIn("Welcome to AlphaAgent", text)
        self.assertIn("/start YOUR-CODE", text)

    def test_start_with_valid_code_links_the_chat(self):
        # A code is only redeemable inside its issue window, so the fixture must
        # stamp one - an un-timestamped code is now correctly refused.
        link = TelegramLink.objects.create(
            user=self.user,
            chat_id=-self.user.id,
            link_code="ABC12345",
            link_code_issued_at=timezone.now(),
        )

        handle_update(message_update(self.chat_id, "/start ABC12345"))

        link.refresh_from_db()
        self.assertEqual(link.chat_id, self.chat_id)
        self.assertEqual(link.telegram_username, "tester")
        self.assertEqual(link.link_code, "")
        self.assertIn("Linked", self.client_fake.last_text)

    def test_invalid_code_is_rejected(self):
        handle_update(message_update(self.chat_id, "/start NOPE"))
        self.assertIn("not valid", self.client_fake.last_text)

    def test_unlinked_chat_cannot_read_the_portfolio(self):
        handle_update(message_update(self.chat_id, "/balance"))
        self.assertIn("not linked", self.client_fake.last_text.lower())

    def test_unlink_deactivates(self):
        link = TelegramLink.objects.create(user=self.user, chat_id=self.chat_id)
        handle_update(message_update(self.chat_id, "/unlink"))
        link.refresh_from_db()
        self.assertFalse(link.is_active)


class ReportTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        TelegramLink.objects.create(user=self.user, chat_id=self.chat_id)

    def test_balance_report_contains_the_headline_numbers(self):
        handle_update(message_update(self.chat_id, "/balance"))
        text = self.client_fake.last_text
        self.assertIn("Portfolio Balance", text)
        self.assertIn("$10,000.00", text)
        self.assertIn("Autopilot", text)

    def test_balance_message_escapes_html_in_reasoning(self):
        self.portfolio.balance_usd = Decimal("999.00")
        self.portfolio.save(update_fields=["balance_usd"])
        text = build_balance_message(self.portfolio)
        self.assertNotIn("<script>", text)

    def test_positions_report_with_no_holdings(self):
        handle_update(message_update(self.chat_id, "/positions"))
        self.assertIn("No open positions", self.client_fake.last_text)

    def test_thoughts_report_with_no_activity(self):
        handle_update(message_update(self.chat_id, "/thoughts"))
        self.assertIn("No agent activity", self.client_fake.last_text)

    def test_help_lists_the_commands(self):
        handle_update(message_update(self.chat_id, "/help"))
        text = self.client_fake.last_text
        self.assertIn("/balance", text)
        self.assertIn("/pending", text)

    def test_unknown_command_gets_help(self):
        handle_update(message_update(self.chat_id, "/nonsense"))
        self.assertIn("/balance", self.client_fake.last_text)

    def test_pause_and_resume_toggle_autonomy(self):
        self.portfolio.is_autonomous = True
        self.portfolio.save(update_fields=["is_autonomous"])

        handle_update(message_update(self.chat_id, "/pause"))
        self.portfolio.refresh_from_db()
        self.assertFalse(self.portfolio.is_autonomous)

        handle_update(message_update(self.chat_id, "/resume"))
        self.portfolio.refresh_from_db()
        self.assertTrue(self.portfolio.is_autonomous)


class ApprovalCallbackTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        TelegramLink.objects.create(user=self.user, chat_id=self.chat_id)
        self.reco = create_recommendation(
            self.portfolio,
            TradeProposal(action="BUY", amount=2.0, sentiment="BULLISH", reasoning="Strong."),
            Decimal("100.00"),
            ticker="AAPL",
        )

    def test_approve_button_executes_the_trade(self):
        from services.market_data import PriceQuote

        quote = PriceQuote(ticker="AAPL", symbol="AAPL", price=Decimal("100.00"))
        with patch("services.recommendations.get_latest_quote", return_value=quote):
            handle_update(callback_update(self.chat_id, f"ap:{self.reco.id}"))

        self.reco.refresh_from_db()
        self.assertEqual(self.reco.status, RecommendationStatus.EXECUTED)
        self.assertEqual(self.reco.decided_via, DecidedVia.TELEGRAM)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertTrue(self.client_fake.answers[-1]["text"])

    def test_reject_button_closes_without_trading(self):
        handle_update(callback_update(self.chat_id, f"rj:{self.reco.id}"))

        self.reco.refresh_from_db()
        self.assertEqual(self.reco.status, RecommendationStatus.REJECTED)
        self.assertEqual(self.reco.decided_via, DecidedVia.TELEGRAM)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_approving_twice_is_refused(self):
        handle_update(callback_update(self.chat_id, f"rj:{self.reco.id}"))
        self.client_fake.answers.clear()

        handle_update(callback_update(self.chat_id, f"ap:{self.reco.id}"))

        self.assertTrue(self.client_fake.answers[-1]["alert"])
        self.assertIn("already", self.client_fake.answers[-1]["text"].lower())

    def test_unknown_recommendation_id_is_handled(self):
        handle_update(callback_update(self.chat_id, "ap:999999"))
        self.assertTrue(self.client_fake.answers[-1]["alert"])

    def test_malformed_callback_data_is_handled(self):
        handle_update(callback_update(self.chat_id, "ap:not-a-number"))
        self.assertTrue(self.client_fake.answers)

    def test_why_button_returns_full_reasoning(self):
        handle_update(callback_update(self.chat_id, f"why:{self.reco.id}"))
        self.assertIn("Strong.", self.client_fake.last_text)

    def test_pending_report_lists_the_recommendation(self):
        handle_update(callback_update(self.chat_id, "report:pending"))
        joined = " ".join(m["text"] for m in self.client_fake.messages)
        self.assertIn("AAPL", joined)
        self.assertIn("Approve Trade", str(self.client_fake.messages[-1]["reply_markup"]))

    # -- authorisation ----------------------------------------------------
    def test_a_user_cannot_approve_someone_elses_recommendation(self):
        """`callback_data` is client-supplied, so ownership is re-checked here."""
        attacker = make_user("attacker")
        attacker_chat = 777002
        TelegramLink.objects.create(user=attacker, chat_id=attacker_chat)
        make_portfolio(attacker, balance="10000.00")

        handle_update(callback_update(attacker_chat, f"ap:{self.reco.id}"))

        self.reco.refresh_from_db()
        self.assertEqual(self.reco.status, RecommendationStatus.PENDING)
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertTrue(self.client_fake.answers[-1]["alert"])
        self.assertIn("not found", self.client_fake.answers[-1]["text"].lower())

    def test_a_user_cannot_read_someone_elses_reasoning(self):
        attacker = make_user("snooper")
        attacker_chat = 777003
        TelegramLink.objects.create(user=attacker, chat_id=attacker_chat)

        handle_update(callback_update(attacker_chat, f"why:{self.reco.id}"))

        self.assertNotIn("Strong.", " ".join(m["text"] for m in self.client_fake.messages))


class NotificationTests(TelegramTestCase):
    def test_notify_sends_buttons_to_the_linked_chat(self):
        TelegramLink.objects.create(user=self.user, chat_id=self.chat_id)
        reco = create_recommendation(
            self.portfolio,
            TradeProposal(action="BUY", amount=1.0, sentiment="BULLISH", reasoning="Buy."),
            Decimal("100.00"),
            ticker="AAPL",
        )

        result = notify_recommendation(reco.id)

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["chat_id"], self.chat_id)
        markup = self.client_fake.messages[-1]["reply_markup"]
        self.assertIn("Approve Trade", str(markup))

    def test_notify_skips_when_the_user_never_linked(self):
        reco = create_recommendation(
            self.portfolio,
            TradeProposal(action="BUY", amount=1.0, sentiment="BULLISH", reasoning="Buy."),
            Decimal("100.00"),
            ticker="AAPL",
        )
        self.assertEqual(notify_recommendation(reco.id)["status"], "not_linked")

    def test_notify_respects_the_opt_out(self):
        TelegramLink.objects.create(
            user=self.user, chat_id=self.chat_id, notify_recommendations=False
        )
        reco = create_recommendation(
            self.portfolio,
            TradeProposal(action="BUY", amount=1.0, sentiment="BULLISH", reasoning="Buy."),
            Decimal("100.00"),
            ticker="AAPL",
        )
        self.assertEqual(notify_recommendation(reco.id)["status"], "not_linked")

    @override_settings(TELEGRAM_CONFIG={**TELEGRAM_TEST_CONFIG, "BOT_TOKEN": ""})
    def test_notify_is_a_noop_when_the_bot_is_unconfigured(self):
        TelegramLink.objects.create(user=self.user, chat_id=self.chat_id)
        reco = create_recommendation(
            self.portfolio,
            TradeProposal(action="BUY", amount=1.0, sentiment="BULLISH", reasoning="Buy."),
            Decimal("100.00"),
            ticker="AAPL",
        )
        self.assertEqual(notify_recommendation(reco.id)["status"], "disabled")

    def test_missing_recommendation_is_reported(self):
        self.assertEqual(notify_recommendation(999999)["status"], "missing")

    def test_send_failure_is_reported_not_raised(self):
        TelegramLink.objects.create(user=self.user, chat_id=self.chat_id)
        reco = create_recommendation(
            self.portfolio,
            TradeProposal(action="BUY", amount=1.0, sentiment="BULLISH", reasoning="Buy."),
            Decimal("100.00"),
            ticker="AAPL",
        )
        self.client_fake.fail_next = "send"
        self.assertEqual(notify_recommendation(reco.id)["status"], "error")


class ResilienceTests(TelegramTestCase):
    def test_unknown_update_shapes_do_not_raise(self):
        for update in (
            {},
            {"message": {}},
            {"message": {"chat": {}, "text": ""}},
            {"callback_query": {}},
        ):
            handle_update(update)  # must not raise

    def test_internal_errors_are_swallowed(self):
        with patch("telegram_bot._cmd_balance", side_effect=RuntimeError("boom")):
            TelegramLink.objects.create(user=self.user, chat_id=self.chat_id)
            handle_update(message_update(self.chat_id, "/balance"))  # must not raise


@override_settings(TELEGRAM_CONFIG=TELEGRAM_TEST_CONFIG, CACHES=TEST_CACHES)
class WebhookViewTests(TestCase):
    def setUp(self):
        self.user = make_user("webhook_user")
        self.portfolio = make_portfolio(self.user)
        TelegramLink.objects.create(user=self.user, chat_id=4242)

    def test_webhook_rejects_a_bad_secret(self):
        response = self.client.post(
            "/api/telegram/webhook/",
            data={"update_id": 1},
            content_type="application/json",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        self.assertEqual(response.status_code, 403)

    def test_webhook_accepts_the_correct_secret(self):
        with patch("core.views.handle_update") as handler:
            response = self.client.post(
                "/api/telegram/webhook/",
                data={"update_id": 1},
                content_type="application/json",
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
            )
        self.assertEqual(response.status_code, 200)
        handler.assert_called_once()

    def test_webhook_always_acks_even_if_handling_explodes(self):
        with patch("core.views.handle_update", side_effect=RuntimeError("boom")):
            response = self.client.post(
                "/api/telegram/webhook/",
                data={"update_id": 1},
                content_type="application/json",
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
            )
        # Telegram retries forever on a non-2xx, so we always acknowledge.
        self.assertEqual(response.status_code, 200)

    def test_webhook_is_disabled_without_a_bot_token(self):
        with override_settings(TELEGRAM_CONFIG={**TELEGRAM_TEST_CONFIG, "BOT_TOKEN": ""}):
            response = self.client.post(
                "/api/telegram/webhook/",
                data={"update_id": 1},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)


@override_settings(TELEGRAM_CONFIG=TELEGRAM_TEST_CONFIG, CACHES=TEST_CACHES)
class TelegramApiTests(TestCase):
    def setUp(self):
        from core.tests.helpers import auth_client

        self.user = make_user("api_user")
        self.portfolio = make_portfolio(self.user)
        self.client = auth_client(self.user)

    def test_link_endpoint_reports_unlinked(self):
        response = self.client.get("/api/telegram/link/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["linked"])

    def test_link_code_generation(self):
        response = self.client.post("/api/telegram/link/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["link_code"])
        self.assertIn("/start", body["instructions"])

        link = TelegramLink.objects.get(user=self.user)
        self.assertEqual(link.link_code, body["link_code"])

    def test_link_endpoint_503_without_a_bot_token(self):
        with override_settings(TELEGRAM_CONFIG={**TELEGRAM_TEST_CONFIG, "BOT_TOKEN": ""}):
            response = self.client.post("/api/telegram/link/")
        self.assertEqual(response.status_code, 503)

    def test_linked_state_is_reported(self):
        TelegramLink.objects.create(
            user=self.user, chat_id=999, telegram_username="someone", is_active=True
        )
        body = self.client.get("/api/telegram/link/").json()
        self.assertTrue(body["linked"])
        self.assertEqual(body["telegram_username"], "someone")

    def test_link_endpoint_requires_authentication(self):
        from rest_framework.test import APIClient

        self.assertEqual(APIClient().get("/api/telegram/link/").status_code, 401)


@override_settings(TELEGRAM_CONFIG=TELEGRAM_TEST_CONFIG, CACHES=TEST_CACHES)
class PortfolioScopeTests(TestCase):
    """A linked chat must only ever see its own portfolio."""

    def test_chat_sees_only_its_own_portfolio(self):
        owner = make_user("owner")
        other = make_user("other")
        owner_portfolio = make_portfolio(owner, balance="111.00")
        make_portfolio(other, balance="999.00")
        TelegramLink.objects.create(user=owner, chat_id=31337)

        fake = FakeTelegramClient()
        with patch("telegram_bot.get_client", return_value=fake):
            handle_update(message_update(31337, "/balance"))

        self.assertIn("$111.00", fake.last_text)
        self.assertNotIn("$999.00", fake.last_text)
        self.assertEqual(Portfolio.objects.get(user=owner).pk, owner_portfolio.pk)

    def test_advisory_sweep_is_queued_for_the_linked_portfolio(self):
        user = make_user("sweeper")
        portfolio = make_portfolio(user)
        TelegramLink.objects.create(user=user, chat_id=8888)

        fake = FakeTelegramClient()
        with (
            patch("telegram_bot.get_client", return_value=fake),
            # Dispatched by task name, not by importing tasks: telegram_bot must
            # not depend on tasks, or the two form an import cycle.
            patch("config.celery.app.send_task") as send_task,
        ):
            handle_update(message_update(8888, "/analyse"))

        send_task.assert_called_once_with("tasks.advisory_sweep_task", args=[portfolio.id])
        self.assertIn("Advisory sweep queued", fake.last_text)

    def test_trade_recommendation_is_never_created_for_another_user(self):
        user = make_user("a")
        other = make_user("b")
        portfolio = make_portfolio(user)
        TelegramLink.objects.create(user=other, chat_id=1010)

        reco = create_recommendation(
            portfolio,
            TradeProposal(action="BUY", amount=1.0, sentiment="BULLISH", reasoning="x"),
            Decimal("50.00"),
            ticker="TSLA",
        )
        self.assertEqual(TradeRecommendation.objects.filter(portfolio=portfolio).count(), 1)
        self.assertEqual(reco.portfolio.user, user)
