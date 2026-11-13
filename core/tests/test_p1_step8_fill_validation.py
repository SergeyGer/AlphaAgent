"""P1 step 8: simulated fills are revalidated at the execution instant.

The defects these tests exercise were reproduced against the pre-fix tree:

* a buy approved at a decision price of 100 filled at a later mark of 3000 for
  its decision-time quantity, taking cash from ``10000`` to ``-20000``;
* two sells of 6 and 8 against a holding of 10 both filled, crediting proceeds
  for 12 shares (``1000 -> 2400``) while the position was clamped to zero;
* a day that realised a loss of 800 against a 500 daily limit still approved a
  new BUY in the same day, because the simulator passed ``realised_pnl_today=0``
  to the guard at every decision.

The tests hold the repair: one fill-time validation path for immediate and
deferred orders, cash including fees, eligible holdings, the actual daily
realised P&L, and an archive that reproduces its timeline, fill decisions and
digest.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from core.models import Portfolio
from core.tests.helpers import make_portfolio, make_user
from services.experiment_execution import (
    Opportunity,
    execute_opportunity_set,
    validate_opportunity_set,
)
from services.experiment_rebuild import rebuild_comparison_report
from services.experiment_simulation import (
    FILL_VALIDATION_POLICY,
    REASON_FILL_CLIPPED_CASH,
    REASON_FILL_CLIPPED_HOLDING,
    DecisionInput,
    SnapshotInput,
    simulate_arm_timeline,
)
from services.portfolio_accounting import PriceMark

OPPORTUNITY_SET_PATH = (
    Path(settings.BASE_DIR) / "docs/spec/fixtures/experiments/alphaagent-arms.v1.json"
)

RISK_RULES = {
    "max_trade_allocation_pct": "1000.00",
    "max_portfolio_exposure_pct": "1000.00",
    "max_ticker_concentration_pct": "1000.00",
    "max_sector_concentration_pct": "1000.00",
    "max_turnover_pct": "100.00",
    "min_cash_buffer_pct": "0.00",
    "daily_loss_limit_usd": "500.00",
}


def parse_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def opportunity(key: str, ticker: str, when: str) -> Opportunity:
    return Opportunity(key=key, ticker=ticker, scheduled_at=parse_at(when), snapshot={})


def snapshot(ticker: str, price: str, when: str) -> SnapshotInput:
    return SnapshotInput(
        id=f"snapshot-{when}",
        content_hash="hash",
        portfolio_version="v1",
        analysis_cutoff=parse_at(when),
        captured_at=parse_at(when),
        quotes={
            ticker: {
                "price_usd": price,
                "as_of": when,
                "source": "declared_fixture_quote",
            }
        },
    )


def decision(
    key: str, ticker: str, when: str, action: str, quantity: str, price: str
) -> DecisionInput:
    return DecisionInput(
        run_id=f"run-{key}",
        opportunity_key=key,
        ticker=ticker,
        decision_at=parse_at(when),
        action=action,
        requested_quantity=Decimal(quantity),
        rationale="step 8",
        snapshot=snapshot(ticker, price, when),
    )


def portfolio_factory(cash: Decimal, positions_value: Decimal) -> Portfolio:
    return Portfolio(
        balance_usd=cash,
        risk_profile="medium",
        max_trade_allocation_pct=Decimal(RISK_RULES["max_trade_allocation_pct"]),
        max_portfolio_exposure_pct=Decimal(RISK_RULES["max_portfolio_exposure_pct"]),
        max_ticker_concentration_pct=Decimal(RISK_RULES["max_ticker_concentration_pct"]),
        max_sector_concentration_pct=Decimal(RISK_RULES["max_sector_concentration_pct"]),
        max_turnover_pct=Decimal(RISK_RULES["max_turnover_pct"]),
        min_cash_buffer_pct=Decimal(RISK_RULES["min_cash_buffer_pct"]),
        daily_loss_limit_usd=Decimal(RISK_RULES["daily_loss_limit_usd"]),
    )


def simulate(
    *,
    opportunities: list[Opportunity],
    decisions: dict[str, DecisionInput],
    marks: list[PriceMark],
    starting_state: dict,
    assumptions: dict | None = None,
) -> dict:
    return simulate_arm_timeline(
        arm="V0",
        opportunities=opportunities,
        decisions=decisions,
        marks=marks,
        window_start=parse_at("2026-02-03T00:00:00Z"),
        window_end=parse_at("2026-02-05T00:00:00Z"),
        starting_state=starting_state,
        execution_assumptions=assumptions or {"fee_bps": "0", "guard_policy_version": "guard.v1"},
        risk_rules=RISK_RULES,
        guard_portfolio_factory=portfolio_factory,
    )


def fill_steps(timeline: dict) -> list[dict]:
    return [step for step in timeline["steps"] if step["event"] == "fill"]


class CashAtFillTests(TestCase):
    """8A - a buy that fits at the proposal price is clipped at the fill price."""

    def test_a_later_price_or_fee_that_exceeds_cash_clips_and_never_overdraws(self):
        opportunity_one = opportunity("buy-1", "AAPL", "2026-02-03T10:00:00Z")
        decision_one = decision("buy-1", "AAPL", "2026-02-03T10:00:00Z", "BUY", "10", "100")
        # Decision price 100 fits; the executable mark is 3000.
        timeline = simulate(
            opportunities=[opportunity_one],
            decisions={"buy-1": decision_one},
            marks=[
                PriceMark(
                    ticker="AAPL",
                    at=parse_at("2026-02-03T11:00:00Z"),
                    price=Decimal("3000"),
                    source="declared",
                )
            ],
            starting_state={"cash": Decimal("10000"), "positions": {}},
        )
        order = timeline["orders"][0]
        self.assertEqual(order["guard_status"], "approved")
        self.assertEqual(order["fill_rule_status"], "deferred")
        self.assertLess(Decimal(order["fill"]["quantity"]), Decimal("10"))
        self.assertGreaterEqual(timeline["ending_cash"], Decimal("0"))
        self.assertEqual(order["fill_validation"]["policy"], FILL_VALIDATION_POLICY)
        self.assertTrue(order["fill_validation"]["cash_includes_fees"])

        # A fee that exceeds the available cash is clipped against cash+fee, not
        # just the notional, and the fill still leaves non-negative cash.
        fee_timeline = simulate(
            opportunities=[opportunity("buy-fee", "AAPL", "2026-02-03T10:00:00Z")],
            decisions={
                "buy-fee": decision("buy-fee", "AAPL", "2026-02-03T10:00:00Z", "BUY", "10", "100")
            },
            marks=[
                PriceMark(
                    ticker="AAPL",
                    at=parse_at("2026-02-03T11:00:00Z"),
                    price=Decimal("1000"),
                    source="declared",
                )
            ],
            starting_state={"cash": Decimal("10000"), "positions": {}},
            assumptions={"fee_bps": "20000", "guard_policy_version": "guard.v1"},
        )
        fee_order = fee_timeline["orders"][0]
        self.assertEqual(fee_order["fill_rule_status"], "deferred")
        self.assertTrue(fee_order["fill_validation"]["clipped"])
        self.assertEqual(fee_order["fill_validation"]["clip_reason"], REASON_FILL_CLIPPED_CASH)
        self.assertLess(Decimal(fee_order["fill"]["quantity"]), Decimal("10"))
        self.assertGreaterEqual(fee_timeline["ending_cash"], Decimal("0"))
        for step in fill_steps(fee_timeline):
            self.assertGreaterEqual(Decimal(step["cash_after_usd"]), Decimal("0"))
        self.assertLessEqual(
            Decimal(fee_order["fill"]["quantity"]) * Decimal("1000")
            + Decimal(fee_order["fill"]["fee_usd"]),
            Decimal("10000.00"),
        )


class EligibleHoldingAtFillTests(TestCase):
    """8B - a second sell never credits proceeds for shares the arm did not hold."""

    def test_two_sells_beyond_the_holding_clip_the_second_and_never_overcredit(self):
        opportunities = [
            opportunity("sell-1", "AAPL", "2026-02-03T10:00:00Z"),
            opportunity("sell-2", "AAPL", "2026-02-03T10:30:00Z"),
        ]
        decisions = {
            "sell-1": decision("sell-1", "AAPL", "2026-02-03T10:00:00Z", "SELL", "6", "100"),
            "sell-2": decision("sell-2", "AAPL", "2026-02-03T10:30:00Z", "SELL", "8", "100"),
        }
        marks = [
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-03T11:00:00Z"),
                price=Decimal("100"),
                source="declared",
            ),
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-03T11:05:00Z"),
                price=Decimal("100"),
                source="declared",
            ),
        ]
        timeline = simulate(
            opportunities=opportunities,
            decisions=decisions,
            marks=marks,
            starting_state={"cash": Decimal("1000"), "positions": {"AAPL": Decimal("10")}},
        )

        self.assertEqual(
            [fill.quantity for fill in timeline["fills"]],
            [Decimal("6.00000000"), Decimal("4.00000000")],
        )
        self.assertLessEqual(sum(fill.quantity for fill in timeline["fills"]), Decimal("10"))
        self.assertEqual(timeline["ending_cash"], Decimal("2000.00000000"))
        self.assertEqual(timeline["ending_positions"].get("AAPL", Decimal("0")), Decimal("0"))
        second = timeline["orders"][1]
        self.assertTrue(second["fill_validation"]["clipped"])
        self.assertIn(
            second["fill_validation"]["clip_reason"],
            {REASON_FILL_CLIPPED_HOLDING, "clipped_by_guard_at_fill"},
        )
        self.assertEqual(second["fill_validation"]["holdings_before"], "4.00000000")
        self.assertEqual(second["fill_validation"]["approved_quantity_at_decision"], "8.00000000")
        self.assertEqual(second["fill_validation"]["filled_quantity"], "4.00000000")
        # Proceeds are for four shares, not eight: 1000 + 600 + 400, never 2400.
        self.assertNotEqual(timeline["ending_cash"], Decimal("2400"))


class DailyLossAtDecisionTests(TestCase):
    """8C - the actual realised P&L blocks new risk that day, and resets next day."""

    def setUp(self):
        self.opportunities = [
            opportunity("day1-buy", "AAPL", "2026-02-03T09:30:00Z"),
            opportunity("day1-sell", "AAPL", "2026-02-03T14:00:00Z"),
            opportunity("day1-buy2", "AAPL", "2026-02-03T14:30:00Z"),
            opportunity("day2-buy", "AAPL", "2026-02-04T09:30:00Z"),
        ]
        self.decisions = {
            "day1-buy": decision("day1-buy", "AAPL", "2026-02-03T09:30:00Z", "BUY", "10", "100"),
            "day1-sell": decision("day1-sell", "AAPL", "2026-02-03T14:00:00Z", "SELL", "10", "20"),
            "day1-buy2": decision("day1-buy2", "AAPL", "2026-02-03T14:30:00Z", "BUY", "5", "20"),
            "day2-buy": decision("day2-buy", "AAPL", "2026-02-04T09:30:00Z", "BUY", "5", "20"),
        }
        self.marks = [
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-03T09:30:00Z"),
                price=Decimal("100"),
                source="declared",
            ),
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-03T14:00:00Z"),
                price=Decimal("20"),
                source="declared",
            ),
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-03T14:30:00Z"),
                price=Decimal("20"),
                source="declared",
            ),
            # The next-day buy is deferred to this later mark.
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-04T10:00:00Z"),
                price=Decimal("20"),
                source="declared",
            ),
        ]

    def test_the_loss_blocks_same_day_risk_allows_the_next_day_and_is_passed_to_the_decision(self):
        timeline = simulate(
            opportunities=self.opportunities,
            decisions=self.decisions,
            marks=self.marks,
            starting_state={"cash": Decimal("10000"), "positions": {}},
        )
        by_opportunity = {order["opportunity"]: order for order in timeline["orders"]}

        self.assertEqual(by_opportunity["day1-buy"]["fill_rule_status"], "filled")
        self.assertEqual(by_opportunity["day1-sell"]["fill_rule_status"], "filled")
        blocked = by_opportunity["day1-buy2"]
        self.assertEqual(blocked["guard_status"], "rejected")
        self.assertIn("daily loss limit", blocked["guard_reason"])
        self.assertEqual(blocked["realised_pnl_today_usd"], "-800.00")
        self.assertIsNone(blocked["fill"])

        # The next day's decision sees the reset value, and its deferred fill is
        # applied at the next mark after the fill-time recheck.
        next_day = by_opportunity["day2-buy"]
        self.assertEqual(next_day["guard_status"], "approved")
        self.assertEqual(next_day["realised_pnl_today_usd"], "0.00")
        self.assertEqual(next_day["fill_rule_status"], "deferred")
        self.assertEqual(next_day["fill_validation"]["realised_pnl_today_before_usd"], "0.00")
        self.assertEqual(next_day["fill_validation"]["guard_status"], "approved")
        self.assertEqual(next_day["fill_validation"]["status"], "filled")
        self.assertEqual(timeline["ending_positions"]["AAPL"], Decimal("5.00000000"))
        self.assertEqual(timeline["realised_pnl"]["by_day_usd"]["2026-02-03"], "-800.00")
        self.assertEqual(timeline["realised_pnl"]["today_usd"], "0.00")
        self.assertEqual(timeline["realised_pnl"]["cost_basis"], "weighted_average_cost.v1")
        self.assertEqual(timeline["realised_pnl"]["fee_policy"], "fee_reduces_realised_pnl.v1")
        self.assertEqual(timeline["method_versions"]["fill_validation"], FILL_VALIDATION_POLICY)

    def test_a_deferred_fill_is_blocked_when_the_loss_lands_before_its_execution(self):
        opportunities = [
            opportunity("establish", "AAPL", "2026-02-03T09:30:00Z"),
            opportunity("buy-before-loss", "MSFT", "2026-02-03T13:00:00Z"),
            opportunity("loss", "AAPL", "2026-02-03T14:00:00Z"),
        ]
        decisions = {
            "establish": decision("establish", "AAPL", "2026-02-03T09:30:00Z", "BUY", "10", "100"),
            "buy-before-loss": decision(
                "buy-before-loss", "MSFT", "2026-02-03T13:00:00Z", "BUY", "5", "50"
            ),
            "loss": decision("loss", "AAPL", "2026-02-03T14:00:00Z", "SELL", "10", "20"),
        }
        marks = [
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-03T09:30:00Z"),
                price=Decimal("100"),
                source="declared",
            ),
            PriceMark(
                ticker="AAPL",
                at=parse_at("2026-02-03T14:30:00Z"),
                price=Decimal("20"),
                source="declared",
            ),
            PriceMark(
                ticker="MSFT",
                at=parse_at("2026-02-03T15:00:00Z"),
                price=Decimal("50"),
                source="declared",
            ),
        ]
        timeline = simulate(
            opportunities=opportunities,
            decisions=decisions,
            marks=marks,
            starting_state={"cash": Decimal("10000"), "positions": {}},
        )
        by_opportunity = {order["opportunity"]: order for order in timeline["orders"]}
        queued = by_opportunity["buy-before-loss"]
        # Approved when proposed, rejected when the fill instant arrives after the
        # loss: the decision did not see the future, and the fill did see the loss.
        self.assertEqual(queued["guard_status"], "approved")
        self.assertEqual(queued["realised_pnl_today_usd"], "0.00")
        self.assertEqual(queued["fill_rule_status"], "rejected")
        self.assertEqual(queued["fill_validation"]["guard_status"], "rejected")
        self.assertIn("daily loss limit", queued["fill_validation"]["guard_reason"])
        self.assertEqual(queued["fill_validation"]["realised_pnl_today_before_usd"], "-800.00")
        self.assertIsNone(queued["fill"])
        self.assertNotIn("MSFT", timeline["ending_positions"])
        self.assertEqual(timeline["ending_cash"], Decimal("9200.00000000"))


class ArchivedFillValidationRebuildTests(TestCase):
    """8D - the frozen archive reproduces the timeline, fills and digest."""

    def test_rebuild_reproduces_the_timeline_fill_decisions_and_digest(self):
        portfolio = make_portfolio(make_user("step8d"), balance="10000.00")
        document = json.loads(OPPORTUNITY_SET_PATH.read_text(encoding="utf-8"))
        archive = json.loads(
            json.dumps(
                execute_opportunity_set(
                    opportunity_set=validate_opportunity_set(copy.deepcopy(document)),
                    portfolio=portfolio,
                    provider_mode="stub",
                )
            )
        )
        rebuilt = rebuild_comparison_report(copy.deepcopy(archive))
        self.assertEqual(rebuilt["status"], "reproduced")
        self.assertEqual(rebuilt["difference_count"], 0, rebuilt["differences"][:5])
        self.assertTrue(rebuilt["contents_hash"]["matches"])
        self.assertEqual(rebuilt["rebuilt_report"]["contents_hash"], archive["contents_hash"])

        for archived_arm, rebuilt_arm in zip(
            archive["arms"], rebuilt["rebuilt_report"]["arms"], strict=True
        ):
            if archived_arm["key"] == "V4":
                continue
            archived_trajectory = archived_arm["trajectory"]
            rebuilt_trajectory = rebuilt_arm["trajectory"]
            self.assertEqual(
                archived_trajectory["simulation"]["steps"],
                rebuilt_trajectory["simulation"]["steps"],
            )
            self.assertEqual(
                archived_trajectory["simulation"]["steps_hash"],
                rebuilt_trajectory["simulation"]["steps_hash"],
            )
            self.assertEqual(archived_trajectory["orders"], rebuilt_trajectory["orders"])
            self.assertEqual(
                archived_trajectory["simulation"]["fill_validation_policy"],
                FILL_VALIDATION_POLICY,
            )
            self.assertIn("realised_pnl", archived_trajectory["simulation"])
        self.assertTrue(rebuilt["synthetic"])
        self.assertEqual(rebuilt["synthetic_label"], "synthetic_simulation")
