"""Dry-run the AI crew **without touching the database**.

This is the safe way to validate an LLM configuration: it exercises the real
CrewAI pipeline (both agents, both read-only tools, the ``output_pydantic``
contract and the task guardrail) and prints what the AI *would* propose - but it
never calls the execution guard and never writes a ``Transaction``,
``Asset`` or ``AgentDecisionLog`` row.

Usage::

    python manage.py dry_run_agent                    # whole watchlist
    python manage.py dry_run_agent --ticker AAPL
    python manage.py dry_run_agent --portfolio-id 1   # real portfolio context
    python manage.py dry_run_agent --no-news          # skip RSS scraping
"""

from __future__ import annotations

import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from ai_agent import AlphaAgentOrchestrator, DecisionContext, run_alpha_agent
from core.models import Asset, Portfolio
from services.ledger import replay_positions
from services.market_data import get_latest_quote
from services.news import build_news_report


class Command(BaseCommand):
    help = "Run the CrewAI analyst/CIO pipeline in read-only mode (no trades, no DB writes)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--ticker",
            action="append",
            dest="tickers",
            help="Ticker to analyse (repeatable). Defaults to ALPHA_WATCHLIST.",
        )
        parser.add_argument(
            "--portfolio-id",
            type=int,
            default=None,
            help="Use this portfolio's real cash/position/risk context (still read-only).",
        )
        parser.add_argument(
            "--no-news",
            action="store_true",
            help="Skip the RSS news tool (faster, uses neutral sentiment).",
        )

    def handle(self, *args, **options) -> None:
        from django.conf import settings

        tickers = [t.upper() for t in (options["tickers"] or settings.ALPHA_WATCHLIST)]
        orchestrator = AlphaAgentOrchestrator()

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("AlphaAgent dry run - read only"))
        self.stdout.write(f"  provider     : {orchestrator.provider}")
        self.stdout.write(f"  model        : {settings.AI_CONFIG.get('MODEL')}")
        self.stdout.write(f"  configured   : {orchestrator.is_configured}")
        self.stdout.write(f"  tickers      : {', '.join(tickers)}")
        self.stdout.write(
            self.style.WARNING("  (no trades will be placed and no rows will be written)")
        )

        portfolio = None
        positions = {}
        if options["portfolio_id"] is not None:
            try:
                portfolio = Portfolio.objects.select_related("user").get(pk=options["portfolio_id"])
            except Portfolio.DoesNotExist as exc:
                raise CommandError(f"Portfolio {options['portfolio_id']} does not exist") from exc
            positions = replay_positions(portfolio)
            self.stdout.write(
                f"  portfolio    : #{portfolio.id} ({portfolio.user.username}), "
                f"cash ${portfolio.balance_usd}, risk {portfolio.risk_profile}"
            )
        self.stdout.write("")

        failures = 0
        for ticker in tickers:
            self.stdout.write(self.style.MIGRATE_HEADING(f"── {ticker} " + "─" * 40))

            position = positions.get(ticker)
            asset = (
                Asset.objects.filter(portfolio=portfolio, ticker=ticker).first()
                if portfolio
                else None
            )
            quote = get_latest_quote(
                ticker,
                fallback_price=asset.avg_purchase_price if asset else None,
            )
            news = None
            if not options["no_news"]:
                news = build_news_report(ticker)
                self.stdout.write(
                    f"  news      : {news.headline_count} headlines, "
                    f"sentiment {news.sentiment.label} "
                    f"(score {news.sentiment.score:+.3f}) "
                    f"from {', '.join(news.sources_used) or 'no source'}"
                )

            context = DecisionContext(
                ticker=ticker,
                risk_profile=portfolio.risk_profile if portfolio else "medium",
                cash_balance=portfolio.balance_usd if portfolio else Decimal("10000.00"),
                position_amount=position.amount if position else Decimal("0"),
                avg_purchase_price=position.avg_cost if position else Decimal("0"),
                current_price=quote.price if quote else None,
                price_change_pct=quote.change_pct if quote else None,
                max_trade_budget_usd=(
                    portfolio.max_trade_budget_usd if portfolio else Decimal("500.00")
                ),
                daily_loss_limit_usd=(
                    portfolio.daily_loss_limit_usd if portfolio else Decimal("500.00")
                ),
                news_report=news,
                portfolio_id=portfolio.id if portfolio else None,
            )
            self.stdout.write(
                f"  price     : {quote.price if quote else 'unavailable'}"
                + (f" ({quote.source}, {quote.change_pct:+.2f}%)" if quote else "")
            )

            try:
                result = run_alpha_agent(context)
            except Exception as exc:
                failures += 1
                self.stdout.write(self.style.ERROR(f"  FAILED: {type(exc).__name__}: {exc}"))
                continue

            if result.used_fallback:
                failures += 1
                style = self.style.WARNING
                self.stdout.write(style(f"  source    : FALLBACK ({result.error})"))
            else:
                style = self.style.SUCCESS
                self.stdout.write(style("  source    : CrewAI (live LLM)"))

            self.stdout.write(
                "  proposal  : "
                + json.dumps(result.proposal.to_payload(), ensure_ascii=False)[:400]
            )
            self.stdout.write(f"  tokens    : {result.tokens_used}")
            self.stdout.write(f"  cost      : ${result.api_cost_usd}")
            self.stdout.write(f"  latency   : {result.latency_ms} ms")
            self.stdout.write("  reasoning :")
            for line in (result.reasoning or "").splitlines():
                self.stdout.write(f"      {line}")
            self.stdout.write("")

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Done. {len(tickers) - failures}/{len(tickers)} used the live LLM."
            )
        )
        if failures:
            self.stdout.write(
                self.style.WARNING("Some tickers fell back - see the source/error lines above.")
            )
