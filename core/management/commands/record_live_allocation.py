"""Make one real provider CIO call and record the request/response artifact.

Usage::

    python manage.py record_live_allocation
    python manage.py record_live_allocation --input docs/spec/fixtures/fix08-live-input.v1.json \
        --output docs/spec/fixtures/fix08-live-allocation.v1.json

What it does
------------
* Builds a demo account from an **explicitly labelled operator-supplied** input
  fixture: holdings, prices and classifications, with no price history. The
  brief therefore reports ``risk_statistics: not_available`` and the declared
  ``label_only`` correlation policy rather than inventing a correlation.
* Calls the real provider adapter (:class:`BudgetedAllocationClient` over the
  configured ``StageClient``) exactly **once**. ``max_repairs=0`` and
  ``provider_retries=0`` mean a malformed answer is recorded as a failure, not
  re-prompted into a second billed call.
* Creates the plan in ``proposed`` state. It **never applies it** and asserts no
  ledger row was written.
* Writes a JSON artifact with the input, the brief, the exact prompt, the parsed
  response, token and cost provenance, the plan review payload and an explicit
  statement of what is real and what is not.

The artifact is labelled ``mode: "live"`` and ``source: "<provider>"``. A stub
response is never accepted here: this command has no stub mode on purpose. If
the call fails, a failure artifact is written and the command exits non-zero
rather than substituting a deterministic answer.

No retry is attempted by this command. Re-running it would be a second billed
call; the default output path is refused when it already exists unless
``--overwrite`` is given explicitly.
"""

from __future__ import annotations

import json
from datetime import UTC
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.allocation_models import EXECUTION_MODE_LIVE, PlanStatus
from core.decision_json import content_hash, json_safe, utc_iso
from core.models import Asset, InstrumentClassification, Portfolio, Transaction
from services.allocation_provider import BudgetedAllocationClient
from services.debate_protocol import ProtocolBudget
from services.portfolio_allocation import (
    PROMPT_TEMPLATE_VERSION,
    PROTOCOL_VERSION,
    PortfolioCIO,
    build_allocation_prompt,
    create_plan,
    plan_as_dict,
)
from services.portfolio_brief import (
    CandidateCase,
    CandidateClaim,
    build_allocation_brief,
    capture_allocation_snapshot,
    load_classifications,
)
from services.portfolio_policy import AllocationPolicy, CostModel
from services.portfolio_review import build_plan_review

DEFAULT_INPUT = Path("docs/spec/fixtures/fix08-live-input.v1.json")
DEFAULT_OUTPUT = Path("docs/spec/fixtures/fix08-live-allocation.v1.json")
DEFAULT_USERNAME = "demo-fix08-live"


class Command(BaseCommand):
    help = "Make one real provider CIO call and record a labelled request/response artifact."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input fixture path.")
        parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Artifact path.")
        parser.add_argument("--username", default=DEFAULT_USERNAME, help="Demo account.")
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite an existing artifact. This is a second billed call; use rarely.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete and recreate the demo account first (demo-* usernames only).",
        )

    def handle(self, *args, **options) -> None:
        input_path = self._resolve(options["input"])
        output_path = self._resolve(options["output"])
        if output_path.exists() and not options["overwrite"]:
            raise CommandError(
                f"{output_path} already exists. Re-running would be a second billed provider "
                "call. Pass --overwrite only if that is intended."
            )
        fixture = json.loads(input_path.read_text(encoding="utf-8"))
        if int(fixture.get("fixture_version", 0)) != 1:
            raise CommandError("Unsupported recording input fixture version.")
        if fixture.get("synthetic_input_data") is not True:
            raise CommandError(
                "The recording input must state synthetic_input_data=true: these are "
                "operator-supplied demonstration inputs, not market history."
            )
        provider, model, stage_client = self._provider()
        self.stdout.write(
            "LIVE RECORDING - one real provider call. "
            f"provider={provider} model={model} input={input_path.name} "
            "input_data=operator_supplied_demonstration"
        )

        budget = ProtocolBudget(
            stage_timeout_seconds=90,
            run_deadline_seconds=180,
            max_input_tokens=60_000,
            max_output_tokens=4_000,
            schema_repair_attempts=0,
            provider_retries=0,
            provider_retry_backoff_seconds=0,
            monetary_budget_usd=Decimal("0.25"),
        )
        client = BudgetedAllocationClient(stage_client, budget=budget)

        portfolio, brief, policy, cost_model = self._prepare(
            fixture, options["username"], reset=options["reset"]
        )
        existing_transactions = Transaction.objects.filter(portfolio=portfolio).count()
        prompt = build_allocation_prompt(brief.as_dict())
        cio = PortfolioCIO(client=client, policy=policy, max_repairs=0)
        error: Exception | None = None
        proposal = None
        try:
            proposal = cio.decide(brief)
        except Exception as exc:  # recorded honestly below; no stub substitution
            error = exc

        recorded_at = timezone.now().astimezone(UTC)
        record: dict = {
            "fixture_version": 1,
            "schema_version": "fix08.live_allocation.record.v1",
            "label": (
                "Real recorded provider run: one Anthropic CIO call on an operator-supplied "
                "demonstration brief. The model response is real; the input data is not "
                "market history."
            ),
            "run_kind": "real_recorded_model_run",
            "status": "completed" if error is None else "failed",
            "mode": "live",
            "source": provider,
            "provider": provider,
            "model": model,
            "protocol_version": PROTOCOL_VERSION,
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "recorded_at": recorded_at.isoformat(),
            "parameters": dict(client.parameters),
            "budget": budget.as_dict(),
            "input": {
                "fixture_path": str(input_path.relative_to(Path(settings.BASE_DIR)))
                if input_path.is_relative_to(Path(settings.BASE_DIR))
                else str(input_path),
                "input_data_classification": fixture["input_data_classification"],
                "synthetic_input_data": True,
                "realised_history": False,
                "note": fixture["note"],
                "portfolio": fixture["portfolio"],
                "holdings": fixture["holdings"],
                "quotes": fixture["quotes"],
                "classifications": fixture["classifications"],
                "candidates": fixture.get("candidates", []),
                "price_history": None,
                "price_history_note": fixture.get("price_history_note"),
            },
            "request": {
                "brief": json_safe(brief.as_dict()),
                "prompt_sha256": content_hash(prompt),
                "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                "prompt": prompt,
                "reproducible_in_shape": (
                    "The brief and prompt template version determine the prompt text. "
                    "Re-rendering build_allocation_prompt(brief.as_dict()) reproduces it "
                    "byte-for-byte; the model's answer is not reproducible."
                ),
            },
            "response": None,
            "usage": json_safe(client.report()),
            "plan": None,
            "approval": {
                "required": True,
                "state": "not_decided",
                "note": "The plan is proposed and unapproved; no trade was placed.",
            },
            "traded": False,
            "limitations": [
                "The input holdings, prices and classifications are operator-supplied "
                "demonstration data, not realised market history.",
                "No price history was supplied or fetched, so correlation and covariance "
                "are not available. No correlation was invented.",
                "The provider adapter reports tokens; it does not receive a provider-billed "
                "dollar amount. Any dollar figure here is a price-table estimate, not a bill.",
                "This is one sample. The recorded plan would not be regenerated by asking "
                "the model again, even with an identical prompt.",
            ],
        }

        if error is not None:
            record["response"] = None
            record["error"] = {
                "type": type(error).__name__,
                "code": str(getattr(error, "code", "") or type(error).__name__),
                "detail": str(error)[:4000],
            }
            record["usage"] = json_safe(client.report())
            self._write(output_path, record)
            raise CommandError(
                f"The provider call failed ({type(error).__name__}: {error}). A failure "
                f"artifact was written to {output_path}; no response was substituted."
            )

        if proposal is None:  # pragma: no cover - the error branch returns above
            raise CommandError("The provider returned no validated proposal.")
        metadata = dict(proposal.model_metadata or {})
        plan, created = create_plan(
            portfolio,
            brief=brief,
            proposal=proposal,
            submission_key="fix08-live-recording.v1",
            policy=policy,
            cost_model=cost_model,
            execution_mode=EXECUTION_MODE_LIVE,
        )
        plan_metadata = dict(plan.model_metadata or {})
        plan_metadata["synthetic_response"] = False
        plan_metadata["analysis_mode"] = "live"
        plan.model_metadata = json_safe(plan_metadata)
        plan.save(update_fields=["model_metadata", "updated_at"])

        snapshot_transactions = Transaction.objects.filter(portfolio=portfolio).count()
        if snapshot_transactions != existing_transactions:
            raise CommandError(
                "The recording path wrote a ledger row. This must never happen before an "
                "explicit apply; refusing to record a misleading artifact."
            )
        if plan.status != PlanStatus.PROPOSED:
            raise CommandError(f"Expected a proposed plan; got {plan.status}.")

        report = json_safe(client.report())
        usage = report.get("usage", {})
        record["response"] = {
            "parsed_response": json_safe(proposal.raw),
            "validated_proposal": json_safe(proposal.as_dict()),
            "response_sha256": next(
                (
                    call.get("response_hash")
                    for call in metadata.get("calls", [])
                    if call.get("status") == "accepted" and call.get("response_hash")
                ),
                None,
            ),
            "note": (
                "The raw provider text was decoded and validated by the target contract. "
                "This object is the validated JSON that was accepted."
            ),
        }
        record["plan"] = {
            "plan_id": str(plan.id),
            "created": created,
            "plan_version": plan.plan_version,
            "status": plan.status,
            "execution_mode": plan.execution_mode,
            "plan": plan_as_dict(plan),
            "review": json_safe(build_plan_review(plan)),
        }
        record["usage"] = report
        record["cost"] = {
            "billed_cost_usd": usage.get("billed_cost_usd"),
            "billed_cost_reported": usage.get("cost_reported"),
            "billed_cost_unknown_calls": usage.get("billed_cost_unknown_calls"),
            "billed_cost_unknown_reason": usage.get("billed_cost_unknown_reason"),
            "price_table_estimated_cost_usd": usage.get("price_table_cost_usd"),
            "price_table_cost_complete": usage.get("price_table_cost_complete"),
            "input_tokens": metadata.get("input_tokens"),
            "output_tokens": metadata.get("output_tokens"),
            "token_accounting": metadata.get("token_accounting"),
            "note": (
                "The provider reports tokens, not a billed dollar amount. Billed cost is "
                "null; the price-table figure is an estimate."
            ),
        }
        record["plan"]["review"]["provenance"]["synthetic_response"] = False
        self._write(output_path, record)

        self.stdout.write("")
        self.stdout.write(f"Recorded one provider response to {output_path}")
        self.stdout.write(
            f"model={model} input_tokens={metadata.get('input_tokens')} "
            f"output_tokens={metadata.get('output_tokens')} "
            f"estimated_price_table_cost_usd={usage.get('price_table_cost_usd')} "
            f"billed_cost_usd={usage.get('billed_cost_usd')}"
        )
        self.stdout.write(
            f"plan={plan.id} status={plan.status} traded=false transactions="
            f"{snapshot_transactions - existing_transactions}"
        )

    # -- setup -------------------------------------------------------------
    def _resolve(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        return path

    def _provider(self) -> tuple[str, str, object]:
        try:
            from ai_agent import AlphaAgentOrchestrator, CrewAIStageClient
        except Exception as exc:  # pragma: no cover - optional provider dependency
            raise CommandError(
                f"The provider adapter could not be imported: {type(exc).__name__}: {exc}"
            ) from exc
        orchestrator = AlphaAgentOrchestrator()
        if not orchestrator.is_configured:
            raise CommandError(
                "No LLM credential is configured (AI_LLM_API_KEY / AI_LLM_PROVIDER). "
                "This command makes one real call and has no stub mode."
            )
        stage = CrewAIStageClient(orchestrator)
        self.stdout.write(
            "Provider adapter: "
            f"{stage.provider}/{stage.model} "
            f"parameters={json.dumps(stage.parameters, sort_keys=True)}"
        )
        return stage.provider, stage.model, stage

    def _prepare(
        self, fixture: dict, username: str, *, reset: bool
    ) -> tuple[Portfolio, object, AllocationPolicy, CostModel]:
        if reset:
            if not username.startswith("demo"):
                raise CommandError("--reset is restricted to a demo-* account.")
            User.objects.filter(username=username).delete()
        spec = fixture["portfolio"]
        with transaction.atomic():
            user, user_created = User.objects.get_or_create(
                username=username, defaults={"email": ""}
            )
            if user_created:
                user.set_unusable_password()
                user.save(update_fields=["password"])
            portfolio = Portfolio.objects.filter(user=user).first()
            if portfolio is None:
                portfolio = Portfolio.objects.create(
                    user=user,
                    balance_usd=Decimal(str(spec["balance_usd"])),
                    risk_profile=str(spec.get("risk_profile", "medium")),
                    max_trade_allocation_pct=Decimal(str(spec["max_trade_allocation_pct"])),
                    max_portfolio_exposure_pct=Decimal(str(spec["max_portfolio_exposure_pct"])),
                    max_ticker_concentration_pct=Decimal(str(spec["max_ticker_concentration_pct"])),
                    max_sector_concentration_pct=Decimal(str(spec["max_sector_concentration_pct"])),
                    max_turnover_pct=Decimal(str(spec["max_turnover_pct"])),
                    min_cash_buffer_pct=Decimal(str(spec["min_cash_buffer_pct"])),
                    daily_loss_limit_usd=Decimal(str(spec["daily_loss_limit_usd"])),
                )
            if portfolio.assets.exists() or portfolio.transactions.exists():
                raise CommandError(
                    "The demo account already holds or has traded an asset. Use --reset "
                    "for a clean recording account; reusing it would change the plan."
                )
            for holding in fixture["holdings"]:
                Asset.objects.create(
                    portfolio=portfolio,
                    ticker=str(holding["ticker"]).upper(),
                    amount=Decimal(str(holding["amount"])),
                    avg_purchase_price=Decimal(str(holding["avg_cost"])),
                )
            for row in fixture["classifications"]:
                InstrumentClassification.objects.update_or_create(
                    portfolio=portfolio,
                    ticker=str(row["ticker"]).upper(),
                    defaults={
                        "sector": str(row["sector"]),
                        "asset_class": str(row["asset_class"]),
                        "source": "fixture",
                        "source_detail": "fix08-live-input.v1.json",
                    },
                )
        quotes = {
            str(ticker).upper(): Decimal(str(price)) for ticker, price in fixture["quotes"].items()
        }
        candidates = []
        for row in fixture.get("candidates", []):
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
            candidates.append(
                CandidateCase(
                    ticker=str(row["ticker"]).upper(),
                    action=str(row.get("action", "BUY")),
                    rationale=str(row.get("rationale", "")),
                    strongest_objection=str(row.get("strongest_objection", "")),
                    thesis=str(row.get("thesis", "")),
                    invalidation_condition=str(row.get("invalidation_condition", "")),
                    run_id="",
                    mode="operator_supplied",
                    decided_at=utc_iso(timezone.now()),
                    claims=claims,
                )
            )
        policy = AllocationPolicy.from_portfolio(portfolio)
        cost_model = CostModel.from_config()
        snapshot = capture_allocation_snapshot(portfolio, quotes=quotes, as_of=timezone.now())
        classifications = load_classifications(portfolio)
        brief = build_allocation_brief(
            portfolio,
            snapshot,
            quotes=quotes,
            price_history=None,
            candidates=candidates,
            classifications=classifications,
            policy=policy,
            cost_model=cost_model,
            history_source="not_fetched",
            candidate_source="operator_supplied_demonstration",
        )
        return portfolio, brief, policy, cost_model

    # -- output ------------------------------------------------------------
    def _write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
            encoding="utf-8",
        )
