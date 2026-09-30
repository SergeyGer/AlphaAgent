"""AI layer tests: payload contract, parsing, sizing rules and cost accounting."""

from __future__ import annotations

import inspect
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, get_args, get_origin
from unittest.mock import patch

from django.test import TestCase, override_settings

from ai_agent import (
    AlphaAgentOrchestrator,
    DecisionContext,
    HeuristicDecisionEngine,
    TradeProposal,
    _build_tools,
    _proposal_guardrail,
    estimate_cost,
    parse_proposal,
)
from services.news import NewsReport
from services.sentiment import SentimentResult

# A configured-but-offline LLM profile. Constructing ``crewai.LLM`` performs no
# network I/O, so the crew can be built for real in tests.
CREW_CONFIG = {
    "PROVIDER": "deepseek",
    "MODEL": "deepseek-reasoner",
    "API_KEY": "test-key-never-used",
    "BASE_URL": "",
    "TEMPERATURE": 0.2,
    "MAX_TOKENS": 256,
    "REQUEST_TIMEOUT": 30,
    "MAX_RETRIES": 1,
    "ALLOW_HEURISTIC_FALLBACK": True,
    "NEWS_ARTICLE_LIMIT": 10,
    "PRICE_CACHE_TTL": 60,
    "NEWS_CACHE_TTL": 900,
}


class TradeProposalContractTests(TestCase):
    """The guardrail payload must be exactly what the spec mandates."""

    def test_valid_payload_keys(self):
        payload = TradeProposal(
            action="BUY", amount=0.5, sentiment="BULLISH", reasoning="strong demand"
        ).to_payload()
        self.assertEqual(set(payload.keys()), {"action", "amount", "sentiment", "reasoning"})

    def test_lowercase_is_normalised(self):
        parsed = TradeProposal(action="buy", amount=1, sentiment="bullish", reasoning="x")
        self.assertEqual(parsed.action, "BUY")
        self.assertEqual(parsed.sentiment, "BULLISH")

    def test_invalid_action_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            TradeProposal(action="YOLO", amount=1, sentiment="BULLISH", reasoning="x")

    def test_invalid_sentiment_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            TradeProposal(action="BUY", amount=1, sentiment="EXCITED", reasoning="x")

    def test_negative_amount_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            TradeProposal(action="BUY", amount=-1, sentiment="BULLISH", reasoning="x")

    def test_extra_keys_are_ignored(self):
        parsed = TradeProposal.model_validate(
            {
                "action": "HOLD",
                "amount": 0,
                "sentiment": "NEUTRAL",
                "reasoning": "wait",
                "confidence": 0.9,
                "ticker": "AAPL",
            }
        )
        self.assertEqual(parsed.action, "HOLD")


class ProposalParsingTests(TestCase):
    def test_parses_plain_json(self):
        parsed = parse_proposal(
            '{"action": "SELL", "amount": 2.5, "sentiment": "BEARISH", "reasoning": "downgrade"}'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.action, "SELL")
        self.assertEqual(parsed.amount, 2.5)

    def test_parses_markdown_fenced_json(self):
        raw = 'Here is my decision:\n```json\n{"action": "BUY", "amount": 1, "sentiment": "BULLISH", "reasoning": "beat"}\n```'
        parsed = parse_proposal(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.action, "BUY")

    def test_parses_json_embedded_in_prose(self):
        raw = 'Analysis done. {"action": "HOLD", "amount": 0, "sentiment": "NEUTRAL", "reasoning": "flat"} End.'
        parsed = parse_proposal(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.action, "HOLD")

    def test_unparseable_output_returns_none(self):
        self.assertIsNone(parse_proposal("I think we should probably buy some stock."))
        self.assertIsNone(parse_proposal(None))
        self.assertIsNone(parse_proposal(""))


class HeuristicEngineTests(TestCase):
    """The deterministic fallback must respect risk profile and guardrails."""

    def _context(self, **overrides) -> DecisionContext:
        defaults = dict(
            ticker="AAPL",
            risk_profile="medium",
            cash_balance=Decimal("10000.00"),
            position_amount=Decimal("0"),
            avg_purchase_price=Decimal("0"),
            current_price=Decimal("100.00"),
            price_change_pct=Decimal("0.00"),
            max_trade_budget_usd=Decimal("500.00"),
            daily_loss_limit_usd=Decimal("500.00"),
        )
        defaults.update(overrides)
        return DecisionContext(**defaults)

    def _report(self, label: str, score: float) -> NewsReport:
        report = NewsReport(ticker="AAPL")
        report.sentiment = SentimentResult(
            label=label, score=score, confidence=0.8, articles_scored=10
        )
        report.articles = []
        return report

    def test_bullish_signal_produces_buy(self):
        engine = HeuristicDecisionEngine()
        ctx = self._context(news_report=self._report("BULLISH", 0.8))
        decision = engine.decide(ctx)
        self.assertEqual(decision.action, "BUY")
        # medium risk => 50% of the $500 budget => $250 / $100 = 2.5 units
        self.assertAlmostEqual(decision.amount, 2.5, places=6)

    def test_low_risk_sizes_smaller_than_high_risk(self):
        engine = HeuristicDecisionEngine()
        report = self._report("BULLISH", 0.9)
        low = engine.decide(self._context(risk_profile="low", news_report=report))
        high = engine.decide(self._context(risk_profile="high", news_report=report))
        self.assertLess(low.amount, high.amount)
        self.assertAlmostEqual(low.amount, 1.25, places=6)  # 25% of 500 / 100
        self.assertAlmostEqual(high.amount, 4.0, places=6)  # 80% of 500 / 100

    def test_bearish_signal_with_position_produces_sell(self):
        engine = HeuristicDecisionEngine()
        ctx = self._context(
            news_report=self._report("BEARISH", -0.9),
            position_amount=Decimal("10"),
            avg_purchase_price=Decimal("90.00"),
        )
        decision = engine.decide(ctx)
        self.assertEqual(decision.action, "SELL")
        self.assertAlmostEqual(decision.amount, 5.0, places=6)  # 50% of position

    def test_neutral_signal_holds(self):
        engine = HeuristicDecisionEngine()
        ctx = self._context(news_report=self._report("NEUTRAL", 0.0))
        decision = engine.decide(ctx)
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.amount, 0.0)

    def test_bearish_without_position_holds(self):
        engine = HeuristicDecisionEngine()
        ctx = self._context(news_report=self._report("BEARISH", -0.9))
        self.assertEqual(engine.decide(ctx).action, "HOLD")

    def test_bullish_without_cash_budget_holds(self):
        engine = HeuristicDecisionEngine()
        ctx = self._context(
            news_report=self._report("BULLISH", 0.9), max_trade_budget_usd=Decimal("0.00")
        )
        self.assertEqual(engine.decide(ctx).action, "HOLD")

    def test_sell_signal_without_position_is_reported_accurately(self):
        """The audit trail must not claim 'neutral band' when the signal breached."""
        engine = HeuristicDecisionEngine()
        ctx = self._context(news_report=self._report("BEARISH", -0.9))
        reasoning = engine.decide(ctx).reasoning
        self.assertIn("breaches SELL threshold", reasoning)
        self.assertIn("no open position", reasoning)
        self.assertNotIn("inside the neutral band", reasoning)

    def test_buy_signal_without_budget_is_reported_accurately(self):
        engine = HeuristicDecisionEngine()
        ctx = self._context(
            news_report=self._report("BULLISH", 0.9), max_trade_budget_usd=Decimal("0.00")
        )
        reasoning = engine.decide(ctx).reasoning
        self.assertIn("exceeds BUY threshold", reasoning)
        self.assertIn("suppressed", reasoning)
        self.assertNotIn("inside the neutral band", reasoning)

    def test_reasoning_is_always_populated(self):
        engine = HeuristicDecisionEngine()
        decision = engine.decide(self._context(news_report=self._report("NEUTRAL", 0.0)))
        self.assertIn("DETERMINISTIC ENGINE", decision.reasoning)
        self.assertGreater(len(decision.reasoning), 40)


class OrchestratorFallbackTests(TestCase):
    """With no API key the orchestrator must degrade gracefully, never crash."""

    @override_settings(
        AI_CONFIG={"API_KEY": "", "PROVIDER": "deepseek", "MODEL": "deepseek-reasoner"}
    )
    def test_runs_heuristic_when_unconfigured(self):
        orchestrator = AlphaAgentOrchestrator()
        self.assertFalse(orchestrator.is_configured)

        ctx = DecisionContext(
            ticker="TSLA",
            risk_profile="medium",
            cash_balance=Decimal("5000.00"),
            position_amount=Decimal("0"),
            avg_purchase_price=Decimal("0"),
            current_price=Decimal("250.00"),
            max_trade_budget_usd=Decimal("250.00"),
            news_report=NewsReport(ticker="TSLA"),
        )
        result = orchestrator.run(ctx)
        self.assertEqual(result.source, "heuristic_fallback")
        self.assertTrue(result.used_fallback)
        self.assertIn(result.proposal.action, {"BUY", "SELL", "HOLD"})
        self.assertIsInstance(result.proposal, TradeProposal)


class CostAccountingTests(TestCase):
    def test_estimate_cost_scales_with_tokens(self):
        cheap = estimate_cost(1_000, "deepseek-reasoner")
        pricey = estimate_cost(1_000_000, "deepseek-reasoner")
        self.assertGreater(pricey, cheap)
        self.assertGreater(cheap, Decimal("0"))

    def test_unknown_model_uses_default_pricing(self):
        self.assertGreater(estimate_cost(10_000, "some-future-model"), Decimal("0"))


class CrewBuildTests(TestCase):
    """Regression cover for the *configured-LLM* path.

    These tests exist because a PEP 563 ``from __future__ import annotations``
    import turned ``_proposal_guardrail``'s return annotation into the string
    ``'tuple[bool, Any]'``. CrewAI's ``Task`` validator calls
    ``get_origin()`` on that annotation, gets ``None``, and aborts crew
    construction with "If return type is annotated, it must be Tuple[bool, Any]".
    Nothing in the suite ever built a crew, so the whole LLM path was untested.
    """

    def _context(self, **overrides) -> DecisionContext:
        defaults = dict(
            ticker="AAPL",
            risk_profile="high",
            cash_balance=Decimal("10000.00"),
            position_amount=Decimal("5"),
            avg_purchase_price=Decimal("100.00"),
            current_price=Decimal("329.40"),
            price_change_pct=Decimal("-1.20"),
            max_trade_budget_usd=Decimal("500.00"),
            daily_loss_limit_usd=Decimal("500.00"),
            news_report=NewsReport(ticker="AAPL"),
            portfolio_id=1,
        )
        defaults.update(overrides)
        return DecisionContext(**defaults)

    def test_guardrail_return_annotation_satisfies_crewai_contract(self):
        """Pins the exact runtime contract CrewAI's validator enforces."""
        annotation = inspect.signature(_proposal_guardrail).return_annotation
        self.assertIs(
            get_origin(annotation),
            tuple,
            "guardrail return annotation must be a real generic alias, not a string "
            "(check for a `from __future__ import annotations` import)",
        )
        self.assertEqual(get_args(annotation), (bool, Any))

    def test_tool_annotations_are_runtime_objects_not_strings(self):
        """CrewAI introspects ``_run`` to build the tool argument schema."""
        market_tool, news_tool, financial_tool, history_tool = _build_tools()
        for tool in (market_tool, news_tool, financial_tool, history_tool):
            signature = inspect.signature(tool._run)
            self.assertNotIsInstance(
                signature.return_annotation, str, f"{tool.name} return annotation is a string"
            )
            self.assertIs(signature.parameters["ticker"].annotation, str)

    def test_guardrail_accepts_a_valid_payload(self):
        raw = '{"action": "BUY", "amount": 1.5, "sentiment": "BULLISH", "reasoning": "ok"}'
        success, payload = _proposal_guardrail(SimpleNamespace(pydantic=None, raw=raw))
        self.assertTrue(success)
        self.assertIsInstance(payload, TradeProposal)
        self.assertEqual(payload.action, "BUY")
        self.assertEqual(payload.amount, 1.5)

    def test_guardrail_rejects_prose_and_asks_for_retry(self):
        success, message = _proposal_guardrail(
            SimpleNamespace(pydantic=None, raw="I reckon we should buy the dip.")
        )
        self.assertFalse(success)
        self.assertIsInstance(message, str)
        self.assertIn("JSON", message)

    def test_crew_constructs_against_real_crewai_validation(self):
        """The exact operation that crashed the Celery worker."""
        from crewai import Process

        orchestrator = AlphaAgentOrchestrator(CREW_CONFIG)
        self.assertTrue(orchestrator.is_configured)

        market_tool, news_tool, financial_tool, history_tool = _build_tools()
        crew = orchestrator._build_crew(
            self._context(),
            {
                "market": market_tool,
                "news": news_tool,
                "financial": financial_tool,
                "history": history_tool,
            },
        )

        self.assertEqual(crew.process, Process.sequential)
        self.assertEqual(len(crew.agents), 3)
        self.assertEqual(len(crew.tasks), 3)
        self.assertIsNotNone(crew.tasks[-1].guardrail)
        self.assertEqual(
            [agent.role for agent in crew.agents],
            [
                "Bullish Research Analyst",
                "Risk Assessor (Short Seller)",
                "Chief Investment Officer (CIO)",
            ],
        )

    def test_run_uses_crew_path_and_accounts_for_tokens(self):
        """End-to-end through the LLM branch, with only ``kickoff`` stubbed."""
        orchestrator = AlphaAgentOrchestrator(CREW_CONFIG)
        fake_output = SimpleNamespace(
            pydantic=TradeProposal(
                action="BUY", amount=2.0, sentiment="BULLISH", reasoning="crew says buy"
            ),
            raw="...",
            token_usage=SimpleNamespace(total_tokens=4321),
        )

        with patch("crewai.Crew.kickoff", return_value=fake_output):
            result = orchestrator.run(self._context())

        self.assertEqual(result.source, "crewai")
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.tokens_used, 4321)
        self.assertGreater(result.api_cost_usd, Decimal("0"))
        self.assertEqual(result.proposal.action, "BUY")
        self.assertEqual(result.reasoning, "crew says buy")

    def test_run_falls_back_when_the_provider_raises(self):
        orchestrator = AlphaAgentOrchestrator(CREW_CONFIG)
        with patch("crewai.Crew.kickoff", side_effect=RuntimeError("provider 500")):
            result = orchestrator.run(self._context())
        self.assertEqual(result.source, "heuristic_fallback")
        self.assertIn("provider 500", result.error)

    def test_run_falls_back_when_the_crew_emits_garbage(self):
        orchestrator = AlphaAgentOrchestrator(CREW_CONFIG)
        junk = SimpleNamespace(pydantic=None, raw="no json here at all", token_usage=None)
        with patch("crewai.Crew.kickoff", return_value=junk):
            result = orchestrator.run(self._context())
        self.assertEqual(result.source, "heuristic_fallback")
        self.assertEqual(result.error, "unparseable_llm_output")

    def test_anthropic_provider_builds_a_crew(self):
        """`claude-*` resolves through CrewAI's *native* Anthropic provider.

        That path needs the `crewai[anthropic]` extra. Without it, construction
        dies with "Anthropic native provider not available" - which is exactly
        what happened with AI_LLM_PROVIDER=anthropic in .env.
        """
        config = {**CREW_CONFIG, "PROVIDER": "anthropic", "MODEL": "claude-3-5-sonnet-latest"}
        orchestrator = AlphaAgentOrchestrator(config)
        market_tool, news_tool, financial_tool, history_tool = _build_tools()

        crew = orchestrator._build_crew(
            self._context(),
            {
                "market": market_tool,
                "news": news_tool,
                "financial": financial_tool,
                "history": history_tool,
            },
        )
        self.assertEqual(len(crew.agents), 3)
        self.assertEqual(len(crew.tasks), 3)

    def test_missing_provider_sdk_raises_actionable_error(self):
        """A missing provider SDK must be translated to a pip-oriented message.

        CrewAI's own hint is `uv add "crewai[anthropic]"`, which is wrong for a
        pip/requirements project and was leaking into the audit trail verbatim.
        """
        from ai_agent import LLMProviderError

        orchestrator = AlphaAgentOrchestrator({**CREW_CONFIG, "PROVIDER": "watson"})
        with (
            patch(
                "crewai.LLM",
                side_effect=ImportError("Watson native provider not available"),
            ),
            self.assertRaises(LLMProviderError) as ctx,
        ):
            orchestrator.build_llm()

        message = str(ctx.exception)
        self.assertIn("pip install", message)
        self.assertIn("requirements.txt", message)
        self.assertNotIn("uv add", message)

    def test_missing_provider_degrades_instead_of_crashing(self):
        """The task must still produce an audit row when the provider is absent."""
        orchestrator = AlphaAgentOrchestrator({**CREW_CONFIG, "PROVIDER": "watson"})
        with patch(
            "crewai.LLM",
            side_effect=ImportError("Watson native provider not available"),
        ):
            result = orchestrator.run(self._context())

        self.assertEqual(result.source, "heuristic_fallback")
        self.assertIn("watson", result.error.lower())
