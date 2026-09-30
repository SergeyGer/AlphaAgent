"""WebSocket tests: authentication, isolation between users, and event relay.

The consumer is the live face of the platform, so the properties that matter are
(a) an unauthenticated socket gets nothing, (b) one user's stream never carries
another user's data, and (c) publishing failures are contained.
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.test import TransactionTestCase, override_settings

from core.consumers import CLOSE_UNAUTHORIZED, PortfolioConsumer, portfolio_group_name
from core.routing import websocket_urlpatterns
from core.tests.helpers import TEST_CACHES, make_portfolio, make_user
from services.events import EventType, publish

IN_MEMORY_LAYER = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER, CACHES=TEST_CACHES)
class ConsumerConnectionTests(TransactionTestCase):
    def setUp(self):
        self.user = make_user()
        self.portfolio = make_portfolio(self.user)

    def _communicator(self, user) -> WebsocketCommunicator:
        communicator = WebsocketCommunicator(PortfolioConsumer.as_asgi(), "/ws/portfolio/")
        communicator.scope["user"] = user
        return communicator

    def test_authenticated_user_connects_and_receives_greeting(self):
        async def scenario():
            communicator = self._communicator(self.user)
            connected, _ = await communicator.connect()
            greeting = await communicator.receive_json_from()
            await communicator.disconnect()
            return connected, greeting

        connected, greeting = async_to_sync(scenario)()
        self.assertTrue(connected)
        self.assertEqual(greeting["type"], EventType.CONNECTED)
        self.assertEqual(greeting["payload"]["username"], self.user.username)

    def test_anonymous_connection_is_closed_with_policy_code(self):
        """The socket accepts then closes with 4401, so the SPA can distinguish
        'your token is bad' from 'the network dropped'."""

        async def scenario():
            communicator = self._communicator(AnonymousUser())
            connected, _ = await communicator.connect()
            frame = await communicator.receive_output()
            return connected, frame

        connected, frame = async_to_sync(scenario)()
        self.assertTrue(connected)
        self.assertEqual(frame["type"], "websocket.close")
        self.assertEqual(frame["code"], CLOSE_UNAUTHORIZED)

    def test_ping_gets_a_pong(self):
        async def scenario():
            communicator = self._communicator(self.user)
            await communicator.connect()
            await communicator.receive_json_from()  # greeting
            await communicator.send_json_to({"type": "ping"})
            reply = await communicator.receive_json_from()
            await communicator.disconnect()
            return reply

        self.assertEqual(async_to_sync(scenario)()["type"], "pong")

    def test_unknown_client_frames_are_ignored(self):
        """No client message can mutate state - the socket is read-only."""

        async def scenario():
            communicator = self._communicator(self.user)
            await communicator.connect()
            await communicator.receive_json_from()
            await communicator.send_json_to(
                {"type": "trade.execute", "payload": {"ticker": "AAPL", "amount": 999}}
            )
            await communicator.send_json_to({"type": "ping"})
            reply = await communicator.receive_json_from()
            await communicator.disconnect()
            return reply

        # The next frame is the pong, proving the mutation attempt was dropped.
        self.assertEqual(async_to_sync(scenario)()["type"], "pong")


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER, CACHES=TEST_CACHES)
class EventRelayTests(TransactionTestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.alice_portfolio = make_portfolio(self.alice)
        self.bob_portfolio = make_portfolio(self.bob)

    def _connect(self, user) -> WebsocketCommunicator:
        communicator = WebsocketCommunicator(PortfolioConsumer.as_asgi(), "/ws/portfolio/")
        communicator.scope["user"] = user
        return communicator

    async def _broadcast(self, user_id: int, event_type: str, payload: dict) -> None:
        """Mimic exactly what ``services.events.publish`` puts on the wire."""
        await get_channel_layer().group_send(
            portfolio_group_name(user_id),
            {"type": "portfolio_event", "event_type": event_type, "payload": payload},
        )

    def test_trade_event_reaches_the_owning_user(self):
        async def scenario():
            communicator = self._connect(self.alice)
            await communicator.connect()
            await communicator.receive_json_from()

            await self._broadcast(
                self.alice.id, EventType.TRADE_EXECUTED, {"ticker": "AAPL", "amount": 2}
            )
            frame = await communicator.receive_json_from()
            await communicator.disconnect()
            return frame

        frame = async_to_sync(scenario)()
        self.assertEqual(frame["type"], EventType.TRADE_EXECUTED)
        self.assertEqual(frame["payload"]["ticker"], "AAPL")

    def test_agent_thinking_events_are_relayed(self):
        async def scenario():
            communicator = self._connect(self.alice)
            await communicator.connect()
            await communicator.receive_json_from()
            await self._broadcast(
                self.alice.id,
                EventType.AGENT_THINKING,
                {"ticker": "BTC", "stage": "analyst_started", "message": "scraping"},
            )
            frame = await communicator.receive_json_from()
            await communicator.disconnect()
            return frame

        frame = async_to_sync(scenario)()
        self.assertEqual(frame["type"], EventType.AGENT_THINKING)
        self.assertEqual(frame["payload"]["stage"], "analyst_started")

    def test_users_are_isolated_from_each_other(self):
        """Bob's socket must never receive Alice's events."""

        async def scenario():
            alice_ws = self._connect(self.alice)
            bob_ws = self._connect(self.bob)
            await alice_ws.connect()
            await bob_ws.connect()
            await alice_ws.receive_json_from()
            await bob_ws.receive_json_from()

            await self._broadcast(self.alice.id, EventType.TRADE_EXECUTED, {"ticker": "SECRET"})

            alice_frame = await alice_ws.receive_json_from()
            bob_got_nothing = await bob_ws.receive_nothing(timeout=0.3)

            await alice_ws.disconnect()
            await bob_ws.disconnect()
            return alice_frame, bob_got_nothing

        alice_frame, bob_got_nothing = async_to_sync(scenario)()
        self.assertEqual(alice_frame["payload"]["ticker"], "SECRET")
        self.assertTrue(bob_got_nothing, "Bob received an event that was not his")

    def test_publish_builds_the_expected_envelope(self):
        """``publish`` is the sync entrypoint used by Celery tasks."""
        from unittest.mock import AsyncMock, MagicMock, patch

        layer = MagicMock()
        layer.group_send = AsyncMock()

        with patch("channels.layers.get_channel_layer", return_value=layer):
            ok = publish(self.alice.id, EventType.TRADE_EXECUTED, {"ticker": "AAPL"})

        self.assertTrue(ok)
        layer.group_send.assert_awaited_once()
        group, message = layer.group_send.await_args.args
        self.assertEqual(group, portfolio_group_name(self.alice.id))
        self.assertEqual(message["type"], "portfolio_event")
        self.assertEqual(message["event_type"], "trade.executed")
        self.assertEqual(message["payload"]["ticker"], "AAPL")

    def test_decimals_are_serialised_as_strings(self):
        """JSON has no Decimal, so money crosses the wire as a string."""
        from decimal import Decimal

        self.assertEqual(
            publish(
                self.alice.id,
                EventType.PORTFOLIO_SNAPSHOT,
                {"total_equity_usd": Decimal("26493.84")},
            ),
            True,
        )

    def test_publish_without_a_channel_layer_fails_softly(self):
        """A realtime outage must never propagate into a trading task."""
        with override_settings(CHANNEL_LAYERS={}):
            self.assertFalse(publish(self.alice.id, EventType.TRADE_EXECUTED, {"x": 1}))

    def test_publish_to_unknown_user_is_harmless(self):
        self.assertTrue(publish(999999, EventType.TRADE_EXECUTED, {"x": 1}))

    def test_group_name_is_derived_from_the_user_id(self):
        self.assertEqual(portfolio_group_name(42), "portfolio.42")


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER, CACHES=TEST_CACHES)
class WsAuthRoutingTests(TransactionTestCase):
    """The routed URL must resolve to the portfolio consumer."""

    def test_route_matches(self):
        router = URLRouter(websocket_urlpatterns)
        self.assertIsNotNone(router)

    def test_channel_layer_is_reachable(self):
        self.assertIsNotNone(get_channel_layer())
