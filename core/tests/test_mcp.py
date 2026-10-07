"""MCP server tests.

Two layers:

* **Protocol tests** drive the real server through the official MCP client over
  an in-memory transport, asserting the handshake, the advertised tool list and
  the JSON payloads. This is what proves an external client (Claude Code,
  Cursor, Ollama) can actually use the server.
* **Contract tests** pin the safety property that matters: no MCP tool may
  touch the database or expose anything that could move money.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.tests.helpers import TEST_CACHES, make_portfolio, make_user
from mcp_server import server as mcp_server
from services.fundamentals import FinancialHealth, PriceHistory
from services.market_data import PriceQuote
from services.news import NewsArticle, NewsReport
from services.sentiment import SentimentResult

IN_MEMORY = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def _quote(price: str = "329.40") -> PriceQuote:
    return PriceQuote(
        ticker="AAPL",
        symbol="AAPL",
        price=Decimal(price),
        previous_close=Decimal("330.22"),
        source="yfinance",
    )


def _report() -> NewsReport:
    report = NewsReport(ticker="AAPL")
    report.articles = [
        NewsArticle(
            title="Apple beats expectations",
            source="Wire",
            polarity=3.0,
            sentiment="BULLISH",
        ),
        NewsArticle(
            title="Apple faces margin pressure",
            source="Wire",
            polarity=-2.5,
            sentiment="BEARISH",
        ),
        NewsArticle(title="Apple holds event", source="Wire"),
    ]
    report.sentiment = SentimentResult(label="BULLISH", score=0.2, confidence=0.7)
    return report


def call_tool(name: str, arguments: dict) -> dict:
    """Invoke a tool through the real MCP protocol, in memory."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async def scenario():
        async with create_connected_server_and_client_session(
            mcp_server.mcp._mcp_server
        ) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            return result

    result = asyncio.run(scenario())
    assert not result.isError, f"{name} returned an error: {result.content[0].text}"
    return json.loads(result.content[0].text)


def list_tools() -> list:
    from mcp.shared.memory import create_connected_server_and_client_session

    async def scenario():
        async with create_connected_server_and_client_session(
            mcp_server.mcp._mcp_server
        ) as session:
            await session.initialize()
            return (await session.list_tools()).tools

    return asyncio.run(scenario())


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=IN_MEMORY)
class McpProtocolTests(TestCase):
    def test_server_identifies_itself(self):
        self.assertEqual(mcp_server.mcp.name, "alphaagent-market-data")
        self.assertIn("read-only", mcp_server.mcp.instructions.lower())

    def test_advertises_the_expected_tools(self):
        names = {tool.name for tool in list_tools()}
        self.assertEqual(
            names,
            {
                "get_market_price",
                "get_news_sentiment",
                "get_price_history",
                "get_financial_health",
                "get_market_snapshot",
                "list_watchlist",
            },
        )

    def test_every_tool_documents_itself(self):
        """An undocumented tool is unusable by an LLM client."""
        for tool in list_tools():
            self.assertTrue(tool.description, f"{tool.name} has no description")
            self.assertGreater(len(tool.description), 30, f"{tool.name} description too thin")

    def test_tool_schemas_declare_their_arguments(self):
        schemas = {tool.name: tool.inputSchema for tool in list_tools()}
        self.assertIn("ticker", schemas["get_market_price"]["properties"])
        self.assertIn("ticker", schemas["get_news_sentiment"]["properties"])
        self.assertIn("limit", schemas["get_news_sentiment"]["properties"])

    def test_list_watchlist_returns_configured_tickers(self):
        payload = call_tool("list_watchlist", {})
        self.assertEqual(payload["count"], len(payload["tickers"]))
        self.assertIn("AAPL", payload["tickers"])


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=IN_MEMORY)
class McpToolPayloadTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_market_price_payload(self):
        with patch("mcp_server.server.get_latest_quote", return_value=_quote()):
            payload = call_tool("get_market_price", {"ticker": "AAPL"})
        self.assertEqual(payload["price"], "329.40")
        self.assertEqual(payload["ticker"], "AAPL")

    def test_market_price_degrades_instead_of_raising(self):
        with patch("mcp_server.server.get_latest_quote", return_value=None):
            payload = call_tool("get_market_price", {"ticker": "NOPE"})
        self.assertTrue(payload["degraded"])
        self.assertIn("error", payload)

    def test_news_payload_splits_coverage(self):
        with patch("mcp_server.server.build_news_report", return_value=_report()):
            payload = call_tool("get_news_sentiment", {"ticker": "AAPL"})
        self.assertEqual(payload["headline_count"], 3)
        self.assertEqual(payload["positive_count"], 1)
        self.assertEqual(payload["negative_count"], 1)
        self.assertEqual(payload["neutral_count"], 1)
        sentiments = {a["sentiment"] for a in payload["articles"]}
        self.assertEqual(sentiments, {"BULLISH", "BEARISH", "NEUTRAL"})

    def test_news_limit_is_clamped(self):
        with patch("mcp_server.server.build_news_report", return_value=_report()) as builder:
            call_tool("get_news_sentiment", {"ticker": "AAPL", "limit": 9999})
        self.assertLessEqual(builder.call_args.kwargs["limit"], 30)

    def test_price_history_payload(self):
        history = PriceHistory(
            ticker="AAPL",
            points=[("2026-01-02", Decimal("180.00"))],
            latest=Decimal("329.40"),
            sma_50=Decimal("321.98"),
            sma_200=Decimal("288.33"),
        )
        with patch("mcp_server.server._price_history", return_value=history):
            payload = call_tool("get_price_history", {"ticker": "AAPL"})
        self.assertEqual(payload["sma_200"], "288.33")
        self.assertEqual(payload["points"], [["2026-01-02", "180.00"]])

    def test_price_history_degrades(self):
        with patch("mcp_server.server._price_history", return_value=None):
            payload = call_tool("get_price_history", {"ticker": "NOPE"})
        self.assertTrue(payload["degraded"])

    def test_financial_health_surfaces_red_flags(self):
        health = FinancialHealth(
            ticker="AAPL",
            debt_to_equity=Decimal("210.00"),
            red_flags=["High leverage"],
        )
        with patch("mcp_server.server._financial_health", return_value=health):
            payload = call_tool("get_financial_health", {"ticker": "AAPL"})
        self.assertEqual(payload["red_flags"], ["High leverage"])
        self.assertFalse(payload["degraded"])

    def test_snapshot_combines_every_section(self):
        history = PriceHistory(ticker="AAPL", latest=Decimal("329.40"))
        with (
            patch("mcp_server.server.get_latest_quote", return_value=_quote()),
            patch("mcp_server.server._price_history", return_value=history),
            patch(
                "mcp_server.server._financial_health", return_value=FinancialHealth(ticker="AAPL")
            ),
            patch("mcp_server.server.build_news_report", return_value=_report()),
        ):
            payload = call_tool("get_market_snapshot", {"ticker": "AAPL"})
        self.assertEqual(payload["price"]["price"], "329.40")
        self.assertEqual(payload["news"]["sentiment"]["label"], "BULLISH")
        self.assertFalse(payload["degraded"])


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=IN_MEMORY)
class McpIsolationTests(TestCase):
    """The MCP surface must never reach portfolio state or the ledger."""

    #: Words that would indicate a tool can read or mutate account state.
    FORBIDDEN = (
        "portfolio",
        "trade",
        "buy",
        "sell",
        "execute",
        "approve",
        "balance",
        "position",
        "transaction",
        "recommendation",
        "credential",
        "token",
    )

    def test_no_tool_name_suggests_account_access(self):
        for tool in list_tools():
            lowered = tool.name.lower()
            for word in self.FORBIDDEN:
                self.assertNotIn(word, lowered, f"{tool.name} looks like account access")

    def test_tool_descriptions_do_not_advertise_mutations(self):
        for tool in list_tools():
            text = (tool.description or "").lower()
            self.assertNotIn("place a trade", text)
            self.assertNotIn("execute", text.split("cannot")[0])

    def test_tools_do_not_query_the_database(self):
        """Calling every tool must issue no ORM queries at all."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        user = make_user("mcp_probe")
        make_portfolio(user, balance="5000.00")

        with (
            patch("mcp_server.server.get_latest_quote", return_value=_quote()),
            patch("mcp_server.server._price_history", return_value=None),
            patch("mcp_server.server._financial_health", return_value=FinancialHealth()),
            patch("mcp_server.server.build_news_report", return_value=_report()),
            CaptureQueriesContext(connection) as queries,
        ):
            call_tool("get_market_price", {"ticker": "AAPL"})
            call_tool("get_news_sentiment", {"ticker": "AAPL"})
            call_tool("get_price_history", {"ticker": "AAPL"})
            call_tool("get_financial_health", {"ticker": "AAPL"})
            call_tool("get_market_snapshot", {"ticker": "AAPL"})

        self.assertEqual(
            len(queries),
            0,
            "An MCP tool touched the database:\n"
            + "\n".join(q["sql"][:120] for q in queries.captured_queries),
        )


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=IN_MEMORY)
class McpLoggingTests(TestCase):
    """Under the stdio transport, stdout carries the protocol."""

    def test_logging_is_forced_to_stderr(self):
        import logging
        import sys

        for handler in logging.root.handlers:
            if isinstance(handler, logging.StreamHandler):
                self.assertIsNot(
                    handler.stream,
                    sys.stdout,
                    "a stdout log handler would corrupt the stdio transport",
                )

    def test_transport_aliases_resolve(self):
        self.assertEqual(mcp_server.DEFAULT_PORT, 8100)
        self.assertEqual(mcp_server.mcp.settings.streamable_http_path, "/mcp")


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=IN_MEMORY)
class McpCrewRoutingTests(TestCase):
    """The crew can source its tools from MCP instead of in-process calls."""

    def _orchestrator(self, **overrides):
        from ai_agent import AlphaAgentOrchestrator

        config = {
            "PROVIDER": "deepseek",
            "MODEL": "deepseek-reasoner",
            "API_KEY": "unused",
            "TEMPERATURE": 0.2,
            "MAX_TOKENS": 256,
            "REQUEST_TIMEOUT": 30,
            "MAX_RETRIES": 1,
            "NEWS_ARTICLE_LIMIT": 10,
            "ALLOW_HEURISTIC_FALLBACK": True,
            "TOOLS_VIA_MCP": False,
            "MCP_SERVER_URL": "",
            # Part of the config contract: the server rejects anonymous requests,
            # so the client refuses to call it without a secret rather than
            # building a reference that would fail at runtime.
            "MCP_SHARED_SECRET": "test-shared-secret",
        }
        config.update(overrides)
        return AlphaAgentOrchestrator(config)

    def _context(self):
        from ai_agent import DecisionContext

        return DecisionContext(
            ticker="AAPL",
            risk_profile="high",
            cash_balance=Decimal("10000"),
            position_amount=Decimal("0"),
            avg_purchase_price=Decimal("0"),
            current_price=Decimal("329.40"),
            max_trade_budget_usd=Decimal("500"),
            news_report=_report(),
            portfolio_id=1,
        )

    def test_disabled_by_default(self):
        self.assertEqual(self._orchestrator().mcp_servers(["get_market_price"]), [])

    def test_enabled_without_a_url_is_ignored(self):
        orchestrator = self._orchestrator(TOOLS_VIA_MCP=True, MCP_SERVER_URL="")
        self.assertEqual(orchestrator.mcp_servers(["get_market_price"]), [])

    def test_enabled_with_a_url_returns_one_filtered_reference(self):
        orchestrator = self._orchestrator(TOOLS_VIA_MCP=True, MCP_SERVER_URL="http://mcp:8100/mcp")
        refs = orchestrator.mcp_servers(["get_market_price"])
        self.assertEqual(len(refs), 1)

        predicate = refs[0].tool_filter
        # CrewAI namespaces MCP tool names, so the filter matches on the suffix.
        self.assertTrue(predicate({"name": "mcp_get_market_price"}))
        self.assertFalse(predicate({"name": "mcp_get_news_sentiment"}))

    def test_bear_agent_is_given_the_fundamentals_tool(self):
        orchestrator = self._orchestrator(TOOLS_VIA_MCP=True, MCP_SERVER_URL="http://mcp:8100/mcp")
        refs = orchestrator.mcp_servers(
            ["get_financial_health", "get_price_history", "get_news_sentiment"]
        )
        predicate = refs[0].tool_filter
        self.assertTrue(predicate({"name": "x_get_financial_health"}))
        self.assertTrue(predicate({"name": "x_get_price_history"}))
        self.assertFalse(predicate({"name": "x_get_market_price"}))

    def test_crew_uses_mcp_refs_and_no_local_tools_when_enabled(self):
        orchestrator = self._orchestrator(TOOLS_VIA_MCP=True, MCP_SERVER_URL="http://mcp:8100/mcp")
        market, news, financial, history = _tools()
        crew = orchestrator._build_crew(
            self._context(),
            {"market": market, "news": news, "financial": financial, "history": history},
        )
        self.assertEqual(len(crew.agents), 3)
        for agent in crew.agents:
            self.assertEqual(len(agent.tools), 0, f"{agent.role} kept local tools")
            self.assertEqual(len(agent.mcps or []), 1)

    def test_crew_uses_local_tools_when_disabled(self):
        orchestrator = self._orchestrator()
        market, news, financial, history = _tools()
        crew = orchestrator._build_crew(
            self._context(),
            {"market": market, "news": news, "financial": financial, "history": history},
        )
        tool_counts = [len(agent.tools) for agent in crew.agents]
        self.assertEqual(tool_counts, [2, 3, 1])
        for agent in crew.agents:
            self.assertFalse(agent.mcps)


def _tools():
    from ai_agent import _build_tools

    return _build_tools()
