"""DRF serializers for the AlphaAgent API."""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from core.models import (
    AgentDecisionLog,
    Asset,
    Portfolio,
    PortfolioSnapshot,
    TelegramLink,
    TradeRecommendation,
    Transaction,
)

__all__ = [
    "AgentDecisionLogSerializer",
    "AssetSerializer",
    "PortfolioSerializer",
    "ToggleAutonomySerializer",
    "TransactionSerializer",
]

CENT = Decimal("0.01")


class AssetSerializer(serializers.ModelSerializer):
    """A held position enriched with live valuation."""

    ticker = serializers.CharField(read_only=True)
    market_price = serializers.SerializerMethodField()
    market_value_usd = serializers.SerializerMethodField()
    cost_basis_usd = serializers.SerializerMethodField()
    unrealised_pnl_usd = serializers.SerializerMethodField()
    unrealised_pnl_pct = serializers.SerializerMethodField()
    price_source = serializers.SerializerMethodField()
    allocation_pct = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = [
            "id",
            "ticker",
            "amount",
            "avg_purchase_price",
            "market_price",
            "price_source",
            "cost_basis_usd",
            "market_value_usd",
            "unrealised_pnl_usd",
            "unrealised_pnl_pct",
            "allocation_pct",
        ]
        read_only_fields = fields

    # -- helpers -----------------------------------------------------------
    def _quote(self, obj: Asset):
        return (self.context.get("price_map") or {}).get(obj.ticker.upper())

    def _price(self, obj: Asset) -> Decimal:
        quote = self._quote(obj)
        if quote is not None:
            return quote.price
        return obj.avg_purchase_price

    def get_market_price(self, obj: Asset) -> str:
        return str(self._price(obj).quantize(CENT))

    def get_price_source(self, obj: Asset) -> str:
        quote = self._quote(obj)
        return quote.source if quote is not None else "cost_basis"

    def get_cost_basis_usd(self, obj: Asset) -> str:
        return str(obj.cost_basis_usd)

    def get_market_value_usd(self, obj: Asset) -> str:
        return str(obj.market_value_usd(self._price(obj)))

    def get_unrealised_pnl_usd(self, obj: Asset) -> str:
        return str((obj.market_value_usd(self._price(obj)) - obj.cost_basis_usd).quantize(CENT))

    def get_unrealised_pnl_pct(self, obj: Asset) -> str:
        basis = obj.cost_basis_usd
        if basis <= 0:
            return "0.00"
        pnl = obj.market_value_usd(self._price(obj)) - basis
        return str((pnl / basis * Decimal("100")).quantize(CENT))

    def get_allocation_pct(self, obj: Asset) -> str:
        equity = (self.context.get("metrics") or {}).get("total_equity_usd")
        if not equity:
            return "0.00"
        total = Decimal(str(equity))
        if total <= 0:
            return "0.00"
        return str((obj.market_value_usd(self._price(obj)) / total * Decimal("100")).quantize(CENT))


class PortfolioSerializer(serializers.ModelSerializer):
    """The authenticated user's portfolio + nested assets + live metrics."""

    username = serializers.CharField(source="user.username", read_only=True)
    risk_profile_display = serializers.CharField(source="get_risk_profile_display", read_only=True)
    assets = AssetSerializer(many=True, read_only=True)
    metrics = serializers.SerializerMethodField()
    is_autonomous = serializers.BooleanField(read_only=True)
    max_trade_budget_usd = serializers.SerializerMethodField()

    class Meta:
        model = Portfolio
        fields = [
            "id",
            "username",
            "balance_usd",
            "risk_profile",
            "risk_profile_display",
            "is_autonomous",
            "max_trade_allocation_pct",
            "max_trade_budget_usd",
            "daily_loss_limit_usd",
            "created_at",
            "metrics",
            "assets",
        ]
        read_only_fields = fields

    def get_metrics(self, obj: Portfolio) -> dict:
        return self.context.get("metrics") or {}

    def get_max_trade_budget_usd(self, obj: Portfolio) -> str:
        return str(obj.max_trade_budget_usd)


class ToggleAutonomySerializer(serializers.Serializer):
    """Explicit payload for the safe autonomy toggle.

    Two accepted forms:

    * ``{"is_autonomous": true|false}`` - set an explicit state (preferred).
    * ``{}`` - flip the current state.

    Turning autonomy **on** additionally requires ``confirm=true``, so a stray
    request can never silently hand trading control to the AI.
    """

    is_autonomous = serializers.BooleanField(
        required=False,
        allow_null=True,
        default=None,
        help_text="Target state. Omit to flip the current value.",
    )
    confirm = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Must be true when the request results in autonomy being ENABLED.",
    )

    def validate(self, attrs: dict) -> dict:
        portfolio: Portfolio = self.context["portfolio"]

        requested = attrs.get("is_autonomous")
        target = (not portfolio.is_autonomous) if requested is None else bool(requested)
        attrs["is_autonomous"] = target
        attrs["changed"] = target != portfolio.is_autonomous

        if target and not attrs.get("confirm"):
            raise serializers.ValidationError(
                {
                    "confirm": (
                        "Enabling autonomous trading lets the AI execute trades without "
                        "further approval. Resend with confirm=true to proceed."
                    )
                }
            )

        if target and not portfolio.is_autonomous:
            blockers: list[str] = []
            if portfolio.balance_usd <= 0:
                blockers.append("portfolio cash balance is $0.00")
            if portfolio.max_trade_allocation_pct <= 0:
                blockers.append("max_trade_allocation_pct is 0%")
            if blockers:
                raise serializers.ValidationError(
                    {"is_autonomous": "Cannot enable autonomy: " + "; ".join(blockers) + "."}
                )
        return attrs


class TransactionSerializer(serializers.ModelSerializer):
    gross_value_usd = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)

    class Meta:
        model = Transaction
        fields = [
            "id",
            "ticker",
            "tx_type",
            "amount",
            "price",
            "gross_value_usd",
            "executed_by",
            "timestamp",
        ]
        read_only_fields = fields


class AgentDecisionLogSerializer(serializers.ModelSerializer):
    """Audit-trail row for one AI agent invocation."""

    ticker = serializers.SerializerMethodField()
    transaction_id = serializers.IntegerField(source="transaction.id", read_only=True, default=None)

    class Meta:
        model = AgentDecisionLog
        fields = [
            "id",
            "action_taken",
            "market_sentiment",
            "ticker",
            "transaction_id",
            "tokens_used",
            "api_cost_usd",
            "reasoning",
            "bull_case",
            "bear_case",
            "created_at",
        ]
        read_only_fields = fields

    def get_ticker(self, obj: AgentDecisionLog) -> str | None:
        if obj.transaction_id and obj.transaction:
            return obj.transaction.ticker
        return None


# ---------------------------------------------------------------------------
# Phase 2: dashboard, approvals and integrations
# ---------------------------------------------------------------------------
class PortfolioSnapshotSerializer(serializers.ModelSerializer):
    """One point on the equity curve."""

    class Meta:
        model = PortfolioSnapshot
        fields = [
            "id",
            "captured_at",
            "total_equity_usd",
            "cash_balance_usd",
            "positions_value_usd",
            "unrealised_pnl_usd",
            "realised_pnl_today_usd",
        ]
        read_only_fields = fields


class TradeRecommendationSerializer(serializers.ModelSerializer):
    """An AI proposal awaiting (or having received) a human decision."""

    is_actionable = serializers.BooleanField(read_only=True)
    transaction_id = serializers.IntegerField(source="transaction.id", read_only=True, default=None)

    class Meta:
        model = TradeRecommendation
        fields = [
            "id",
            "ticker",
            "action",
            "amount",
            "price",
            "notional_usd",
            "sentiment",
            "reasoning",
            "status",
            "decided_via",
            "decided_at",
            "expires_at",
            "created_at",
            "is_actionable",
            "transaction_id",
        ]
        read_only_fields = fields


class TelegramLinkSerializer(serializers.ModelSerializer):
    """The caller's Telegram binding (never exposes other users' chats)."""

    class Meta:
        model = TelegramLink
        fields = [
            "chat_id",
            "telegram_username",
            "is_active",
            "notify_trades",
            "notify_recommendations",
            "linked_at",
            "last_seen_at",
        ]
        read_only_fields = ["chat_id", "telegram_username", "linked_at", "last_seen_at"]


class NewsArticleSerializer(serializers.Serializer):
    """One headline with its individual polarity."""

    title = serializers.CharField()
    source = serializers.CharField()
    url = serializers.CharField()
    published_at = serializers.CharField(allow_null=True)
    summary = serializers.CharField()
    polarity = serializers.FloatField()
    sentiment = serializers.CharField()


class MarketNewsSerializer(serializers.Serializer):
    """News coverage split into the two sides of the debate."""

    ticker = serializers.CharField()
    headline_count = serializers.IntegerField()
    sentiment = serializers.DictField()
    sources_used = serializers.ListField(child=serializers.CharField())
    degraded = serializers.BooleanField()
    fetched_at = serializers.DateTimeField()
    positive = NewsArticleSerializer(many=True)
    negative = NewsArticleSerializer(many=True)
    neutral = NewsArticleSerializer(many=True)


class TelegramLinkCodeSerializer(serializers.Serializer):
    """Response shape for a freshly minted pairing code."""

    link_code = serializers.CharField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)
    instructions = serializers.CharField(read_only=True)
