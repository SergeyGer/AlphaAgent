"""Seed a demo user, portfolio and API token.

Usage::

    python manage.py seed_demo --username demo --password demo-pass-123
    python manage.py seed_demo --autonomous --risk high --balance 25000
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

from core.models import Asset, ExecutedBy, Portfolio, Transaction, TxType


class Command(BaseCommand):
    help = "Create (or update) a demo AlphaAgent user with a funded portfolio and API token."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--username", default="demo")
        parser.add_argument("--password", default="demo-pass-123")
        parser.add_argument("--email", default="")
        parser.add_argument("--balance", default="10000.00")
        parser.add_argument("--risk", default="medium", choices=["low", "medium", "high"])
        parser.add_argument("--allocation-pct", default="5.00")
        parser.add_argument("--loss-limit", default="500.00")
        parser.add_argument(
            "--autonomous",
            action="store_true",
            help="Enable autonomous AI trading for the seeded portfolio.",
        )
        parser.add_argument(
            "--seed-position",
            action="store_true",
            help="Seed an AAPL position (and matching BUY transaction) to demo SELL logic.",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        username = options["username"]
        balance = Decimal(options["balance"])

        user, created = User.objects.get_or_create(
            username=username, defaults={"email": options["email"]}
        )
        if created:
            user.set_password(options["password"])
            user.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS(f"Created user '{username}'."))
        else:
            self.stdout.write(f"User '{username}' already exists - updating.")

        portfolio, _ = Portfolio.objects.get_or_create(user=user)
        portfolio.balance_usd = balance
        portfolio.risk_profile = options["risk"]
        portfolio.max_trade_allocation_pct = Decimal(options["allocation_pct"])
        portfolio.daily_loss_limit_usd = Decimal(options["loss_limit"])
        portfolio.is_autonomous = bool(options["autonomous"])
        portfolio.save()

        if options["seed_position"]:
            self._seed_position(portfolio)

        token, _ = Token.objects.get_or_create(user=user)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("AlphaAgent demo ready"))
        self.stdout.write(f"  user            : {username}")
        self.stdout.write(f"  portfolio id    : {portfolio.id}")
        self.stdout.write(f"  balance         : ${portfolio.balance_usd}")
        self.stdout.write(f"  risk profile    : {portfolio.risk_profile}")
        self.stdout.write(f"  autonomous      : {portfolio.is_autonomous}")
        self.stdout.write(f"  per-trade budget: ${portfolio.max_trade_budget_usd}")
        self.stdout.write(f"  daily loss limit: ${portfolio.daily_loss_limit_usd}")
        self.stdout.write(f"  API token       : {token.key}")
        self.stdout.write("")
        self.stdout.write("Try:")
        self.stdout.write(
            f'  curl -H "Authorization: Token {token.key}" http://127.0.0.1:8000/api/portfolio/'
        )

    def _seed_position(self, portfolio: Portfolio) -> None:
        ticker, amount, price = "AAPL", Decimal("10"), Decimal("180.00")
        asset, _ = Asset.objects.get_or_create(portfolio=portfolio, ticker=ticker)
        if asset.amount == 0:
            asset.amount = amount
            asset.avg_purchase_price = price
            asset.save(update_fields=["amount", "avg_purchase_price"])
            Transaction.objects.create(
                portfolio=portfolio,
                ticker=ticker,
                tx_type=TxType.BUY,
                amount=amount,
                price=price,
                executed_by=ExecutedBy.USER,
            )
            portfolio.balance_usd = max(portfolio.balance_usd - (amount * price), Decimal("0.00"))
            portfolio.save(update_fields=["balance_usd"])
            self.stdout.write(f"Seeded {amount} {ticker} @ ${price}.")
