"""AlphaAgent AI layer - CrewAI orchestration with a strict guardrail pipeline.

Design contract
---------------
* Three agents run in a **sequential** process, structured as an adversarial
  debate so that no single narrative reaches the decision unchallenged:
  1. *Bullish Research Analyst* - builds the strongest evidence-based bull case
     from the latest headlines and price action
     (tools: :class:`NewsSentimentTool`, :class:`PriceHistoryTool`).
  2. *Risk Assessor (Short Seller)* - builds the bear case from independent
     evidence: leverage, liquidity, cash burn, valuation and technical breakdown
     (tools: :class:`FinancialHealthTool`, :class:`PriceHistoryTool`,
     :class:`NewsSentimentTool`).
  3. *Chief Investment Officer* - adjudicates between the two arguments and
     decides BUY/SELL/HOLD given the live price and the investor's risk profile
     (tool: :class:`MarketPriceTool`, backed by ``yfinance``).
* **No tool touches the database.** Tools are strictly read-only and return JSON.
  Persistence happens only in ``tasks.py`` *after* the execution guard approves
  the proposal.
* The crew must emit a validated payload::

      {"action": "BUY"|"SELL"|"HOLD", "amount": float,
       "sentiment": "BULLISH"|"BEARISH"|"NEUTRAL", "reasoning": "string"}

  enforced twice: by the Pydantic model :class:`TradeProposal` (``output_pydantic``)
  and by a CrewAI task ``guardrail`` that re-parses and rejects malformed output.

When no LLM credential is configured (or the provider errors out), the module
falls back to :class:`HeuristicDecisionEngine` - a deterministic, fully offline
decision engine - so the trading loop remains testable and operable.
"""

# NOTE: deliberately NO ``from __future__ import annotations`` in this module.
#
# PEP 563 turns every annotation into a *string*. CrewAI's Task validator
# inspects the guardrail callable at runtime with
# ``get_origin(sig.return_annotation) is tuple``, which returns ``None`` for the
# string ``'tuple[bool, Any]'`` and makes Task construction fail with
# "If return type is annotated, it must be Tuple[bool, Any]".
# CrewAI's BaseTool likewise introspects ``_run``'s annotations to build the
# tool argument schema. Python 3.12 evaluates ``tuple[bool, Any]``,
# ``Decimal | None`` and ``dict[str, Any]`` natively, so eager annotations are
# both correct and required here.

import json
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Literal

# CrewAI persists credentials under HOME at import time; redirect first.
from runtime_env import ensure_writable_runtime_home

ensure_writable_runtime_home()

from django.conf import settings  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator  # noqa: E402

from services.fundamentals import get_financial_health, get_price_history  # noqa: E402
from services.llm_hooks import build_llm_interceptor  # noqa: E402
from services.market_data import get_latest_quote  # noqa: E402
from services.news import NewsReport, build_news_report  # noqa: E402

logger = logging.getLogger("alphaagent.ai")

__all__ = [
    "AgentRunResult",
    "AlphaAgentOrchestrator",
    "DecisionContext",
    "HeuristicDecisionEngine",
    "LLMProviderError",
    "TradeProposal",
    "run_alpha_agent",
]

Action = Literal["BUY", "SELL", "HOLD"]
SentimentLabel = Literal["BULLISH", "BEARISH", "NEUTRAL"]

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


# ---------------------------------------------------------------------------
# Guardrail payload
# ---------------------------------------------------------------------------
class TradeProposal(BaseModel):
    """The **only** structure the AI layer is allowed to emit.

    Tools and agents never mutate state; they produce this payload and the
    Celery execution guard in ``tasks.py`` decides whether it becomes a trade.
    """

    model_config = ConfigDict(extra="ignore")

    action: Action = Field(description="Trading decision: BUY, SELL or HOLD.")
    amount: float = Field(
        default=0.0,
        ge=0.0,
        description="Quantity of the asset to trade (units/shares). 0 for HOLD.",
    )
    sentiment: SentimentLabel = Field(description="Market sentiment driving the decision.")
    reasoning: str = Field(
        min_length=1,
        description="Chain-of-thought explaining the decision.",
    )

    @field_validator("action", "sentiment", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, value: Any) -> Any:
        if value is None or value == "":
            return 0.0
        if isinstance(value, str):
            value = value.strip().replace(",", "").replace("$", "")
        return value

    @field_validator("reasoning", mode="before")
    @classmethod
    def _coerce_reasoning(cls, value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return " ".join(str(item) for item in value)
        return str(value)

    def to_payload(self) -> dict[str, Any]:
        """Exactly the four contract keys, JSON-serialisable."""
        return {
            "action": self.action,
            "amount": round(float(self.amount), 8),
            "sentiment": self.sentiment,
            "reasoning": self.reasoning,
        }


def parse_proposal(raw: Any) -> TradeProposal | None:
    """Best-effort conversion of arbitrary agent output into a TradeProposal."""
    if raw is None:
        return None
    if isinstance(raw, TradeProposal):
        return raw
    if isinstance(raw, BaseModel):
        try:
            return TradeProposal.model_validate(raw.model_dump())
        except ValidationError:
            return None
    if isinstance(raw, dict):
        try:
            return TradeProposal.model_validate(raw)
        except ValidationError as exc:
            logger.warning("Proposal dict failed validation: %s", exc)
            return None

    text = str(raw).strip()
    if not text:
        return None

    candidates: list[str] = [text]
    match = _JSON_BLOCK_RE.search(text)
    if match:
        candidates.append(match.group(0))
    # Strip markdown fences.
    candidates.append(re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip())

    for candidate in candidates:
        try:
            return TradeProposal.model_validate(json.loads(candidate))
        except (json.JSONDecodeError, ValidationError):
            continue

    # Last resort: tolerant repair for slightly malformed LLM JSON.
    try:
        import json_repair  # type: ignore[import-not-found]

        repaired = json_repair.loads(text)
        if isinstance(repaired, dict):
            return TradeProposal.model_validate(repaired)
    except Exception as exc:  # optional dependency / unrepairable text
        logger.debug("json_repair could not salvage agent output: %s", exc)

    logger.error("Unable to parse a TradeProposal from agent output: %.300s", text)
    return None


# ---------------------------------------------------------------------------
# Decision context / result
# ---------------------------------------------------------------------------
@dataclass
class DecisionContext:
    """Everything the agents need, assembled by the Celery task (read-only)."""

    ticker: str
    risk_profile: str
    cash_balance: Decimal
    position_amount: Decimal
    avg_purchase_price: Decimal
    current_price: Decimal | None = None
    price_change_pct: Decimal | None = None
    max_trade_budget_usd: Decimal = Decimal("0.00")
    daily_loss_limit_usd: Decimal = Decimal("0.00")
    realised_pnl_today_usd: Decimal = Decimal("0.00")
    news_report: NewsReport | None = None
    #: Optional evidence for the Risk Assessor and the deterministic debate.
    financial_health: Any = None
    price_history: Any = None
    portfolio_id: int | None = None

    @property
    def position_value_usd(self) -> Decimal:
        if self.current_price is None:
            return Decimal("0.00")
        return (self.position_amount * self.current_price).quantize(Decimal("0.01"))

    @property
    def unrealised_pnl_usd(self) -> Decimal:
        if self.current_price is None or self.position_amount == 0:
            return Decimal("0.00")
        basis = self.position_amount * self.avg_purchase_price
        return (self.position_value_usd - basis).quantize(Decimal("0.01"))

    def as_brief(self) -> str:
        """Compact factual brief handed to the CIO agent."""
        price = f"${self.current_price}" if self.current_price is not None else "unavailable"
        change = f"{self.price_change_pct}%" if self.price_change_pct is not None else "unknown"
        return (
            f"Ticker: {self.ticker}\n"
            f"Investor risk profile: {self.risk_profile}\n"
            f"Live price: {price} (session change: {change})\n"
            f"Cash available: ${self.cash_balance}\n"
            f"Current position: {self.position_amount} units "
            f"(avg cost ${self.avg_purchase_price}, value ${self.position_value_usd}, "
            f"unrealised P&L ${self.unrealised_pnl_usd})\n"
            f"Max budget for this single trade (hard guardrail): ${self.max_trade_budget_usd}\n"
            f"Daily loss limit: ${self.daily_loss_limit_usd} "
            f"(realised today: ${self.realised_pnl_today_usd})"
        )


@dataclass
class AgentRunResult:
    """Outcome of one agent invocation."""

    proposal: TradeProposal
    reasoning: str
    #: The two adversarial arguments the CIO adjudicated between.
    bull_case: str = ""
    bear_case: str = ""
    tokens_used: int = 0
    api_cost_usd: Decimal = Decimal("0.00000")
    source: str = "crewai"  # crewai | heuristic_fallback
    error: str | None = None
    latency_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def used_fallback(self) -> bool:
        return self.source != "crewai"


# ---------------------------------------------------------------------------
# CrewAI tools - strictly read-only, DB-free
# ---------------------------------------------------------------------------
def _build_tools():
    """Import CrewAI lazily and construct the read-only tool set."""
    from crewai.tools import BaseTool

    class MarketPriceTool(BaseTool):
        """yfinance-backed live price lookup. Read-only, no DB access."""

        name: str = "get_market_price"
        description: str = (
            "Fetch the latest market price for a ticker (e.g. AAPL, TSLA, BTC). "
            "Returns JSON with price, previous_close, change_pct and source. "
            "This tool is read-only and never modifies the portfolio."
        )

        def _run(self, ticker: str) -> str:
            quote = get_latest_quote(ticker)
            if quote is None:
                return json.dumps({"ticker": ticker, "error": "price unavailable"})
            return json.dumps(quote.as_dict())

    class NewsSentimentTool(BaseTool):
        """RSS scraper + lexicon sentiment scorer. Read-only, no DB access."""

        name: str = "get_news_sentiment"
        description: str = (
            "Scrape the latest financial news headlines for a ticker and return "
            "JSON containing the headlines plus an aggregated sentiment label "
            "(BULLISH/BEARISH/NEUTRAL) and score. Read-only."
        )

        def _run(self, ticker: str) -> str:
            limit = int(getattr(settings, "AI_CONFIG", {}).get("NEWS_ARTICLE_LIMIT", 10))
            report = build_news_report(ticker, limit=limit)
            return json.dumps(report.as_dict())

    class FinancialHealthTool(BaseTool):
        """Balance-sheet and valuation facts. Read-only, no DB access."""

        name: str = "get_financial_health"
        description: str = (
            "Fetch balance-sheet and valuation facts for a ticker: total debt, "
            "debt-to-equity, current ratio, profit margin, free cash flow, "
            "trailing P/E, analyst target price and derived red flags. "
            "Use this to build a bearish case grounded in fundamentals. "
            "Read-only."
        )

        def _run(self, ticker: str) -> str:
            health = get_financial_health(ticker)
            return json.dumps(health.as_dict())

    class PriceHistoryTool(BaseTool):
        """Trend and drawdown statistics. Read-only, no DB access."""

        name: str = "get_price_history"
        description: str = (
            "Fetch daily closes over a period plus trend statistics: 50- and "
            "200-day moving averages, distance from the period high (drawdown), "
            "and total change. Use this to identify technical breakdowns. "
            "Read-only."
        )

        def _run(self, ticker: str, period: str = "1y") -> str:
            history = get_price_history(ticker, period=period or "1y")
            if history is None:
                return json.dumps({"ticker": ticker, "error": "history unavailable"})
            payload = history.as_dict()
            # The chart points are noise for a reasoning agent.
            payload.pop("points", None)
            return json.dumps(payload)

    return MarketPriceTool(), NewsSentimentTool(), FinancialHealthTool(), PriceHistoryTool()


# ---------------------------------------------------------------------------
# Deterministic fallback engine
# ---------------------------------------------------------------------------
class HeuristicDecisionEngine:
    """Offline, deterministic decision engine.

    Used when no LLM credential is available or the provider fails. It applies
    the same inputs the agents receive (sentiment + price momentum + risk
    profile) through explicit rules, so behaviour is reproducible and auditable.
    """

    # Fraction of the per-trade budget to deploy, per risk profile.
    _RISK_SIZING: ClassVar[dict[str, float]] = {"low": 0.25, "medium": 0.50, "high": 0.80}
    _BUY_THRESHOLD: ClassVar[float] = 0.15
    _SELL_THRESHOLD: ClassVar[float] = -0.15

    def debate(self, context: DecisionContext) -> tuple[str, str]:
        """Produce bull and bear write-ups from the same evidence the agents get.

        Used when no LLM is configured. It is deliberately blunt: it reports what
        the data shows rather than simulating conviction, and it says so when a
        side has no supporting evidence.
        """
        news = context.news_report
        positives = news.positive_articles if news else []
        negatives = news.negative_articles if news else []

        # -- bull ----------------------------------------------------------
        bull = [f"BULL CASE - {context.ticker} [deterministic engine]"]
        if positives:
            bull.append(f"{len(positives)} of {news.headline_count} headlines are positive:")
            bull.extend(f"  + [{a.polarity:+.2f}] {a.title}" for a in positives[:5])
        else:
            bull.append("No positive headlines were found in the current coverage.")

        if context.price_change_pct is not None:
            direction = "up" if context.price_change_pct >= 0 else "down"
            bull.append(f"Session price action: {direction} {abs(context.price_change_pct)}%.")
        if context.price_history is not None and context.price_history.change_pct is not None:
            bull.append(
                f"Trend over {context.price_history.period}: {context.price_history.change_pct:+}%."
            )
        if context.position_amount > 0:
            bull.append(
                f"Existing position of {context.position_amount} units, "
                f"unrealised P&L ${context.unrealised_pnl_usd}."
            )
        bull.append(
            "Assessment: this is a mechanical read of the available evidence, not an "
            "endorsement. Configure an LLM for a reasoned bull case."
        )

        # -- bear ----------------------------------------------------------
        bear = [f"BEAR CASE - {context.ticker} [deterministic engine]"]
        if negatives:
            bear.append(f"{len(negatives)} of {news.headline_count} headlines are negative:")
            bear.extend(f"  - [{a.polarity:+.2f}] {a.title}" for a in negatives[:5])
        else:
            bear.append("No negative headlines were found in the current coverage.")

        health = context.financial_health
        if health is not None and getattr(health, "red_flags", None):
            bear.append("Fundamental red flags:")
            bear.extend(f"  - {flag}" for flag in health.red_flags)
        elif health is not None and getattr(health, "degraded", False):
            bear.append("No fundamental data available (degraded source).")
        else:
            bear.append("No fundamental red flags detected in the available data.")

        history = context.price_history
        if history is not None and getattr(history, "degradation_flags", None):
            bear.extend(f"  - {flag}" for flag in history.degradation_flags)
        if context.daily_loss_limit_usd > 0 and context.realised_pnl_today_usd <= (
            -context.daily_loss_limit_usd
        ):
            bear.append(
                f"Portfolio stop-loss already breached today "
                f"(${context.realised_pnl_today_usd} vs -${context.daily_loss_limit_usd})."
            )
        bear.append(
            "Assessment: mechanical read of the negative evidence above, with no "
            "adversarial reasoning applied. Configure an LLM for a genuine bear case."
        )

        return "\n".join(bull), "\n".join(bear)

    def decide(self, context: DecisionContext, reason_prefix: str = "") -> TradeProposal:
        sentiment_label = "NEUTRAL"
        sentiment_score = 0.0
        if context.news_report is not None:
            sentiment_label = context.news_report.sentiment.label
            sentiment_score = context.news_report.sentiment.score

        momentum = float(context.price_change_pct) if context.price_change_pct is not None else 0.0
        # Momentum confirms (or contradicts) the headline signal.
        composite = sentiment_score + max(-0.5, min(0.5, momentum / 10.0))

        price = context.current_price
        budget = context.max_trade_budget_usd
        risk = (context.risk_profile or "medium").lower()
        sizing = self._RISK_SIZING.get(risk, 0.50)

        action: Action = "HOLD"
        amount = 0.0
        notes: list[str] = []

        can_buy = price is not None and price > 0 and budget > 0
        has_position = context.position_amount > 0
        tradable = price is not None and price > 0

        if composite > self._BUY_THRESHOLD:
            if can_buy:
                action = "BUY"
                spend = (budget * Decimal(str(sizing))).quantize(Decimal("0.01"))
                amount = float((spend / price).quantize(Decimal("0.00000001")))
                notes.append(
                    f"Composite signal {composite:+.3f} exceeds BUY threshold "
                    f"{self._BUY_THRESHOLD:+.2f}; deploying {sizing:.0%} of the "
                    f"${budget} per-trade budget (risk profile: {risk})."
                )
            else:
                if price is None or price <= 0:
                    blocker = "no reliable market price is available"
                else:
                    blocker = (
                        f"the per-trade budget is $0.00 ({portfolio_allocation_hint(context)})"
                    )
                notes.append(
                    f"Composite signal {composite:+.3f} exceeds BUY threshold "
                    f"{self._BUY_THRESHOLD:+.2f}, but the BUY is suppressed because "
                    f"{blocker}. Holding instead."
                )
        elif composite < self._SELL_THRESHOLD:
            if has_position and tradable:
                action = "SELL"
                # De-risk: shed a risk-scaled slice of the position.
                fraction = Decimal(str(self._RISK_SIZING.get(risk, 0.50)))
                amount = float((context.position_amount * fraction).quantize(Decimal("0.00000001")))
                notes.append(
                    f"Composite signal {composite:+.3f} breaches SELL threshold "
                    f"{self._SELL_THRESHOLD:+.2f}; reducing {fraction:.0%} of the open "
                    f"position ({context.position_amount} units)."
                )
            else:
                if not has_position:
                    blocker = "there is no open position to liquidate"
                else:
                    blocker = "no reliable market price is available"
                notes.append(
                    f"Composite signal {composite:+.3f} breaches SELL threshold "
                    f"{self._SELL_THRESHOLD:+.2f}, but the SELL is suppressed because "
                    f"{blocker}. Holding instead."
                )
        else:
            notes.append(
                f"Composite signal {composite:+.3f} is inside the neutral band "
                f"[{self._SELL_THRESHOLD:+.2f}, {self._BUY_THRESHOLD:+.2f}]; no action taken."
            )

        reasoning = (
            f"{reason_prefix}"
            f"[DETERMINISTIC ENGINE] {context.ticker} analysis. "
            f"Headline sentiment={sentiment_label} (score {sentiment_score:+.3f}, "
            f"headlines={context.news_report.headline_count if context.news_report else 0}); "
            f"session momentum={momentum:+.2f}%; composite={composite:+.3f}. "
            + " ".join(notes)
            + f" Position={context.position_amount} units, cash=${context.cash_balance}, "
            f"unrealised P&L=${context.unrealised_pnl_usd}."
        ).strip()

        return TradeProposal(
            action=action,
            amount=amount,
            sentiment=sentiment_label,  # type: ignore[arg-type]
            reasoning=reasoning,
        )


def portfolio_allocation_hint(context: DecisionContext) -> str:
    """Human-readable explanation of why the per-trade budget is zero."""
    if context.cash_balance <= 0:
        return "the portfolio has no cash"
    return "max_trade_allocation_pct is 0%"


class LLMProviderError(RuntimeError):
    """Raised when the configured LLM provider cannot be initialised."""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class AlphaAgentOrchestrator:
    """Builds and runs the three-agent adversarial debate crew."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or getattr(settings, "AI_CONFIG", {})
        self._llm: Any = None

    # -- LLM plumbing ------------------------------------------------------
    @property
    def api_key(self) -> str:
        return str(self.config.get("API_KEY") or "").strip()

    @property
    def provider(self) -> str:
        return str(self.config.get("PROVIDER") or "deepseek").lower()

    @property
    def is_configured(self) -> bool:
        """True when an LLM credential is available for the crew."""
        return bool(self.api_key) and self.provider not in {"none", "disabled", ""}

    def build_llm(self) -> Any:
        """Instantiate the CrewAI LLM (DeepSeek-R1 or GPT-4o)."""
        if self._llm is not None:
            return self._llm

        from crewai import LLM

        model = str(self.config.get("MODEL") or "deepseek-reasoner")
        provider = self.provider

        # LiteLLM routes on a "<provider>/<model>" prefix. DeepSeek needs one
        # prepended; OpenAI and Anthropic accept the bare name, so no branch is
        # required for them (an empty branch would be dead code).
        if provider == "deepseek" and not model.startswith("deepseek/"):
            model = f"deepseek/{model}"

        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": self.api_key,
            "temperature": self.config.get("TEMPERATURE", 0.2),
            "max_tokens": self.config.get("MAX_TOKENS", 2048),
            "timeout": self.config.get("REQUEST_TIMEOUT", 90),
        }
        base_url = str(self.config.get("BASE_URL") or "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        elif provider == "deepseek":
            kwargs["base_url"] = "https://api.deepseek.com/v1"

        # Headers the provider requires but the LLM library does not send -
        # notably Anthropic's `anthropic-workspace-id`. CrewAI runs the
        # interceptor against the real httpx request before it is sent.
        interceptor = build_llm_interceptor(self.config)
        if interceptor is not None:
            kwargs["interceptor"] = interceptor

        logger.info("Initialising LLM: provider=%s model=%s", provider, model)
        try:
            self._llm = LLM(**kwargs)
        except ImportError as exc:
            # CrewAI resolves some models through *native* providers that need an
            # optional SDK (e.g. claude-* -> crewai[anthropic]). CrewAI's own hint
            # says `uv add`, which is wrong for this pip/requirements project.
            raise LLMProviderError(
                f"LLM provider '{provider}' is not installed in this environment "
                f"({exc}). Install the matching extra, e.g. "
                f'`pip install "crewai[{provider}]"`, add it to requirements.txt, '
                f"and rebuild the image (`docker compose build`). "
                f"Alternatively set AI_LLM_PROVIDER=deepseek or openai."
            ) from exc
        return self._llm

    # -- MCP tool routing --------------------------------------------------
    def mcp_servers(self, allowed: list[str]) -> list[Any]:
        """Build CrewAI MCP references for one agent, filtered to ``allowed``.

        Returns an empty list when MCP routing is disabled or unconfigured, so
        callers can fall back to the in-process tools.
        """
        if not self.config.get("TOOLS_VIA_MCP"):
            return []
        url = str(self.config.get("MCP_SERVER_URL") or "").strip()
        if not url:
            logger.warning("TOOLS_VIA_MCP is on but AI_MCP_SERVER_URL is empty")
            return []

        try:
            from crewai.mcp.config import MCPServerHTTP
        except ImportError as exc:  # pragma: no cover - older CrewAI
            logger.warning("CrewAI MCP config unavailable: %s", exc)
            return []

        def predicate(tool: dict) -> bool:
            # CrewAI namespaces MCP tools, so match on the suffix.
            name = str(tool.get("name", ""))
            return any(name.endswith(suffix) for suffix in allowed)

        return [MCPServerHTTP(url=url, streamable=True, tool_filter=predicate)]

    # -- Agents ------------------------------------------------------------
    def _build_crew(self, context: DecisionContext, tools: dict[str, Any]) -> Any:
        """Build the adversarial three-agent crew.

        Structure: **bull vs bear, then an adjudicator.**

        A single analyst feeding a single decision-maker is a hallucination
        amplifier - whatever the analyst asserts becomes the premise. Forcing a
        second agent to argue the opposite case, from its own independent
        evidence, means the CIO adjudicates a genuine disagreement instead of
        rubber-stamping a summary.
        """
        from crewai import Agent, Crew, Process, Task

        llm = self.build_llm()
        limit = int(self.config.get("NEWS_ARTICLE_LIMIT", 10))

        bull = Agent(
            role="Bullish Research Analyst",
            goal=(
                f"Build the strongest *evidence-based* bullish case for {context.ticker} "
                f"from the latest {limit} news articles and the current price action."
            ),
            backstory=(
                "You are a veteran buy-side research analyst. Your job is to construct the "
                "most compelling honest bull case: growth drivers, catalysts, upgrades, "
                "product momentum and competitive wins. You are an advocate, but never a "
                "fabricator - every claim must trace to a headline your tool returned. If "
                "the bullish evidence is genuinely weak, you say so plainly rather than "
                "inventing it."
            ),
            tools=[]
            if (mcp_bull := self.mcp_servers(["get_news_sentiment", "get_price_history"]))
            else [tools["news"], tools["history"]],
            mcps=mcp_bull,
            llm=llm,
            verbose=False,
            allow_delegation=False,
            max_iter=6,
            max_retry_limit=int(self.config.get("MAX_RETRIES", 2)),
        )

        bear = Agent(
            role="Risk Assessor (Short Seller)",
            goal=(
                f"Build the strongest *evidence-based* bearish case for {context.ticker}: "
                "find every material risk, and state plainly when the evidence does not "
                "support one."
            ),
            backstory=(
                "You are a short seller who is paid to find what the bulls miss: "
                "deteriorating balance sheets, cash burn, technical breakdowns, "
                "valuation stretch, competitive threats, litigation and demand "
                "weakness. You are adversarial by design, but you are also a "
                "professional: you cite the fundamentals and price levels your tools "
                "returned, you distinguish a real red flag from a merely unflattering "
                "number, and you explicitly report when you could NOT find a credible "
                "bear case. Fabricating risks is as damaging as missing them."
            ),
            tools=[]
            if (
                mcp_bear := self.mcp_servers(
                    ["get_financial_health", "get_price_history", "get_news_sentiment"]
                )
            )
            else [tools["financial"], tools["history"], tools["news"]],
            mcps=mcp_bear,
            llm=llm,
            verbose=False,
            allow_delegation=False,
            max_iter=8,
            max_retry_limit=int(self.config.get("MAX_RETRIES", 2)),
        )

        cio = Agent(
            role="Chief Investment Officer (CIO)",
            goal=(
                "Adjudicate between the bull and bear cases, then issue a single "
                "investment decision (BUY/SELL/HOLD) and return ONLY the required JSON."
            ),
            backstory=(
                "You are the CIO of a disciplined systematic fund. Two analysts have "
                "argued opposite sides; you decide. You weigh the stronger argument on "
                "the evidence, you discount rhetoric that is not backed by data, you "
                "respect the investor's risk mandate and cash budget, and you are "
                "comfortable returning HOLD when the two cases are balanced. You always "
                "answer with a single valid JSON object and no additional prose."
            ),
            tools=[] if (mcp_cio := self.mcp_servers(["get_market_price"])) else [tools["market"]],
            mcps=mcp_cio,
            llm=llm,
            verbose=False,
            allow_delegation=False,
            max_iter=6,
            max_retry_limit=int(self.config.get("MAX_RETRIES", 2)),
        )

        bull_task = Task(
            description=(
                f"Build the BULL CASE for {context.ticker}.\n"
                f"1. Call `get_news_sentiment` with ticker='{context.ticker}' to get "
                f"the latest {limit} headlines. The payload separates POSITIVE and "
                "NEGATIVE coverage - work from the positive side.\n"
                "2. Call `get_price_history` for the current trend.\n"
                "3. Argue why this asset could rise from here.\n\n"
                "Rules:\n"
                "- Cite specific headlines. Never invent a fact your tools did not return.\n"
                "- Acknowledge the strongest counter-argument you can see, then rebut it "
                "or concede it.\n"
                "- If the bullish evidence is weak, say so explicitly. An honest weak "
                "case is more useful than a fabricated strong one."
            ),
            expected_output=(
                "A written bull case: the thesis, the specific catalysts and headlines "
                "supporting it, the price/trend context, and an honest note on the "
                "weaknesses in the argument."
            ),
            agent=bull,
        )

        bear_task = Task(
            description=(
                f"Build the BEAR CASE for {context.ticker}. You are the adversarial "
                "check on the previous analyst.\n"
                f"1. Call `get_news_sentiment` with ticker='{context.ticker}' and work "
                "from the NEGATIVE coverage.\n"
                "2. Call `get_financial_health` and report debt, liquidity, cash flow, "
                "valuation and any red flags it derives.\n"
                "3. Call `get_price_history` and look for technical deterioration: "
                "price below the 50/200-day averages, drawdown from the high.\n"
                "4. Argue what could go wrong and how far the price could fall.\n\n"
                "Rules:\n"
                "- Cite the specific figures and headlines your tools returned.\n"
                "- Distinguish a genuine red flag from a merely unremarkable number.\n"
                "- Crypto has no balance sheet: if `get_financial_health` returns "
                "degraded data, say so and argue from news and technicals instead.\n"
                "- If you cannot find a credible bear case, state that explicitly. "
                "Never manufacture a risk to fill the page."
            ),
            expected_output=(
                "A written bear case: the specific risks, the supporting figures and "
                "headlines, the technical picture, downside scenarios, and an explicit "
                "statement of how strong the bearish evidence actually is."
            ),
            agent=bear,
        )

        decision_task = Task(
            description=(
                "Act as CIO. Two analysts have argued opposite sides of this trade. "
                "Adjudicate and decide.\n\n"
                f"MARKET & PORTFOLIO BRIEF\n{context.as_brief()}\n\n"
                "BULL CASE\n{bull_task_output}\n\n"
                "BEAR CASE\n{bear_task_output}\n\n"
                "Use the `get_market_price` tool to confirm the live price before sizing.\n"
                "Weighing rules:\n"
                "- Judge each case on the evidence it cites, not on how confident it "
                "sounds. Rhetoric without data is worth nothing.\n"
                "- If the bull case is materially stronger, BUY. If the bear case is, "
                "SELL. If they are genuinely balanced, HOLD - that is a valid, "
                "disciplined answer, not a failure.\n"
                "- Explain in `reasoning` which argument won and why, and name the "
                "single strongest point on the losing side.\n\n"
                "Hard rules:\n"
                f"- Never propose spending more than ${context.max_trade_budget_usd}.\n"
                "- `amount` is a QUANTITY of the asset in units, NOT a dollar figure.\n"
                "- HOLD must use amount 0.\n"
                "- SELL must not exceed the currently held position.\n"
                "- Return ONLY the JSON object, with no markdown fences and no prose."
            ),
            expected_output=(
                'A single JSON object: {"action": "BUY"|"SELL"|"HOLD", "amount": <float>, '
                '"sentiment": "BULLISH"|"BEARISH"|"NEUTRAL", "reasoning": "<string>"}'
            ),
            agent=cio,
            context=[bull_task, bear_task],
            output_pydantic=TradeProposal,
            guardrail=_proposal_guardrail,
        )

        return Crew(
            agents=[bull, bear, cio],
            tasks=[bull_task, bear_task, decision_task],
            process=Process.sequential,
            verbose=False,
            memory=False,
            cache=False,
        )

    # -- Execution ---------------------------------------------------------
    def run(self, context: DecisionContext) -> AgentRunResult:
        """Run the crew; fall back to the deterministic engine on any failure."""
        started = time.monotonic()
        fallback = HeuristicDecisionEngine()

        if not self.is_configured:
            reason = (
                "No LLM credential configured (AI_LLM_API_KEY empty) - "
                "using the deterministic decision engine."
            )
            logger.info("AI fallback for %s: %s", context.ticker, reason)
            proposal = fallback.decide(context, reason_prefix=f"[{reason}] ")
            bull, bear = fallback.debate(context)
            return AgentRunResult(
                proposal=proposal,
                reasoning=proposal.reasoning,
                bull_case=bull,
                bear_case=bear,
                source="heuristic_fallback",
                error=reason,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        try:
            market_tool, news_tool, financial_tool, history_tool = _build_tools()
            crew = self._build_crew(
                context,
                {
                    "market": market_tool,
                    "news": news_tool,
                    "financial": financial_tool,
                    "history": history_tool,
                },
            )
            logger.info(
                "Kicking off CrewAI for %s (portfolio=%s)", context.ticker, context.portfolio_id
            )
            result = crew.kickoff()
        except Exception as exc:
            logger.exception("CrewAI run failed for %s: %s", context.ticker, exc)
            if not self.config.get("ALLOW_HEURISTIC_FALLBACK", True):
                raise
            proposal = fallback.decide(
                context, reason_prefix=f"[LLM UNAVAILABLE: {type(exc).__name__}] "
            )
            bull, bear = fallback.debate(context)
            return AgentRunResult(
                proposal=proposal,
                reasoning=proposal.reasoning,
                bull_case=bull,
                bear_case=bear,
                source="heuristic_fallback",
                error=f"{type(exc).__name__}: {diagnose_provider_error(exc)}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        proposal = parse_proposal(getattr(result, "pydantic", None)) or parse_proposal(
            getattr(result, "raw", "")
        )
        if proposal is None:
            logger.error("Crew returned an unparseable payload for %s", context.ticker)
            if not self.config.get("ALLOW_HEURISTIC_FALLBACK", True):
                raise RuntimeError("CrewAI returned an unparseable TradeProposal")
            proposal = fallback.decide(context, reason_prefix="[UNPARSEABLE LLM OUTPUT] ")
            bull, bear = fallback.debate(context)
            return AgentRunResult(
                proposal=proposal,
                reasoning=proposal.reasoning,
                bull_case=bull,
                bear_case=bear,
                source="heuristic_fallback",
                error="unparseable_llm_output",
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        tokens_used = _extract_tokens(result)
        bull_case, bear_case = _extract_debate(result)
        return AgentRunResult(
            bull_case=bull_case,
            bear_case=bear_case,
            proposal=proposal,
            reasoning=proposal.reasoning or str(getattr(result, "raw", "")),
            tokens_used=tokens_used,
            api_cost_usd=estimate_cost(tokens_used, str(self.config.get("MODEL", ""))),
            source="crewai",
            latency_ms=int((time.monotonic() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# CrewAI guardrail
# ---------------------------------------------------------------------------
def _proposal_guardrail(output: Any) -> tuple[bool, Any]:
    """Task guardrail: reject anything that is not a valid TradeProposal.

    Returning ``(False, "<reason>")`` makes CrewAI re-prompt the agent; returning
    ``(True, proposal)`` accepts the validated payload.
    """
    candidate = getattr(output, "pydantic", None)
    proposal = parse_proposal(candidate)
    if proposal is None:
        proposal = parse_proposal(getattr(output, "raw", None))
    if proposal is None:
        return (
            False,
            "Output is not valid JSON matching "
            '{"action": "BUY"|"SELL"|"HOLD", "amount": <float>, '
            '"sentiment": "BULLISH"|"BEARISH"|"NEUTRAL", "reasoning": "<string>"}. '
            "Reply with the JSON object only.",
        )
    return True, proposal


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------
def diagnose_provider_error(exc: Exception) -> str:
    """Turn a provider error into an actionable message.

    A workspace-scoping rejection is an opaque HTTP 400 buried several frames
    deep in the agent executor. Restating it with the fix attached turns a
    confusing failure into a one-line configuration change.
    """
    text = str(exc)
    lowered = text.lower()

    if "workspace" in lowered and "anthropic" in lowered:
        return (
            "Anthropic rejected the request because the API key is not scoped to a "
            "workspace. Set AI_LLM_WORKSPACE_ID to your workspace ID (Anthropic "
            "Console -> Settings -> Workspaces), or use a workspace-scoped API key. "
            f"Provider said: {text[:300]}"
        )
    if "not_found_error" in lowered and "model" in lowered:
        return (
            "The provider does not recognise the configured model ID - it has "
            "probably been retired. Run `manage.py dry_run_agent` after updating "
            f"AI_LLM_MODEL. Provider said: {text[:300]}"
        )
    if "authentication" in lowered or "401" in lowered or "invalid api key" in lowered:
        return f"The provider rejected the credentials. Provider said: {text[:300]}"
    return text[:500]


def _extract_debate(result: Any) -> tuple[str, str]:
    """Pull the bull and bear write-ups out of a CrewOutput.

    The crew runs bull -> bear -> CIO sequentially, so tasks_output is ordered.
    We index defensively rather than assuming a length, because a partially
    failed crew can return fewer outputs.
    """
    outputs = list(getattr(result, "tasks_output", None) or [])

    def raw(index: int) -> str:
        if index >= len(outputs):
            return ""
        return str(getattr(outputs[index], "raw", "") or "").strip()

    return raw(0), raw(1)


def _extract_tokens(result: Any) -> int:
    """Pull a total token count out of a CrewOutput across API variants."""
    for source in (getattr(result, "token_usage", None), getattr(result, "usage_metrics", None)):
        if source is None:
            continue
        for attr in ("total_tokens", "total"):
            value = getattr(source, attr, None)
            if value is None and isinstance(source, dict):
                value = source.get(attr)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
    return 0


def estimate_cost(tokens_used: int, model: str) -> Decimal:
    """Approximate USD cost from the configured price table.

    Matches the exact model id first, then the longest prefix, so a dated id like
    ``claude-haiku-4-5-20251001`` resolves against the ``claude-haiku-4-5`` entry.

    Logs a warning when it falls back. Previously every model absent from the
    table was priced at the default with no signal at all, which is how a
    ``claude-*`` model came to be reported at DeepSeek rates - roughly half the
    real cost - with nothing in the logs to suggest anything was wrong.
    """
    pricing = getattr(settings, "AI_MODEL_PRICING", {})
    default = getattr(settings, "AI_DEFAULT_PRICING", {"input": 0.27, "output": 1.10})
    key = (model or "").split("/")[-1]

    rates = pricing.get(key)
    if rates is None and key:
        candidates = [k for k in pricing if key.startswith(k)]
        if candidates:
            rates = pricing[max(candidates, key=len)]
    if rates is None:
        logger.warning(
            "No price entry for model %r - cost is approximate. Add it to "
            "AI_MODEL_PRICING in config/settings.py.",
            key or "<unset>",
        )
        rates = default
    # Token split is not exposed per-bucket; assume a 70/30 input/output mix.
    blended_per_million = rates["input"] * 0.7 + rates["output"] * 0.3
    cost = Decimal(str(tokens_used)) / Decimal("1000000") * Decimal(str(blended_per_million))
    return cost.quantize(Decimal("0.00001"))


# ---------------------------------------------------------------------------
# Public entrypoint used by tasks.py
# ---------------------------------------------------------------------------
def run_alpha_agent(context: DecisionContext) -> AgentRunResult:
    """Execute the bull -> bear -> CIO debate for one portfolio/ticker pair."""
    orchestrator = AlphaAgentOrchestrator()
    if context.news_report is None:
        limit = int(getattr(settings, "AI_CONFIG", {}).get("NEWS_ARTICLE_LIMIT", 10))
        context.news_report = build_news_report(context.ticker, limit=limit)
    return orchestrator.run(context)
