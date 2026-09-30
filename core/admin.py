"""Django admin registrations for operational visibility."""

from __future__ import annotations

from django.contrib import admin

from core.models import AgentDecisionLog, Asset, Portfolio, Transaction


class AssetInline(admin.TabularInline):
    model = Asset
    extra = 0
    fields = ("ticker", "amount", "avg_purchase_price")
    ordering = ("ticker",)


@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "balance_usd",
        "risk_profile",
        "is_autonomous",
        "max_trade_allocation_pct",
        "daily_loss_limit_usd",
        "created_at",
    )
    list_filter = ("risk_profile", "is_autonomous")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at",)
    inlines = [AssetInline]
    list_select_related = ("user",)


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("id", "portfolio", "ticker", "amount", "avg_purchase_price")
    list_filter = ("ticker",)
    search_fields = ("ticker", "portfolio__user__username")
    list_select_related = ("portfolio", "portfolio__user")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "portfolio",
        "ticker",
        "tx_type",
        "amount",
        "price",
        "gross_value_usd",
        "executed_by",
        "timestamp",
    )
    list_filter = ("tx_type", "executed_by", "ticker")
    search_fields = ("ticker", "portfolio__user__username")
    date_hierarchy = "timestamp"
    list_select_related = ("portfolio", "portfolio__user")
    readonly_fields = ("timestamp",)

    @admin.display(description="Notional (USD)")
    def gross_value_usd(self, obj: Transaction) -> str:
        return f"${obj.gross_value_usd}"


@admin.register(AgentDecisionLog)
class AgentDecisionLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "portfolio",
        "action_taken",
        "market_sentiment",
        "tokens_used",
        "api_cost_usd",
        "created_at",
    )
    list_filter = ("market_sentiment", "created_at")
    search_fields = ("action_taken", "reasoning", "portfolio__user__username")
    date_hierarchy = "created_at"
    list_select_related = ("portfolio", "portfolio__user", "transaction")
    readonly_fields = ("created_at",)
