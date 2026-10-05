"""AlphaAgent domain models.

Four tables back the platform:

``Portfolio``        one per user, holds cash + risk configuration.
``Asset``            a position (ticker/amount/average cost) inside a portfolio.
``Transaction``      an immutable ledger row for every BUY/SELL execution.
``AgentDecisionLog`` the explainable-AI audit trail (chain-of-thought + cost).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
ZERO_USD = Decimal("0.00")


class RiskProfile(models.TextChoices):
    """Investor risk appetite, drives the AI position-sizing policy."""

    LOW = "low", "Conservative"
    MEDIUM = "medium", "Moderate"
    HIGH = "high", "Aggressive"


class TxType(models.TextChoices):
    BUY = "BUY", "Buy"
    SELL = "SELL", "Sell"


class ExecutedBy(models.TextChoices):
    USER = "USER", "User"
    AI = "AI", "AI Agent"


class MarketSentiment(models.TextChoices):
    BULLISH = "BULLISH", "Bullish"
    BEARISH = "BEARISH", "Bearish"
    NEUTRAL = "NEUTRAL", "Neutral"


class Portfolio(models.Model):
    """A user's investment account and its autonomous-trading guardrails."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="portfolio",
        primary_key=False,
    )
    balance_usd = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("10000.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Uninvested cash available for new positions.",
    )
    risk_profile = models.CharField(
        max_length=6,
        choices=RiskProfile.choices,
        default=RiskProfile.MEDIUM,
        db_index=True,
    )
    is_autonomous = models.BooleanField(
        default=False,
        help_text="When True the AI agents may execute trades without human approval.",
    )
    max_trade_allocation_pct = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=Decimal("5.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Maximum share of cash the AI may commit to a single trade (percent).",
    )
    daily_loss_limit_usd = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("500.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Portfolio-level stop-loss: max realised loss per UTC day.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Portfolio"
        verbose_name_plural = "Portfolios"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(balance_usd__gte=0), name="portfolio_balance_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(max_trade_allocation_pct__gte=0) & Q(max_trade_allocation_pct__lte=100),
                name="portfolio_allocation_pct_range",
            ),
            models.CheckConstraint(
                condition=Q(daily_loss_limit_usd__gte=0),
                name="portfolio_loss_limit_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"Portfolio<{self.user.username}> ${self.balance_usd}"

    # -- Derived helpers ---------------------------------------------------
    @property
    def max_trade_budget_usd(self) -> Decimal:
        """Hard cash ceiling for one AI-proposed trade."""
        budget = (self.balance_usd * self.max_trade_allocation_pct / Decimal("100")).quantize(
            Decimal("0.01")
        )
        return max(budget, ZERO_USD)


class Asset(models.Model):
    """A position held inside a portfolio."""

    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.CASCADE,
        related_name="assets",
    )
    ticker = models.CharField(max_length=10, db_index=True)
    amount = models.DecimalField(
        max_digits=18,
        decimal_places=8,
        default=Decimal("0.00000000"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    avg_purchase_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        verbose_name = "Asset"
        verbose_name_plural = "Assets"
        ordering = ["ticker"]
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "ticker"], name="asset_unique_ticker_per_portfolio"
            ),
            models.CheckConstraint(condition=Q(amount__gte=0), name="asset_amount_non_negative"),
            models.CheckConstraint(
                condition=Q(avg_purchase_price__gte=0),
                name="asset_avg_price_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.ticker} x{self.amount} ({self.portfolio.user.username})"

    @property
    def cost_basis_usd(self) -> Decimal:
        return (self.amount * self.avg_purchase_price).quantize(Decimal("0.01"))

    def market_value_usd(self, price: Decimal | None = None) -> Decimal:
        """Position value at ``price`` (falls back to the average cost)."""
        effective = price if price is not None else self.avg_purchase_price
        return (self.amount * effective).quantize(Decimal("0.01"))


class Transaction(models.Model):
    """Immutable ledger entry - written only by the execution guard."""

    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    ticker = models.CharField(max_length=10, db_index=True)
    tx_type = models.CharField(max_length=4, choices=TxType.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=8)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    executed_by = models.CharField(max_length=10, choices=ExecutedBy.choices)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Transaction"
        verbose_name_plural = "Transactions"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["portfolio", "-timestamp"], name="tx_portfolio_recent_idx"),
            models.Index(fields=["portfolio", "ticker"], name="tx_portfolio_ticker_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="tx_amount_positive"),
            models.CheckConstraint(condition=Q(price__gt=0), name="tx_price_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.tx_type} {self.amount} {self.ticker} @ {self.price} ({self.executed_by})"

    @property
    def gross_value_usd(self) -> Decimal:
        """Notional traded (positive for both BUY and SELL)."""
        return (self.amount * self.price).quantize(Decimal("0.01"))

    @property
    def signed_cash_flow_usd(self) -> Decimal:
        """Cash impact on the portfolio balance."""
        value = self.gross_value_usd
        return -value if self.tx_type == TxType.BUY else value


class AgentDecisionLog(models.Model):
    """Explainable-AI audit record for one agent invocation."""

    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.CASCADE,
        related_name="decision_logs",
    )
    transaction = models.OneToOneField(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decision_log",
    )
    reasoning = models.TextField(
        help_text="Full chain-of-thought captured from the reasoning model.",
    )
    # Structured debate output. The bull and bear agents argue from the same
    # evidence; storing both lets the dashboard show *why* the CIO decided as it
    # did instead of presenting a single unexplained verdict.
    bull_case = models.TextField(
        blank=True,
        default="",
        help_text="Bullish argument produced by the research analyst.",
    )
    bear_case = models.TextField(
        blank=True,
        default="",
        help_text="Bearish argument produced by the risk assessor / short seller.",
    )
    action_taken = models.CharField(max_length=255)
    market_sentiment = models.CharField(
        max_length=20,
        choices=MarketSentiment.choices,
        default=MarketSentiment.NEUTRAL,
        db_index=True,
    )
    tokens_used = models.IntegerField(default=0)
    api_cost_usd = models.DecimalField(max_digits=8, decimal_places=5, default=Decimal("0.00000"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Agent decision log"
        verbose_name_plural = "Agent decision logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["portfolio", "-created_at"], name="log_portfolio_recent_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(tokens_used__gte=0), name="log_tokens_non_negative"),
            models.CheckConstraint(condition=Q(api_cost_usd__gte=0), name="log_cost_non_negative"),
        ]

    def __str__(self) -> str:
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {self.action_taken}"


# ---------------------------------------------------------------------------
# Phase 2: dashboard, human-in-the-loop approval and chat integrations
# ---------------------------------------------------------------------------
class PortfolioSnapshot(models.Model):
    """Point-in-time valuation of a portfolio.

    The immutable ledger can reconstruct *positions*, but not their historical
    market value. This table is the time series behind the equity chart, and is
    written by ``capture_portfolio_snapshots_task`` and after every execution.
    """

    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.CASCADE,
        related_name="snapshots",
    )
    total_equity_usd = models.DecimalField(max_digits=16, decimal_places=2)
    cash_balance_usd = models.DecimalField(max_digits=16, decimal_places=2)
    positions_value_usd = models.DecimalField(max_digits=16, decimal_places=2)
    unrealised_pnl_usd = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO_USD)
    realised_pnl_today_usd = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO_USD)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Portfolio snapshot"
        verbose_name_plural = "Portfolio snapshots"
        ordering = ["-captured_at"]
        indexes = [
            models.Index(fields=["portfolio", "-captured_at"], name="snap_portfolio_recent_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.portfolio_id} @ {self.captured_at:%Y-%m-%d %H:%M} = ${self.total_equity_usd}"


class RecommendationStatus(models.TextChoices):
    PENDING = "PENDING", "Awaiting approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    EXPIRED = "EXPIRED", "Expired"
    EXECUTED = "EXECUTED", "Executed"
    BLOCKED = "BLOCKED", "Blocked by guardrail"


class DecidedVia(models.TextChoices):
    WEB = "WEB", "Web dashboard"
    TELEGRAM = "TELEGRAM", "Telegram"
    API = "API", "REST API"
    AUTO = "AUTO", "Automatic"


class TradeRecommendation(models.Model):
    """A trade the AI wants to make, held for human approval.

    This is the advisory counterpart to autonomous execution. **Approving a
    recommendation does not bypass the execution guard** - the trade is
    re-validated against current prices and limits before it reaches the ledger.
    """

    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.CASCADE,
        related_name="recommendations",
    )
    ticker = models.CharField(max_length=10, db_index=True)
    action = models.CharField(max_length=4, choices=TxType.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=8)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    notional_usd = models.DecimalField(max_digits=16, decimal_places=2)
    sentiment = models.CharField(
        max_length=20, choices=MarketSentiment.choices, default=MarketSentiment.NEUTRAL
    )
    reasoning = models.TextField(blank=True)
    status = models.CharField(
        max_length=10,
        choices=RecommendationStatus.choices,
        default=RecommendationStatus.PENDING,
        db_index=True,
    )
    decided_via = models.CharField(max_length=10, choices=DecidedVia.choices, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    decision_log = models.ForeignKey(
        AgentDecisionLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendations",
    )
    transaction = models.OneToOneField(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendation",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Trade recommendation"
        verbose_name_plural = "Trade recommendations"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["portfolio", "status", "-created_at"], name="reco_portfolio_status_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="reco_amount_positive"),
            models.CheckConstraint(condition=Q(price__gt=0), name="reco_price_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.amount} {self.ticker} [{self.status}]"

    @property
    def is_pending(self) -> bool:
        return self.status == RecommendationStatus.PENDING

    @property
    def is_actionable(self) -> bool:
        """Pending and not past its expiry."""
        if not self.is_pending:
            return False
        return not (self.expires_at and self.expires_at <= timezone.now())


class TelegramLink(models.Model):
    """Pairs a Telegram chat with a platform user.

    Users obtain a short-lived ``link_code`` from the API and send
    ``/start <code>`` to the bot; the resulting row is what lets the bot show
    *their* portfolio and act on *their* recommendations.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="telegram_link",
    )
    chat_id = models.BigIntegerField(unique=True, db_index=True)
    telegram_username = models.CharField(max_length=64, blank=True)
    link_code = models.CharField(max_length=32, blank=True, db_index=True)
    # When the current ``link_code`` was issued. The API has always advertised a
    # 15-minute window (it returns ``expires_at``), but the bot's lookup had no
    # time comparison at all, so an unused code stayed valid forever. A leaked
    # code was therefore a permanent credential.
    link_code_issued_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    notify_trades = models.BooleanField(default=True)
    notify_recommendations = models.BooleanField(default=True)
    linked_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Telegram link"
        verbose_name_plural = "Telegram links"
        ordering = ["-linked_at"]

    #: How long an unused ``link_code`` stays valid.
    LINK_CODE_TTL = timedelta(minutes=15)

    def __str__(self) -> str:
        return f"{self.user.username} <-> chat {self.chat_id}"

    def link_code_is_valid(self, now=None) -> bool:
        """Whether the stored ``link_code`` may still be redeemed."""
        if not self.link_code or not self.link_code_issued_at:
            return False
        return (now or timezone.now()) < self.link_code_issued_at + self.LINK_CODE_TTL

    @classmethod
    def redeemable(cls, code: str, now=None):
        """QuerySet of links whose ``code`` is live and unexpired."""
        now = now or timezone.now()
        return cls.objects.filter(
            link_code__iexact=code,
            link_code_issued_at__gte=now - cls.LINK_CODE_TTL,
            is_active=True,
        )
