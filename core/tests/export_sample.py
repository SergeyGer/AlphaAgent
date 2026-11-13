"""Build the feature-complete run used to pin the export contract.

The committed sample `docs/spec/fixtures/decision-run-export.v1.json` is an
export the authenticated endpoint produced from this run. The run is
**authored fixture data** (`mode="synthetic"`, `source="fixture"`): its claims,
rebuttal, guard results and provider-shaped fields were written for this test.
The export's `run.origin` block says so, and the viewer labels it Synthetic.
That is deliberately different from a provider-backed run record, which the
`provider_backed=True` branch exercises for the Step 11 recording path.

The Python test compares a fresh export's *shape* against the committed sample,
and the viewer's Vitest suite imports the same sample unchanged. That pairing is
the cross-language contract: the browser test proves the shape is importable, and
the Django test proves the endpoint still emits that shape.

`scripts/build_export_sample.py` calls this builder and writes the fixture, so
regenerating a drifted sample is one command rather than a manual capture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.decision_models import (
    ClaimStance,
    ClaimType,
    DecisionRecord,
    GuardRecord,
    OutcomeMaturity,
    OutcomeRecord,
    RunEventType,
    RunStage,
)
from services.decision_runs import append_event, create_run, transition_status
from services.evidence import create_claim, create_evidence_item, create_rebuttal, create_snapshot

D = Decimal

# Fixed instants: the sample is a recorded event, not a clock reading.
CAPTURED_AT = datetime(2026, 11, 12, 13, 58, tzinfo=UTC)
CUTOFF = datetime(2026, 11, 12, 13, 59, 30, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 12, 14, 0, 30, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 11, 12, 14, 3, 30, tzinfo=UTC)


def _event_time(seconds: int) -> datetime:
    return STARTED_AT + timedelta(seconds=seconds)


def create_export_sample_run(
    portfolio,
    *,
    fixture_id: str = "export-contract-aapl-2026-11-12",
    provider_backed: bool = False,
):
    """Create one run carrying every field the export endpoint emits.

    The default is an **authored fixture**: ``mode="synthetic"``,
    ``source="fixture"`` and ``protocol_version="synthetic.v1"``. Its claims,
    rebuttal, guard results and provider-shaped fields are authored test data,
    so the export must never present it as a provider debate.

    ``provider_backed=True`` builds the other side of the comparison: a run
    record with ``mode="live"``, ``source="crewai"`` and a provider/model/usage
    record. It is still test data at the file level (``test_record`` says so),
    but the provenance path it exercises is the one Step 11's recording will use.
    """
    if provider_backed:
        mode, source, protocol_version = "live", "crewai", "debate.v1"
        fixture_id = ""
        protocol_name = "debate.v1"
        prompt_template_version = "debate.prompt.v3"
        provider, model = "anthropic", "claude-haiku-5-5"
    else:
        mode, source, protocol_version = "synthetic", "fixture", "synthetic.v1"
        protocol_name = "synthetic.v1"
        prompt_template_version = "synthetic.export-contract.v1"
        # Provider-shaped fields stay in the sample so the export contract can
        # be exercised, but they are declared authored values, not a call.
        provider, model = "fixture", "authored_fixture"

    snapshot = create_snapshot(
        portfolio=portfolio,
        portfolio_version="portfolio-v7",
        analysis_cutoff=CUTOFF,
        captured_at=CAPTURED_AT,
        holdings=[
            {
                "ticker": "AAPL",
                "quantity": "10.00000000",
                "avg_cost": "90.0",
                "price": "90.0",
                "price_source": "provided_quote",
            },
            {
                "ticker": "MSFT",
                "quantity": "2.50000000",
                "avg_cost": "300.0",
                "price_source": "avg_cost_fallback",
            },
        ],
        cash={"balance_usd": "1000.0", "currency": "USD"},
        quotes={
            "AAPL": {"price_usd": "90.0", "currency": "USD"},
            "MSFT": {"price_usd": "300.0", "currency": "USD"},
        },
    )
    run = create_run(
        portfolio=portfolio,
        ticker="AAPL",
        protocol_version=protocol_version,
        mode=mode,
        source=source,
        snapshot=snapshot,
        started_at=STARTED_AT,
        fixture_id=fixture_id,
    )
    run.protocol_metadata = {
        "protocol": {
            "name": protocol_name,
            "prompt_template_version": prompt_template_version,
            "provider": provider,
            "model": model,
            "parameters": {
                "temperature": 0.0,
                "top_p": 0.95,
                "max_tokens": 300,
                "request_timeout_seconds": 90.0,
                "note": "Unicode in a parameter note: ünïcode 你好",
            },
            "tool_versions": {"market_data": "1.4.0"},
            "rounds": 1,
        },
        "limits": {"run_deadline_seconds": 180, "monetary_budget_usd": "0.25"},
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "cost_usd": None,
            "cost_reported": False,
        },
        "counts": {"claims": 2, "rebuttals": 1},
        "analyst_consensus": "split",
        "deterministic_replay": {
            "snapshot_hash": snapshot.content_hash,
            "note": "Events replay deterministically; regenerating model output does not.",
        },
    }
    if provider_backed:
        # Keep the provider-backed path honest about being test data: the
        # record shape is real, the provider call for this file is not.
        run.protocol_metadata["test_record"] = True
    run.save(update_fields=["protocol_metadata"])

    transition_status(run, "collecting", occurred_at=_event_time(5))
    transition_status(run, "analysing", occurred_at=_event_time(10))
    append_event(
        run,
        RunEventType.STAGE_STARTED,
        stage=RunStage.ANALYSING,
        occurred_at=_event_time(10),
        payload={
            "temperature": 0.0,
            "limit": 300.0,
            "unicode": "ünïcode 你好",
            "settings": {
                "max_position_pct": 90.0,
                "timeout_seconds": 300.0,
                "slippage": 0.0025,
                "model": model,
            },
        },
    )

    bull_evidence = create_evidence_item(
        snapshot=snapshot,
        source_type="news_article",
        source_uri="fixture://export-contract/bull",
        publisher="Recorded Wire",
        retrieved_at=CUTOFF - timedelta(seconds=30),
        published_at=CUTOFF - timedelta(minutes=2),
        excerpt="Unicode evidence — 你好: services demand grew 12.5% and 90.0% of it was recurring.",
        is_redistributable=True,
    )
    bear_evidence = create_evidence_item(
        snapshot=snapshot,
        source_type="fundamentals",
        source_uri="fixture://export-contract/bear",
        publisher="Recorded Wire",
        retrieved_at=CUTOFF - timedelta(seconds=20),
        published_at=None,
        excerpt="A non-redistributable body that must not leave the server.",
        is_redistributable=False,
    )
    for item, seconds in ((bull_evidence, 14), (bear_evidence, 15)):
        append_event(
            run,
            RunEventType.EVIDENCE_RECORDED,
            stage=RunStage.COLLECTING,
            agent_role="system",
            occurred_at=_event_time(seconds),
            payload={
                "evidence_id": str(item.id),
                "source_type": item.source_type,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "available_at": item.available_at.isoformat() if item.available_at else None,
            },
        )
    bull_claim = create_claim(
        run=run,
        agent_role="bull",
        stance=ClaimStance.BULL,
        statement="Recurring services demand grew 12.5% — ünïcode 你好.",
        claim_type=ClaimType.OBSERVED_FACT,
        evidence_ids=[bull_evidence.id],
        horizon="2 quarters",
        limitations="A single quarter is not a trend.",
        created_at=_event_time(16),
    )
    bear_claim = create_claim(
        run=run,
        agent_role="bear",
        stance=ClaimStance.BEAR,
        statement="The multiple assumes 300.0 basis points of margin expansion.",
        claim_type=ClaimType.INTERPRETATION,
        evidence_ids=[bear_evidence.id],
        created_at=_event_time(18),
    )
    for claim in (bull_claim, bear_claim):
        append_event(
            run,
            RunEventType.CLAIM_CREATED,
            stage=RunStage.ANALYSING,
            agent_role=claim.agent_role,
            occurred_at=claim.created_at,
            payload={
                "claim_id": str(claim.claim_id),
                "stance": claim.stance,
                "claim_type": claim.claim_type,
                "evidence_ids": [str(item.id) for item in claim.evidence.all()],
            },
        )

    transition_status(run, "rebutting", occurred_at=_event_time(40))
    rebuttal = create_rebuttal(
        run=run,
        responding_agent="bear",
        target_claim=bull_claim,
        response_type="qualify",
        explanation="The growth is real but concentrated in one product line.",
        evidence_ids=[bear_evidence.id],
        position_change="No position change; the objection is about durability.",
        created_at=_event_time(45),
    )
    append_event(
        run,
        RunEventType.REBUTTAL_CREATED,
        stage=RunStage.REBUTTING,
        agent_role=rebuttal.responding_agent,
        occurred_at=_event_time(45),
        payload={
            "rebuttal_id": str(rebuttal.id),
            "target_claim_id": str(bull_claim.claim_id),
            "response_type": rebuttal.response_type,
        },
    )

    transition_status(run, "adjudicating", occurred_at=_event_time(60))
    decision = DecisionRecord.objects.create(
        run=run,
        action="BUY",
        requested_quantity=D("4.0"),
        concise_rationale="Résumé — 你好: buy four shares, sized by the guard.",
        decisive_claim_ids=[str(bull_claim.claim_id)],
        strongest_objection="The margin expansion may not persist.",
        thesis="Services mix improves through 2027.",
        invalidation_condition="Recurring growth below 5% for two quarters.",
        engine_source="crewai",
        created_at=_event_time(65),
    )
    append_event(
        run,
        RunEventType.DECISION_PROPOSED,
        stage=RunStage.ADJUDICATING,
        agent_role="cio",
        occurred_at=_event_time(65),
        payload={
            "decision_id": str(decision.id),
            "action": "BUY",
            "requested_quantity": "4.0",
            "decisive_claim_ids": [str(bull_claim.claim_id)],
            "unicode": "décision 你好",
        },
    )
    transition_status(run, "validating", occurred_at=_event_time(75))

    preflight = GuardRecord.objects.create(
        proposal=decision,
        policy_version="guard.v1",
        evaluated_portfolio_version="portfolio-v7",
        quote_time=_event_time(76),
        quote_source="recorded_quote",
        quote_retrieved_at=_event_time(76),
        status="approved",
        requested_quantity=D("10.0"),
        allowed_quantity=D("10.0"),
        binding_rule="max_trade_allocation_pct",
        reason_codes=[],
        evaluation_key=f"export-contract:{run.id}:guard:preflight",
        numerical_checks={
            "requested_notional_usd": "900.0",
            "threshold_pct": 90.0,
            "max_notional_usd": 300.0,
            "checks": [
                {
                    "rule_code": "max_trade_allocation_pct",
                    "outcome": "pass",
                    "threshold": "1000.0",
                    "actual": "900.0",
                    "unit": "USD",
                },
                {
                    "rule_code": "daily_loss_limit_usd",
                    "outcome": "not_evaluated",
                    "threshold": "500.0",
                    "actual": None,
                    "unit": "USD",
                },
            ],
        },
        created_at=_event_time(76),
    )
    final = GuardRecord.objects.create(
        proposal=decision,
        policy_version="guard.v1",
        evaluated_portfolio_version="portfolio-v8",
        quote_time=_event_time(80),
        quote_source="recorded_quote",
        quote_retrieved_at=_event_time(81),
        status="clamped",
        requested_quantity=D("10.0"),
        allowed_quantity=D("4.0"),
        binding_rule="max_trade_allocation_pct",
        reason_codes=["quantity_clamped_to_budget"],
        evaluation_key=f"export-contract:{run.id}:guard:final",
        numerical_checks={
            "requested_notional_usd": "900.0",
            "allowed_notional_usd": "360.0",
            "budget_usd": 400.0,
            "ratio": 0.4,
            "checks": [
                {
                    "rule_code": "max_trade_allocation_pct",
                    "outcome": "fail",
                    "threshold": "400.0",
                    "actual": "900.0",
                    "unit": "USD",
                },
                {
                    "rule_code": "max_portfolio_exposure_pct",
                    "outcome": "pass",
                    "threshold": "90.0",
                    "actual": "54.5",
                    "unit": "pct",
                },
            ],
        },
        created_at=_event_time(80),
    )
    for guard in (preflight, final):
        append_event(
            run,
            RunEventType.GUARD_EVALUATED,
            stage=RunStage.VALIDATING,
            occurred_at=guard.created_at,
            payload={
                "guard_id": str(guard.id),
                "status": guard.status,
                "requested_quantity": str(guard.requested_quantity),
                "allowed_quantity": str(guard.allowed_quantity),
                "binding_rule": guard.binding_rule,
                "quote_time": guard.created_at.isoformat(),
            },
            dedupe_key=f"guard_evaluated:{guard.id}",
        )

    append_event(
        run,
        RunEventType.LIMITATION_RECORDED,
        stage=RunStage.VALIDATING,
        occurred_at=_event_time(90),
        payload={
            "limitation": "The benchmark interval is verified at session-date precision only.",
            "unicode_note": "Ünïcode limitation 你好",
        },
    )
    transition_status(run, "completed", occurred_at=COMPLETED_AT)

    OutcomeRecord.objects.create(
        decision=decision,
        horizon_days=1,
        method_version="attribution.v2",
        maturity=OutcomeMaturity.UNAVAILABLE,
        unavailable_reason="No first-session mark was available for AAPL.",
        created_at=_event_time(100),
    )
    OutcomeRecord.objects.create(
        decision=decision,
        horizon_days=7,
        method_version="attribution.v1",
        maturity=OutcomeMaturity.EVALUATED,
        price_at_decision=D("90.0"),
        price_at_horizon=D("88.2"),
        forward_return_pct=D("-2.0000"),
        decision_return_pct=D("-2.0000"),
        benchmark_ticker="SPY",
        benchmark_return_pct=D("-0.4000"),
        signal_diagnostics={
            "signal_score": "-2.0000",
            "measurement_basis": {
                "basis": "proposal_close",
                "horizon_policy": "first_session_at_or_after_target",
                "reference_at": _event_time(65).isoformat(),
            },
            "note": "Superseded revision; kept as provenance.",
        },
        measurement_basis="proposal_close",
        revision=1,
        evaluated_at=_event_time(110),
        created_at=_event_time(110),
    )
    OutcomeRecord.objects.create(
        decision=decision,
        horizon_days=7,
        method_version="attribution.v2",
        maturity=OutcomeMaturity.EVALUATED,
        price_at_decision=D("90.0"),
        price_at_horizon=D("88.2"),
        price_observed_at_decision=_event_time(65),
        price_observed_at=_event_time(66),
        price_source="recorded_close",
        forward_return_pct=D("-2.0000"),
        decision_return_pct=D("-2.0000"),
        benchmark_ticker="SPY",
        benchmark_return_pct=D("-0.4000"),
        benchmark_observed_at_decision=_event_time(65),
        benchmark_price_observed_at=_event_time(66),
        benchmark_price_source="recorded_close",
        signal_diagnostics={
            "signal_score": "-2.0000",
            "measurement_basis": {
                "basis": "proposal_close",
                "horizon_policy": "first_session_at_or_after_target",
                "reference_at": _event_time(65).isoformat(),
            },
            "benchmark_interval_verified": True,
        },
        measurement_basis="proposal_close",
        revision=2,
        evaluated_at=_event_time(115),
        created_at=_event_time(115),
    )
    OutcomeRecord.objects.create(
        decision=decision,
        horizon_days=30,
        method_version="attribution.v2",
        maturity=OutcomeMaturity.PENDING,
        benchmark_ticker="SPY",
        created_at=_event_time(120),
    )
    return run


__all__ = [
    "CAPTURED_AT",
    "COMPLETED_AT",
    "CUTOFF",
    "STARTED_AT",
    "create_export_sample_run",
]
