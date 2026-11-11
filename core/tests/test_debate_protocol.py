"""Phase 2 debate-protocol tests: the specification's acceptance scenarios.

The protocol is exercised end to end against an in-memory stub client - no API
key, no network, no CrewAI import on this path. Each test class maps onto a
numbered scenario from DEB-01 to DEB-05:

``IndependentFirstRoundTests``     DEB-01, acceptance scenario 1 (independence)
``RebuttalRoundTests``             DEB-02, acceptance scenarios 1 and 2
``FailurePathTests``               DEB-04, acceptance scenario 3
``AdjudicationTests``              DEB-03
``SingleAgentBaselineTests``       acceptance scenario 4
``ReproducibilityTests``           DEB-05
``ApiExposureTests``               mode="live" is visible through the API
``RunRecordingTests``              the producer links to the Phase 0 audit row

The stub parses evidence ids out of the prompt, exactly as a model would read
them, so a test that cites evidence is also proving the ids are in the prompt.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status

from ai_agent import AgentRunResult, DecisionContext, TradeProposal, run_alpha_agent
from core.decision_errors import DecisionRunError
from core.decision_models import (
    AgentRole,
    Claim,
    ClaimStance,
    DecisionRecord,
    DecisionRun,
    DecisionStatus,
    EvidenceItem,
    InputSnapshot,
    Rebuttal,
    RunEventType,
    RunMode,
    RunSource,
)
from core.models import AgentDecisionLog
from core.tests.helpers import TEST_CACHES, auth_client, make_portfolio
from services.debate_protocol import (
    BASELINE_PROTOCOL_VERSION,
    HEURISTIC_PROTOCOL_VERSION,
    MAX_INITIAL_CLAIMS,
    MAX_REBUTTALS_PER_AGENT,
    PROMPT_TEMPLATE_VERSION,
    PROTOCOL_VERSION,
    REBUTTAL_ROUNDS,
    Adjudication,
    BudgetExhausted,
    DebateProtocol,
    MissingRequiredCase,
    ModelUsage,
    ProtocolBudget,
    ProviderError,
    ProviderTransientError,
    RebuttalTargetError,
    StageSchemaError,
    StageTimeout,
)
from services.decision_runs import link_legacy_log, stage_states
from services.fundamentals import FinancialHealth, PriceHistory
from services.market_data import PriceQuote
from services.news import NewsArticle, NewsReport

#: A fixed authoring instant for article timestamps. The live cutoff is "now",
#: so these are safely in the past.
RECORDED_AT = datetime(2026, 2, 3, 14, 30, tzinfo=UTC)

BULL_MARKER = "Services revenue is accelerating while the installed base keeps growing"
BEAR_MARKER = "A supplier demand warning points to a weaker hardware cycle"

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CATALOG_ID_RE = re.compile(rf'"id": "({UUID})"')
CLAIM_ID_RE = re.compile(rf'"claim_id": "({UUID})"')
VALID_TARGET_RE = re.compile(rf"^- ({UUID})$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Test doubles and fixtures
# ---------------------------------------------------------------------------
def catalog_ids(prompt: str) -> list[str]:
    """Evidence ids the prompt offers, in catalog order."""
    return list(dict.fromkeys(CATALOG_ID_RE.findall(prompt)))


def opposing_claim_ids(prompt: str) -> list[str]:
    """Claim ids the prompt says may be targeted.

    On the first attempt that is the OPPOSING VALIDATED CLAIMS block; on a
    repair prompt the same ids are repeated as a ``VALID TARGET CLAIM IDS``
    list (and the repair prompt deliberately does not restate the whole case).
    """
    found = CLAIM_ID_RE.findall(prompt)
    if not found:
        found = VALID_TARGET_RE.findall(prompt)
    return list(dict.fromkeys(found))


class StubStageClient:
    """The injectable model seam. Deterministic, offline, and scriptable."""

    provider = "stub"
    model = "stub-debate-1"
    parameters: ClassVar[dict] = {"temperature": 0.0, "max_output_tokens": 4096}

    def __init__(self, handlers, *, failures=None, cost=Decimal("0.0005")):
        self.handlers = handlers
        self.failures = {stage: list(items) for stage, items in (failures or {}).items()}
        self.cost = cost
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
            cost_usd=self.cost,
            reported=True,
        )
        return reply

    def stage_calls(self, stage: str) -> list[str]:
        return [prompt for called, prompt in self.calls if called == stage]


def make_context(**overrides) -> DecisionContext:
    news = NewsReport(
        ticker="AAPL",
        articles=[
            NewsArticle(
                title="Apple services revenue beats estimates",
                source="wire-service",
                url="https://example.invalid/aapl-services",
                published_at="2026-02-03T12:00:00Z",
                summary="Services growth accelerated for a third quarter.",
                polarity=0.62,
                sentiment="BULLISH",
            ),
            NewsArticle(
                title="Apple supplier warns of soft handset demand",
                source="wire-service",
                url="https://example.invalid/aapl-supplier",
                published_at="2026-02-03T11:00:00Z",
                summary="A key supplier cut its outlook for the first half.",
                polarity=-0.55,
                sentiment="BEARISH",
            ),
        ],
        fetched_at=RECORDED_AT - timedelta(minutes=5),
    )
    defaults = dict(
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
        financial_health=FinancialHealth(
            ticker="AAPL",
            total_debt=Decimal("100000"),
            current_ratio=Decimal("1.10"),
            trailing_pe=Decimal("31.5"),
            red_flags=["Net debt rose year over year"],
        ),
        price_history=PriceHistory(
            ticker="AAPL",
            latest=Decimal("225.00"),
            sma_200=Decimal("230.00"),
            drawdown_from_high_pct=Decimal("-8.50"),
            change_pct=Decimal("4.20"),
        ),
        portfolio_id=None,
    )
    defaults.update(overrides)
    return DecisionContext(**defaults)


def bull_case(client: StubStageClient, prompt: str) -> str:
    ids = catalog_ids(prompt)
    return json.dumps(
        {
            "summary": (
                "The bull case rests on services growth and a recovering trend. "
                "It is strong on services and weaker on hardware."
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
                "The bear case rests on a supplier demand warning and leverage. "
                "It is credible but not overwhelming."
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
                        "The supplier warning is real but it is one component; the "
                        "bull case did not deny hardware risk."
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
                "unresolved and the two arguments are balanced. HOLD."
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


def baseline_analysis(client: StubStageClient, prompt: str) -> str:
    return json.dumps(
        {
            "action": "HOLD",
            "amount": 0,
            "sentiment": "NEUTRAL",
            "reasoning": (
                "No debate was held: this is the one-agent baseline. The evidence is "
                "balanced between services growth and the supplier warning, so HOLD."
            ),
            "decisive_claim_ids": [],
            "strongest_objection": (
                "The supplier warning could mark the start of a hardware downcycle."
            ),
            "unresolved_uncertainty": "Whether services demand survives a hardware slowdown.",
            "thesis": "",
            "invalidation_condition": "",
        }
    )


def standard_handlers() -> dict:
    return {
        "bull_case": bull_case,
        "bear_case": bear_case,
        "bull_rebuttal": bull_rebuttal,
        "bear_rebuttal": bear_rebuttal,
        "adjudication": adjudication,
    }


def events_of(run: DecisionRun) -> list:
    return list(run.events.order_by("sequence"))


def event_of(run: DecisionRun, event_type: str, **payload) -> object:
    for event in events_of(run):
        if event.event_type != event_type:
            continue
        if all(event.payload.get(key) == value for key, value in payload.items()):
            return event
    raise AssertionError(f"no {event_type} event with {payload} in run {run.id}")


class ProtocolTestCase(TestCase):
    """Shared portfolio, context and budget; no live model, no network."""

    def setUp(self):
        self.portfolio = make_portfolio()
        self.context = make_context(portfolio_id=self.portfolio.id)
        self.budget = ProtocolBudget(
            stage_timeout_seconds=5.0,
            run_deadline_seconds=30.0,
            max_input_tokens=200_000,
            max_output_tokens=100_000,
            schema_repair_attempts=1,
            provider_retries=2,
            provider_retry_backoff_seconds=0.0,
            monetary_budget_usd=Decimal("10.00"),
        )

    def protocol(self, client: StubStageClient, **budget_overrides) -> DebateProtocol:
        budget = replace(self.budget, **budget_overrides) if budget_overrides else self.budget
        return DebateProtocol(
            client=client,
            budget=budget,
            config={"MAX_EVIDENCE_ITEMS": 20},
            provider=client.provider,
            model=client.model,
            parameters=client.parameters,
        )

    def run_debate(self, client: StubStageClient, *, budget_overrides=None, **kwargs):
        protocol = self.protocol(client, **(budget_overrides or {}))
        return protocol.run(self.context, portfolio=self.portfolio, **kwargs)


# ---------------------------------------------------------------------------
# DEB-01: independent first round
# ---------------------------------------------------------------------------
class IndependentFirstRoundTests(ProtocolTestCase):
    def test_round_one_cases_are_independent_and_persisted_before_rebuttal(self):
        """Acceptance scenario 1, DEB-01.

        Independence is checked in the *prompts*, not by relying on sequencing:
        the bull prompt must not contain the bear's output, and vice versa. The
        event log then proves both cases were recorded before the rebuttal stage
        started.
        """
        client = StubStageClient(standard_handlers())
        outcome = self.run_debate(client)
        self.assertIsNotNone(outcome.run)

        bull_prompts = client.stage_calls("bull_case")
        bear_prompts = client.stage_calls("bear_case")
        self.assertEqual(len(bull_prompts), 1)
        self.assertEqual(len(bear_prompts), 1)

        # Neither prompt contains the other side's committed case.
        self.assertNotIn(BEAR_MARKER, bull_prompts[0])
        self.assertNotIn(BULL_MARKER, bear_prompts[0])
        # Both sides were offered the identical evidence catalog, so isolation
        # is "cannot see the opponent", not "was given less information".
        self.assertEqual(catalog_ids(bull_prompts[0]), catalog_ids(bear_prompts[0]))
        self.assertTrue(catalog_ids(bull_prompts[0]))

        run = outcome.run
        events = events_of(run)
        claim_events = [event for event in events if event.event_type == RunEventType.CLAIM_CREATED]
        self.assertEqual(len(claim_events), 4)
        last_claim_seq = max(event.sequence for event in claim_events)
        rebutting_seq = next(
            event.sequence
            for event in events
            if event.event_type == RunEventType.STATUS_CHANGED
            and event.payload.get("to") == DecisionStatus.REBUTTING
        )
        self.assertLess(
            last_claim_seq,
            rebutting_seq,
            "a claim was persisted after the rebuttal stage began",
        )
        first_rebuttal_seq = min(
            event.sequence for event in events if event.event_type == RunEventType.REBUTTAL_CREATED
        )
        self.assertLess(rebutting_seq, first_rebuttal_seq)

        for claim in Claim.objects.filter(run=run):
            self.assertEqual(claim.case_round, 1)
        # The protocol finishes the analysis; the guard/execution lifecycle completes it.
        self.assertEqual(run.status, DecisionStatus.VALIDATING)
        self.assertEqual(run.mode, RunMode.LIVE)
        self.assertFalse(run.is_synthetic)

    def test_each_case_records_its_own_summary_and_concession_flag(self):
        client = StubStageClient(standard_handlers())
        outcome = self.run_debate(client)
        completed = [
            event
            for event in events_of(outcome.run)
            if event.event_type == RunEventType.STAGE_COMPLETED
            and event.agent_role == AgentRole.BULL
        ]
        self.assertEqual(len(completed), 1)
        self.assertFalse(completed[0].payload["concession"])
        self.assertIn("services growth", completed[0].payload["summary"].lower())


# ---------------------------------------------------------------------------
# DEB-02: one explicit rebuttal round
# ---------------------------------------------------------------------------
class RebuttalRoundTests(ProtocolTestCase):
    def test_bear_disputes_bull_and_the_cio_links_the_disputed_claim(self):
        """Acceptance scenario 1: dispute -> response -> CIO links the claim."""
        client = StubStageClient(standard_handlers())
        outcome = self.run_debate(client)
        disputed = client.state["disputed_claim_id"]

        dispute = Rebuttal.objects.get(run=outcome.run, response_type="dispute")
        self.assertEqual(dispute.responding_agent, AgentRole.BEAR)
        self.assertEqual(str(dispute.target_claim.claim_id), disputed)
        self.assertTrue(dispute.position_change)
        self.assertEqual(dispute.evidence.count(), 1)

        decision = DecisionRecord.objects.get(run=outcome.run)
        self.assertIn(disputed, decision.decisive_claim_ids)
        self.assertTrue(decision.strongest_objection)
        self.assertEqual(decision.action, "HOLD")
        self.assertIsNone(decision.requested_quantity)

        proposed = event_of(outcome.run, RunEventType.DECISION_PROPOSED)
        self.assertIn(disputed, proposed.payload["decisive_claim_ids"])
        self.assertFalse(proposed.payload["analyst_consensus"])
        self.assertTrue(proposed.payload["unresolved_uncertainty"])
        self.assertEqual(proposed.payload["disagreement"]["disputed_claim_ids"], [disputed])

        # The bull answered with a qualification and declared the position change.
        qualify = Rebuttal.objects.get(run=outcome.run, response_type="qualify")
        self.assertEqual(qualify.responding_agent, AgentRole.BULL)
        self.assertTrue(qualify.position_change.lower().startswith("qualified"))
        created = [
            event
            for event in events_of(outcome.run)
            if event.event_type == RunEventType.REBUTTAL_CREATED
        ]
        self.assertEqual(len(created), 2)
        changed = {event.payload["position_changed"] for event in created}
        self.assertEqual(changed, {True, False}, "position changes must be declared per response")
        self.assertTrue(all(event.payload["round"] == 1 for event in created))

    def test_bear_with_no_credible_objection_concedes_and_protocol_completes(self):
        """Acceptance scenario 2: a concession completes the protocol."""

        def conceding_bear_case(client, prompt):
            return json.dumps(
                {
                    "summary": (
                        "No credible bear case was found. The supplier warning is real, "
                        "but it does not contradict the services evidence."
                    ),
                    "claims": [],
                }
            )

        def conceding_bear_rebuttal(client, prompt):
            targets = opposing_claim_ids(prompt)
            return json.dumps(
                {
                    "responses": [
                        {
                            "target_claim_id": targets[0],
                            "response_type": "agree",
                            "explanation": (
                                "The services claim is supported by the cited evidence "
                                "and I cannot dispute it."
                            ),
                            "evidence_ids": catalog_ids(prompt)[:1],
                            "position_change": (
                                "Conceded: the bull evidence is stronger than anything I "
                                "could put against it."
                            ),
                        }
                    ]
                }
            )

        handlers = {
            **standard_handlers(),
            "bear_case": conceding_bear_case,
            "bear_rebuttal": conceding_bear_rebuttal,
        }
        client = StubStageClient(handlers)
        outcome = self.run_debate(client)

        run = outcome.run
        # The protocol finishes the analysis; the guard/execution lifecycle completes it.
        self.assertEqual(run.status, DecisionStatus.VALIDATING)
        self.assertEqual(
            Claim.objects.filter(run=run, stance=ClaimStance.BEAR).count(),
            0,
            "a bear claim was manufactured to fill a quota",
        )
        self.assertEqual(Claim.objects.filter(run=run, stance=ClaimStance.BULL).count(), 2)

        concession = Rebuttal.objects.get(run=run, response_type="agree")
        self.assertEqual(concession.responding_agent, AgentRole.BEAR)
        self.assertTrue(concession.position_change.lower().startswith("conceded"))
        self.assertIsNotNone(DecisionRecord.objects.filter(run=run).first())

        # The bear had nothing to rebut, so the bull's rebuttal stage was
        # skipped and said so rather than inventing a target.
        limitations = [
            event.payload.get("limitation", "")
            for event in events_of(run)
            if event.event_type == RunEventType.LIMITATION_RECORDED
        ]
        self.assertTrue(
            any("no validated claim to rebut" in text for text in limitations),
            limitations,
        )
        self.assertEqual(
            len(client.stage_calls("bull_rebuttal")), 0, "bull rebutted a non-existent case"
        )

        metadata = run.protocol_metadata
        self.assertFalse(metadata["counts"]["bear_claims"])
        self.assertTrue(metadata["analyst_consensus"])
        proposed = event_of(run, RunEventType.DECISION_PROPOSED)
        self.assertTrue(proposed.payload["analyst_consensus"])

        detail = auth_client(self.portfolio.user).get(
            reverse("core:decision-run-detail-rich", args=[str(run.id)])
        )
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        body = detail.json()
        self.assertTrue(body["has_rebuttals"])
        self.assertEqual(body["rebuttals"][0]["response_type"], "agree")

    def test_claims_and_responses_are_bounded_and_excess_items_are_recorded(self):
        """DEB-02 bounds: at most 3 claims and 2 responses, extras flagged."""

        def over_long_bull_case(client, prompt):
            ids = catalog_ids(prompt)
            claims = [
                {
                    "statement": f"Bull claim number {index} is asserted from the catalog",
                    "claim_type": "observed_fact",
                    "evidence_ids": ids[:1],
                    "horizon": "30d",
                    "limitations": "",
                }
                for index in range(1, 6)
            ]
            return json.dumps({"summary": "Five claims, four too many.", "claims": claims})

        def over_long_bear_rebuttal(client, prompt):
            targets = opposing_claim_ids(prompt)
            return json.dumps(
                {
                    "responses": [
                        {
                            "target_claim_id": target,
                            "response_type": "dispute",
                            "explanation": "This claim does not survive the demand warning.",
                            "evidence_ids": catalog_ids(prompt)[-1:],
                            "position_change": f"Unchanged after response {index}.",
                        }
                        for index, target in enumerate(targets[:3], start=1)
                    ]
                }
            )

        handlers = {
            **standard_handlers(),
            "bull_case": over_long_bull_case,
            "bear_rebuttal": over_long_bear_rebuttal,
        }
        client = StubStageClient(handlers)
        outcome = self.run_debate(client)
        run = outcome.run

        self.assertEqual(
            Claim.objects.filter(run=run, stance=ClaimStance.BULL).count(),
            MAX_INITIAL_CLAIMS,
        )
        self.assertEqual(
            Rebuttal.objects.filter(run=run, responding_agent=AgentRole.BEAR).count(),
            MAX_REBUTTALS_PER_AGENT,
        )
        excluded = run.protocol_metadata["excluded_items"]
        claim_exclusions = [
            item
            for item in excluded
            if item["kind"] == "claim" and f"{MAX_INITIAL_CLAIMS}-claim bound" in item["reason"]
        ]
        rebuttal_exclusions = [
            item
            for item in excluded
            if item["kind"] == "rebuttal"
            and f"{MAX_REBUTTALS_PER_AGENT}-response bound" in item["reason"]
        ]
        self.assertEqual(len(claim_exclusions), 2)
        self.assertEqual(len(rebuttal_exclusions), 1)
        # Every exclusion is also announced on the event log, not just stored.
        limitation_events = [
            event
            for event in events_of(run)
            if event.event_type == RunEventType.LIMITATION_RECORDED
        ]
        self.assertGreaterEqual(len(limitation_events), 3)
        self.assertEqual(run.protocol_metadata["limits"]["max_initial_claims"], 3)
        self.assertEqual(run.protocol_metadata["limits"]["max_rebuttals_per_agent"], 2)
        self.assertEqual(run.protocol_metadata["limits"]["rebuttal_rounds"], 1)
        self.assertEqual(REBUTTAL_ROUNDS, 1)

    def test_claim_citing_evidence_outside_the_catalog_is_excluded_and_flagged(self):
        """DEB-02: an unsupported claim is excluded *and recorded*, never dropped."""

        def unsupported_bull(client, prompt):
            return json.dumps(
                {
                    "summary": "One claim, but I could not support it from the catalog.",
                    "claims": [
                        {
                            "statement": "A claim with no catalog support at all",
                            "claim_type": "observed_fact",
                            "evidence_ids": ["22222222-2222-4222-8222-222222222222"],
                            "horizon": "30d",
                            "limitations": "",
                        }
                    ],
                }
            )

        def conceding_bear(client, prompt):
            return json.dumps(
                {
                    "summary": "No credible bear case was found.",
                    "claims": [],
                }
            )

        client = StubStageClient(
            {
                **standard_handlers(),
                "bull_case": unsupported_bull,
                "bear_case": conceding_bear,
            }
        )
        outcome = self.run_debate(client)
        run = outcome.run

        # The bounded repair was spent on it, and it still was not supportable.
        self.assertEqual(len(client.stage_calls("bull_case")), 2)
        self.assertEqual(Claim.objects.filter(run=run, stance=ClaimStance.BULL).count(), 0)
        excluded = [
            item for item in run.protocol_metadata["excluded_items"] if item["kind"] == "claim"
        ]
        self.assertTrue(any("outside the catalog" in item["reason"] for item in excluded), excluded)
        limitations = [
            event.payload.get("limitation", "")
            for event in events_of(run)
            if event.event_type == RunEventType.LIMITATION_RECORDED
        ]
        self.assertTrue(any("outside the catalog" in text for text in limitations), limitations)
        # The protocol finishes the analysis; the guard/execution lifecycle completes it.
        self.assertEqual(run.status, DecisionStatus.VALIDATING)

    def test_invalid_rebuttal_target_gets_one_bounded_repair_then_succeeds(self):
        attempts = {"count": 0}

        def repairing_bear_rebuttal(client, prompt):
            attempts["count"] += 1
            targets = opposing_claim_ids(prompt)
            if attempts["count"] == 1:
                return json.dumps(
                    {
                        "responses": [
                            {
                                "target_claim_id": "claim-that-does-not-exist",
                                "response_type": "dispute",
                                "explanation": "Aimed at nothing.",
                                "evidence_ids": catalog_ids(prompt)[-1:],
                                "position_change": "Unchanged.",
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "responses": [
                        {
                            "target_claim_id": targets[0],
                            "response_type": "dispute",
                            "explanation": "Aimed at the real claim after the repair.",
                            "evidence_ids": catalog_ids(prompt)[-1:],
                            "position_change": "Unchanged after repair.",
                        }
                    ]
                }
            )

        client = StubStageClient({**standard_handlers(), "bear_rebuttal": repairing_bear_rebuttal})
        outcome = self.run_debate(client)

        self.assertEqual(attempts["count"], 2, "the stage was not repaired exactly once")
        repair_prompts = client.stage_calls("bear_rebuttal")
        self.assertIn("VALID TARGET CLAIM IDS", repair_prompts[1])
        self.assertIn("schema repair", repair_prompts[1])
        # The protocol finishes the analysis; the guard/execution lifecycle completes it.
        self.assertEqual(outcome.run.status, DecisionStatus.VALIDATING)
        self.assertEqual(
            Rebuttal.objects.filter(run=outcome.run, response_type="dispute").count(), 1
        )
        self.assertEqual(outcome.run.protocol_metadata["usage"]["schema_repairs_used"], 1)

    def test_invalid_rebuttal_target_fails_the_stage_after_the_repair(self):
        def always_bad(client, prompt):
            return json.dumps(
                {
                    "responses": [
                        {
                            "target_claim_id": "still-not-a-claim",
                            "response_type": "dispute",
                            "explanation": "Never fixes the target.",
                            "evidence_ids": catalog_ids(prompt)[-1:],
                            "position_change": "Unchanged.",
                        }
                    ]
                }
            )

        def empty_bull_rebuttal(client, prompt):
            return json.dumps(
                {
                    "responses": [],
                    "no_response_reason": "No material response to the bear claims.",
                }
            )

        client = StubStageClient(
            {
                **standard_handlers(),
                "bull_rebuttal": empty_bull_rebuttal,
                "bear_rebuttal": always_bad,
            }
        )
        with self.assertRaises(RebuttalTargetError) as ctx:
            self.run_debate(client)

        failure = ctx.exception
        run = failure.run
        self.assertIsNotNone(run)
        self.assertEqual(failure.code, "invalid_rebuttal_target")
        self.assertEqual(failure.stage, "rebutting")
        self.assertEqual(len(client.stage_calls("bear_rebuttal")), 2)

        run.refresh_from_db()
        self.assertEqual(run.status, DecisionStatus.FAILED)
        self.assertEqual(run.error_code, "invalid_rebuttal_target")
        self.assertEqual(Rebuttal.objects.filter(run=run).count(), 0)
        self.assertFalse(DecisionRecord.objects.filter(run=run).exists())
        states = {item["stage"]: item["state"] for item in stage_states(run)["stages"]}
        self.assertEqual(states["rebutting"], "failed")
        self.assertFalse(
            any(event.event_type == RunEventType.REBUTTAL_CREATED for event in events_of(run))
        )


# ---------------------------------------------------------------------------
# DEB-04 / acceptance scenario 3: typed failure, no fictional rebuttal
# ---------------------------------------------------------------------------
class FailurePathTests(ProtocolTestCase):
    @override_settings(
        AI_CONFIG={
            "API_KEY": "",
            "PROVIDER": "deepseek",
            "MODEL": "deepseek-reasoner",
            "ALLOW_HEURISTIC_FALLBACK": True,
        }
    )
    def test_stage_timeout_fails_the_stage_and_records_no_completed_rebuttal(self):
        """Acceptance scenario 3: the UI-visible state names the failed stage."""
        client = StubStageClient(
            standard_handlers(),
            failures={
                "bull_rebuttal": [
                    StageTimeout(
                        "stage 'bull_rebuttal' did not return within 0.01s",
                        stage="rebutting",
                    )
                ]
            },
        )
        with self.assertRaises(StageTimeout) as ctx:
            self.run_debate(client)

        run = ctx.exception.run
        self.assertIsNotNone(run)
        run.refresh_from_db()
        self.assertEqual(run.status, DecisionStatus.FAILED)
        self.assertEqual(run.error_code, "stage_timeout")
        self.assertIn("bull_rebuttal", run.error_detail)

        failed_event = event_of(run, RunEventType.STAGE_FAILED)
        self.assertEqual(failed_event.stage, "rebutting")
        self.assertEqual(failed_event.payload["code"], "stage_timeout")
        self.assertTrue(failed_event.payload["fallback_allowed"])

        states = {item["stage"]: item["state"] for item in stage_states(run)["stages"]}
        self.assertEqual(states["analysing"], "complete")
        self.assertEqual(states["rebutting"], "failed")
        self.assertEqual(states["adjudicating"], "not_run")

        # Claims are real records of what happened; no rebuttal was completed.
        self.assertEqual(Claim.objects.filter(run=run).count(), 4)
        self.assertEqual(Rebuttal.objects.filter(run=run).count(), 0)
        self.assertFalse(DecisionRecord.objects.filter(run=run).exists())

        client = auth_client(self.portfolio.user)
        body = client.get(reverse("core:decision-run-detail-rich", args=[str(run.id)])).json()
        self.assertEqual(body["run"]["status"], "failed")
        self.assertEqual(body["run"]["failure"]["error_code"], "stage_timeout")
        self.assertFalse(body["has_rebuttals"])
        self.assertEqual(body["rebuttals"], [])
        states = {item["stage"]: item["state"] for item in body["run"]["stage_states"]["stages"]}
        self.assertEqual(states["rebutting"], "failed")
        self.assertEqual(body["rationale"], None)

    @override_settings(
        AI_CONFIG={
            "API_KEY": "",
            "PROVIDER": "deepseek",
            "MODEL": "deepseek-reasoner",
            "ALLOW_HEURISTIC_FALLBACK": True,
        }
    )
    def test_timeout_records_a_separate_fallback_attempt_with_no_debate(self):
        """Acceptance scenario 3: the fallback label is visible and separate."""
        client = StubStageClient(
            standard_handlers(),
            failures={
                "bull_rebuttal": [StageTimeout("provider call timed out", stage="rebutting")]
            },
        )
        result = run_alpha_agent(self.context, model_client=client)

        self.assertEqual(result.source, "heuristic_fallback")
        self.assertTrue(result.used_fallback)
        self.assertIn("stage_timeout", result.error)
        self.assertEqual(result.protocol_version, HEURISTIC_PROTOCOL_VERSION)
        self.assertFalse(result.extra["debate_executed"])

        failed_run = DecisionRun.objects.get(status=DecisionStatus.FAILED)
        fallback_run = DecisionRun.objects.get(mode=RunMode.HEURISTIC)
        self.assertEqual(str(failed_run.id), result.extra["fallback_of"])
        self.assertEqual(str(fallback_run.id), result.decision_run_id)
        self.assertEqual(fallback_run.source, RunSource.HEURISTIC_FALLBACK)
        self.assertEqual(fallback_run.snapshot_id, failed_run.snapshot_id)

        # The fallback did not inherit the failed attempt's debate.
        self.assertFalse(Claim.objects.filter(run=fallback_run).exists())
        self.assertFalse(Rebuttal.objects.filter(run=fallback_run).exists())
        # The failed attempt keeps the claims it really recorded, and no
        # rebuttal: the timeout happened before any response was accepted.
        self.assertEqual(Claim.objects.filter(run=failed_run).count(), 4)
        self.assertFalse(Rebuttal.objects.filter(run=failed_run).exists())
        self.assertTrue(DecisionRecord.objects.filter(run=fallback_run).exists())
        self.assertFalse(DecisionRecord.objects.filter(run=failed_run).exists())

        # The failed run names the fallback it was replaced by.
        limitation = next(
            event
            for event in events_of(failed_run)
            if event.event_type == RunEventType.LIMITATION_RECORDED
            and event.payload.get("fallback_run_id")
        )
        self.assertEqual(limitation.payload["fallback_run_id"], str(fallback_run.id))
        self.assertEqual(limitation.payload["fallback_mode"], RunMode.HEURISTIC)
        failed_run.refresh_from_db()
        self.assertEqual(failed_run.protocol_metadata["fallback"]["run_id"], str(fallback_run.id))
        self.assertFalse(failed_run.protocol_metadata["fallback"]["debate_executed"])

        api = auth_client(self.portfolio.user)
        failed_body = api.get(
            reverse("core:decision-run-detail-rich", args=[str(failed_run.id)])
        ).json()
        self.assertEqual(failed_body["run"]["status"], "failed")
        self.assertEqual(failed_body["run"]["protocol"]["fallback"]["run_id"], str(fallback_run.id))
        self.assertFalse(failed_body["has_rebuttals"])

        fallback_body = api.get(
            reverse("core:decision-run-detail-rich", args=[str(fallback_run.id)])
        ).json()
        self.assertEqual(fallback_body["run"]["mode"], RunMode.HEURISTIC)
        self.assertFalse(fallback_body["run"]["is_synthetic"])
        self.assertFalse(fallback_body["has_rebuttals"])
        self.assertEqual(fallback_body["claims"], [])
        self.assertEqual(fallback_body["rebuttals"], [])
        self.assertEqual(fallback_body["verdict"]["action"], "HOLD")

    def test_unparseable_case_after_repair_is_a_missing_required_case(self):
        """DEB-04: a missing required case stops the protocol with a typed failure."""
        client = StubStageClient(
            {**standard_handlers(), "bull_case": "I could not decide on a case."}
        )
        with self.assertRaises(MissingRequiredCase) as ctx:
            self.run_debate(client)

        failure = ctx.exception
        self.assertEqual(failure.code, "missing_required_case")
        self.assertEqual(failure.stage, "analysing")
        self.assertEqual(len(client.stage_calls("bull_case")), 2)
        run = failure.run
        run.refresh_from_db()
        self.assertEqual(run.status, DecisionStatus.FAILED)
        self.assertEqual(run.error_code, "missing_required_case")
        self.assertFalse(Claim.objects.filter(run=run).exists())
        self.assertFalse(DecisionRecord.objects.filter(run=run).exists())
        states = {item["stage"]: item["state"] for item in stage_states(run)["stages"]}
        self.assertEqual(states["analysing"], "failed")

    def test_transient_provider_errors_are_retried_with_backoff(self):
        client = StubStageClient(
            standard_handlers(),
            failures={
                "bull_case": [
                    ProviderTransientError("429 rate limit", stage="analysing"),
                    ProviderTransientError("503 temporarily unavailable", stage="analysing"),
                ]
            },
        )
        outcome = self.run_debate(client)

        # The protocol finishes the analysis; the guard/execution lifecycle completes it.
        self.assertEqual(outcome.run.status, DecisionStatus.VALIDATING)
        self.assertEqual(len(client.stage_calls("bull_case")), 3)
        usage = outcome.run.protocol_metadata["usage"]
        self.assertEqual(usage["provider_retries_used"], 2)
        attempts = outcome.run.protocol_metadata["attempts"]
        self.assertEqual(len([item for item in attempts if item["kind"] == "provider_retry"]), 2)

    def test_exhausted_retries_are_a_typed_provider_failure(self):
        client = StubStageClient(
            standard_handlers(),
            failures={
                "bull_case": [
                    ProviderTransientError("429 rate limit", stage="analysing"),
                    ProviderTransientError("429 rate limit", stage="analysing"),
                    ProviderTransientError("429 rate limit", stage="analysing"),
                ]
            },
        )
        with self.assertRaises(ProviderError) as ctx:
            self.run_debate(client)

        self.assertEqual(ctx.exception.code, "provider_error")
        self.assertEqual(len(client.stage_calls("bull_case")), 3)
        run = ctx.exception.run
        run.refresh_from_db()
        self.assertEqual(run.status, DecisionStatus.FAILED)
        self.assertEqual(run.error_code, "provider_error")
        self.assertEqual(Rebuttal.objects.filter(run=run).count(), 0)

    def test_output_token_budget_stops_the_protocol_with_a_typed_failure(self):
        client = StubStageClient(standard_handlers())
        with self.assertRaises(BudgetExhausted) as ctx:
            self.run_debate(client, budget_overrides={"max_output_tokens": 8})

        self.assertEqual(ctx.exception.code, "budget_exhausted")
        self.assertEqual(ctx.exception.budget, "output_tokens")
        run = ctx.exception.run
        run.refresh_from_db()
        self.assertEqual(run.status, DecisionStatus.FAILED)
        self.assertEqual(run.error_code, "budget_exhausted")
        self.assertIn("output token budget", run.error_detail)

    def test_monetary_budget_stops_the_protocol(self):
        client = StubStageClient(standard_handlers(), cost=Decimal("2.00"))
        with self.assertRaises(BudgetExhausted) as ctx:
            self.run_debate(client, budget_overrides={"monetary_budget_usd": Decimal("1.00")})

        self.assertEqual(ctx.exception.budget, "monetary")
        run = ctx.exception.run
        run.refresh_from_db()
        self.assertEqual(run.status, DecisionStatus.FAILED)
        self.assertEqual(run.protocol_metadata["usage"]["cost_reported"], True)
        self.assertTrue(Claim.objects.filter(run=run).exists())

    def test_run_deadline_stops_the_protocol(self):
        # The deadline includes evidence capture, which does database work. Keep
        # a wide margin between that setup cost and the sleeping stage so a
        # loaded test runner cannot trip the deadline before the first model
        # call and turn this into a timing flake rather than a protocol check.
        def slow_bull_case(client, prompt):
            time.sleep(1.8)
            return bull_case(client, prompt)

        client = StubStageClient({**standard_handlers(), "bull_case": slow_bull_case})
        with self.assertRaises(BudgetExhausted) as ctx:
            self.run_debate(client, budget_overrides={"run_deadline_seconds": 1.5})

        self.assertEqual(ctx.exception.code, "run_deadline_exceeded")
        run = ctx.exception.run
        run.refresh_from_db()
        self.assertEqual(run.status, DecisionStatus.FAILED)
        self.assertEqual(run.error_code, "run_deadline_exceeded")
        self.assertEqual(len(client.stage_calls("bull_case")), 1)
        self.assertEqual(len(client.stage_calls("bear_case")), 0)


# ---------------------------------------------------------------------------
# DEB-03: adjudication
# ---------------------------------------------------------------------------
class AdjudicationTests(ProtocolTestCase):
    def test_hold_is_a_valid_validated_proposal(self):
        client = StubStageClient(standard_handlers())
        outcome = self.run_debate(client)
        decision = outcome.run.decision

        self.assertEqual(decision.action, "HOLD")
        self.assertIsNone(decision.requested_quantity)
        self.assertTrue(decision.concise_rationale)
        self.assertTrue(decision.strongest_objection)
        self.assertEqual(decision.engine_source, RunSource.CREWAI)
        self.assertTrue(outcome.proposal.unresolved_uncertainty)

        body = (
            auth_client(self.portfolio.user)
            .get(reverse("core:decision-run-detail-rich", args=[str(outcome.run.id)]))
            .json()
        )
        self.assertEqual(body["verdict"]["action"], "HOLD")
        self.assertEqual(body["verdict"]["requested_quantity"], None)
        self.assertIn("supplier warning", body["verdict"]["strongest_objection"])
        self.assertIn("HOLD", body["rationale"])

    def test_cio_must_acknowledge_the_strongest_objection(self):
        def no_objection(client, prompt):
            return json.dumps(
                {
                    "action": "BUY",
                    "amount": 1,
                    "sentiment": "BULLISH",
                    "reasoning": "Buying because the bull case won.",
                    "decisive_claim_ids": [],
                    "strongest_objection": "",
                    "unresolved_uncertainty": "",
                    "thesis": "Services growth.",
                    "invalidation_condition": "Services growth below 5%.",
                }
            )

        client = StubStageClient({**standard_handlers(), "adjudication": no_objection})
        with self.assertRaises(StageSchemaError) as ctx:
            self.run_debate(client)

        failure = ctx.exception
        self.assertEqual(failure.code, "invalid_stage_output")
        self.assertEqual(failure.stage, "adjudicating")
        self.assertEqual(len(client.stage_calls("adjudication")), 2)
        run = failure.run
        run.refresh_from_db()
        self.assertEqual(run.status, DecisionStatus.FAILED)
        states = {item["stage"]: item["state"] for item in stage_states(run)["stages"]}
        self.assertEqual(states["adjudicating"], "failed")
        self.assertEqual(states["validating"], "not_run")
        self.assertFalse(DecisionRecord.objects.filter(run=run).exists())

    def test_unknown_decisive_claim_ids_are_dropped_and_flagged(self):
        def hallucinating_cio(client, prompt):
            payload = json.loads(adjudication(client, prompt))
            payload["decisive_claim_ids"] = ["00000000-0000-4000-8000-000000000000"]
            return json.dumps(payload)

        client = StubStageClient({**standard_handlers(), "adjudication": hallucinating_cio})
        outcome = self.run_debate(client)
        decision = outcome.run.decision
        self.assertEqual(decision.decisive_claim_ids, [])
        reasons = [
            item["reason"]
            for item in outcome.run.protocol_metadata["excluded_items"]
            if item["kind"] == "adjudication"
        ]
        self.assertTrue(any("not claims in this run" in reason for reason in reasons))


# ---------------------------------------------------------------------------
# Acceptance scenario 4: the one-agent baseline
# ---------------------------------------------------------------------------
class SingleAgentBaselineTests(ProtocolTestCase):
    def test_baseline_reuses_the_identical_snapshot_and_proposal_contract(self):
        debate_client = StubStageClient(standard_handlers())
        debate = self.run_debate(debate_client)
        snapshot = debate.snapshot
        self.assertIsNotNone(snapshot)

        baseline_client = StubStageClient({"baseline_analysis": baseline_analysis})
        baseline = self.protocol(baseline_client).run_single_agent_baseline(
            self.context, portfolio=self.portfolio, snapshot=snapshot
        )

        self.assertEqual(baseline.run.snapshot_id, snapshot.id)
        self.assertEqual(baseline.run.protocol_version, BASELINE_PROTOCOL_VERSION)
        self.assertEqual(baseline.run.mode, RunMode.LIVE)
        self.assertFalse(baseline.run.is_synthetic)
        # The protocol finishes the analysis; the guard/execution lifecycle completes it.
        self.assertEqual(baseline.run.status, DecisionStatus.VALIDATING)
        self.assertTrue(baseline.recorded)

        # The debate stages did not run.
        self.assertFalse(Claim.objects.filter(run=baseline.run).exists())
        self.assertFalse(Rebuttal.objects.filter(run=baseline.run).exists())
        self.assertEqual(len(baseline_client.stage_calls("bull_case")), 0)
        self.assertEqual(len(baseline_client.stage_calls("bear_case")), 0)
        self.assertEqual(len(baseline_client.stage_calls("bull_rebuttal")), 0)
        self.assertEqual(len(baseline_client.stage_calls("bear_rebuttal")), 0)
        self.assertEqual(len(baseline_client.stage_calls("baseline_analysis")), 1)
        prompt = baseline_client.stage_calls("baseline_analysis")[0]
        self.assertIn("single-agent baseline", prompt)
        self.assertNotIn("OPPOSING VALIDATED CLAIMS", prompt)
        self.assertEqual(
            set(catalog_ids(prompt)),
            set(catalog_ids(debate_client.stage_calls("bull_case")[0])),
        )

        # Same final proposal contract: the DecisionRecord fields differ in
        # content (a different snapshot state and no debate) but not in shape.
        debate_decision = debate.run.decision
        baseline_decision = baseline.run.decision
        for field in (
            "action",
            "requested_quantity",
            "concise_rationale",
            "decisive_claim_ids",
            "strongest_objection",
            "thesis",
            "invalidation_condition",
            "engine_source",
        ):
            self.assertTrue(hasattr(baseline_decision, field), field)
        # A HOLD records no requested quantity, which is the contract (None is
        # "nothing was asked for", 0 would be a malformed proposal).
        self.assertIsNone(baseline_decision.requested_quantity)
        self.assertTrue(baseline_decision.strongest_objection)
        self.assertEqual(baseline_decision.action, "HOLD")
        self.assertEqual(baseline_decision.decisive_claim_ids, [])
        self.assertNotEqual(baseline_decision.concise_rationale, debate_decision.concise_rationale)

        states = {item["stage"]: item["state"] for item in stage_states(baseline.run)["stages"]}
        self.assertEqual(states["rebutting"], "not_run")
        self.assertEqual(states["analysing"], "complete")
        self.assertEqual(states["adjudicating"], "complete")

        proposed = event_of(baseline.run, RunEventType.DECISION_PROPOSED)
        self.assertTrue(proposed.payload["baseline"])
        self.assertIsNone(proposed.payload["analyst_consensus"])
        self.assertEqual(
            baseline.run.protocol_metadata["protocol"]["prompt_template_version"],
            PROMPT_TEMPLATE_VERSION,
        )

        body = (
            auth_client(self.portfolio.user)
            .get(reverse("core:decision-run-detail-rich", args=[str(baseline.run.id)]))
            .json()
        )
        self.assertFalse(body["has_rebuttals"])
        self.assertEqual(body["run"]["mode"], RunMode.LIVE)
        self.assertEqual(body["claims"], [])

    @override_settings(
        DEBATE_CONFIG={
            "PROTOCOL": "single_agent.v1",
            "STAGE_TIMEOUT_SECONDS": 5,
            "RUN_DEADLINE_SECONDS": 30,
            "MAX_INPUT_TOKENS": 200_000,
            "MAX_OUTPUT_TOKENS": 100_000,
            "SCHEMA_REPAIR_ATTEMPTS": 1,
            "PROVIDER_RETRIES": 2,
            "PROVIDER_RETRY_BACKOFF_SECONDS": 0,
            "MONETARY_BUDGET_USD": "10.00",
            "MAX_EVIDENCE_ITEMS": 20,
        }
    )
    def test_single_agent_protocol_is_selectable_through_configuration(self):
        client = StubStageClient({"baseline_analysis": baseline_analysis})
        result = run_alpha_agent(self.context, model_client=client)
        run = DecisionRun.objects.get(pk=result.decision_run_id)
        self.assertEqual(run.protocol_version, BASELINE_PROTOCOL_VERSION)
        self.assertEqual(run.mode, RunMode.LIVE)
        self.assertFalse(Claim.objects.filter(run=run).exists())
        self.assertFalse(Rebuttal.objects.filter(run=run).exists())
        self.assertEqual(len(client.stage_calls("baseline_analysis")), 1)

    def test_baseline_cannot_record_decisive_claims_it_never_made(self):
        def baseline_with_fake_claim(client, prompt):
            payload = json.loads(baseline_analysis(client, prompt))
            payload["decisive_claim_ids"] = ["11111111-1111-4111-8111-111111111111"]
            return json.dumps(payload)

        baseline_client = StubStageClient({"baseline_analysis": baseline_with_fake_claim})
        baseline = self.protocol(baseline_client).run_single_agent_baseline(
            self.context, portfolio=self.portfolio
        )
        self.assertEqual(baseline.run.decision.decisive_claim_ids, [])
        self.assertTrue(
            any(
                "not claims in this run" in item["reason"]
                for item in baseline.run.protocol_metadata["excluded_items"]
            )
        )


# ---------------------------------------------------------------------------
# DEB-05: reproducibility
# ---------------------------------------------------------------------------
class ReproducibilityTests(ProtocolTestCase):
    def test_run_records_provider_model_parameters_prompts_tools_and_raw_outputs(self):
        client = StubStageClient(standard_handlers())
        outcome = self.run_debate(client)
        run = outcome.run
        metadata = run.protocol_metadata

        protocol = metadata["protocol"]
        self.assertEqual(protocol["provider"], "stub")
        self.assertEqual(protocol["model"], "stub-debate-1")
        self.assertEqual(protocol["parameters"]["temperature"], 0.0)
        self.assertEqual(protocol["prompt_template_version"], PROMPT_TEMPLATE_VERSION)
        self.assertEqual(
            set(protocol["tool_versions"]), {"news", "fundamentals", "price_history", "market_data"}
        )
        self.assertEqual(protocol["snapshot_hash"], run.snapshot.content_hash)
        self.assertEqual(protocol["rounds"], 1)
        self.assertEqual(protocol["limits"]["max_initial_claims"], 3)

        self.assertEqual(
            set(metadata["raw_outputs"]),
            {"bull_case", "bear_case", "bull_rebuttal", "bear_rebuttal", "adjudication"},
        )
        self.assertEqual(
            metadata["raw_outputs"]["bull_case"]["claims"][0]["statement"], BULL_MARKER
        )
        self.assertEqual(metadata["usage"]["calls"], 5)
        self.assertEqual(metadata["usage"]["token_accounting"], "provider_reported")
        self.assertEqual(metadata["counts"]["rebuttal_rounds_run"], 1)

        self.assertTrue(run.model_hash)
        self.assertTrue(run.config_hash)
        self.assertTrue(run.prompt_hash)
        self.assertEqual(run.input_tokens, metadata["usage"]["input_tokens"])
        self.assertEqual(run.output_tokens, metadata["usage"]["output_tokens"])
        self.assertEqual(Decimal(str(run.total_cost_usd)), Decimal(metadata["usage"]["cost_usd"]))

        note = metadata["deterministic_replay"]
        self.assertIn("replay deterministically", note)
        self.assertIn("does not", note)
        self.assertIn("new sample", note)

    def test_protocol_identity_is_stable_across_runs_of_the_same_configuration(self):
        first = self.run_debate(StubStageClient(standard_handlers()))
        second = self.run_debate(StubStageClient(standard_handlers()))

        self.assertEqual(first.run.model_hash, second.run.model_hash)
        self.assertEqual(first.run.config_hash, second.run.config_hash)
        self.assertEqual(first.run.prompt_hash, second.run.prompt_hash)
        self.assertNotEqual(first.run.snapshot_id, second.run.snapshot_id)

    def test_raw_outputs_are_not_part_of_the_read_contract(self):
        client = StubStageClient(standard_handlers())
        outcome = self.run_debate(client)
        body = (
            auth_client(self.portfolio.user)
            .get(reverse("core:decision-run-detail", args=[str(outcome.run.id)]))
            .json()
        )
        self.assertEqual(body["protocol"]["provider"], "stub")
        self.assertEqual(body["protocol"]["model"], "stub-debate-1")
        self.assertEqual(body["protocol"]["prompt_template_version"], PROMPT_TEMPLATE_VERSION)
        self.assertIn("limits", body["protocol"])
        self.assertNotIn("raw_outputs", body["protocol"])


# ---------------------------------------------------------------------------
# The API must expose the produced run as live, not synthetic
# ---------------------------------------------------------------------------
class ApiExposureTests(ProtocolTestCase):
    def test_live_protocol_run_is_not_synthetic_and_exposes_its_rebuttals(self):
        client = StubStageClient(standard_handlers())
        outcome = self.run_debate(client)
        run = outcome.run
        self.assertNotEqual(run.mode, "synthetic")
        self.assertFalse(run.is_synthetic)

        api = auth_client(self.portfolio.user)
        detail = api.get(reverse("core:decision-run-detail-rich", args=[str(run.id)]))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        body = detail.json()
        self.assertEqual(body["run"]["mode"], RunMode.LIVE)
        self.assertFalse(body["run"]["is_synthetic"])
        self.assertEqual(body["run"]["provenance"]["mode"], RunMode.LIVE)
        self.assertEqual(body["run"]["protocol_version"], PROTOCOL_VERSION)
        self.assertTrue(body["has_rebuttals"])
        response_types = {item["response_type"] for item in body["rebuttals"]}
        self.assertIn("dispute", response_types)
        self.assertIn("qualify", response_types)
        self.assertEqual(len(body["cases"]["bull"]), 2)
        self.assertEqual(len(body["cases"]["bear"]), 2)
        self.assertTrue(body["decisive_claim_ids"])
        self.assertTrue(body["strongest_objection"])

        events = api.get(reverse("core:decision-run-events", args=[str(run.id)])).json()
        types = [event["type"] for event in events["events"]]
        self.assertIn("claim.created", types)
        self.assertIn("rebuttal.created", types)
        self.assertIn("decision.proposed", types)
        self.assertLess(
            max(index for index, value in enumerate(types) if value == "claim.created"),
            min(index for index, value in enumerate(types) if value == "rebuttal.created"),
        )

        listed = api.get(reverse("core:decision-run-list")).json()["results"]
        item = next(entry for entry in listed if entry["id"] == str(run.id))
        self.assertEqual(item["mode"], RunMode.LIVE)
        self.assertEqual(item["source"], RunSource.CREWAI)
        self.assertFalse(item["is_synthetic"])

    def test_unrecorded_run_writes_no_rows(self):
        client = StubStageClient(standard_handlers())
        outcome = self.protocol(client).run(self.context, portfolio=self.portfolio, record=False)
        self.assertFalse(outcome.recorded)
        self.assertIsNone(outcome.run)
        self.assertIsInstance(outcome.proposal, Adjudication)
        self.assertEqual(outcome.proposal.action, "HOLD")
        for model in (
            DecisionRun,
            InputSnapshot,
            EvidenceItem,
            Claim,
            Rebuttal,
            DecisionRecord,
        ):
            self.assertFalse(model.objects.exists(), model.__name__)

    @override_settings(
        AI_CONFIG={
            "API_KEY": "",
            "PROVIDER": "deepseek",
            "MODEL": "deepseek-reasoner",
            "ALLOW_HEURISTIC_FALLBACK": True,
        }
    )
    def test_unconfigured_llm_is_recorded_as_heuristic_not_synthetic(self):
        result = run_alpha_agent(self.context)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.source, "heuristic_fallback")
        self.assertEqual(result.protocol_version, HEURISTIC_PROTOCOL_VERSION)

        run = DecisionRun.objects.get(pk=result.decision_run_id)
        self.assertEqual(run.mode, RunMode.HEURISTIC)
        self.assertEqual(run.source, RunSource.HEURISTIC_FALLBACK)
        self.assertFalse(run.is_synthetic)
        self.assertFalse(Claim.objects.filter(run=run).exists())
        self.assertFalse(Rebuttal.objects.filter(run=run).exists())
        self.assertTrue(DecisionRecord.objects.filter(run=run).exists())

        body = (
            auth_client(self.portfolio.user)
            .get(reverse("core:decision-run-detail-rich", args=[str(run.id)]))
            .json()
        )
        self.assertEqual(body["run"]["mode"], RunMode.HEURISTIC)
        self.assertFalse(body["run"]["is_synthetic"])
        self.assertFalse(body["has_rebuttals"])
        self.assertEqual(body["rebuttals"], [])
        proposed = event_of(run, RunEventType.DECISION_PROPOSED)
        self.assertTrue(proposed.payload["no_debate"])
        self.assertEqual(run.protocol_metadata["fallback"]["debate_executed"], False)

    @override_settings(
        AI_CONFIG={
            "API_KEY": "test-key",
            "PROVIDER": "deepseek",
            "MODEL": "deepseek-reasoner",
            "ALLOW_HEURISTIC_FALLBACK": True,
        },
        DEBATE_CONFIG={"PROTOCOL": "legacy_sequential"},
    )
    def test_legacy_sequential_pipeline_is_preserved_behind_configuration(self):
        fake_output = SimpleNamespace(
            pydantic=TradeProposal(
                action="BUY", amount=1.0, sentiment="BULLISH", reasoning="legacy crew"
            ),
            raw="",
            token_usage=SimpleNamespace(total_tokens=10),
        )
        with patch("crewai.Crew.kickoff", return_value=fake_output):
            result = run_alpha_agent(self.context, model_client=StubStageClient({}))

        self.assertEqual(result.source, "crewai")
        self.assertEqual(result.protocol_version, "legacy_sequential")
        self.assertFalse(result.extra["recorded"])
        self.assertIsNone(result.decision_run_id)
        self.assertFalse(DecisionRun.objects.exists())


# ---------------------------------------------------------------------------
# Producer <-> Phase 0 audit row link (DATA-04)
# ---------------------------------------------------------------------------
class RunRecordingTests(ProtocolTestCase):
    def _make_log(self) -> AgentDecisionLog:
        return AgentDecisionLog.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            action_taken="HOLD - fixture of the run link",
            reasoning="A decision produced by the debate protocol.",
        )

    def test_run_links_to_the_audit_row_once(self):
        outcome = self.run_debate(StubStageClient(standard_handlers()))
        log = self._make_log()

        link_legacy_log(outcome.run, log)
        outcome.run.refresh_from_db()
        self.assertEqual(outcome.run.legacy_log_id, log.id)

        # Idempotent for the same row, and refused for a second one.
        link_legacy_log(outcome.run, log)
        with self.assertRaises(DecisionRunError):
            link_legacy_log(outcome.run, self._make_log())

    @override_settings(CACHES=TEST_CACHES)
    def test_the_task_links_the_run_to_the_audit_row_it_explains(self):
        """The producer is wired into the pipeline, not just available to it."""
        from tasks import run_alpha_agent_task

        portfolio = make_portfolio(autonomous=True)
        outcome = self.protocol(StubStageClient(standard_handlers())).run(
            make_context(portfolio_id=portfolio.id), portfolio=portfolio
        )
        decision = outcome.run.decision
        result = AgentRunResult(
            proposal=TradeProposal(
                action=decision.action,
                amount=0.0,
                sentiment="NEUTRAL",
                reasoning="HOLD from the debate protocol.",
            ),
            reasoning="HOLD from the debate protocol.",
            source="crewai",
            decision_run_id=str(outcome.run.id),
            protocol_version=PROTOCOL_VERSION,
        )
        price = PriceQuote(ticker="AAPL", symbol="AAPL", price=Decimal("225.00"))
        with (
            patch("tasks.run_alpha_agent", return_value=result),
            patch("tasks.get_latest_quote", return_value=price),
            patch("tasks.build_news_report", return_value=NewsReport(ticker="AAPL")),
            patch("tasks.get_financial_health", return_value=None),
            patch("tasks.get_price_history", return_value=None),
        ):
            task_result = run_alpha_agent_task.apply(args=[portfolio.id, "AAPL"]).get()

        self.assertEqual(task_result["status"], "hold")
        log = AgentDecisionLog.objects.get(portfolio=portfolio)
        outcome.run.refresh_from_db()
        self.assertEqual(outcome.run.legacy_log_id, log.id)

    def test_a_run_of_another_portfolio_cannot_be_linked(self):
        outcome = self.run_debate(StubStageClient(standard_handlers()))
        other_log = AgentDecisionLog.objects.create(
            portfolio=make_portfolio(),
            ticker="AAPL",
            action_taken="HOLD - other portfolio",
            reasoning="Belongs to another portfolio.",
        )
        with self.assertRaises(DecisionRunError):
            link_legacy_log(outcome.run, other_log)


# ---------------------------------------------------------------------------
# FIX-07A: cost and deadline semantics
# ---------------------------------------------------------------------------
class CostAndDeadlineTests(ProtocolTestCase):
    """Five cost quantities are kept apart, and the deadline is a bound.

    Every test here fails on the pre-FIX-07 code: the legacy path priced tokens
    with a price table and called it the billed cost; an unreadable usage summary
    became ``(0, 0)`` and therefore a measured zero; the monetary ceiling was
    checked once before the retry loop; and each call used the stage timeout
    instead of the remaining run deadline.
    """

    def _client(self, *, usage=None, model="stub-debate-1", cost=Decimal("0.0005"), failures=None):
        client = StubStageClient(standard_handlers(), cost=cost, failures=failures)
        client.model = model
        client.usage = usage
        return client

    def _no_usage_client(self, *, model="stub-debate-1"):
        """A client that answers correctly but reports nothing about usage."""

        class SilentUsageClient(StubStageClient):
            def complete(self, stage, prompt):
                self.calls.append((stage, prompt))
                pending = self.failures.get(stage) or []
                if pending:
                    raise pending.pop(0)
                handler = self.handlers[stage]
                self.last_usage = None
                return handler(self, prompt) if callable(handler) else handler

        return SilentUsageClient(standard_handlers())

    def _protocol_with_model(self, client, *, model=None, **budget_overrides):
        budget = replace(self.budget, **budget_overrides) if budget_overrides else self.budget
        return DebateProtocol(
            client=client,
            budget=budget,
            config={"MAX_EVIDENCE_ITEMS": 20},
            provider=client.provider,
            model=model or client.model,
            parameters=client.parameters,
        )

    def test_missing_usage_is_null_with_a_reason_not_zero(self):
        """A client that reports no usage must not produce a measured zero."""
        client = self._no_usage_client()
        outcome = self._protocol_with_model(client).run(self.context, portfolio=self.portfolio)
        usage = outcome.run.protocol_metadata["usage"]

        self.assertEqual(usage["token_accounting"], "estimated_chars_per_4_tokens")
        self.assertEqual(usage["estimated_tokens"]["input"] > 0, True)
        self.assertIsNone(usage["billed_cost_usd"])
        self.assertFalse(usage["cost_reported"])
        self.assertIsNone(usage["cost_usd"])
        self.assertTrue(usage["billed_cost_unknown_reason"])
        # The test client's model id has no price-table entry either, so the
        # price-table quantity is unknown too - not zero and not a guess.
        self.assertIsNone(usage["price_table_cost_usd"])
        self.assertIsNone(outcome.run.total_cost_usd)
        self.assertEqual(outcome.run.cost_basis, "unknown")

    def test_a_retry_that_would_breach_the_monetary_budget_is_refused_before_it_is_made(self):
        """The ceiling is checked before every retry, not once before the loop."""

        class BilledFailureClient(StubStageClient):
            """Fails its first call after the provider has already charged for it."""

            def complete(self, stage, prompt):
                self.calls.append((stage, prompt))
                if stage == "bull_case" and not getattr(self, "failed_once", False):
                    self.failed_once = True
                    self.last_usage = ModelUsage(
                        input_tokens=1000,
                        output_tokens=200,
                        cost_usd=Decimal("2.00"),
                        reported=True,
                    )
                    raise ProviderTransientError("429 rate limit", stage="analysing")
                handler = self.handlers[stage]
                reply = handler(self, prompt) if callable(handler) else handler
                self.last_usage = ModelUsage(
                    input_tokens=max(1, len(prompt) // 4),
                    output_tokens=max(1, len(reply) // 4),
                    cost_usd=self.cost,
                    reported=True,
                )
                return reply

        client = BilledFailureClient(standard_handlers(), cost=Decimal("0.0005"))
        # The first, failed attempt reports $2.00. The run's ceiling is $1.00. The
        # retry would spend again, so it must never be issued.
        with self.assertRaises(BudgetExhausted) as ctx:
            self._protocol_with_model(client, monetary_budget_usd=Decimal("1.00")).run(
                self.context, portfolio=self.portfolio
            )

        self.assertEqual(ctx.exception.code, "budget_exhausted")
        self.assertEqual(ctx.exception.budget, "monetary")
        self.assertEqual(len(client.stage_calls("bull_case")), 1)
        usage = ctx.exception.run.protocol_metadata["usage"]
        self.assertGreaterEqual(Decimal(usage["monetary_ceiling_spent_usd"]), Decimal("1.00"))

    def test_a_retry_near_the_deadline_fails_typed_instead_of_overrunning(self):
        """Backoff that would outlive the run deadline is a typed failure."""
        client = StubStageClient(
            standard_handlers(),
            failures={
                "bull_case": [
                    ProviderTransientError("503 temporarily unavailable", stage="analysing")
                ]
            },
        )
        # A 10s backoff cannot fit inside a 1s run deadline. The protocol must not
        # sleep through the deadline and must not start the retry.
        with self.assertRaises(BudgetExhausted) as ctx:
            self._protocol_with_model(
                client,
                run_deadline_seconds=1.0,
                provider_retry_backoff_seconds=10.0,
            ).run(self.context, portfolio=self.portfolio)

        self.assertEqual(ctx.exception.code, "run_deadline_exceeded")
        self.assertEqual(len(client.stage_calls("bull_case")), 1)

    def test_a_call_timeout_is_bounded_by_the_remaining_run_deadline(self):
        """The effective timeout is the smaller of the stage and the deadline."""

        def slow(client, prompt):
            time.sleep(1.2)
            return bull_case(client, prompt)

        client = StubStageClient({**standard_handlers(), "bull_case": slow})
        with self.assertRaises(BudgetExhausted) as ctx:
            self._protocol_with_model(
                client,
                stage_timeout_seconds=30.0,
                run_deadline_seconds=0.5,
            ).run(self.context, portfolio=self.portfolio)

        self.assertEqual(ctx.exception.code, "run_deadline_exceeded")
        self.assertIn("deadline", ctx.exception.detail)
        self.assertEqual(len(client.stage_calls("bull_case")), 1)

    def test_a_timed_out_call_that_can_be_reconciled_is_accounted(self):
        """A timed-out call is attributed when the client's counters moved."""

        class RecoveringClient(StubStageClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.reconciled = 0

            def reconcile_timed_out_call(self, *, stage=""):
                self.reconciled += 1
                return ModelUsage(
                    input_tokens=1200,
                    output_tokens=300,
                    cost_usd=None,
                    reported=True,
                    tokens_measured=True,
                    price_table_cost_usd=None,
                    cost_unknown_reason="provider_reported_cost_unavailable",
                )

        client = RecoveringClient(
            {**standard_handlers(), "bull_case": lambda c, p: time.sleep(0.6)}
        )
        with self.assertRaises(StageTimeout):
            self._protocol_with_model(client, stage_timeout_seconds=0.2).run(
                self.context, portfolio=self.portfolio
            )

        self.assertEqual(client.reconciled, 1)

    def test_a_timed_out_call_that_cannot_be_reconciled_is_labelled(self):
        """An abandoned call whose charge is unknown is never recorded as zero."""

        def slow(client, prompt):
            time.sleep(0.6)
            return bull_case(client, prompt)

        client = StubStageClient({**standard_handlers(), "bull_case": slow})

        def reconcile(*, stage=""):
            return None

        client.reconcile_timed_out_call = reconcile
        with self.assertRaises(StageTimeout) as ctx:
            self._protocol_with_model(client, stage_timeout_seconds=0.2).run(
                self.context, portfolio=self.portfolio
            )

        usage = ctx.exception.run.protocol_metadata["usage"]
        self.assertEqual(usage["timed_out_calls"], 1)
        self.assertEqual(usage["unreconciled_timeout_calls"], 1)
        self.assertIsNone(usage["billed_cost_usd"])
        self.assertTrue(usage["unreconciled_charge_note"])
        kinds = [item["kind"] for item in ctx.exception.run.protocol_metadata["attempts"]]
        self.assertIn("timeout_unreconciled", kinds)

    def test_unknown_model_pricing_is_null_not_a_guessed_cost(self):
        """An unpriced model yields an unknown price-table quantity."""
        client = StubStageClient(standard_handlers(), cost=None)
        outcome = self._protocol_with_model(client, model="definitely-not-a-priced-model").run(
            self.context, portfolio=self.portfolio
        )

        usage = outcome.run.protocol_metadata["usage"]
        self.assertIsNone(usage["price_table_cost_usd"])
        self.assertTrue(usage["price_table_cost_unknown_calls"])
        self.assertEqual(usage["price_table_cost_unknown_reason"], "model_not_in_price_table")
        self.assertIsNone(usage["billed_cost_usd"])

    def test_billed_measured_and_estimated_costs_are_never_interchanged(self):
        """A provider that reports a charge produces a billed figure, not a table one."""
        client = StubStageClient(standard_handlers(), cost=Decimal("0.25"))
        outcome = self._protocol_with_model(client).run(self.context, portfolio=self.portfolio)

        usage = outcome.run.protocol_metadata["usage"]
        self.assertTrue(usage["cost_reported"])
        self.assertEqual(usage["billed_cost_usd"], "1.250000")
        self.assertEqual(usage["measured_tokens"]["input"] > 0, True)
        self.assertEqual(usage["timed_out_calls"], 0)
        self.assertEqual(
            Decimal(str(outcome.run.total_cost_usd)), Decimal(usage["billed_cost_usd"])
        )

    def test_the_run_metadata_does_not_promise_a_strict_billed_ceiling(self):
        client = StubStageClient(standard_handlers())
        outcome = self._protocol_with_model(client).run(self.context, portfolio=self.portfolio)

        cost = outcome.run.protocol_metadata["cost_accounting"]
        self.assertFalse(cost["strict_billed_ceiling"])
        self.assertFalse(cost["monetary_ceiling"]["strict_billed_ceiling"])
        self.assertTrue(cost["monetary_ceiling"]["late_charges_possible"])
        self.assertIn("not", cost["monetary_ceiling"]["note"])
        self.assertTrue(
            outcome.run.protocol_metadata["protocol"]["cost_ceiling"][
                "checked_before_every_attempt"
            ]
        )
