"""Run one paper portfolio allocation end to end with a stubbed CIO (Phase 4).

Usage::

    python manage.py run_allocation --reset --apply
    python manage.py run_allocation --apply --fail-leg AAPL
    python manage.py run_allocation --username demo-allocation --stub docs/spec/fixtures/phase4-allocation.v1.json

What it guarantees
------------------
* **No model key and no network.** The only model client is
  :class:`services.portfolio_allocation.StaticAllocationClient`, and its
  provenance is recorded as ``provider: "stub"``. Market prices and history come
  from the fixture; nothing is fetched.
* **Synthetic, and it says so.** The fixture is authored demonstration data. The
  risk statistics are computed deterministically from the supplied series, but
  the series is synthetic, and the plan is engineering/paper evidence rather
  than realised performance.
* **It can fail visibly.** ``--fail-leg`` removes a ticker's current mark at
  apply time, which is a realistic stale-feed case. The plan halts, the atomic
  transaction rolls back, and the command prints the failure and the plan state.
* **It is idempotent.** Re-running with the same submission key returns the
  existing plan instead of creating another one. ``--reset`` deletes the demo
  account first, so a repeatable paper run is one flag away.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.allocation_models import EXECUTION_MODE_PAPER, PortfolioPlan
from core.models import Asset, InstrumentClassification, Portfolio
from services.portfolio_allocation import (
    AllocationError,
    PlanApplication,
    PlanError,
    PortfolioCIO,
    StaticAllocationClient,
    apply_plan,
    create_plan,
    plan_as_dict,
)
from services.portfolio_brief import (
    CandidateCase,
    CandidateClaim,
    build_allocation_brief,
    capture_allocation_snapshot,
)
from services.portfolio_policy import AllocationPolicy, CostModel

DEFAULT_STUB = Path("docs/spec/fixtures/phase4-allocation.v1.json")
DEFAULT_SUBMISSION_KEY = "phase4-paper.v1"
CENT = Decimal("0.01")
ZERO = Decimal("0")


class Command(BaseCommand):
    help = "Run one paper portfolio allocation with a stubbed CIO (no model key, no network)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--stub",
            default=str(DEFAULT_STUB),
            help=f"Fixture path relative to BASE_DIR (default: {DEFAULT_STUB}).",
        )
        parser.add_argument(
            "--username",
            default="demo-allocation",
            help="Demo account to use (default: demo-allocation).",
        )
        parser.add_argument(
            "--submission-key",
            default=DEFAULT_SUBMISSION_KEY,
            help=f"Idempotency key (default: {DEFAULT_SUBMISSION_KEY}).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the plan to the ledger after creating it.",
        )
        parser.add_argument(
            "--fail-leg",
            default="",
            help=(
                "Remove this ticker's current mark at apply time, to demonstrate a leg "
                "failure and the atomic rollback."
            ),
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Delete and recreate the demo account before running. Demo only: this "
                "removes the account's plans, ledger and snapshots."
            ),
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["stub"])
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        if not path.exists():
            raise CommandError(f"Allocation fixture not found: {path}")
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if int(fixture.get("fixture_version", 0)) != 1:
            raise CommandError("Unsupported allocation fixture version.")
        if not fixture.get("synthetic"):
            # The command exists to demonstrate the paper path. Running it on a
            # fixture that claims to be realised history would blur that line.
            raise CommandError("This command only runs the synthetic paper fixture.")
        if options["reset"]:
            if not (options["username"] or "").startswith("demo"):
                raise CommandError("--reset is restricted to a demo-* account.")
            deleted, _ = User.objects.filter(username=options["username"]).delete()
            self.stdout.write(f"RESET removed the demo account ({deleted} row(s))")

        portfolio = self._ensure_portfolio(fixture, options["username"], reset=options["reset"])
        quotes = self._quotes(fixture, portfolio)
        classifications = self._upsert_classifications(fixture, portfolio)
        price_history = self._price_history(fixture["history"], timezone.now())
        candidates = self._candidates(fixture.get("candidates", []), timezone.now())
        policy = AllocationPolicy.from_portfolio(portfolio)
        cost_model = CostModel.from_config()

        snapshot = capture_allocation_snapshot(portfolio, quotes=quotes, as_of=timezone.now())
        brief = build_allocation_brief(
            portfolio,
            snapshot,
            quotes=quotes,
            price_history=price_history,
            candidates=candidates,
            classifications=classifications,
            policy=policy,
            cost_model=cost_model,
            history_source="fixture",
            candidate_source="fixture",
            evidence_sufficient=True,
        )
        self.stdout.write(
            "PAPER ALLOCATION - synthetic fixture; no model key, no network. "
            f"provider=stub model=stub-model fixture={path.name}"
        )
        self.stdout.write("")
        self._emit({"phase": "brief", "brief": brief.as_dict()})

        submission_key = (options["submission_key"] or "").strip()
        existing = (
            PortfolioPlan.objects.filter(portfolio=portfolio, submission_key=submission_key).first()
            if submission_key
            else None
        )
        proposal = None
        if existing is not None:
            plan = existing
            created = False
            self.stdout.write(
                f"IDEMPOTENT existing plan {plan.id} for submission key {submission_key!r}; "
                "no second plan or ledger row was created."
            )
        else:
            response = copy.deepcopy(fixture["model_response"])
            response["portfolio_version"] = brief.portfolio_version
            client = StaticAllocationClient(response)
            try:
                proposal = PortfolioCIO(client=client, policy=policy).decide(brief)
            except AllocationError as exc:
                self._emit({"phase": "error", "stage": "cio", "error": str(exc)})
                raise CommandError(str(exc)) from exc
            try:
                plan, created = create_plan(
                    portfolio,
                    brief=brief,
                    proposal=proposal,
                    submission_key=submission_key,
                    policy=policy,
                    cost_model=cost_model,
                    execution_mode=EXECUTION_MODE_PAPER,
                )
            except AllocationError as exc:
                self._emit({"phase": "error", "stage": "plan", "error": str(exc)})
                raise CommandError(str(exc)) from exc
            self._emit(
                {
                    "phase": "targets",
                    "requested_target_weights": proposal.target_weights_json(),
                    "requested_sum": str(sum(proposal.target_weights.values(), ZERO)),
                    "target_weights": plan.target_weights,
                    "target_sum": str(
                        sum((Decimal(value) for value in plan.target_weights.values()), ZERO)
                    ),
                    "deviations": plan.deviations,
                    "constraints": plan.constraints,
                }
            )

        self._emit({"phase": "plan", "created": created, "plan": plan_as_dict(plan)})

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write(
                "Not applied (--apply not passed). The plan is stored and can be "
                "inspected or applied with the same command and key."
            )
            return

        apply_quotes = dict(quotes)
        fail_leg = (options["fail_leg"] or "").strip().upper()
        if fail_leg:
            if fail_leg not in apply_quotes:
                raise CommandError(f"--fail-leg {fail_leg} is not a priced ticker in the fixture.")
            apply_quotes.pop(fail_leg, None)
            self.stdout.write(
                f"SIMULATING a missing current mark for {fail_leg}; apply should halt."
            )
        try:
            application = apply_plan(plan.id, quotes=apply_quotes)
        except PlanError as exc:
            plan.refresh_from_db()
            self._emit(
                {
                    "phase": "application_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "plan": plan_as_dict(plan),
                }
            )
            return
        self._emit({"phase": "application", **self._application(application)})

    # -- setup -------------------------------------------------------------
    def _ensure_portfolio(self, fixture: dict, username: str, *, reset: bool) -> Portfolio:
        user, user_created = User.objects.get_or_create(username=username, defaults={"email": ""})
        if user_created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        portfolio, portfolio_created = Portfolio.objects.get_or_create(user=user)
        spec = fixture["portfolio"]
        if portfolio_created or reset:
            portfolio.balance_usd = Decimal(str(spec["balance_usd"]))
            portfolio.risk_profile = str(spec.get("risk_profile", "medium"))
            portfolio.max_trade_allocation_pct = Decimal(str(spec["max_trade_allocation_pct"]))
            portfolio.max_portfolio_exposure_pct = Decimal(str(spec["max_portfolio_exposure_pct"]))
            portfolio.max_ticker_concentration_pct = Decimal(
                str(spec["max_ticker_concentration_pct"])
            )
            portfolio.max_sector_concentration_pct = Decimal(
                str(spec["max_sector_concentration_pct"])
            )
            portfolio.max_turnover_pct = Decimal(str(spec["max_turnover_pct"]))
            portfolio.min_cash_buffer_pct = Decimal(str(spec["min_cash_buffer_pct"]))
            portfolio.daily_loss_limit_usd = Decimal(str(spec["daily_loss_limit_usd"]))
            portfolio.save()

        if not portfolio.assets.exists() and not portfolio.transactions.exists():
            for holding in fixture["holdings"]:
                Asset.objects.create(
                    portfolio=portfolio,
                    ticker=str(holding["ticker"]).upper(),
                    amount=Decimal(str(holding["amount"])),
                    avg_purchase_price=Decimal(str(holding["avg_cost"])),
                )
        return portfolio

    def _quotes(self, fixture: dict, portfolio: Portfolio) -> dict[str, Decimal]:
        quotes = {
            str(ticker).upper(): Decimal(str(price)) for ticker, price in fixture["quotes"].items()
        }
        # A demo account that already holds something not in the fixture still
        # needs a mark to capture a snapshot. Cost is the labelled fallback.
        for asset in portfolio.assets.all():
            quotes.setdefault(asset.ticker.upper(), asset.avg_purchase_price)
        return quotes

    def _upsert_classifications(self, fixture: dict, portfolio: Portfolio) -> dict[str, dict]:
        classifications: dict[str, dict] = {}
        for row in fixture.get("classifications", []):
            ticker = str(row["ticker"]).upper()
            InstrumentClassification.objects.update_or_create(
                portfolio=portfolio,
                ticker=ticker,
                defaults={
                    "sector": row["sector"],
                    "asset_class": row["asset_class"],
                    "source": row.get("source", "fixture"),
                    "source_detail": "phase4-allocation.v1.json",
                },
            )
            classifications[ticker] = {
                "sector": row["sector"],
                "asset_class": row["asset_class"],
                "source": row.get("source", "fixture"),
            }
        return classifications

    def _price_history(self, spec: dict, as_of) -> dict:
        days = int(spec.get("days", 0))
        if days < 2:
            raise CommandError("The fixture history must provide at least two days.")
        history: dict[str, list[tuple]] = {}
        for ticker, params in spec.get("series", {}).items():
            start = Decimal(str(params.get("start", "100")))
            step = Decimal(str(params.get("step", "0")))
            cycle = Decimal(str(params.get("cycle", "0")))
            points = []
            for index in range(days):
                day = as_of.date() - timedelta(days=days - 1 - index)
                price = start + step * index + cycle * Decimal((index % 3) - 1)
                if price <= ZERO:
                    price = CENT
                points.append((day, price.quantize(CENT)))
            history[str(ticker).upper()] = points
        return history

    def _candidates(self, rows: list[dict], as_of) -> list[CandidateCase]:
        cases = []
        for row in rows:
            claims = [
                CandidateClaim(
                    claim_id=str(claim["claim_id"]),
                    agent_role=str(claim.get("agent_role", "bull")),
                    stance=str(claim.get("stance", "bull")),
                    claim_type=str(claim.get("claim_type", "interpretation")),
                    statement=str(claim["statement"]),
                    evidence_keys=[str(key) for key in claim.get("evidence_keys", [])],
                )
                for claim in row.get("claims", [])
            ]
            cases.append(
                CandidateCase(
                    ticker=str(row["ticker"]).upper(),
                    action=str(row.get("action", "BUY")),
                    rationale=str(row.get("rationale", "")),
                    strongest_objection=str(row.get("strongest_objection", "")),
                    thesis=str(row.get("thesis", "")),
                    invalidation_condition=str(row.get("invalidation_condition", "")),
                    run_id=f"fixture-{row['ticker']}",
                    mode="fixture",
                    decided_at=as_of.isoformat(),
                    claims=claims,
                )
            )
        return cases

    # -- output ------------------------------------------------------------
    def _emit(self, payload: dict) -> None:
        self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
        self.stdout.write("")

    def _application(self, application: PlanApplication) -> dict:
        return {
            "plan_id": str(application.plan.id),
            "status": application.plan.status,
            "already_applied": application.already_applied,
            "transaction_ids": [row.id for row in application.transactions],
            "legs": [
                {
                    "sequence": leg.sequence,
                    "ticker": leg.ticker,
                    "side": leg.side,
                    "status": leg.status,
                    "quantity": str(leg.quantity),
                    "price": str(leg.price),
                    "transaction_id": leg.transaction_id,
                }
                for leg in application.legs
            ],
        }
