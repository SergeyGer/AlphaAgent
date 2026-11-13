"""P0 acceptance: approval terms binding and redelivery identity.

The two defects this module exercises against the tree before the change:

1. Web and Telegram submitted only the recommendation id. The service refreshed
   the recommendation in place and incremented its terms version, so a stale
   screen executed terms the operator never saw.
2. The Celery duplicate-delivery path returned ``decision_run_id: None`` and
   bypassed the audit recovery path; after autonomy was disabled a redelivery
   did not even reach it. A legacy fill with no ``ExecutionAttempt`` stayed
   unlinked.

Every test asserts the ledger as well as the response: a refusal must write
nothing, and a replay must return the original row rather than a second one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ai_agent import AgentRunResult, TradeProposal
from core.decision_models import (
    DecisionExecutionState,
    DecisionRun,
    ExecutionAttempt,
)
from core.models import (
    AgentDecisionLog,
    AIUsageLedger,
    RecommendationApproval,
    RecommendationStatus,
    TelegramLink,
    Transaction,
)
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio, make_user
from services.market_data import PriceQuote, _fetch_from_yfinance
from services.news import NewsReport
from services.recommendation_terms import short_reference, terms_reference
from services.recommendations import (
    approve_recommendation,
    create_recommendation,
    recommendation_terms_reference,
)
from telegram_bot import handle_update

D = Decimal

TELEGRAM_TEST_CONFIG = {
    "BOT_TOKEN": "123456:TEST-TOKEN",
    "WEBHOOK_SECRET": "test-webhook-secret",
    "WEBHOOK_URL": "",
    "REQUEST_TIMEOUT": 5,
    "MAX_MESSAGE_CHARS": 4000,
    "ENABLED": True,
}


class _FakeTelegramClient:
    def __init__(self) -> None:
        self.answers: list[dict] = []
        self.edits: list[dict] = []

    def send_message(self, chat_id, text, *, reply_markup=None, disable_notification=False):
        return {"message_id": 1}

    def answer_callback_query(self, callback_query_id, text="", *, show_alert=False):
        self.answers.append({"id": callback_query_id, "text": text, "alert": show_alert})
        return {}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {}


def _callback_update(chat_id: int, data: str) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb-p0",
            "from": {"id": chat_id, "username": "tester"},
            "data": data,
            "message": {"message_id": 42, "chat": {"id": chat_id, "type": "private"}},
        },
    }


def _proposal(action: str = "BUY", amount: float = 2.0) -> TradeProposal:
    return TradeProposal(
        action=action,
        amount=amount,
        sentiment="BULLISH",
        reasoning="P0 acceptance case.",
    )


def _quote(price: str, *, as_of: datetime | None = None, source: str = "test_market_data"):
    moment = as_of or timezone.now()
    return PriceQuote(
        ticker="AAPL",
        symbol="AAPL",
        price=D(price),
        source=source,
        as_of=moment,
        retrieved_at=timezone.now(),
    )


def _run(portfolio, ticker: str = "AAPL", **kwargs) -> DecisionRun:
    kwargs.setdefault("status", "awaiting_approval")
    kwargs.setdefault("execution_state", DecisionExecutionState.AWAITING_APPROVAL)
    return DecisionRun.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        protocol_version="test.v1",
        mode="live",
        source="crewai",
        **kwargs,
    )


@override_settings(CACHES=TEST_CACHES)
class StaleApprovalTermsAcceptanceTests(TestCase):
    """1A: two sessions plus a Telegram button opened at v1 cannot approve v2."""

    def setUp(self):
        self.user = make_user("p0-1a")
        self.portfolio = make_portfolio(self.user, balance="10000.00", autonomous=False)
        self.reco = create_recommendation(
            self.portfolio,
            _proposal(),
            D("100.00"),
            ticker="AAPL",
            quote_observed_at=timezone.now(),
            price_source="test_market_data",
        )
        self.client = auth_client(self.user)
        self.client.force_authenticate(user=self.user)
        TelegramLink.objects.create(user=self.user, chat_id=555101)

    def _api_ref(self) -> tuple[int, str]:
        response = self.client.get(reverse("core:portfolio-recommendations"))
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.json()["results"] if item["id"] == self.reco.id)
        self.assertEqual(row["terms_version"], 1)
        self.assertTrue(row["terms_ref"])
        return row["terms_version"], row["terms_ref"]

    def _post(self, version, ref, key="1a"):
        return self.client.post(
            reverse("core:recommendation-approve", args=[self.reco.id]),
            {
                "terms_version": version,
                "terms_ref": ref,
                "idempotency_key": f"1a-{key}",
            },
            format="json",
        )

    def test_1a_fresh_telegram_button_executes_the_current_terms(self):
        from telegram_bot import recommendation_keyboard_for

        keyboard = recommendation_keyboard_for(self.reco)
        approve = keyboard["inline_keyboard"][0][0]["callback_data"]
        fake = _FakeTelegramClient()
        with (
            patch("telegram_bot.get_client", return_value=fake),
            patch("services.recommendations.get_latest_quote", return_value=_quote("100")),
        ):
            handle_update(_callback_update(555101, approve))
        self.reco.refresh_from_db()
        self.assertEqual(self.reco.status, RecommendationStatus.EXECUTED)
        self.assertEqual(self.reco.decided_via, "TELEGRAM")
        self.assertEqual(Transaction.objects.count(), 1)

    def test_1a_stale_web_and_telegram_actions_are_refused_and_current_version_fills(self):
        version1, ref1 = self._api_ref()

        # A price move beyond tolerance generates v2 and no fill.
        with patch("services.recommendations.get_latest_quote", return_value=_quote("120")):
            drift = self._post(version1, ref1, key="drift")
        self.assertEqual(drift.status_code, 200)
        self.assertEqual(drift.json()["reason_code"], "price_drift_requires_approval")
        self.assertEqual(drift.json()["terms_version"], 2)
        ref2 = drift.json()["revised_terms"]["terms_ref"]
        self.assertNotEqual(ref1, ref2)
        self.assertEqual(Transaction.objects.count(), 0)

        # Stale browser session 1: v1 reference is refused before any write.
        with patch("services.recommendations.get_latest_quote", return_value=_quote("120")):
            stale_web = self._post(version1, ref1, key="stale-web")
        self.assertEqual(stale_web.status_code, 409)
        self.assertEqual(stale_web.json()["reason_code"], "approval_terms_reference_stale")
        self.assertEqual(Transaction.objects.count(), 0)

        # Stale Telegram button: same old version/reference in callback data.
        fake = _FakeTelegramClient()
        with (
            patch("telegram_bot.get_client", return_value=fake),
            patch("services.recommendations.get_latest_quote", return_value=_quote("120")),
        ):
            handle_update(
                _callback_update(555101, f"ap:{self.reco.id}:v{version1}:{short_reference(ref1)}")
            )
        self.assertTrue(fake.answers[-1]["alert"])
        self.assertEqual(Transaction.objects.count(), 0)
        self.reco.refresh_from_db()
        self.assertEqual(self.reco.status, RecommendationStatus.PENDING)
        self.assertEqual(self.reco.terms_version, 2)

        # The fresh version still approves exactly once.
        with patch("services.recommendations.get_latest_quote", return_value=_quote("120")):
            fresh = self._post(2, ref2, key="fresh")
        self.assertEqual(fresh.status_code, 200)
        self.assertTrue(fresh.json()["executed"])
        self.assertEqual(Transaction.objects.count(), 1)
        self.reco.refresh_from_db()
        self.assertEqual(self.reco.status, RecommendationStatus.EXECUTED)
        self.assertEqual(self.reco.terms_version, 2)


@override_settings(CACHES=TEST_CACHES)
class MissingAndForeignTermsAcceptanceTests(TestCase):
    """1B: missing, foreign and stale versions are refused; current v fills."""

    def setUp(self):
        self.user = make_user("p0-1b")
        self.portfolio = make_portfolio(self.user, balance="10000.00", autonomous=False)
        self.other_user = make_user("p0-1b-other")
        self.other_portfolio = make_portfolio(self.other_user, balance="10000.00", autonomous=False)
        self.reco = create_recommendation(self.portfolio, _proposal(), D("100.00"), ticker="AAPL")
        self.foreign = create_recommendation(
            self.other_portfolio, _proposal(), D("50.00"), ticker="MSFT"
        )
        # Give the foreign recommendation a version this one does not have, so
        # a numeric version copied from it is observably stale here.
        self.foreign.terms_version = 2
        self.foreign.save(update_fields=["terms_version"])
        self.client = auth_client(self.user)
        self.client.force_authenticate(user=self.user)

    def _post(self, body):
        return self.client.post(
            reverse("core:recommendation-approve", args=[self.reco.id]), body, format="json"
        )

    def test_1b_missing_and_foreign_versions_are_refused_without_a_fill(self):
        missing = self._post({})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["reason_code"], "approval_terms_version_required")
        self.assertEqual(Transaction.objects.count(), 0)

        foreign_ref = self._post({"terms_ref": terms_reference(self.foreign)})
        self.assertEqual(foreign_ref.status_code, 409)
        self.assertEqual(foreign_ref.json()["reason_code"], "approval_terms_reference_stale")
        self.assertEqual(Transaction.objects.count(), 0)

        foreign_version = self._post({"terms_version": self.foreign.terms_version})
        self.assertEqual(foreign_version.status_code, 409)
        self.assertEqual(foreign_version.json()["reason_code"], "approval_terms_version_stale")
        self.assertEqual(Transaction.objects.count(), 0)

        # An in-tolerance move on the current version still approves.
        current_ref = recommendation_terms_reference(self.reco)
        with patch("services.recommendations.get_latest_quote", return_value=_quote("101")):
            accepted = self._post({"terms_version": 1, "terms_ref": current_ref})
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["executed"])
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(Transaction.objects.get().price, D("101.00"))


class ObservationProvenanceAcceptanceTests(TestCase):
    """1C: fast and daily prices keep their own observation instants."""

    def _daily_frame(self, rows):
        return pd.DataFrame(
            {"Close": [price for _, price in rows]}, index=pd.to_datetime([d for d, _ in rows])
        )

    def _fake_ticker(self, *, fast_price, previous_close, intraday, daily):
        class FakeTicker:
            def __init__(self, symbol):
                self.fast_info = SimpleNamespace(
                    last_price=fast_price, previous_close=previous_close
                )

            def history(self, period="5d", interval="1d", auto_adjust=False, prepost=False):
                if interval == "1d":
                    return daily
                return intraday

        return FakeTicker

    def test_1c_fast_price_never_inherits_the_daily_candle_close(self):
        from services.quote_policy import session_close_utc

        now = timezone.now()
        today = now.astimezone(ZoneInfo("America/New_York")).date()
        yesterday = today - timedelta(days=1)
        two_days = today - timedelta(days=2)
        intraday_at = now - timedelta(minutes=5)
        # A daily candle whose close is still in the future: unfinished.
        unfinished = today if session_close_utc(today) > now else today + timedelta(days=1)

        intraday = pd.DataFrame(
            {"Close": [120.10, 120.25]},
            index=pd.to_datetime([intraday_at - timedelta(minutes=1), intraday_at]),
        )
        # The fast price and daily close deliberately differ.
        daily = self._daily_frame([(two_days, 99.0), (yesterday, 100.0), (unfinished, 101.0)])
        fake = self._fake_ticker(
            fast_price=120.0, previous_close=110.0, intraday=intraday, daily=daily
        )
        with patch("yfinance.Ticker", fake):
            quote = _fetch_from_yfinance("AAPL")

        self.assertEqual(quote.price, D("120.25"))
        self.assertEqual(quote.source, "yfinance_intraday")
        self.assertEqual(quote.as_of, intraday_at)
        self.assertNotEqual(quote.as_of, session_close_utc(unfinished))
        self.assertEqual(quote.previous_close, D("100.00"))
        self.assertEqual(quote.previous_close_as_of, session_close_utc(yesterday))

        # Fast price only: the daily close must not stamp it.
        fake = self._fake_ticker(
            fast_price=120.0,
            previous_close=110.0,
            intraday=pd.DataFrame(),
            daily=daily,
        )
        with patch("yfinance.Ticker", fake):
            untimed = _fetch_from_yfinance("AAPL")
        self.assertEqual(untimed.price, D("120.00"))
        self.assertEqual(untimed.source, "yfinance_untimed")
        self.assertIsNone(untimed.as_of)
        self.assertEqual(untimed.previous_close_as_of, session_close_utc(yesterday))

        # Daily close selected: both prices come from the daily family, each
        # with its own session-close observation.
        fake = self._fake_ticker(
            fast_price=None,
            previous_close=None,
            intraday=pd.DataFrame(),
            daily=daily,
        )
        with patch("yfinance.Ticker", fake):
            closed = _fetch_from_yfinance("AAPL")
        self.assertEqual(closed.price, D("100.00"))
        self.assertEqual(closed.source, "yfinance_daily_close")
        self.assertEqual(closed.as_of, session_close_utc(yesterday))
        self.assertEqual(closed.previous_close, D("99.00"))
        self.assertEqual(closed.previous_close_as_of, session_close_utc(two_days))
        self.assertNotEqual(closed.as_of, session_close_utc(unfinished))

    def test_1c_the_approval_audit_records_each_observation_separately(self):
        user = make_user("p0-1c")
        portfolio = make_portfolio(user, balance="10000.00", autonomous=False)
        displayed_at = timezone.now() - timedelta(minutes=2)
        evaluated_at = timezone.now() - timedelta(minutes=1)
        reco = create_recommendation(
            portfolio,
            _proposal(),
            D("100.00"),
            ticker="AAPL",
            quote_observed_at=displayed_at,
            quote_retrieved_at=displayed_at,
            price_source="test_market_data",
        )
        ref = recommendation_terms_reference(reco)
        with patch(
            "services.recommendations.get_latest_quote",
            return_value=_quote("100.50", as_of=evaluated_at, source="yfinance_intraday"),
        ):
            outcome = approve_recommendation(
                reco.id,
                terms_version=reco.terms_version,
                terms_ref=ref,
                portfolio_id=portfolio.id,
            )
        self.assertTrue(outcome.executed, outcome.as_dict())

        approval = RecommendationApproval.objects.get(recommendation=reco, accepted=True)
        self.assertEqual(approval.quote_observed_at, displayed_at)
        self.assertEqual(approval.evaluated_quote_observed_at, evaluated_at)
        self.assertEqual(approval.evaluated_quote_source, "yfinance_intraday")
        self.assertEqual(approval.terms_ref, ref)
        attempt = ExecutionAttempt.objects.get(execution_key=f"recommendation:{reco.id}")
        self.assertEqual(attempt.approved_terms.get("terms_ref"), ref)
        self.assertEqual(Transaction.objects.get().price_observed_at, evaluated_at)
        reco.refresh_from_db()
        self.assertEqual(reco.quote_observed_at, displayed_at)


@override_settings(CACHES=TEST_CACHES)
class CeleryRedeliveryAcceptanceTests(TestCase):
    """2A/2C: a redelivered task returns its original execution."""

    def setUp(self):
        from config.celery import app as celery_app

        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", False)
        from tasks import run_alpha_agent_task

        self.task = run_alpha_agent_task
        self.user = make_user("p0-2a")
        self.portfolio = make_portfolio(self.user, balance="10000.00", autonomous=True)
        self.run = _run(self.portfolio, status="validating", execution_state="")

    def _agent_result(self) -> AgentRunResult:
        return AgentRunResult(
            proposal=_proposal(),
            reasoning="redelivery acceptance",
            source="crewai",
            decision_run_id=str(self.run.id),
        )

    def _deliver(self, task_id: str):
        with (
            patch("tasks.get_latest_quote", return_value=_quote("100")),
            patch("tasks.build_news_report", return_value=NewsReport(ticker="AAPL")),
        ):
            return self.task.apply(args=[self.portfolio.id, "AAPL"], task_id=task_id).get()

    def test_2a_redelivery_returns_the_same_fill_and_run_even_after_autonomy_is_disabled(self):
        with patch("tasks.run_alpha_agent", return_value=self._agent_result()) as agent_mock:
            first = self._deliver("p0-2a-task")
            second = self._deliver("p0-2a-task")
            self.portfolio.is_autonomous = False
            self.portfolio.save(update_fields=["is_autonomous"])
            after_disable = self._deliver("p0-2a-task")
        self.assertEqual(agent_mock.call_count, 1)

        self.assertEqual(first["transaction_id"], second["transaction_id"])
        self.assertEqual(first["transaction_id"], after_disable["transaction_id"])
        self.assertEqual(first["decision_run_id"], str(self.run.id))
        self.assertEqual(second["decision_run_id"], str(self.run.id))
        self.assertEqual(after_disable["decision_run_id"], str(self.run.id))
        self.assertTrue(after_disable["duplicate_delivery"])
        self.assertEqual(after_disable["outcome"], DecisionExecutionState.EXECUTED)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(AgentDecisionLog.objects.filter(portfolio=self.portfolio).count(), 1)
        self.assertEqual(
            ExecutionAttempt.objects.filter(execution_key="celery:p0-2a-task").count(), 1
        )
        # A redelivery returns before the budget reservation: one paid call,
        # not three.
        self.assertEqual(AIUsageLedger.objects.count(), 1)

    def test_2c_a_legacy_fill_missing_its_audit_link_is_recovered_without_a_fill_or_call(self):
        legacy_tx = Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type="BUY",
            amount=D("1"),
            price=D("100"),
            executed_by="AI",
            execution_key="celery:p0-2c-task",
            execution_request_hash="",
        )
        legacy_log = AgentDecisionLog.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            transaction=legacy_tx,
            reasoning="Legacy autonomous decision.",
            action_taken="Purchased 1 AAPL @ $100.00",
            market_sentiment="NEUTRAL",
        )
        legacy_run = _run(
            self.portfolio,
            status="completed",
            execution_state=DecisionExecutionState.EXECUTED,
            legacy_log=legacy_log,
        )
        ExecutionAttempt.objects.filter(execution_key="celery:p0-2c-task").delete()

        with patch(
            "tasks.run_alpha_agent", side_effect=AssertionError("provider called")
        ) as provider_mock:
            result = self._deliver("p0-2c-task")

        self.assertEqual(provider_mock.call_count, 0)
        self.assertEqual(result["transaction_id"], legacy_tx.id)
        self.assertEqual(result["decision_run_id"], str(legacy_run.id))
        self.assertTrue(result["duplicate_delivery"])
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(AgentDecisionLog.objects.filter(portfolio=self.portfolio).count(), 1)
        attempt = ExecutionAttempt.objects.get(execution_key="celery:p0-2c-task")
        self.assertEqual(attempt.transaction_id, legacy_tx.id)
        self.assertEqual(attempt.decision_run_id, legacy_run.id)
        self.assertTrue(attempt.recovered)


@override_settings(CACHES=TEST_CACHES)
class AuthenticatedApprovalReplayAcceptanceTests(TestCase):
    """2B: repeated authenticated approval returns the original fill and run."""

    def setUp(self):
        self.user = make_user("p0-2b")
        self.portfolio = make_portfolio(self.user, balance="10000.00", autonomous=False)
        self.run = _run(self.portfolio)
        self.reco = create_recommendation(
            self.portfolio,
            _proposal(),
            D("100.00"),
            ticker="AAPL",
            decision_run=self.run,
        )
        self.client = auth_client(self.user)
        self.client.force_authenticate(user=self.user)
        self.ref = recommendation_terms_reference(self.reco)

    def _post(self, body):
        return self.client.post(
            reverse("core:recommendation-approve", args=[self.reco.id]), body, format="json"
        )

    def test_2b_repeat_returns_the_original_fill_and_a_stale_identity_is_refused(self):
        body = {
            "terms_version": 1,
            "terms_ref": self.ref,
            "idempotency_key": "p0-2b-key",
        }
        with patch("services.recommendations.get_latest_quote", return_value=_quote("100")):
            first = self._post(body)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["executed"])
        first_transaction = first.json()["transaction_id"]
        self.assertEqual(first.json()["run_id"], str(self.run.id))
        self.assertEqual(Transaction.objects.count(), 1)

        with patch("services.recommendations.get_latest_quote", return_value=_quote("100")):
            repeat = self._post(body)
        self.assertEqual(repeat.status_code, 200)
        self.assertTrue(repeat.json()["replayed"])
        self.assertEqual(repeat.json()["transaction_id"], first_transaction)
        self.assertEqual(repeat.json()["run_id"], str(self.run.id))
        self.assertEqual(Transaction.objects.count(), 1)

        with patch("services.recommendations.get_latest_quote", return_value=_quote("100")):
            wrong_key = self._post({**body, "idempotency_key": "not-the-completed-action"})
        self.assertEqual(wrong_key.status_code, 409)
        self.assertEqual(wrong_key.json()["reason_code"], "approval_identity_mismatch")
        self.assertEqual(Transaction.objects.count(), 1)

        stale = self._post({**body, "terms_version": 999})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["reason_code"], "approval_terms_version_stale")
        self.assertEqual(Transaction.objects.count(), 1)


class TelegramTermsCarriageTests(TestCase):
    """The Telegram message and button expose and echo the displayed terms."""

    def test_message_and_keyboard_carry_the_version_and_reference(self):
        from telegram_bot import build_recommendation_message, recommendation_keyboard_for

        user = make_user("p0-tg")
        portfolio = make_portfolio(user, balance="10000.00")
        reco = create_recommendation(portfolio, _proposal(), D("100"), ticker="AAPL")
        message = build_recommendation_message(reco)
        self.assertIn("Terms: v1", message)
        keyboard = recommendation_keyboard_for(reco)
        approve = keyboard["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(approve.startswith(f"ap:{reco.id}:v1:"))
        self.assertIn(short_reference(terms_reference(reco)), approve)
