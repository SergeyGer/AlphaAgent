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
# Public surface
# ---------------------------------------------------------------------------
# The Phase 1, 3 and 4 models are imported at the bottom of this module so
# Django's app loader registers them, and they are re-exported so callers can
# keep importing the whole schema from ``core.models``. Naming them here makes
# that re-export an explicit contract instead of an incidental side effect; it
# is also what tells linters that the bottom-of-file imports are intentional.
__all__ = [
    "ZERO_USD",
    "AICostState",
    "AIUsageLedger",
    "AgentDecisionLog",
    "AgentRole",
    "AllocationSubmission",
    "AllocationSubmissionStatus",
    "Asset",
    "CashFlow",
    "CashFlowType",
    "Claim",
    "ClaimStance",
    "ClaimType",
    "DecidedVia",
    "DecisionAction",
    "DecisionExecutionState",
    "DecisionOutcome",
    "DecisionRecord",
    "DecisionRun",
    "DecisionStatus",
    "EventOutbox",
    "EvidenceItem",
    "EvidenceSourceType",
    "ExecutedBy",
    "ExecutionAttempt",
    "Experiment",
    "ExperimentEvaluationMode",
    "ExperimentRun",
    "ExperimentRunStatus",
    "ExperimentStatus",
    "ExperimentVariant",
    "GuardRecord",
    "GuardStatus",
    "InputSnapshot",
    "InstrumentClassification",
    "InstrumentClassificationSource",
    "MarketSentiment",
    "OutboxStatus",
    "OutcomeEvaluation",
    "OutcomeEvaluationState",
    "OutcomeMaturity",
    "OutcomeRecord",
    "PlanApproval",
    "PlanApprovalDecision",
    "PlanExecutionAttempt",
    "PlanLegPriority",
    "PlanLegStatus",
    "PlanSide",
    "PlanStatus",
    "Portfolio",
    "PortfolioPlan",
    "PortfolioPlanLeg",
    "PortfolioSnapshot",
    "PositionThesis",
    "PrimaryMetricFamily",
    "Rebuttal",
    "RebuttalResponseType",
    "RecommendationApproval",
    "RecommendationStatus",
    "RiskProfile",
    "RunEvent",
    "RunEventType",
    "RunMode",
    "RunSource",
    "RunStage",
    "TelegramLink",
    "ThesisStatus",
    "TradeRecommendation",
    "Transaction",
    "TxType",
]

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
    #: Ceiling on total invested capital, as a percentage of equity.
    #:
    #: The per-trade limit alone is unbounded across a sequence: Beat sweeps every
    #: fifteen minutes, so twenty individually-permitted 5% buys reach fully
    #: invested without any single decision looking wrong. This is the cumulative
    #: control ADR-0001 lists as missing.
    max_portfolio_exposure_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("90.00"),
        help_text="Maximum percentage of equity that may be held in positions.",
    )
    #: Ceiling on one instrument, as a percentage of equity. Concentration is a
    #: different risk from exposure: a portfolio 90% invested in one name passes
    #: every per-trade check and is not diversified at all.
    max_ticker_concentration_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("30.00"),
        help_text="Maximum percentage of equity that may be held in one instrument.",
    )
    #: Ceiling on one sector, as a percentage of equity.
    #:
    #: ADR-0015 recorded that no sector limit existed: two 25% positions in
    #: highly correlated names pass both previous ceilings and are not
    #: diversified in any meaningful sense. This is the Phase 4 control that
    #: closes it. It can only be evaluated for instruments whose sector is
    #: explicitly classified; unclassified exposure is reported as incomplete,
    #: never as a zero.
    max_sector_concentration_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("40.00"),
        help_text="Maximum percentage of equity that may be held in one classified sector.",
    )
    #: Ceiling on one rebalance plan's total traded notional, as a percentage of
    #: equity. Both buys and sells count, matching the Phase 3 turnover formula.
    max_turnover_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("25.00"),
        help_text="Maximum traded notional in one allocation plan, as a percentage of equity.",
    )
    #: Minimum uninvested cash after an allocation plan, as a percentage of
    #: equity. This is a buffer, not a target: it can be stricter than the
    #: exposure ceiling allows (90% exposure implies 10% cash).
    min_cash_buffer_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("10.00"),
        help_text="Minimum cash to retain after an allocation plan, as a percentage of equity.",
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
            # A ceiling is a percentage of equity, so 0-100 is the only range
            # that means anything. The database enforces it as a last line of
            # defence behind the guard, the same way the other limits are.
            models.CheckConstraint(
                condition=Q(max_portfolio_exposure_pct__gte=0)
                & Q(max_portfolio_exposure_pct__lte=100),
                name="portfolio_exposure_pct_range",
            ),
            models.CheckConstraint(
                condition=Q(max_ticker_concentration_pct__gte=0)
                & Q(max_ticker_concentration_pct__lte=100),
                name="portfolio_concentration_pct_range",
            ),
            models.CheckConstraint(
                condition=Q(max_sector_concentration_pct__gte=0)
                & Q(max_sector_concentration_pct__lte=100),
                name="portfolio_sector_pct_range",
            ),
            models.CheckConstraint(
                condition=Q(max_turnover_pct__gte=0) & Q(max_turnover_pct__lte=100),
                name="portfolio_turnover_pct_range",
            ),
            models.CheckConstraint(
                condition=Q(min_cash_buffer_pct__gte=0) & Q(min_cash_buffer_pct__lte=100),
                name="portfolio_cash_buffer_pct_range",
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
    #: Fee charged on this fill, if one was recorded.
    #:
    #: NULL and zero are different claims and the distinction is deliberate: a
    #: zero says a fee was measured and it was nil, while NULL says no fee was
    #: recorded at all. Existing rows are NULL after the additive migration
    #: because no fee was ever recorded for them; backfilling a zero would turn
    #: "unknown" into a measurement nobody made. Phase 3's accounting reports
    #: ``fees_complete=false`` when any fill in the window is NULL.
    fee = models.DecimalField(
        max_digits=12,
        decimal_places=8,
        null=True,
        blank=True,
        help_text="Fee charged on this fill. NULL means no fee was recorded; 0 means a zero fee was recorded.",
    )
    executed_by = models.CharField(max_length=10, choices=ExecutedBy.choices)
    #: Stable identity of the decision this fill came from.
    #:
    #: Celery redelivery, a repeated approval click and a retried plan leg all
    #: reach the execution service with the same intent. The unique constraint is
    #: the database-level guarantee that the second attempt returns the first
    #: row instead of adding another fill; the service checks it first so the
    #: common case is a read rather than an IntegrityError.
    execution_key = models.CharField(max_length=200, null=True, blank=True, unique=True)
    #: The market observation behind ``price``. Null means the producer did not
    #: record it (legacy or an explicit paper estimate); it is never filled with
    #: the execution timestamp to make an untimed fill look observed.
    price_observed_at = models.DateTimeField(null=True, blank=True)
    price_retrieved_at = models.DateTimeField(null=True, blank=True)
    price_source = models.CharField(max_length=64, blank=True, default="")
    price_currency = models.CharField(max_length=8, blank=True, default="USD")
    #: Canonical hash of the execution request that produced this row. A key
    #: reused with a different request is a conflict, not a duplicate.
    execution_request_hash = models.CharField(max_length=64, blank=True, default="")
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
            models.CheckConstraint(
                condition=Q(fee__isnull=True) | Q(fee__gte=0),
                name="tx_fee_non_negative",
            ),
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


class AICostState(models.TextChoices):
    """Lifecycle of one logical model call in the daily spend ledger.

    ``reserved``  an upper bound is held before the provider is called.
    ``billed``    the provider reported the charge; this is a measurement.
    ``estimated`` the provider reported tokens but no dollars; the price-table
                  figure is stored and labelled an estimate, never a bill.
    ``unknown``   no usable cost was reported. The reservation stays held (so
                  the ceiling remains conservative) and the row carries a typed
                  reason. This is never stored as a measured zero.
    """

    RESERVED = "reserved", "Reserved"
    BILLED = "billed", "Billed"
    ESTIMATED = "estimated", "Estimated"
    UNKNOWN = "unknown", "Unknown"


class AIUsageLedger(models.Model):
    """Durable, idempotent daily AI-spend accounting.

    One row per **logical** call, keyed by ``call_key``. The key is unique so a
    Celery redelivery, a retried submission or a replay cannot book the same
    call twice. The reservation is written before the provider is called; the
    row is then reconciled to one of four states and never moved backwards. A
    process restart loses nothing because the state lives here, not in memory.

    ``reserved_usd`` is the amount currently *held* against the ceiling. It is
    released when the charge becomes billed or estimated, and deliberately kept
    for an ``unknown`` charge: the operator cannot prove the call was free, so
    the conservative bound remains committed. ``reserved_upper_bound_usd``
    preserves the bound the run was authorised against, even after release.

    Identity is explicit. ``scope``/``scope_key`` name the allowance a call was
    booked against (a portfolio for an allocation, an experiment namespace for
    an arm attempt), so a cumulative experiment ceiling can be checked without
    reinterpreting the call key. ``pricing_version`` identifies the price table
    the estimate came from, and ``billed_cost_unknown_reason`` records why the
    billed field is null even when an estimate exists.
    """

    call_key = models.CharField(max_length=255, unique=True)
    #: UTC date the reservation belongs to. Set at reserve time, so a call that
    #: settles after midnight is still counted against the day it was authorised
    #: on rather than silently moving to the next day's budget.
    day = models.DateField(db_index=True, default=timezone.localdate)
    provider = models.CharField(max_length=64, blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="")
    stage = models.CharField(max_length=64, blank=True, default="")
    #: Which allowance the call belongs to (``decision``, ``allocation`` or
    #: ``experiment``) and the durable key of that allowance (a portfolio id or
    #: an experiment execution namespace). A scope can carry its own cumulative
    #: ceiling in addition to the daily one.
    scope = models.CharField(max_length=32, blank=True, default="")
    scope_key = models.CharField(max_length=128, blank=True, default="", db_index=True)
    state = models.CharField(
        max_length=16, choices=AICostState.choices, default=AICostState.RESERVED
    )
    reserved_upper_bound_usd = models.DecimalField(
        max_digits=14, decimal_places=6, default=Decimal("0")
    )
    reserved_usd = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal("0"))
    billed_usd = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    estimated_usd = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    unknown_reason = models.CharField(max_length=64, blank=True, default="")
    #: Why the billed field is null, kept even when a price-table estimate was
    #: recorded. ``unknown_reason`` is only populated for an ``unknown`` state.
    billed_cost_unknown_reason = models.CharField(max_length=64, blank=True, default="")
    cost_basis = models.CharField(max_length=32, blank=True, default="")
    #: Identifies the price table the estimate was calculated from. A rate edit
    #: changes the version, so an old estimate can be told from a re-priced one.
    pricing_version = models.CharField(max_length=64, blank=True, default="")
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    #: Measured and estimated token counts are stored separately as well as
    #: summed: a mixed provider reply must not be relabelled as wholly measured.
    measured_input_tokens = models.IntegerField(default=0)
    measured_output_tokens = models.IntegerField(default=0)
    estimated_input_tokens = models.IntegerField(default=0)
    estimated_output_tokens = models.IntegerField(default=0)
    tokens_measured = models.BooleanField(default=False)
    #: Set immediately before a billable provider call. A replayed reservation
    #: that already dispatched must not dispatch a second time.
    dispatched_at = models.DateTimeField(null=True, blank=True)
    #: Set when a confirmed provider charge replaced an estimate or an unknown
    #: outcome, and counting how many accounting updates the row has had.
    confirmed_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "AI usage ledger entry"
        verbose_name_plural = "AI usage ledger"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["day", "state"], name="ai_ledger_day_state_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(reserved_upper_bound_usd__gte=0),
                name="ai_ledger_upper_bound_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(reserved_usd__gte=0), name="ai_ledger_reserved_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(billed_usd__isnull=True) | Q(billed_usd__gte=0),
                name="ai_ledger_billed_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(estimated_usd__isnull=True) | Q(estimated_usd__gte=0),
                name="ai_ledger_estimated_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.call_key} [{self.state}]"


class AgentDecisionLog(models.Model):
    """Explainable-AI audit record for one agent invocation."""

    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.CASCADE,
        related_name="decision_logs",
    )
    #: The instrument this decision was about.
    #:
    #: Denormalised on purpose. The ticker used to be reachable only through
    #: ``transaction``, which meant a decision that executed nothing - a HOLD, or
    #: a HALT when a limit fired - had no ticker at all. That is precisely the
    #: set of decisions most worth reviewing afterwards, so the audit trail could
    #: not answer "what was this about?" for the rows that matter most.
    #:
    #: It also gives decision attribution something to join on: you cannot score
    #: whether a decision was right without knowing what it was a decision about.
    ticker = models.CharField(max_length=32, blank=True, default="", db_index=True)
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
    # The billed split behind `tokens_used`. Recorded so the stored cost is
    # arithmetic that can be re-checked rather than a figure taken on trust, and
    # so a rate change can be re-applied to history. `input_tokens` is the full
    # prompt count and already includes the cached and cache-write portions.
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cached_input_tokens = models.IntegerField(default=0)
    cache_write_tokens = models.IntegerField(default=0)
    #: The **billed** charge, when one was reported. Nullable on purpose: a run
    #: whose provider reported no charge has an *unknown* cost, and storing a zero
    #: there would turn a missing measurement into a measured one. Rows written
    #: before this column was nullable carry the old default zero; their basis is
    #: blank, which is how a reader can tell they were not re-measured.
    api_cost_usd = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True)
    #: Price-table arithmetic over the token split. An estimate, never a bill.
    estimated_cost_usd = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True)
    #: ``billed_provider_reported`` | ``price_table_estimate`` | ``unknown_pricing``
    #: | ``unknown``. Blank means the basis was not recorded.
    cost_basis = models.CharField(max_length=32, blank=True, default="")
    #: The ``AIUsageLedger.call_key`` this row was booked under, when the call
    #: went through the daily-spend ledger. The ledger is the authoritative
    #: count for these calls, so ``services.budget.cost_state`` excludes linked
    #: legacy rows instead of adding the same charge twice. Blank means the row
    #: predates the ledger and is counted from ``api_cost_usd`` / tokens.
    cost_ledger_key = models.CharField(max_length=255, blank=True, default="", db_index=True)
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


class ThesisStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    CLOSED = "CLOSED", "Closed"
    INVALIDATED = "INVALIDATED", "Invalidated"


class PositionThesis(models.Model):
    """Why we hold this instrument, and what would prove us wrong.

    Added because the agent had no memory across runs. Each sweep built a fresh
    crew with no knowledge of what it decided last time or why, so the system
    could sell a position at 10:00 and buy it back at 10:15 without noticing -
    not because it changed its mind, but because it had no record of having one.

    ``memory=False`` on the crew is deliberate (see ADR-0007): a conversational
    memory would grow without bound and make decisions irreproducible. This is the
    narrow version of the same idea - one durable paragraph per open position,
    carried into every run as context, and written by the agent that opened it.

    A thesis is a claim, not a summary. ``invalidation`` exists so the bear has
    something specific to attack: "the bull case is wrong" is not a criticism,
    "gross margin below 40% invalidates this" is.
    """

    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.CASCADE,
        related_name="theses",
    )
    ticker = models.CharField(max_length=32, db_index=True)

    #: Why the position was opened, in the words of the agent that opened it.
    thesis = models.TextField()
    #: The observable condition that would make the thesis wrong. May be blank -
    #: an agent that cannot name one has told you something useful.
    invalidation = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=12, choices=ThesisStatus.choices, default=ThesisStatus.OPEN, db_index=True
    )

    #: Provenance. Which decision opened this, and which one closed it.
    opened_by = models.ForeignKey(
        AgentDecisionLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="theses_opened",
    )
    closed_by = models.ForeignKey(
        AgentDecisionLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="theses_closed",
    )

    opened_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "Position thesis"
        verbose_name_plural = "Position theses"
        ordering = ["-opened_at"]
        constraints = [
            # One live thesis per instrument. Superseded theses stay as history
            # rather than being overwritten, so the record shows what we believed
            # at each point rather than only what we believe now.
            models.UniqueConstraint(
                fields=["portfolio", "ticker"],
                condition=Q(status=ThesisStatus.OPEN),
                name="one_open_thesis_per_portfolio_ticker",
            ),
        ]
        indexes = [
            models.Index(fields=["portfolio", "status"], name="thesis_portfolio_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.ticker} [{self.status}] {self.thesis[:48]}"

    @classmethod
    def open_for(cls, portfolio, ticker: str):
        """The live thesis for this instrument, or ``None``."""
        return cls.objects.filter(
            portfolio=portfolio, ticker=(ticker or "").upper(), status=ThesisStatus.OPEN
        ).first()


class DecisionOutcome(models.Model):
    """What actually happened after a decision, measured at a fixed horizon.

    The audit trail records *why* every decision was made, and does so well. It
    recorded nothing about whether any of them were any good: a SELL that avoided
    a drawdown and a SELL that missed a rally both read as well-argued decisions,
    months later, in the same table.

    This is the other half. For each decision and each horizon, it stores what the
    instrument did next, what that meant for the action taken, and what a broad
    benchmark did over the same window - so the system can be scored rather than
    believed.

    One row per (decision, horizon) rather than per decision, because a decision
    can look wrong at one day and right at thirty. Collapsing them would hide
    exactly the disagreement worth seeing.
    """

    #: Horizons in calendar days. Short and long deliberately: one day measures
    #: whether the timing was lucky, thirty measures whether the thesis held.
    HORIZONS = (1, 7, 30)

    decision = models.ForeignKey(
        AgentDecisionLog,
        on_delete=models.CASCADE,
        related_name="outcomes",
    )
    horizon_days = models.PositiveSmallIntegerField()

    #: Denormalised so a scorecard can be aggregated without joining the log's
    #: transaction, which a HOLD does not have.
    ticker = models.CharField(max_length=32, db_index=True)
    #: BUY / SELL / HOLD / OTHER, normalised from the log's free-text action.
    action = models.CharField(max_length=16)

    price_at_decision = models.DecimalField(max_digits=18, decimal_places=8)
    price_at_horizon = models.DecimalField(max_digits=18, decimal_places=8)
    #: When each instrument price was *observed*, not when the row was written.
    #: ``evaluated_at`` is the evaluation instant; it is not a market observation,
    #: and using it as one (as the Phase 1 bridge did) made the interval look
    #: verified when nothing about the observation had been recorded. Session-date
    #: precision: the provider publishes daily closes.
    price_observed_at_decision = models.DateTimeField(null=True, blank=True)
    price_observed_at = models.DateTimeField(null=True, blank=True)
    price_source = models.CharField(max_length=64, blank=True, default="")

    #: What the instrument did over the window. Sign is the market's, not ours.
    forward_return_pct = models.DecimalField(max_digits=12, decimal_places=4)
    #: What that return was worth *to this action*: positive for a BUY when the
    #: price rose, and positive for a SELL when it fell. This is the column that
    #: answers "was the call right?", and it is zero for a HOLD by construction.
    decision_return_pct = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))

    #: Broad-market return over the same window, for context. Null when the
    #: benchmark could not be priced - a missing benchmark is not a zero one.
    benchmark_return_pct = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )

    #: Benchmark provenance (FIX-07C). A benchmark *return* with no observation
    #: record cannot be interval-checked against the instrument's window: two
    #: prices from different sessions can produce a plausible number for the
    #: wrong interval. These columns store the prices, their observation dates and
    #: the source, so the report can verify the interval at session-date precision
    #: (intraday instants are still not recorded) instead of assuming it.
    benchmark_price_at_decision = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    benchmark_price_at_horizon = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    benchmark_observed_at_decision = models.DateTimeField(null=True, blank=True)
    benchmark_price_observed_at = models.DateTimeField(null=True, blank=True)
    benchmark_price_source = models.CharField(max_length=64, blank=True, default="")

    #: Which definition of return produced this row. ``attribution.v2`` measures
    #: an executed decision from the recorded fill's price and timestamp rather
    #: than from the day's close at proposal time.
    method_version = models.CharField(max_length=32, default="attribution.v1")
    #: ``execution_fill`` or ``proposal_close``. Empty on legacy rows written
    #: before the basis was recorded; they are never relabelled.
    measurement_basis = models.CharField(max_length=32, blank=True, default="")
    #: The instant the horizon was measured from: the fill timestamp for an
    #: executed decision, the proposal timestamp otherwise.
    reference_at = models.DateTimeField(null=True, blank=True)
    #: The fill this score was measured from. Null is meaningful: a HOLD or a
    #: rejected proposal traded nothing and must not be given a fabricated fill.
    fill = models.ForeignKey(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decision_outcomes",
    )
    #: Monotonic revision and the canonical hash of the recorded values. A retry
    #: of identical values keeps them; a genuine correction increments the
    #: revision and archives the prior provenance in ``provenance``.
    revision = models.PositiveIntegerField(default=1)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    #: Provenance that must survive a correction: the basis and observation
    #: times used, plus a ``prior_revisions`` list when this row superseded one.
    provenance = models.JSONField(default=dict, blank=True)

    #: True when the direction was right, False when wrong, NULL when the action
    #: made no directional claim. A HOLD is not a call that can be scored this
    #: way, and counting it as a miss would be dishonest.
    directional_hit = models.BooleanField(null=True, blank=True)

    evaluated_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Decision outcome"
        verbose_name_plural = "Decision outcomes"
        ordering = ["-evaluated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["decision", "horizon_days"], name="outcome_unique_per_horizon"
            ),
        ]
        indexes = [
            models.Index(fields=["ticker", "horizon_days"], name="outcome_ticker_horizon_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.ticker} {self.action} +{self.horizon_days}d = {self.decision_return_pct}%"

    @property
    def beat_benchmark(self) -> bool | None:
        """Whether the action produced more than the market did over the window.

        ``None`` when there is no benchmark, rather than ``False``: an unknown
        comparison is not a loss.
        """
        if self.benchmark_return_pct is None:
            return None
        return self.decision_return_pct > self.benchmark_return_pct


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
    #: The Phase 1/2 decision run this proposal belongs to, when one was recorded.
    #: Approval, rejection and expiry all update that run, so the advisory path
    #: has one lifecycle rather than a proposal with no traceable analysis.
    decision_run = models.ForeignKey(
        "core.DecisionRun",
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
    #: The version of the terms shown to the reviewer. A price move beyond the
    #: approved tolerance increments this and requires a new approval; the old
    #: ``RecommendationApproval`` row stays readable as the superseded terms.
    terms_version = models.PositiveIntegerField(default=1)
    #: Observation provenance of the price displayed on the recommendation.
    #: Null means the producer did not record it; it is never filled with "now"
    #: to make an untimed proposal look current.
    quote_observed_at = models.DateTimeField(null=True, blank=True)
    quote_retrieved_at = models.DateTimeField(null=True, blank=True)
    price_source = models.CharField(max_length=64, blank=True, default="")
    price_currency = models.CharField(max_length=8, blank=True, default="USD")
    #: The drift tolerance that applies to an approval of these terms. Null
    #: means the execution policy default in force at approval.
    price_tolerance_pct = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
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


# ---------------------------------------------------------------------------
# Phase 4: allocation tables (instrument classification, plans, plan legs)
# ---------------------------------------------------------------------------
# Imported last for the same reason as the Phase 1 and Phase 3 models. See
# core/allocation_models.py for why the sector lookup is explicit rather than
# inferred and why a plan leg has a one-to-one ledger row.
from core.allocation_models import (  # noqa: E402
    AllocationSubmission,
    AllocationSubmissionStatus,
    InstrumentClassification,
    InstrumentClassificationSource,
    PlanApproval,
    PlanApprovalDecision,
    PlanExecutionAttempt,
    PlanLegPriority,
    PlanLegStatus,
    PlanSide,
    PlanStatus,
    PortfolioPlan,
    PortfolioPlanLeg,
)

# ---------------------------------------------------------------------------
# Phase 1: decision runs, evidence, claims, guard records, outcomes
# ---------------------------------------------------------------------------
# Imported last and re-exported so Django's app loader registers the models while
# the Phase 0 schema above stays untouched (DATA-04). See core/decision_models.py
# for why they live in a separate module.
from core.decision_models import (  # noqa: E402
    AgentRole,
    Claim,
    ClaimStance,
    ClaimType,
    DecisionAction,
    DecisionExecutionState,
    DecisionRecord,
    DecisionRun,
    DecisionStatus,
    EvidenceItem,
    EvidenceSourceType,
    ExecutionAttempt,
    GuardRecord,
    GuardStatus,
    InputSnapshot,
    OutcomeEvaluation,
    OutcomeEvaluationState,
    OutcomeMaturity,
    OutcomeRecord,
    Rebuttal,
    RebuttalResponseType,
    RunEvent,
    RunEventType,
    RunMode,
    RunSource,
    RunStage,
)

# ---------------------------------------------------------------------------
# Phase 3: measurement tables (external flows, experiments)
# ---------------------------------------------------------------------------
# Imported last for the same reason as the Phase 1 models: the Phase 0 schema and
# its consumer contract stay untouched, and Django registers these tables through
# this module. See core/measurement_models.py.
from core.event_models import EventOutbox, OutboxStatus  # noqa: E402
from core.measurement_models import (  # noqa: E402
    CashFlow,
    CashFlowType,
    Experiment,
    ExperimentEvaluationMode,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
    ExperimentVariant,
    PrimaryMetricFamily,
)


class RecommendationApproval(models.Model):
    """The exact recommendation terms a human approved, frozen at that moment.

    The recommendation row is mutable: a later price move changes what the next
    reviewer sees. This row is not. It names the recommendation version, the
    side, quantity, reference price, tolerance, expiry and the observation
    provenance the reviewer was looking at, plus the policy and portfolio
    versions that decided permission. ``accepted=False`` records an approval
    attempt that was superseded because the market had moved beyond the terms;
    the transaction link is set only for the attempt that actually executed.
    """

    id = models.BigAutoField(primary_key=True)
    recommendation = models.ForeignKey(
        TradeRecommendation,
        on_delete=models.CASCADE,
        related_name="approvals",
    )
    recommendation_version = models.PositiveIntegerField(default=1)
    side = models.CharField(max_length=4, choices=TxType.choices)
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    reference_price = models.DecimalField(max_digits=18, decimal_places=8)
    tolerance_pct = models.DecimalField(max_digits=6, decimal_places=4)
    expires_at = models.DateTimeField(null=True, blank=True)
    quote_observed_at = models.DateTimeField(null=True, blank=True)
    quote_retrieved_at = models.DateTimeField(null=True, blank=True)
    quote_source = models.CharField(max_length=64, blank=True, default="")
    policy_version = models.CharField(max_length=64, blank=True, default="")
    portfolio_version = models.CharField(max_length=64, blank=True, default="")
    terms_hash = models.CharField(max_length=64, blank=True, default="")
    #: The exact server-issued reference the reviewer's screen or Telegram
    #: button carried. It covers the recommendation, portfolio, version, side,
    #: quantity, reference price, tolerance and expiry, so a stale or foreign
    #: action cannot be applied to refreshed terms.
    terms_ref = models.CharField(max_length=64, blank=True, default="", db_index=True)
    #: The idempotency key submitted with this approval, if one was supplied.
    #: A repeated authenticated approval must present the same key before the
    #: original execution is replayed.
    idempotency_key = models.CharField(max_length=160, blank=True, default="")
    #: The quote the approval was evaluated against. This is a different
    #: observation from ``quote_observed_at`` (the displayed reference price):
    #: storing both makes "what the operator saw" and "what the drift check saw"
    #: separately auditable, each with its own observation instant.
    evaluated_price = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)
    evaluated_quote_observed_at = models.DateTimeField(null=True, blank=True)
    evaluated_quote_retrieved_at = models.DateTimeField(null=True, blank=True)
    evaluated_quote_source = models.CharField(max_length=64, blank=True, default="")
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendation_approvals",
    )
    decided_at = models.DateTimeField(default=timezone.now, db_index=True)
    accepted = models.BooleanField(default=False)
    reason_code = models.CharField(max_length=64, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    transaction = models.OneToOneField(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendation_approval",
    )
    legacy_unknown_provenance = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Recommendation approval"
        verbose_name_plural = "Recommendation approvals"
        ordering = ["-decided_at", "-id"]
        indexes = [
            models.Index(fields=["recommendation", "-decided_at"], name="reco_approval_recent_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="reco_approval_qty_positive"),
            models.CheckConstraint(
                condition=Q(reference_price__gt=0), name="reco_approval_price_positive"
            ),
            models.CheckConstraint(
                condition=Q(tolerance_pct__gte=0), name="reco_approval_tolerance_non_negative"
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.recommendation_id} v{self.recommendation_version} "
            f"{self.side} {self.quantity} @ {self.reference_price} "
            f"[{'accepted' if self.accepted else self.reason_code or 'pending'}]"
        )
