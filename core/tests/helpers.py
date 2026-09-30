"""Shared test helpers."""

from __future__ import annotations

import itertools
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Asset, Portfolio

# Isolate tests from the shared Redis cache.
TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "alphaagent-tests",
    }
}

_user_counter = itertools.count(1)


def clear_cache() -> None:
    cache.clear()


def make_user(username: str | None = None, password: str = "s3cret-pass") -> User:
    """Create a user with a guaranteed-unique username by default."""
    username = username or f"user{next(_user_counter)}"
    return User.objects.create_user(username=username, password=password)


def make_portfolio(
    user: User | None = None,
    *,
    balance: str = "10000.00",
    risk: str = "medium",
    autonomous: bool = False,
    allocation_pct: str = "5.00",
    loss_limit: str = "500.00",
) -> Portfolio:
    user = user or make_user()
    return Portfolio.objects.create(
        user=user,
        balance_usd=Decimal(balance),
        risk_profile=risk,
        is_autonomous=autonomous,
        max_trade_allocation_pct=Decimal(allocation_pct),
        daily_loss_limit_usd=Decimal(loss_limit),
    )


def make_asset(
    portfolio: Portfolio, ticker: str = "AAPL", amount: str = "0", avg_price: str = "0.00"
) -> Asset:
    return Asset.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        amount=Decimal(amount),
        avg_purchase_price=Decimal(avg_price),
    )


def auth_client(user: User) -> APIClient:
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client
