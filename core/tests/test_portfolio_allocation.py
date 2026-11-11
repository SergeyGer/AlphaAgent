"""Phase 4 tests: portfolio allocation, constraints and plan execution.

The gate is concurrency and constraint behaviour, so the tests are organised
around the four acceptance scenarios and the four concurrency requirements,
with brief/contract unit tests underneath them. Every test builds a real
snapshot and runs the same service path the paper command uses; none calls a
model or the network.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from core.allocation_models import PlanLegStatus, PlanStatus, PortfolioPlan, PortfolioPlanLeg
from core.decision_json import decimal_string
from core.models import Asset, InstrumentClassification, Portfolio, Transaction
from core.tests.helpers import TEST_CACHES, make_user
from services.portfolio_allocation import (
    PlanLegFailed,
    PlanStaleError,
    PortfolioCIO,
    StaticAllocationClient,
    _cost_reserve_fraction,
    apply_plan,
    build_hold_proposal,
    create_plan,
    revalidate_plan,
    run_allocation,
)
from services.portfolio_brief import (
    CandidateCase,
    CandidateClaim,
    build_allocation_brief,
    build_portfolio_state,
    capture_allocation_snapshot,
)
from services.portfolio_policy import AllocationPolicy, CostModel
from services.portfolio_targets import (
    ALLOWED_FUNDING_SOURCES,
    WEIGHT_TOLERANCE,
    AllocationContractError,
    ProposalStaleError,
    constrain_targets,
    derive_legs,
    validate_target_proposal,
)

D = Decimal
CENT = D("0.01")
ZERO = D("0")
QUOTES = {"AAPL": D("100"), "MSFT": D("320"), "JPM": D("160"), "XYZ": D("90")}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_allocation_portfolio(
    *,
    cash: str = "10000.00",
    holdings: dict[str, tuple[str, str]] | None = None,
    allocation_pct: str = "99.99",
    exposure_pct: str = "90.00",
    name_pct: str = "30.00",
    sector_pct: str = "40.00",
    turnover_pct: str = "100.00",
    cash_buffer_pct: str = "5.00",
) -> Portfolio:
    portfolio = Portfolio.objects.create(
        user=make_user(),
        balance_usd=D(cash),
        risk_profile="medium",
        max_trade_allocation_pct=D(allocation_pct),
        max_portfolio_exposure_pct=D(exposure_pct),
        max_ticker_concentration_pct=D(name_pct),
        max_sector_concentration_pct=D(sector_pct),
        max_turnover_pct=D(turnover_pct),
        min_cash_buffer_pct=D(cash_buffer_pct),
        daily_loss_limit_usd=D("500"),
    )
    for ticker, (amount, avg_cost) in (holdings or {}).items():
        Asset.objects.create(
            portfolio=portfolio,
            ticker=ticker,
            amount=D(amount),
            avg_purchase_price=D(avg_cost),
        )
    return portfolio


def classify(portfolio: Portfolio, mapping: dict[str, str], asset_class: str = "equity") -> None:
    for ticker, sector in mapping.items():
        InstrumentClassification.objects.update_or_create(
            portfolio=portfolio,
            ticker=ticker,
            defaults={
                "sector": sector,
                "asset_class": asset_class,
                "source": "fixture",
                "source_detail": "test",
            },
        )


def price_history(tickers: list[str], *, days: int = 40) -> dict:
    today = timezone.now().date()
    series = {}
    for offset, ticker in enumerate(tickers):
        points = []
        for index in range(days):
            day = today - timedelta(days=days - 1 - index)
            price = D("100") + D(index) * D("0.5") + D(offset) * D("2")
            points.append((day, price))
        series[ticker] = points
    return series


def candidate(
    ticker: str,
    *,
    claim_id: str | None = None,
    evidence: bool = True,
    action: str = "BUY",
) -> CandidateCase:
    claims = []
    if claim_id:
        claims.append(
            CandidateClaim(
                claim_id=claim_id,
                agent_role="bull",
                stance="bull",
                claim_type="forecast",
                statement=f"{ticker} has a favourable case.",
                evidence_keys=[f"fixture://{ticker}"] if evidence else [],
            )
        )
    return CandidateCase(
        ticker=ticker,
        action=action,
        rationale=f"{ticker} is a favourable candidate.",
        strongest_objection="The evidence is limited.",
        thesis=f"{ticker} thesis.",
        invalidation_condition="The thesis breaks.",
        run_id=f"run-{ticker.lower()}",
        mode="fixture",
        decided_at=timezone.now().isoformat(),
        claims=claims,
    )


def build_brief(
    portfolio: Portfolio,
    *,
    quotes: dict | None = None,
    candidates: list[CandidateCase] | None = None,
    classifications: dict[str, str] | None = None,
    history: dict | None = None,
    policy: AllocationPolicy | None = None,
    as_of=None,
    evidence_sufficient: bool | None = None,
):

    quotes = dict(QUOTES if quotes is None else quotes)
    candidates = (
        [
            candidate("MSFT", claim_id="claim-msft"),
            candidate("JPM", claim_id="claim-jpm"),
            candidate("XYZ", claim_id="claim-xyz"),
        ]
        if candidates is None
        else candidates
    )
    if classifications is None:
        classifications = {
            "AAPL": "technology",
            "MSFT": "technology",
            "JPM": "financials",
            "XYZ": "healthcare",
        }
    if policy is None:
        policy = AllocationPolicy.from_portfolio(portfolio)
    if classifications:
        # The explicit lookup is the only sector source. The test passes it in
        # the same shape the production builder reads from the table.
        classify(portfolio, classifications)
    snapshot = capture_allocation_snapshot(portfolio, quotes=quotes, as_of=as_of)
    return build_allocation_brief(
        portfolio,
        snapshot,
        quotes=quotes,
        price_history=history if history is not None else price_history(list(quotes)),
        candidates=candidates,
        classifications={
            ticker: {
                "sector": sector,
                "asset_class": "equity",
                "source": "fixture",
            }
            for ticker, sector in classifications.items()
        },
        policy=policy,
        as_of=as_of,
        evidence_sufficient=evidence_sufficient,
        history_source="test_fixture",
        candidate_source="test_fixture",
    )


def actions_for(brief, weights: dict[str, Decimal], claim_ids: dict[str, list[str]] | None = None):
    claim_ids = claim_ids or {}
    actions = []
    for ticker, weight in sorted(weights.items()):
        if ticker == "CASH":
            actions.append(
                {
                    "ticker": "CASH",
                    "action": "maintain_cash",
                    "rationale": "Cash is a deliberate allocation.",
                    "claim_ids": [],
                }
            )
            continue
        current = brief.state.weight(ticker)
        target = D(str(weight))
        if target > current + WEIGHT_TOLERANCE:
            action = "increase"
        elif target < current - WEIGHT_TOLERANCE:
            action = "exit" if target == ZERO else "reduce"
        else:
            action = "maintain"
        actions.append(
            {
                "ticker": ticker,
                "action": action,
                "rationale": f"{action.title()} {ticker}.",
                "claim_ids": claim_ids.get(ticker, []),
            }
        )
    return actions


def payload_for(
    brief,
    weights: dict[str, Decimal],
    *,
    claim_ids: dict[str, list[str]] | None = None,
    funding_source: str = "cash_and_sales",
    **overrides,
):
    payload = {
        "schema_version": "portfolio_targets.v1",
        "portfolio_version": brief.portfolio_version,
        "objective": "Allocate under the stated constraints.",
        "target_weights": {
            ticker: decimal_string(D(str(weight))) for ticker, weight in weights.items()
        },
        "actions": actions_for(brief, weights, claim_ids),
        "funding_source": funding_source,
        "rationale": "A coherent allocation linked to the candidate claims.",
        "rejected_alternatives": [],
        "unresolved_uncertainties": ["No expected return was supplied."],
        "data_sufficiency": "sufficient",
    }
    payload.update(overrides)
    return payload


def constrain(brief, proposal, *, policy=None, cost_model=None):
    policy = policy or AllocationPolicy.from_portfolio(brief_policy_portfolio(brief))
    cost_model = cost_model or CostModel.from_config()
    sectors = {
        ticker: info["sector"]
        for ticker, info in brief.classifications.items()
        if info.get("sector")
    }
    reserve = _cost_reserve_fraction(
        proposal.target_weights, brief.state, policy=policy, cost_model=cost_model
    )
    constrained = constrain_targets(
        proposal.target_weights,
        brief.state,
        sectors=sectors,
        policy=policy,
        data_quality=brief.data_quality,
        prices=brief.state.prices,
        cost_reserve_fraction=reserve,
    )
    sized = derive_legs(
        constrained,
        brief.state,
        policy=policy,
        cost_model=cost_model,
        sectors=sectors,
        quotes=brief.state.prices,
    )
    return constrained, sized


def brief_policy_portfolio(brief):
    """A portfolio-shaped object for the policy limits recorded in a brief."""
    limits = brief.policy["limits"]

    class _Carrier:
        max_ticker_concentration_pct = D(limits["max_name_weight"]) * 100
        max_sector_concentration_pct = D(limits["max_sector_weight"]) * 100
        max_portfolio_exposure_pct = D(limits["max_total_exposure"]) * 100
        max_turnover_pct = D(limits["max_turnover"]) * 100
        min_cash_buffer_pct = D(limits["min_cash_weight"]) * 100
        max_trade_allocation_pct = D(limits["max_trade_fraction_of_cash"]) * 100

    return _Carrier()


@override_settings(CACHES=TEST_CACHES)
class AllocationFixture(TestCase):
    """A portfolio and brief used by many contract tests."""

    def setUp(self):
        self.portfolio = make_allocation_portfolio(
            cash="10000.00", holdings={"AAPL": ("100", "90")}
        )
        self.brief = build_brief(self.portfolio)
        self.claims = {"MSFT": ["claim-msft"], "JPM": ["claim-jpm"], "XYZ": ["claim-xyz"]}


# ---------------------------------------------------------------------------
# Acceptance scenario 1 - a favourable candidate declines because of sector
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class SectorAcceptanceTests(AllocationFixture):
    def test_a_favourable_candidate_is_declined_because_its_sector_is_already_over_the_limit(self):
        weights = {
            "AAPL": D("0.20"),
            "MSFT": D("0.25"),
            "JPM": D("0.15"),
            "XYZ": D("0.05"),
            "CASH": D("0.35"),
        }
        proposal = validate_target_proposal(
            payload_for(self.brief, weights, claim_ids=self.claims), self.brief
        )
        constrained, sized = constrain(self.brief, proposal)

        self.assertEqual(constrained.weights["MSFT"], ZERO)
        self.assertIn(
            "max_sector_weight_new_risk",
            [deviation["code"] for deviation in constrained.deviations],
        )
        # The rationale (the action) and the deterministic limit are both on
        # the record: the model asked for MSFT, the plan applied zero.
        plan, created = create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=proposal,
            submission_key="sector-decline",
        )
        self.assertTrue(created)
        self.assertEqual(plan.target_weights["MSFT"], "0")
        self.assertEqual(plan.target_weights["CASH"], "0.600000")
        action = next(row for row in plan.actions if row["ticker"] == "MSFT")
        self.assertEqual(action["action"], "increase")
        self.assertEqual(action["applied_target_weight"], "0")
        self.assertTrue(action["rationale"])
        self.assertIn("0.40", plan.deviations[0]["detail"])
        self.assertEqual(plan.constraints["checks"]["max_sector_weight"]["status"], "pass")
        self.assertFalse(any(leg.ticker == "MSFT" for leg in plan.legs.all()))
        # The plan still allocates coherently to the other candidates.
        self.assertEqual([leg.ticker for leg in plan.legs.all()], ["AAPL", "JPM", "XYZ"])
        self.assertEqual(sized.post_trade["status"], "within_limits")

    def test_sector_exposure_is_reported_as_incomplete_not_zero_when_unclassified(self):
        portfolio = make_allocation_portfolio(cash="10000.00", holdings={"AAPL": ("100", "90")})
        brief = build_brief(portfolio, classifications={})
        exposure = brief.as_dict()["exposure"]
        self.assertEqual(exposure["coverage"]["status"], "incomplete")
        self.assertEqual(exposure["sector_exposure"], [])
        self.assertEqual(exposure["unclassified"]["value_usd"], "10000.00")
        self.assertIn("incomplete", exposure["note"])
        self.assertNotEqual(exposure["coverage"]["unclassified_value_usd"], "0.00")


# ---------------------------------------------------------------------------
# Acceptance scenario 2 - two candidates compete for finite cash
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class CashCompetitionAcceptanceTests(TestCase):
    def test_two_attractive_candidates_share_the_limited_cash_in_one_plan(self):
        portfolio = make_allocation_portfolio(
            cash="500.00",
            holdings={},
            name_pct="100.00",
            exposure_pct="90.00",
            cash_buffer_pct="10.00",
        )
        classify(portfolio, {"MSFT": "technology", "JPM": "financials"})
        claims = [
            CandidateClaim(
                claim_id="claim-msft",
                agent_role="bull",
                stance="bull",
                claim_type="forecast",
                statement="MSFT case.",
                evidence_keys=["fixture://msft"],
            ),
            CandidateClaim(
                claim_id="claim-jpm",
                agent_role="bull",
                stance="bull",
                claim_type="forecast",
                statement="JPM case.",
                evidence_keys=["fixture://jpm"],
            ),
        ]
        candidates = [
            CandidateCase(
                ticker="MSFT",
                action="BUY",
                rationale="strong",
                claims=[claims[0]],
                run_id="r-msft",
                mode="fixture",
            ),
            CandidateCase(
                ticker="JPM",
                action="BUY",
                rationale="strong",
                claims=[claims[1]],
                run_id="r-jpm",
                mode="fixture",
            ),
        ]
        quotes = {"MSFT": D("100"), "JPM": D("100")}
        snapshot = capture_allocation_snapshot(portfolio, quotes=quotes)
        brief = build_allocation_brief(
            portfolio,
            snapshot,
            quotes=quotes,
            price_history=price_history(["MSFT", "JPM"], days=30),
            candidates=candidates,
            classifications={
                "MSFT": {"sector": "technology", "asset_class": "equity", "source": "fixture"},
                "JPM": {"sector": "financials", "asset_class": "equity", "source": "fixture"},
            },
            evidence_sufficient=True,
        )
        weights = {"MSFT": D("0.45"), "JPM": D("0.45"), "CASH": D("0.10")}
        proposal = validate_target_proposal(
            payload_for(brief, weights, claim_ids={"MSFT": ["claim-msft"], "JPM": ["claim-jpm"]}),
            brief,
        )
        plan, created = create_plan(
            portfolio, brief=brief, proposal=proposal, submission_key="cash-competition"
        )
        self.assertTrue(created)
        plan_weights_sum = sum(D(value) for value in plan.target_weights.values())
        self.assertEqual(plan_weights_sum, D("1.000000"))
        self.assertLessEqual(
            sum(D(value) for ticker, value in plan.target_weights.items() if ticker != "CASH"),
            D("0.90"),
        )
        legs = list(plan.legs.all())
        self.assertEqual(len(legs), 2)
        self.assertEqual({leg.ticker for leg in legs}, {"MSFT", "JPM"})
        total_buys = sum((leg.notional_usd for leg in legs), ZERO)
        self.assertLessEqual(total_buys, D("450.00"))
        self.assertTrue(all(leg.notional_usd > ZERO for leg in legs))
        # One plan, not two independent ticker tasks: the same cash is not spent twice.
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 0)
        self.assertEqual(PlanStatus.PROPOSED, plan.status)


# ---------------------------------------------------------------------------
# Acceptance scenario 3 - stale snapshot / missing correlation / weak evidence
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class DataQualityAcceptanceTests(TestCase):
    def _portfolio(self):
        # 25% AAPL / 75% cash: inside the 30% name and 40% sector ceilings, so a
        # policy test observes the policy, not an existing limit breach.
        portfolio = make_allocation_portfolio(cash="15000.00", holdings={"AAPL": ("50", "90")})
        classify(portfolio, {"AAPL": "technology", "MSFT": "technology"})
        return portfolio

    def test_a_stale_snapshot_is_labelled_and_policy_blocks_new_risk(self):
        portfolio = self._portfolio()
        # A directly controlled stale snapshot.
        stale_snapshot = capture_allocation_snapshot(
            portfolio,
            quotes={"AAPL": D("100"), "MSFT": D("100")},
            as_of=timezone.now() - timedelta(hours=2),
        )
        stale_brief = build_allocation_brief(
            portfolio,
            stale_snapshot,
            quotes={"AAPL": D("100"), "MSFT": D("100")},
            price_history=price_history(["AAPL", "MSFT"]),
            candidates=[candidate("MSFT", claim_id="claim-msft")],
            classifications={
                "AAPL": {"sector": "technology", "asset_class": "equity", "source": "fixture"},
                "MSFT": {"sector": "technology", "asset_class": "equity", "source": "fixture"},
            },
            evidence_sufficient=True,
        )
        self.assertTrue(stale_brief.data_quality["snapshot_stale"])
        self.assertIn("snapshot_stale", stale_brief.data_quality["flag_codes"])
        self.assertEqual(stale_brief.data_quality["policy_actions"][0]["action"], "block_new_risk")
        weights = {"AAPL": D("0.25"), "MSFT": D("0.25"), "CASH": D("0.50")}
        proposal = validate_target_proposal(
            payload_for(stale_brief, weights, claim_ids={"MSFT": ["claim-msft"]}),
            stale_brief,
        )
        constrained, _sized = constrain(stale_brief, proposal)
        self.assertEqual(constrained.weights["MSFT"], ZERO)
        self.assertEqual(constrained.weights["AAPL"], D("0.25"))
        self.assertEqual(constrained.weights["CASH"], D("0.75"))
        self.assertIn(
            "policy:snapshot_stale",
            [deviation["code"] for deviation in constrained.deviations],
        )

    def test_missing_correlation_history_is_labelled_and_policy_can_retain_cash(self):
        portfolio = self._portfolio()
        policy = AllocationPolicy.from_portfolio(
            portfolio, overrides={"missing_correlation_action": "retain_cash"}
        )
        snapshot = capture_allocation_snapshot(
            portfolio, quotes={"AAPL": D("100"), "MSFT": D("100")}
        )
        brief = build_allocation_brief(
            portfolio,
            snapshot,
            quotes={"AAPL": D("100"), "MSFT": D("100")},
            price_history=None,
            candidates=[candidate("MSFT", claim_id="claim-msft")],
            classifications={
                "AAPL": {"sector": "technology", "asset_class": "equity", "source": "fixture"},
                "MSFT": {"sector": "technology", "asset_class": "equity", "source": "fixture"},
            },
            policy=policy,
            evidence_sufficient=True,
        )
        self.assertTrue(brief.data_quality["missing_correlation_history"])
        self.assertEqual(brief.risk_statistics["status"], "not_available")
        self.assertIn("missing_correlation_history", brief.data_quality["flag_codes"])
        self.assertEqual(brief.data_quality["policy_actions"][0]["action"], "retain_cash")
        weights = {"AAPL": D("0.25"), "MSFT": D("0.25"), "CASH": D("0.50")}
        proposal = validate_target_proposal(
            payload_for(brief, weights, claim_ids={"MSFT": ["claim-msft"]}), brief
        )
        constrained, _sized = constrain(brief, proposal, policy=policy)
        self.assertEqual(constrained.weights["CASH"], D("0.75"))
        self.assertEqual(constrained.weights["MSFT"], ZERO)
        self.assertIn(
            "policy:missing_correlation_history",
            [deviation["code"] for deviation in constrained.deviations],
        )

    def test_insufficient_evidence_is_labelled_and_policy_retains_cash(self):
        portfolio = self._portfolio()
        weak = candidate("MSFT", claim_id="claim-msft", evidence=False)
        brief = build_brief(
            portfolio,
            quotes={"AAPL": D("100"), "MSFT": D("100")},
            candidates=[weak],
            classifications={},
        )
        self.assertTrue(brief.data_quality["insufficient_evidence"])
        self.assertIn("insufficient_evidence", brief.data_quality["flag_codes"])
        self.assertIn(
            "insufficient_evidence",
            [entry["flag"] for entry in brief.data_quality["policy_actions"]],
        )
        weights = {"AAPL": D("0.25"), "MSFT": D("0.25"), "CASH": D("0.50")}
        proposal_payload = payload_for(brief, weights, claim_ids={"MSFT": ["claim-msft"]})
        # The increase is still a valid request; the policy, not the validator,
        # decides that the cash is retained.
        proposal = validate_target_proposal(proposal_payload, brief)
        constrained, _sized = constrain(brief, proposal)
        self.assertEqual(constrained.weights["CASH"], D("0.75"))
        self.assertEqual(constrained.weights["MSFT"], ZERO)
        self.assertIn(
            "policy:insufficient_evidence",
            [deviation["code"] for deviation in constrained.deviations],
        )


# ---------------------------------------------------------------------------
# Acceptance scenario 4 - costs and rounding make a target infeasible
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class InfeasibleTargetAcceptanceTests(TestCase):
    def test_costs_and_rounding_make_a_target_infeasible_without_exceeding_limits(self):
        portfolio = make_allocation_portfolio(
            cash="1000.00",
            holdings={},
            name_pct="100.00",
            exposure_pct="90.00",
            cash_buffer_pct="5.00",
        )
        classify(portfolio, {"XYZ": "healthcare", "JPM": "financials"})
        candidates = [
            candidate("XYZ", claim_id="claim-xyz"),
            candidate("JPM", claim_id="claim-jpm"),
        ]
        quotes = {"XYZ": D("600"), "JPM": D("100")}
        snapshot = capture_allocation_snapshot(portfolio, quotes=quotes)
        brief = build_allocation_brief(
            portfolio,
            snapshot,
            quotes=quotes,
            price_history=price_history(["XYZ", "JPM"], days=30),
            candidates=candidates,
            classifications={
                "XYZ": {"sector": "healthcare", "asset_class": "equity", "source": "fixture"},
                "JPM": {"sector": "financials", "asset_class": "equity", "source": "fixture"},
            },
            evidence_sufficient=True,
        )
        # XYZ's target value is 5% of 1000 = 50, below one 600-dollar lot. JPM's
        # target is 50%, which is executable. The plan must keep the unspent
        # weight in cash and say why rather than scaling XYZ up.
        weights = {"XYZ": D("0.05"), "JPM": D("0.50"), "CASH": D("0.45")}
        proposal = validate_target_proposal(
            payload_for(brief, weights, claim_ids={"XYZ": ["claim-xyz"], "JPM": ["claim-jpm"]}),
            brief,
        )
        policy = AllocationPolicy.from_portfolio(portfolio)
        cost_model = CostModel.from_config(config={"COST_BPS": "10", "FIXED_COST_USD": "1.00"})
        sectors = {
            "XYZ": "healthcare",
            "JPM": "financials",
        }
        reserve = _cost_reserve_fraction(
            proposal.target_weights, brief.state, policy=policy, cost_model=cost_model
        )
        constrained = constrain_targets(
            proposal.target_weights,
            brief.state,
            sectors=sectors,
            policy=policy,
            data_quality=brief.data_quality,
            prices=brief.state.prices,
            cost_reserve_fraction=reserve,
        )
        sized = derive_legs(
            constrained,
            brief.state,
            policy=policy,
            cost_model=cost_model,
            sectors=sectors,
            quotes=brief.state.prices,
        )
        self.assertFalse(any(leg.ticker == "XYZ" for leg in sized.legs))
        self.assertIn(
            "rounding_no_buy",
            [deviation["code"] for deviation in sized.deviations],
        )
        self.assertTrue(any(leg.ticker == "JPM" for leg in sized.legs))
        for leg in sized.legs:
            self.assertGreater(leg.quantity, ZERO)
            self.assertEqual(leg.quantity % policy.lot_size_for(leg.ticker), ZERO)
            self.assertLessEqual(leg.notional_usd + leg.estimated_cost_usd, portfolio.balance_usd)
        self.assertGreater(sized.estimated_cost_usd, ZERO)
        self.assertLessEqual(
            sized.post_trade["total_exposure"],
            str(D("0.90")),
        )
        plan, _created = create_plan(
            portfolio, brief=brief, proposal=proposal, submission_key="infeasible"
        )
        self.assertEqual(sum(D(value) for value in plan.target_weights.values()), D("1.000000"))
        self.assertIn("CASH", plan.target_weights)
        self.assertTrue(plan.legs.filter(ticker="JPM").exists())
        self.assertFalse(plan.legs.filter(ticker="XYZ").exists())


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class TargetContractTests(AllocationFixture):
    def _weights(self):
        return {"AAPL": D("0.20"), "MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.40")}

    def test_a_valid_proposal_normalises_to_exactly_one_including_cash(self):
        proposal = validate_target_proposal(
            payload_for(self.brief, self._weights(), claim_ids=self.claims), self.brief
        )
        self.assertEqual(sum(proposal.target_weights.values()), D("1"))
        self.assertIn("CASH", proposal.target_weights)
        self.assertGreaterEqual(proposal.target_weights["CASH"], ZERO)

    def test_weights_within_the_tolerance_are_normalised_not_rejected(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.400001")}
        proposal = validate_target_proposal(
            payload_for(self.brief, weights, claim_ids=self.claims), self.brief
        )
        self.assertEqual(sum(proposal.target_weights.values()), D("1"))

    def test_weights_outside_the_tolerance_are_rejected(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.399998")}
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(
                payload_for(self.brief, weights, claim_ids=self.claims), self.brief
            )
        self.assertEqual(caught.exception.code, "weights_do_not_sum_to_one")
        self.assertFalse(caught.exception.repairable)

    def test_a_negative_weight_is_rejected_long_only(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("-0.05"), "CASH": D("0.85")}
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(
                payload_for(self.brief, weights, claim_ids=self.claims), self.brief
            )
        self.assertEqual(caught.exception.code, "negative_weight")

    def test_cash_must_be_explicit(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0.25"), "JPM": D("0.15"), "XYZ": D("0.40")}
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(
                payload_for(self.brief, weights, claim_ids=self.claims), self.brief
            )
        self.assertEqual(caught.exception.code, "cash_weight_required")

    def test_an_instrument_outside_the_brief_is_rejected(self):
        weights = {"AAPL": D("0.20"), "FAKE": D("0.20"), "CASH": D("0.60")}
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(
                payload_for(self.brief, weights, claim_ids=self.claims), self.brief
            )
        self.assertEqual(caught.exception.code, "unknown_instrument")

    def test_a_float_weight_is_rejected_decimal_strings_only(self):
        payload = payload_for(self.brief, self._weights(), claim_ids=self.claims)
        payload["target_weights"]["AAPL"] = 0.2
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(payload, self.brief)
        self.assertEqual(caught.exception.code, "decimal_string_required")

    def test_an_increase_without_a_cited_claim_is_rejected(self):
        payload = payload_for(self.brief, self._weights(), claim_ids={})
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(payload, self.brief)
        self.assertEqual(caught.exception.code, "claims_required_for_increase")

    def test_an_unknown_claim_id_is_rejected(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0.25"), "CASH": D("0.55")}
        payload = payload_for(self.brief, weights, claim_ids={"MSFT": ["claim-does-not-exist"]})
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(payload, self.brief)
        self.assertEqual(caught.exception.code, "unknown_claim_id")

    def test_an_action_that_contradicts_the_weights_is_rejected(self):
        payload = payload_for(self.brief, self._weights(), claim_ids=self.claims)
        for row in payload["actions"]:
            if row["ticker"] == "MSFT":
                row["action"] = "reduce"
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(payload, self.brief)
        self.assertEqual(caught.exception.code, "action_weight_mismatch")

    def test_every_held_or_target_instrument_needs_an_action(self):
        payload = payload_for(self.brief, self._weights(), claim_ids=self.claims)
        payload["actions"] = [row for row in payload["actions"] if row["ticker"] != "AAPL"]
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(payload, self.brief)
        self.assertEqual(caught.exception.code, "missing_action")

    def test_the_model_may_not_invent_risk_statistics(self):
        payload = payload_for(self.brief, self._weights(), claim_ids=self.claims)
        payload["expected_returns"] = {"AAPL": "0.10"}
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(payload, self.brief)
        self.assertEqual(caught.exception.code, "invented_risk_statistics")

    def test_a_stale_proposal_version_is_not_repairable(self):
        payload = payload_for(self.brief, self._weights(), claim_ids=self.claims)
        payload["portfolio_version"] = "pv-something-else"
        with self.assertRaises(ProposalStaleError) as caught:
            validate_target_proposal(payload, self.brief)
        self.assertFalse(caught.exception.repairable)
        self.assertEqual(caught.exception.code, "portfolio_version_mismatch")

    def test_a_no_change_proposal_is_valid_and_has_no_increases(self):
        portfolio = make_allocation_portfolio(cash="8000.00", holdings={"AAPL": ("20", "100")})
        brief = build_brief(portfolio, classifications={"AAPL": "technology"})
        weights = {
            "AAPL": D("0.20"),
            "MSFT": D("0"),
            "JPM": D("0"),
            "XYZ": D("0"),
            "CASH": D("0.80"),
        }
        proposal = validate_target_proposal(
            payload_for(brief, weights, funding_source="none"), brief
        )
        self.assertEqual(sum(proposal.target_weights.values()), D("1"))
        self.assertEqual(proposal.target_weights["AAPL"], D("0.2"))
        _constrained, sized = constrain(brief, proposal)
        self.assertEqual(sized.legs, [])

    def test_funding_source_must_be_known(self):
        payload = payload_for(self.brief, self._weights(), claim_ids=self.claims)
        payload["funding_source"] = "leverage"
        with self.assertRaises(AllocationContractError) as caught:
            validate_target_proposal(payload, self.brief)
        self.assertEqual(caught.exception.code, "invalid_funding_source")
        self.assertIn("cash_and_sales", ALLOWED_FUNDING_SOURCES)


# ---------------------------------------------------------------------------
# Brief and risk statistics
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class BriefTests(AllocationFixture):
    def test_the_brief_contains_every_required_input(self):
        payload = self.brief.as_dict()
        for key in (
            "holdings",
            "cash",
            "equity_usd",
            "weights",
            "exposure",
            "limits",
            "transaction_costs",
            "theses",
            "candidate_cases",
            "data_quality",
            "investment_horizon_days",
            "risk_statistics",
            "claim_catalog",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["decision_boundary"], "portfolio")
        self.assertIn("not a guarantee of profitability", payload["objective"])
        self.assertEqual(sum(D(value) for value in payload["weights"].values()), D("1"))

    def test_sector_exposure_is_null_and_incomplete_without_a_classification(self):
        portfolio = make_allocation_portfolio(cash="10000.00", holdings={"AAPL": ("100", "90")})
        brief = build_brief(portfolio, classifications={})
        self.assertEqual(brief.holdings[0]["sector"], None)
        self.assertEqual(brief.exposure["sector_exposure"], [])
        self.assertEqual(brief.exposure["coverage"]["status"], "incomplete")
        self.assertIn("not zero", brief.exposure["note"])

    def test_explicit_classification_populates_sector_and_asset_class(self):
        payload = self.brief.as_dict()
        self.assertEqual(payload["holdings"][0]["sector"], "technology")
        self.assertEqual(payload["holdings"][0]["asset_class"], "equity")
        sector = payload["exposure"]["sector_exposure"][0]
        self.assertEqual(sector["sector"], "technology")
        self.assertEqual(sector["weight"], "0.50000000")

    def test_covariance_and_correlation_are_computed_when_history_exists(self):
        risk = self.brief.as_dict()["risk_statistics"]
        self.assertEqual(risk["status"], "computed")
        self.assertEqual(risk["method_version"], "covariance.sample_daily_returns.v1")
        self.assertEqual(risk["frequency"], "daily")
        self.assertIsNone(risk["annualisation"])
        self.assertGreaterEqual(len(risk["pairs"]), 1)
        self.assertIsNotNone(risk["correlation"][0]["value"])
        self.assertIsNotNone(risk["covariance"][0]["value"])
        self.assertTrue(all(pair["observations"] >= 20 for pair in risk["pairs"]))
        self.assertIn("observations", risk["pairs"][0])

    def test_missing_history_produces_null_correlation_with_a_reason(self):
        portfolio = make_allocation_portfolio(cash="10000.00")
        snapshot = capture_allocation_snapshot(portfolio, quotes={"MSFT": D("100")})
        brief = build_allocation_brief(
            portfolio,
            snapshot,
            quotes={"MSFT": D("100")},
            price_history=None,
            candidates=[],
        )
        risk = brief.as_dict()["risk_statistics"]
        self.assertEqual(risk["status"], "not_available")
        self.assertEqual(risk["correlation"], [])
        self.assertIsNone(risk["observation_window"])
        self.assertEqual(risk["missing_pairs"], [])
        self.assertIn("no_history", risk["quality_flags"])

    def test_insufficient_history_is_null_not_zero(self):
        portfolio = make_allocation_portfolio(
            cash="10000.00",
            holdings={"AAPL": ("10", "100"), "MSFT": ("10", "100")},
        )
        snapshot = capture_allocation_snapshot(
            portfolio, quotes={"AAPL": D("100"), "MSFT": D("100")}
        )
        brief = build_allocation_brief(
            portfolio,
            snapshot,
            quotes={"AAPL": D("100"), "MSFT": D("100")},
            price_history=price_history(["AAPL", "MSFT"], days=6),
            candidates=[],
        )
        risk = brief.as_dict()["risk_statistics"]
        self.assertEqual(risk["status"], "insufficient_history")
        self.assertEqual(risk["correlation"], [])
        self.assertEqual(risk["missing_pairs"][0]["reason"], "insufficient_history")
        self.assertIn("insufficient_history:AAPL|MSFT", risk["quality_flags"])

    def test_an_unpriced_position_makes_equity_null_not_zero(self):
        portfolio = make_allocation_portfolio(cash="1000.00")
        Asset.objects.create(
            portfolio=portfolio, ticker="ZZZ", amount=D("10"), avg_purchase_price=ZERO
        )
        state = build_portfolio_state(portfolio, quotes={})
        self.assertIsNone(state.equity_usd)
        self.assertIn("ZZZ", state.unpriced_tickers)

    def test_a_price_fallback_is_labelled_as_cost_not_market(self):
        portfolio = make_allocation_portfolio(cash="1000.00", holdings={"AAPL": ("10", "90")})
        state = build_portfolio_state(portfolio, quotes={})
        self.assertEqual(state.holding_prices["AAPL"], D("90"))
        brief = build_brief(portfolio, quotes={"MSFT": D("100")}, classifications={})
        self.assertIn("prices_marked_at_cost", brief.data_quality["flag_codes"])


# ---------------------------------------------------------------------------
# Constrained projection and deterministic sizing
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class ConstraintTests(AllocationFixture):
    def test_per_name_and_total_exposure_are_enforced_and_deviations_are_reported(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0"), "JPM": D("0.60"), "CASH": D("0.20")}
        proposal = validate_target_proposal(
            payload_for(self.brief, weights, claim_ids=self.claims), self.brief
        )
        constrained, _sized = constrain(self.brief, proposal)
        self.assertLessEqual(
            constrained.weights["JPM"],
            AllocationPolicy.from_portfolio(self.portfolio).max_name_weight,
        )
        self.assertTrue(
            any(deviation["code"] == "max_name_weight" for deviation in constrained.deviations)
        )
        self.assertEqual(constrained.constraints["checks"]["max_name_weight"]["status"], "pass")
        self.assertEqual(constrained.constraints["checks"]["max_total_exposure"]["status"], "pass")

    def test_turnover_is_capped_and_reported(self):
        portfolio = make_allocation_portfolio(
            cash="10000.00",
            holdings={"AAPL": ("100", "90")},
            turnover_pct="25.00",
        )
        classify(portfolio, {"AAPL": "technology", "MSFT": "technology", "JPM": "financials"})
        brief = build_brief(portfolio)
        weights = {"AAPL": D("0"), "JPM": D("0.50"), "CASH": D("0.50")}
        proposal = validate_target_proposal(
            payload_for(brief, weights, claim_ids=self.claims), brief
        )
        constrained, _sized = constrain(brief, proposal)
        policy = AllocationPolicy.from_portfolio(portfolio)
        self.assertLessEqual(constrained.turnover_fraction, policy.max_turnover + WEIGHT_TOLERANCE)
        self.assertTrue(
            any(deviation["code"] == "max_turnover" for deviation in constrained.deviations)
        )
        self.assertEqual(constrained.constraints["checks"]["max_turnover"]["status"], "pass")

    def test_cash_buffer_wins_when_it_is_stricter_than_exposure(self):
        portfolio = make_allocation_portfolio(
            cash="8000.00",
            holdings={"AAPL": ("20", "100")},
            name_pct="100.00",
            sector_pct="100.00",
            exposure_pct="100.00",
            cash_buffer_pct="20.00",
        )
        brief = build_brief(
            portfolio,
            candidates=[candidate("AAPL", claim_id="claim-aapl")],
            classifications={"AAPL": "technology"},
            quotes={"AAPL": D("100")},
        )
        weights = {"AAPL": D("0.95"), "CASH": D("0.05")}
        proposal = validate_target_proposal(
            payload_for(brief, weights, claim_ids={"AAPL": ["claim-aapl"]}), brief
        )
        constrained, _sized = constrain(brief, proposal)
        self.assertGreaterEqual(constrained.weights["CASH"], D("0.20"))
        self.assertEqual(constrained.constraints["checks"]["cash_buffer"]["status"], "pass")
        self.assertLessEqual(
            sum(weight for ticker, weight in constrained.weights.items() if ticker != "CASH"),
            D("0.80"),
        )

    def test_unclassified_sector_is_not_evaluated_and_not_reported_as_zero(self):
        portfolio = make_allocation_portfolio(cash="10000.00")
        classify(portfolio, {"MSFT": "technology"})
        snapshot = capture_allocation_snapshot(
            portfolio, quotes={"MSFT": D("100"), "JPM": D("100")}
        )
        brief = build_allocation_brief(
            portfolio,
            snapshot,
            quotes={"MSFT": D("100"), "JPM": D("100")},
            price_history=price_history(["MSFT", "JPM"]),
            candidates=[
                CandidateCase(
                    ticker="JPM",
                    action="BUY",
                    rationale="x",
                    claims=[
                        CandidateClaim(
                            claim_id="c-jpm",
                            agent_role="bull",
                            stance="bull",
                            claim_type="forecast",
                            statement="x",
                            evidence_keys=["e"],
                        )
                    ],
                )
            ],
            classifications={
                "MSFT": {"sector": "technology", "asset_class": "equity", "source": "fixture"},
                "JPM": {"sector": None, "asset_class": "equity", "source": "fixture"},
            },
            evidence_sufficient=True,
        )
        weights = {"JPM": D("0.10"), "CASH": D("0.90")}
        proposal = validate_target_proposal(
            payload_for(brief, weights, claim_ids={"JPM": ["c-jpm"]}), brief
        )
        constrained, _sized = constrain(brief, proposal)
        self.assertEqual(constrained.weights["JPM"], D("0.1"))
        self.assertIn(
            "JPM",
            [
                row["ticker"]
                for row in constrained.constraints["checks"]["max_sector_weight"]["not_evaluated"]
            ],
        )

    def test_sells_are_sequenced_before_buys(self):
        weights = {
            "AAPL": D("0.20"),
            "MSFT": D("0"),
            "JPM": D("0.25"),
            "XYZ": D("0.15"),
            "CASH": D("0.40"),
        }
        proposal = validate_target_proposal(
            payload_for(self.brief, weights, claim_ids=self.claims), self.brief
        )
        _constrained, sized = constrain(self.brief, proposal)
        sides = [leg.side for leg in sized.legs]
        self.assertEqual(sides, ["SELL", "BUY", "BUY"])
        self.assertEqual(sized.legs[0].priority, "risk_reducing")
        self.assertTrue(all(leg.priority == "new_risk" for leg in sized.legs[1:]))
        self.assertEqual([leg.sequence for leg in sized.legs], list(range(1, len(sized.legs) + 1)))

    def test_lot_rounding_never_buys_more_than_the_constrained_target(self):
        weights = {
            "AAPL": D("0.20"),
            "MSFT": D("0"),
            "JPM": D("0.25"),
            "XYZ": D("0.15"),
            "CASH": D("0.40"),
        }
        proposal = validate_target_proposal(
            payload_for(self.brief, weights, claim_ids=self.claims), self.brief
        )
        constrained, sized = constrain(self.brief, proposal)
        equity = self.brief.equity_usd
        for leg in sized.legs:
            if leg.side == "BUY":
                target_value = constrained.weights[leg.ticker] * equity
                self.assertLessEqual(leg.notional_usd, target_value)
            else:
                self.assertGreater(leg.quantity, ZERO)


# ---------------------------------------------------------------------------
# Plan lifecycle
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class PlanLifecycleTests(AllocationFixture):
    def _proposal(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.40")}
        return validate_target_proposal(
            payload_for(self.brief, weights, claim_ids=self.claims), self.brief
        )

    def test_a_plan_is_created_from_one_snapshot_with_legs_and_exact_weights(self):
        plan, created = create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=self._proposal(),
            submission_key="lifecycle-create",
        )
        self.assertTrue(created)
        self.assertEqual(plan.snapshot_id, self.brief.snapshot_id)
        self.assertEqual(plan.status, PlanStatus.PROPOSED)
        self.assertEqual(sum(D(value) for value in plan.target_weights.values()), D("1.000000"))
        self.assertEqual(plan.requested_target_weights["CASH"], "0.400000")
        self.assertGreater(plan.legs.count(), 0)

    def test_repeating_a_submission_key_returns_the_existing_plan(self):
        plan, created = create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=self._proposal(),
            submission_key="idempotent-create",
        )
        again, created_again = create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=self._proposal(),
            submission_key="idempotent-create",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(again.id, plan.id)
        self.assertEqual(PortfolioPlan.objects.filter(portfolio=self.portfolio).count(), 1)

    def test_applying_a_plan_is_atomic_and_a_second_apply_is_idempotent(self):
        plan, _ = create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=self._proposal(),
            submission_key="apply-idempotent",
        )
        application = apply_plan(plan.id, quotes=QUOTES)
        self.assertFalse(application.already_applied)
        self.assertEqual(len(application.transactions), plan.legs.count())
        ledger_after_first = Transaction.objects.filter(portfolio=self.portfolio).count()
        again = apply_plan(plan.id, quotes=QUOTES)
        self.assertTrue(again.already_applied)
        self.assertEqual(
            Transaction.objects.filter(portfolio=self.portfolio).count(), ledger_after_first
        )
        self.assertEqual(PortfolioPlanLeg.objects.filter(plan=plan).count(), plan.legs.count())

    def test_a_changed_portfolio_version_requires_revalidation(self):
        plan, _ = create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=self._proposal(),
            submission_key="stale-then-revalidate",
        )
        self.portfolio.balance_usd = D("10100.00")
        self.portfolio.save(update_fields=["balance_usd"])
        with self.assertRaises(PlanStaleError):
            apply_plan(plan.id, quotes=QUOTES)
        plan.refresh_from_db()
        self.assertTrue(plan.revalidation_required)
        self.assertEqual(plan.status, PlanStatus.PROPOSED)
        revalidated = revalidate_plan(plan.id, quotes=QUOTES)
        self.assertFalse(revalidated.revalidation_required)
        self.assertEqual(revalidated.revalidation_count, 1)
        application = apply_plan(plan.id, quotes=QUOTES)
        self.assertEqual(application.plan.status, PlanStatus.APPLIED)

    def test_a_plan_built_from_a_stale_snapshot_requires_revalidation(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.40")}
        proposal = validate_target_proposal(
            payload_for(self.brief, weights, claim_ids=self.claims), self.brief
        )
        # The brief and proposal are from the pre-change state; the plan is
        # created after the portfolio moved. It must refuse to apply until
        # revalidated even though the proposal version is internally consistent.
        self.portfolio.balance_usd = D("10100.00")
        self.portfolio.save(update_fields=["balance_usd"])
        plan, _ = create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=proposal,
            submission_key="stale-snapshot",
        )
        self.assertTrue(plan.revalidation_required)
        with self.assertRaises(PlanStaleError):
            apply_plan(plan.id, quotes=QUOTES)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)

    def test_a_failed_leg_halts_the_plan_and_writes_no_ledger_row(self):
        plan, _ = create_plan(
            self.portfolio,
            brief=self.brief,
            proposal=self._proposal(),
            submission_key="failed-leg",
        )
        with self.assertRaises(PlanLegFailed) as caught:
            apply_plan(plan.id, quotes={"MSFT": D("320"), "JPM": D("160")})
        self.assertEqual(caught.exception.code, "price_unavailable")
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)
        plan.refresh_from_db()
        self.assertEqual(plan.status, PlanStatus.FAILED)
        self.assertEqual(plan.failure_code, "price_unavailable")
        legs = list(plan.legs.order_by("sequence"))
        self.assertEqual(legs[0].status, PlanLegStatus.FAILED)
        self.assertTrue(all(leg.status == PlanLegStatus.SKIPPED for leg in legs[1:]))

    def test_a_no_change_plan_is_a_valid_outcome_with_no_ledger_row(self):
        portfolio = make_allocation_portfolio(cash="8000.00", holdings={"AAPL": ("20", "100")})
        brief = build_brief(portfolio, classifications={"AAPL": "technology"})
        weights = {
            "AAPL": D("0.20"),
            "MSFT": D("0"),
            "JPM": D("0"),
            "XYZ": D("0"),
            "CASH": D("0.80"),
        }
        proposal = validate_target_proposal(
            payload_for(brief, weights, funding_source="none"), brief
        )
        plan, _ = create_plan(
            portfolio,
            brief=brief,
            proposal=proposal,
            submission_key="no-change",
        )
        application = apply_plan(plan.id, quotes={"AAPL": D("100")})
        self.assertEqual(application.plan.status, PlanStatus.NO_CHANGE)
        self.assertEqual(application.transactions, [])
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 0)

    def test_hold_proposal_builds_from_current_weights(self):
        portfolio = make_allocation_portfolio(cash="8000.00", holdings={"AAPL": ("20", "100")})
        brief = build_brief(portfolio, classifications={"AAPL": "technology"})
        proposal = build_hold_proposal(brief, "Policy retains cash.")
        self.assertEqual(sum(proposal.target_weights.values()), D("1"))
        self.assertEqual(proposal.funding_source, "none")
        constrained, sized = constrain(brief, proposal)
        self.assertEqual(sized.legs, [])
        self.assertEqual(constrained.weights["CASH"], brief.state.weight("CASH"))


# ---------------------------------------------------------------------------
# Concurrency gate
# ---------------------------------------------------------------------------
def _concurrent_apply(plan_id, quotes, barrier, results):
    close_old_connections()
    try:
        barrier.wait(timeout=10)
        application = apply_plan(plan_id, quotes=quotes)
        results.append(("applied", application))
    except Exception as exc:
        results.append(("error", exc))
    finally:
        close_old_connections()
        connections.close_all()


@override_settings(CACHES=TEST_CACHES)
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.portfolio = make_allocation_portfolio(
            cash="5000.00",
            holdings={},
            name_pct="30.00",
            exposure_pct="90.00",
            cash_buffer_pct="10.00",
        )
        classify(self.portfolio, {"MSFT": "technology", "JPM": "financials"})
        claims = [
            CandidateClaim(
                claim_id="c-msft",
                agent_role="bull",
                stance="bull",
                claim_type="forecast",
                statement="MSFT.",
                evidence_keys=["e"],
            ),
            CandidateClaim(
                claim_id="c-jpm",
                agent_role="bull",
                stance="bull",
                claim_type="forecast",
                statement="JPM.",
                evidence_keys=["e"],
            ),
        ]
        candidates = [
            CandidateCase(
                ticker="MSFT", action="BUY", rationale="x", claims=[claims[0]], run_id="r1"
            ),
            CandidateCase(
                ticker="JPM", action="BUY", rationale="x", claims=[claims[1]], run_id="r2"
            ),
        ]
        self.quotes = {"MSFT": D("100"), "JPM": D("100")}
        snapshot = capture_allocation_snapshot(self.portfolio, quotes=self.quotes)
        self.brief = build_allocation_brief(
            self.portfolio,
            snapshot,
            quotes=self.quotes,
            price_history=price_history(["MSFT", "JPM"], days=30),
            candidates=candidates,
            classifications={
                "MSFT": {"sector": "technology", "asset_class": "equity", "source": "fixture"},
                "JPM": {"sector": "financials", "asset_class": "equity", "source": "fixture"},
            },
            evidence_sufficient=True,
        )
        self.claims = {"MSFT": ["c-msft"], "JPM": ["c-jpm"]}
        self.weights = {"MSFT": D("0.25"), "JPM": D("0.25"), "CASH": D("0.50")}

    def _plan(self, key):
        proposal = validate_target_proposal(
            payload_for(self.brief, self.weights, claim_ids=self.claims), self.brief
        )
        plan, _created = create_plan(
            self.portfolio, brief=self.brief, proposal=proposal, submission_key=key
        )
        return plan

    def test_two_concurrent_plans_cannot_both_spend_the_same_cash(self):
        plan_a = self._plan("concurrent-a")
        plan_b = self._plan("concurrent-b")
        barrier = threading.Barrier(2)
        results: list[tuple[str, object]] = []
        threads = [
            threading.Thread(
                target=_concurrent_apply,
                args=(plan.id, self.quotes, barrier, results),
            )
            for plan in (plan_a, plan_b)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "an apply thread did not finish")

        outcomes = sorted(kind for kind, _ in results)
        self.assertEqual(outcomes, ["applied", "error"])
        errors = [value for kind, value in results if kind == "error"]
        self.assertIsInstance(errors[0], PlanStaleError)
        portfolio = Portfolio.objects.get(pk=self.portfolio.pk)
        self.assertGreaterEqual(portfolio.balance_usd, ZERO)
        applied_plans = PortfolioPlan.objects.filter(
            portfolio=self.portfolio, status=PlanStatus.APPLIED
        )
        self.assertEqual(applied_plans.count(), 1)
        transactions = Transaction.objects.filter(portfolio=self.portfolio).count()
        self.assertEqual(transactions, 2)
        self.assertEqual(
            PortfolioPlan.objects.filter(
                portfolio=self.portfolio, revalidation_required=True
            ).count(),
            1,
        )

    def test_duplicate_concurrent_apply_adds_no_duplicate_ledger_row(self):
        plan = self._plan("concurrent-duplicate")
        barrier = threading.Barrier(2)
        results: list[tuple[str, object]] = []
        threads = [
            threading.Thread(
                target=_concurrent_apply,
                args=(plan.id, self.quotes, barrier, results),
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "an apply thread did not finish")

        self.assertEqual([kind for kind, _ in results], ["applied", "applied"])
        applications = [value for _kind, value in results]
        self.assertEqual(sum(1 for app in applications if app.already_applied), 1)
        self.assertEqual(
            Transaction.objects.filter(portfolio=self.portfolio).count(),
            plan.legs.count(),
        )
        self.assertEqual(
            PortfolioPlanLeg.objects.filter(plan=plan, transaction__isnull=False).count(),
            plan.legs.count(),
        )

    def test_cumulative_limits_cannot_be_bypassed_by_two_valid_proposals(self):
        plan_a = self._plan("cumulative-a")
        # A second individually valid plan for the same name, one point higher.
        weights = {"MSFT": D("0.29"), "JPM": D("0.25"), "CASH": D("0.46")}
        second = validate_target_proposal(
            payload_for(self.brief, weights, claim_ids=self.claims), self.brief
        )
        plan_b, _ = create_plan(
            self.portfolio, brief=self.brief, proposal=second, submission_key="cumulative-b"
        )
        apply_plan(plan_a.id, quotes=self.quotes)
        with self.assertRaises(PlanStaleError):
            apply_plan(plan_b.id, quotes=self.quotes)
        revalidated = revalidate_plan(plan_b.id, quotes=self.quotes)
        # The second plan is re-derived against the post-first-plan state; it
        # cannot add the original 29% on top of what the first plan already bought.
        if revalidated.legs.exists():
            apply_plan(plan_b.id, quotes=self.quotes)
        portfolio = Portfolio.objects.get(pk=self.portfolio.pk)
        policy = AllocationPolicy.from_portfolio(portfolio)
        total_equity = portfolio.balance_usd + sum(
            (asset.amount * self.quotes[asset.ticker] for asset in portfolio.assets.all()),
            ZERO,
        )
        msft = portfolio.assets.filter(ticker="MSFT").first()
        msft_value = (msft.amount * D("100")) if msft else ZERO
        self.assertLessEqual(
            msft_value,
            (total_equity * policy.max_name_weight).quantize(CENT),
        )
        self.assertLessEqual(
            Transaction.objects.filter(portfolio=self.portfolio).count(),
            plan_a.legs.count() + plan_b.legs.count(),
        )


# ---------------------------------------------------------------------------
# Model seam and end-to-end stub
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class StubModelEndToEndTests(AllocationFixture):
    def test_the_cio_calls_a_stub_once_and_records_the_provenance(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.40")}
        response = payload_for(self.brief, weights, claim_ids=self.claims)
        client = StaticAllocationClient(response)
        proposal = PortfolioCIO(client=client).decide(self.brief)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(proposal.portfolio_version, self.brief.portfolio_version)
        self.assertEqual(proposal.model_metadata["provider"], "stub")
        self.assertEqual(proposal.model_metadata["cost_reported"], False)
        self.assertIn("regenerating model output is not", proposal.model_metadata["note"])

    def test_a_repairable_contract_error_gets_one_repair_and_is_recorded(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.40")}
        good = payload_for(self.brief, weights, claim_ids=self.claims)
        bad = dict(good)
        bad["actions"] = []
        client = StaticAllocationClient([bad, good])
        proposal = PortfolioCIO(client=client).decide(self.brief)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(proposal.model_metadata["repair_attempts"], 1)
        self.assertEqual(proposal.model_metadata["calls"][0]["status"], "rejected")
        self.assertEqual(proposal.model_metadata["calls"][1]["status"], "accepted")

    def test_run_allocation_creates_a_plan_and_no_ledger_row_without_apply(self):
        weights = {"AAPL": D("0.20"), "MSFT": D("0.25"), "JPM": D("0.15"), "CASH": D("0.40")}
        response = payload_for(self.brief, weights, claim_ids=self.claims)
        result = run_allocation(
            self.portfolio,
            client=StaticAllocationClient(response),
            quotes=QUOTES,
            price_history=price_history(list(QUOTES)),
            candidates=[
                candidate("MSFT", claim_id="claim-msft"),
                candidate("JPM", claim_id="claim-jpm"),
                candidate("XYZ", claim_id="claim-xyz"),
            ],
            classifications={
                "AAPL": "technology",
                "MSFT": "technology",
                "JPM": "financials",
                "XYZ": "healthcare",
            },
            submission_key="e2e-no-apply",
            evidence_sufficient=True,
        )
        self.assertTrue(result.created)
        self.assertIsNotNone(result.brief)
        self.assertEqual(result.plan.status, PlanStatus.PROPOSED)
        self.assertEqual(Transaction.objects.filter(portfolio=self.portfolio).count(), 0)
        self.assertEqual(
            sum(D(value) for value in result.plan.target_weights.values()), D("1.000000")
        )


# ---------------------------------------------------------------------------
# The paper command
# ---------------------------------------------------------------------------
@override_settings(CACHES=TEST_CACHES)
class PaperCommandTests(TestCase):
    def test_the_command_runs_end_to_end_and_applies_the_plan(self):
        stdout = StringIO()
        call_command(
            "run_allocation",
            username="demo-command",
            reset=True,
            apply=True,
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn('"phase": "brief"', output)
        self.assertIn('"phase": "targets"', output)
        self.assertIn('"phase": "application"', output)
        self.assertIn('"status": "applied"', output)
        self.assertIn('"target_sum": "1.000000"', output)
        portfolio = Portfolio.objects.get(user__username="demo-command")
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 3)
        plan = PortfolioPlan.objects.get(portfolio=portfolio)
        self.assertEqual(plan.status, PlanStatus.APPLIED)

    def test_the_command_shows_a_leg_failure_and_writes_no_ledger_row(self):
        stdout = StringIO()
        call_command(
            "run_allocation",
            username="demo-command-fail",
            reset=True,
            apply=True,
            fail_leg="AAPL",
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn('"phase": "application_failed"', output)
        portfolio = Portfolio.objects.get(user__username="demo-command-fail")
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 0)
        plan = PortfolioPlan.objects.get(portfolio=portfolio)
        self.assertEqual(plan.status, PlanStatus.FAILED)
        self.assertEqual(plan.failure_code, "price_unavailable")

    def test_the_command_does_not_call_market_data_or_a_model(self):
        stdout = StringIO()
        with patch("services.market_data.get_latest_quote", side_effect=AssertionError("network")):
            call_command(
                "run_allocation",
                username="demo-command-offline",
                reset=True,
                apply=True,
                stdout=stdout,
            )
        portfolio = Portfolio.objects.get(user__username="demo-command-offline")
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 3)
