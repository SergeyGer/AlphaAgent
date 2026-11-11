"""Offline end-to-end demonstration of the Phase 2 debate protocol.

This runs the **real** protocol, the real persistence layer and the real
``/api/decision-runs/`` endpoints, with one substitution: the model client is a
stub. No API key is read, no network call is made and no LLM is invoked, so the
demonstration is reproducible on a laptop with the database up.

It prints two things:

1. the JSON returned by ``GET /api/decision-runs/<id>/detail/`` for a run the
   protocol produced (``mode="live"``, ``has_rebuttals=true``, a ``dispute``);
2. the status, event log and API-visible state of a run whose rebuttal stage
   times out, including the separately recorded heuristic fallback.

Both JSON documents are also written under ``.runtime/demo/`` so they can be
diffed between runs.

Usage::

    venv/bin/python scripts/demo_debate_protocol.py

The script writes demo rows to the configured database (a ``protocol-demo``
user and its portfolio). It places no trades and executes no guard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from rest_framework.authtoken.models import Token  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from ai_agent import DecisionContext, run_alpha_agent  # noqa: E402
from core.models import Portfolio  # noqa: E402
from services.debate_protocol import ModelUsage, StageTimeout  # noqa: E402
from services.fundamentals import FinancialHealth, PriceHistory  # noqa: E402
from services.news import NewsArticle, NewsReport  # noqa: E402

OUTPUT_DIR = REPO_ROOT / ".runtime" / "demo"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CATALOG_ID_RE = re.compile(rf'"id": "({UUID})"')
CLAIM_ID_RE = re.compile(rf'"claim_id": "({UUID})"')
VALID_TARGET_RE = re.compile(rf"^- ({UUID})$", re.MULTILINE)

BULL_MARKER = "Services revenue is accelerating while the installed base keeps growing"
BEAR_MARKER = "A supplier demand warning points to a weaker hardware cycle"


# ---------------------------------------------------------------------------
# Stub model client - the only substitution in this demonstration
# ---------------------------------------------------------------------------
class StubStageClient:
    """Deterministic stand-in for the provider behind ``complete(stage, prompt)``.

    It reads the evidence ids out of the prompt the protocol builds, so the
    claims it returns cite real ``EvidenceItem`` rows. It is marked
    ``provider="stub"`` and that is what the run records.
    """

    provider = "stub"
    model = "stub-debate-1"
    parameters: ClassVar[dict] = {"temperature": 0.0, "max_output_tokens": 4096}

    def __init__(self, handlers, *, failures=None):
        self.handlers = handlers
        self.failures = {stage: list(items) for stage, items in (failures or {}).items()}
        self.calls: list[tuple[str, str]] = []
        self.last_usage: ModelUsage | None = None
        self.state: dict = {}

    def complete(self, stage: str, prompt: str) -> str:
        self.calls.append((stage, prompt))
        pending = self.failures.get(stage) or []
        if pending:
            raise pending.pop(0)
        handler = self.handlers[stage]
        reply = handler(self, prompt) if callable(handler) else handler
        self.last_usage = ModelUsage(
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(reply) // 4),
            cost_usd=Decimal("0.0005"),
            reported=True,
        )
        return reply

    def call_count(self, stage: str) -> int:
        return sum(1 for called, _prompt in self.calls if called == stage)


def catalog_ids(prompt: str) -> list[str]:
    return list(dict.fromkeys(CATALOG_ID_RE.findall(prompt)))


def opposing_claim_ids(prompt: str) -> list[str]:
    ids = CLAIM_ID_RE.findall(prompt) or VALID_TARGET_RE.findall(prompt)
    return list(dict.fromkeys(ids))


def minimal_bull_case(client: StubStageClient, prompt: str) -> str:
    ids = catalog_ids(prompt)
    return json.dumps(
        {
            "summary": "Services growth is the bull case; hardware is the risk.",
            "claims": [
                {
                    "statement": BULL_MARKER,
                    "claim_type": "observed_fact",
                    "evidence_ids": ids[:1],
                    "horizon": "30d",
                    "limitations": "One quarter is not a trend.",
                }
            ],
        }
    )


def minimal_bear_case(client: StubStageClient, prompt: str) -> str:
    ids = catalog_ids(prompt)
    return json.dumps(
        {
            "summary": "The supplier warning is the bear case; it is credible but narrow.",
            "claims": [
                {
                    "statement": BEAR_MARKER,
                    "claim_type": "observed_fact",
                    "evidence_ids": ids[-1:],
                    "horizon": "30d",
                    "limitations": "A single supplier is not the whole chain.",
                }
            ],
        }
    )


def bull_case(client: StubStageClient, prompt: str) -> str:
    ids = catalog_ids(prompt)
    return json.dumps(
        {
            "summary": (
                "The bull case rests on services growth and a recovering trend. It is "
                "strong on services and weaker on hardware."
            ),
            "claims": [
                {
                    "statement": BULL_MARKER,
                    "claim_type": "observed_fact",
                    "evidence_ids": ids[:1],
                    "horizon": "30d",
                    "limitations": "One quarter is not a trend.",
                },
                {
                    "statement": "The drawdown is closing as the 200-day average is tested",
                    "claim_type": "interpretation",
                    "evidence_ids": ids[:1],
                    "horizon": "30d",
                    "limitations": "Moving averages lag the price.",
                },
            ],
        }
    )


def bear_case(client: StubStageClient, prompt: str) -> str:
    ids = catalog_ids(prompt)
    return json.dumps(
        {
            "summary": (
                "The bear case rests on a supplier demand warning and leverage. It is "
                "credible but not overwhelming."
            ),
            "claims": [
                {
                    "statement": BEAR_MARKER,
                    "claim_type": "observed_fact",
                    "evidence_ids": ids[-1:],
                    "horizon": "30d",
                    "limitations": "A single supplier is not the whole chain.",
                },
                {
                    "statement": "Net debt constrains buybacks if rates stay high",
                    "claim_type": "interpretation",
                    "evidence_ids": ids[-1:],
                    "horizon": "90d",
                    "limitations": "Rates may fall.",
                },
            ],
        }
    )


def bull_rebuttal(client: StubStageClient, prompt: str) -> str:
    targets = opposing_claim_ids(prompt)
    return json.dumps(
        {
            "responses": [
                {
                    "target_claim_id": targets[0],
                    "response_type": "qualify",
                    "explanation": (
                        "The supplier warning is real but it is one component; the bull "
                        "case did not deny hardware risk."
                    ),
                    "evidence_ids": catalog_ids(prompt)[:1],
                    "position_change": (
                        "Qualified: hardware risk is material, but services still carry the thesis."
                    ),
                }
            ]
        }
    )


def bear_rebuttal(client: StubStageClient, prompt: str) -> str:
    targets = opposing_claim_ids(prompt)
    client.state["disputed_claim_id"] = targets[0]
    return json.dumps(
        {
            "responses": [
                {
                    "target_claim_id": targets[0],
                    "response_type": "dispute",
                    "explanation": (
                        "Services growth is already in the multiple and the supplier "
                        "warning contradicts the growth claim."
                    ),
                    "evidence_ids": catalog_ids(prompt)[-1:],
                    "position_change": (
                        "Unchanged: the bull claim does not answer the demand warning."
                    ),
                }
            ]
        }
    )


def adjudication(client: StubStageClient, prompt: str) -> str:
    disputed = client.state.get("disputed_claim_id")
    return json.dumps(
        {
            "action": "HOLD",
            "amount": 0,
            "sentiment": "NEUTRAL",
            "reasoning": (
                "The bull case on services is real, but the supplier warning is "
                "unresolved and the two arguments are balanced. HOLD, and the disputed "
                "growth claim is what keeps this unresolved."
            ),
            "decisive_claim_ids": [disputed] if disputed else [],
            "strongest_objection": (
                "The supplier warning points to a weaker hardware cycle than the bull case implies."
            ),
            "unresolved_uncertainty": ("Whether the supplier warning spills into services demand."),
            "thesis": "",
            "invalidation_condition": "",
        }
    )


def handlers(*, minimal: bool = False) -> dict:
    if minimal:
        return {
            **handlers(),
            "bull_case": minimal_bull_case,
            "bear_case": minimal_bear_case,
        }
    return {
        "bull_case": bull_case,
        "bear_case": bear_case,
        "bull_rebuttal": bull_rebuttal,
        "bear_rebuttal": bear_rebuttal,
        "adjudication": adjudication,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_context(
    portfolio_id: int, *, lean: bool = False, minimal: bool = False
) -> DecisionContext:
    """The fixed offline inputs. ``lean`` keeps a single headline and no
    fundamentals/history, which makes the pasted ``/detail/`` response shorter
    without changing its shape."""
    articles = [
        NewsArticle(
            title="Apple services revenue beats estimates",
            source="wire-service",
            url="https://example.invalid/aapl-services",
            published_at="2026-02-03T12:00:00Z",
            summary="Services growth accelerated for a third quarter.",
            polarity=0.62,
            sentiment="BULLISH",
        )
    ]
    if not lean:
        articles.append(
            NewsArticle(
                title="Apple supplier warns of soft handset demand",
                source="wire-service",
                url="https://example.invalid/aapl-supplier",
                published_at="2026-02-03T11:00:00Z",
                summary="A key supplier cut its outlook for the first half.",
                polarity=-0.55,
                sentiment="BEARISH",
            )
        )
    news = NewsReport(
        ticker="AAPL",
        articles=[] if minimal else articles,
        fetched_at=datetime(2026, 2, 3, 14, 25, tzinfo=UTC),
    )
    return DecisionContext(
        ticker="AAPL",
        risk_profile="medium",
        cash_balance=Decimal("10000.00"),
        position_amount=Decimal("10"),
        avg_purchase_price=Decimal("180.00"),
        current_price=Decimal("225.00"),
        price_change_pct=Decimal("1.25"),
        max_trade_budget_usd=Decimal("500.00"),
        daily_loss_limit_usd=Decimal("500.00"),
        news_report=news,
        financial_health=(
            None
            if lean
            else FinancialHealth(
                ticker="AAPL",
                total_debt=Decimal("100000"),
                current_ratio=Decimal("1.10"),
                trailing_pe=Decimal("31.5"),
                red_flags=["Net debt rose year over year"],
            )
        ),
        price_history=(
            None
            if lean
            else PriceHistory(
                ticker="AAPL",
                latest=Decimal("225.00"),
                sma_200=Decimal("230.00"),
                drawdown_from_high_pct=Decimal("-8.50"),
                change_pct=Decimal("4.20"),
            )
        ),
        portfolio_id=portfolio_id,
    )


def ensure_demo_portfolio() -> tuple[User, Portfolio]:
    user, created = User.objects.get_or_create(username="protocol-demo")
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    portfolio, _ = Portfolio.objects.get_or_create(
        user=user,
        defaults={
            "balance_usd": Decimal("10000.00"),
            "risk_profile": "medium",
            "is_autonomous": False,
        },
    )
    return user, portfolio


def api_for(user: User) -> APIClient:
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def get_json(api: APIClient, path: str) -> dict:
    response = api.get(path)
    if response.status_code != 200:  # pragma: no cover - demonstration guard
        raise SystemExit(f"GET {path} -> {response.status_code}: {response.content[:400]!r}")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lean",
        action="store_true",
        help="One headline and no fundamentals/history: keeps the pasted JSON short.",
    )
    parser.add_argument(
        "--minimal",
        action="store_true",
        help="Lean context, one claim per side: smallest faithful detail response.",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # The repository's .env disables the heuristic fallback; the failure-path
    # demonstration needs it on so the separately recorded fallback is visible.
    settings.AI_CONFIG = {**settings.AI_CONFIG, "ALLOW_HEURISTIC_FALLBACK": True}
    # DRF's test client uses Host: testserver. In a real deployment this host is
    # not allowed, and it should not be: the script adds it in-process only.
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "testserver"]
    settings.DEBATE_CONFIG = {
        **settings.DEBATE_CONFIG,
        "STAGE_TIMEOUT_SECONDS": 5,
        "RUN_DEADLINE_SECONDS": 60,
        "MONETARY_BUDGET_USD": "10.00",
        "PROVIDER_RETRY_BACKOFF_SECONDS": 0,
    }

    user, portfolio = ensure_demo_portfolio()
    api = api_for(user)
    minimal = args.minimal
    context = make_context(portfolio.id, lean=args.lean or minimal, minimal=minimal)

    # ---- 1. successful protocol run -----------------------------------
    client = StubStageClient(handlers(minimal=minimal))
    result = run_alpha_agent(context, model_client=client)
    live_run_id = result.decision_run_id
    live_detail = get_json(api, f"/api/decision-runs/{live_run_id}/detail/")

    # ---- 2. failure path: the rebuttal stage times out -----------------
    failing_client = StubStageClient(
        handlers(minimal=minimal),
        failures={
            "bull_rebuttal": [
                StageTimeout(
                    "stub provider did not answer within the stage timeout",
                    stage="rebutting",
                )
            ]
        },
    )
    failed_result = run_alpha_agent(context, model_client=failing_client)
    failed_run_id = failed_result.extra["fallback_of"]
    fallback_run_id = failed_result.decision_run_id

    failed_metadata = get_json(api, f"/api/decision-runs/{failed_run_id}/")
    failed_events = get_json(api, f"/api/decision-runs/{failed_run_id}/events/")
    failed_detail = get_json(api, f"/api/decision-runs/{failed_run_id}/detail/")
    fallback_metadata = get_json(api, f"/api/decision-runs/{fallback_run_id}/")
    fallback_detail = get_json(api, f"/api/decision-runs/{fallback_run_id}/detail/")

    production = {
        "model": "stubbed",
        "network": False,
        "lean_context": args.lean or minimal,
        "claims_per_side": 1 if minimal else 2,
    }
    live_document = {
        "note": (
            "Produced by services/debate_protocol.py with a stubbed model client: "
            "real protocol, real persistence, real API, no LLM call."
        ),
        "method": production,
        "run_id": live_run_id,
        "detail": live_detail,
    }
    failure_document = {
        "note": (
            "The bull rebuttal stage timed out. The failed run is one attempt; the "
            "heuristic fallback is a separate, typed attempt with no debate."
        ),
        "method": production,
        "failed_run": {
            "run_id": failed_run_id,
            "status": failed_metadata["status"],
            "error_code": failed_metadata["error_code"],
            "failure": failed_metadata["failure"],
            "stage_states": failed_metadata["stage_states"],
            "events": failed_events["events"],
            "has_rebuttals": failed_detail["has_rebuttals"],
            "rebuttal_count": len(failed_detail["rebuttals"]),
            "claim_count": len(failed_detail["claims"]),
            "rationale": failed_detail["rationale"],
            "protocol_fallback_label": failed_metadata["protocol"]["fallback"],
        },
        "fallback_run": {
            "run_id": fallback_run_id,
            "mode": fallback_metadata["mode"],
            "source": fallback_metadata["source"],
            "status": fallback_metadata["status"],
            "is_synthetic": fallback_metadata["is_synthetic"],
            "has_rebuttals": fallback_detail["has_rebuttals"],
            "claim_count": len(fallback_detail["claims"]),
            "rebuttal_count": len(fallback_detail["rebuttals"]),
            "verdict_action": (fallback_detail["verdict"] or {}).get("action"),
            "protocol": fallback_metadata["protocol"],
        },
    }

    (OUTPUT_DIR / "live-run-detail.json").write_text(
        json.dumps(live_document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (OUTPUT_DIR / "failure-path.json").write_text(
        json.dumps(failure_document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("=" * 100)
    print("1. LIVE PROTOCOL RUN - GET /api/decision-runs/<id>/detail/")
    print(f"   run {live_run_id}")
    print("=" * 100)
    print(json.dumps(live_detail, indent=2, ensure_ascii=False))

    print()
    print("=" * 100)
    print("2. FAILURE PATH - one stage times out, heuristic fallback recorded separately")
    print(f"   failed run   {failed_run_id}")
    print(f"   fallback run {fallback_run_id}")
    print("=" * 100)
    print(json.dumps(failure_document, indent=2, ensure_ascii=False))

    print()
    print(f"Wrote {OUTPUT_DIR / 'live-run-detail.json'}")
    print(f"Wrote {OUTPUT_DIR / 'failure-path.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
