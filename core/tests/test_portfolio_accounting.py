"""Accounting fixtures and leakage tests for Phase 3 (MET-01..MET-05).

These are the gate the specification names: a portfolio-accounting engine whose
sign conventions, fee handling, external flows, missing data and horizon rules
can be checked independently of any model call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from core.decision_models import OutcomeMaturity
from core.models import ExecutedBy, Transaction, TxType
from core.tests.helpers import make_portfolio, make_user
from services.portfolio_accounting import (
    TWR_METHOD,
    AccountingError,
    CounterfactualRule,
    ExecutionPriceUnavailable,
    ExternalFlow,
    Fill,
    LlmCost,
    NegativePositionError,
    OpeningState,
    PriceBook,
    PriceLeakageError,
    PriceMark,
    assert_price_not_before,
    build_performance,
    derive_opening_state,
    evaluate_counterfactual,
    load_fills,
    positions_at,
    require_fill_price,
    select_fill_price,
)
from services.signal_diagnostics import (
    MetricFamilyError,
    PerDecisionScoreSeries,
    SignalObservation,
    aggregate_observations,
    evaluate_horizon,
)

D = Decimal
T0 = datetime(2026, 2, 2, 14, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def day(number: int) -> datetime:
    return T0 + timedelta(days=number)


def marks_for(ticker: str, points: list[tuple[int, str]], *, source: str = "test") -> list:
    return [PriceMark(ticker, day(index), D(price), source) for index, price in points]


def observation(**overrides) -> SignalObservation:
    defaults = {
        "run_id": "run-1",
        "run_key": "run-1",
        "decision_at": T0,
        "ticker": "AAPL",
        "horizon_days": 7,
        "method_version": "test.v1",
        "maturity": OutcomeMaturity.EVALUATED,
        "proposal_action": "BUY",
        "executed_action": "BUY",
        "execution_status": "executed",
        "counterfactual": False,
        "forward_return_pct": D("10"),
        "action_score_pct": D("10"),
        "directional_hit": True,
        "benchmark_ticker": "SPY",
        "benchmark_return_pct": D("1"),
        "benchmark_matched": True,
        "relative_forward_return_pct": D("9"),
        "action_relative_return_pct": D("9"),
    }
    defaults.update(overrides)
    return SignalObservation(**defaults)


class BuyHoldSellSequenceTests(SimpleTestCase):
    """BUY -> HOLD -> SELL: exposure is acquired, preserved, then closed."""

    def setUp(self):
        self.fills = [
            Fill(
                at=day(1),
                ticker="AAPL",
                side=TxType.BUY,
                quantity=D("10"),
                price=D("100"),
                fee=D("0"),
            ),
            Fill(
                at=day(4),
                ticker="AAPL",
                side=TxType.SELL,
                quantity=D("10"),
                price=D("120"),
                fee=D("0"),
            ),
        ]
        self.marks = marks_for(
            "AAPL", [(0, "100"), (1, "100"), (2, "110"), (3, "115"), (4, "120"), (5, "120")]
        )
        self.performance = build_performance(
            opening=OpeningState(at=T0, cash=D("10000"), positions={}),
            end=day(5),
            fills=self.fills,
            marks=self.marks,
        )
        self.report = self.performance.as_dict()

    def test_equity_follows_cash_plus_marked_positions(self):
        self.assertEqual(self.report["opening_equity_usd"], "10000.00")
        self.assertEqual(self.report["closing_equity_usd"], "10200.00")
        self.assertEqual(self.report["closing_cash_usd"], "10200.00")
        self.assertEqual(self.report["closing_positions_value_usd"], "0.00")

        by_time = {point["at"]: point for point in self.report["equity_series"]}
        held = by_time[day(2).isoformat().replace("+00:00", "Z")]
        self.assertEqual(held["equity_usd"], "10100.00")
        self.assertEqual(held["positions_value_usd"], "1100.00")

    def test_hold_preserves_exposure_without_a_trade(self):
        by_time = {point["at"]: point for point in self.report["equity_series"]}
        day_two = by_time[day(2).isoformat().replace("+00:00", "Z")]
        day_three = by_time[day(3).isoformat().replace("+00:00", "Z")]

        self.assertEqual(day_two["positions_value_usd"], "1100.00")
        self.assertEqual(day_three["positions_value_usd"], "1150.00")
        # No event happened between the two observations; a HOLD is the absence
        # of a trade and its exposure is carried, not closed.
        self.assertEqual(day_three["events"], [])

    def test_sell_closes_exposure_and_does_not_become_a_short(self):
        by_time = {point["at"]: point for point in self.report["equity_series"]}
        after_sell = by_time[day(4).isoformat().replace("+00:00", "Z")]
        self.assertEqual(after_sell["positions_value_usd"], "0.00")
        self.assertEqual(self.report["exposure"]["end_gross_pct"], "0.0000")
        self.assertIn("short", self.report["methodology"]["short_policy"].lower())
        self.assertEqual(self.report["trade_count"], 2)

    def test_return_and_drawdown_are_computed_from_accounting(self):
        self.assertEqual(self.report["time_weighted_return_pct"], "2.0000")
        self.assertEqual(self.report["max_drawdown_pct"], "0.0000")
        self.assertEqual(self.report["methodology"]["twr_method"], TWR_METHOD)
        self.assertFalse(self.report["approximate"])
        self.assertEqual(self.report["turnover"]["traded_notional_usd"], "2200.00")
        self.assertEqual(
            self.report["turnover"]["formula"],
            "sum(abs(quantity * fill_price)) / ((opening_equity + closing_equity) / 2)",
        )
        cash = self.report["cash_reconciliation"]
        self.assertEqual(cash["status"], "matched")
        self.assertEqual(cash["expected_closing_cash_usd"], "10200.00")
        self.assertEqual(cash["difference_usd"], "0.00")
        self.assertIn("opening_cash", cash["formula"])

    def test_contribution_analysis_records_quantities_entries_exits_and_weights(self):
        block = self.report["contribution_analysis"]
        row = next(item for item in block["rows"] if item["ticker"] == "AAPL")

        self.assertEqual(row["opening_quantity"], "0")
        self.assertEqual(row["closing_quantity"], "0")
        self.assertEqual(len(row["entries"]), 1)
        self.assertEqual(len(row["exits"]), 1)
        self.assertEqual(row["entries"][0]["quantity"], "10")
        self.assertEqual(row["entries"][0]["capital_weight_pct"], "10.0000")
        self.assertEqual(row["exits"][0]["side"], "SELL")
        self.assertIsNotNone(row["average_capital_weight_pct"])
        self.assertEqual(row["realised_and_unrealised_pnl_usd"], "200.00")
        self.assertTrue(block["reconciles_to_equity_change"])
        self.assertEqual(block["basis"], "fills_and_marks.v1")
        # Per-instrument values are published so a contribution can be checked.
        point = next(
            item
            for item in self.report["equity_series"]
            if item["at"] == day(2).isoformat().replace("+00:00", "Z")
        )
        self.assertEqual(point["position_values_usd"], {"AAPL": "1100.00"})

    def test_sharpe_is_explicitly_absent_with_its_disclosure(self):
        sharpe = self.report["risk_adjusted"]["sharpe"]
        self.assertFalse(sharpe["included"])
        self.assertIsNone(sharpe["value"])
        self.assertEqual(sharpe["frequency"], "event_driven")
        self.assertIsNone(sharpe["annualization"])
        self.assertIsNone(sharpe["risk_free_rate"])
        self.assertIn("fewer than", sharpe["sample_limits"])

    def test_an_oversell_is_refused_rather_than_treated_as_a_short(self):
        with self.assertRaises(NegativePositionError):
            build_performance(
                opening=OpeningState(at=T0, cash=D("10000"), positions={}),
                end=day(2),
                fills=[
                    Fill(
                        at=day(1),
                        ticker="AAPL",
                        side=TxType.SELL,
                        quantity=D("1"),
                        price=D("100"),
                    )
                ],
                marks=marks_for("AAPL", [(0, "100"), (1, "100")]),
            )


class FeeAccountingTests(SimpleTestCase):
    """A recorded fee and an unrecorded fee must not look the same."""

    def setUp(self):
        self.marks = marks_for("AAPL", [(0, "100"), (1, "100"), (2, "100")])
        self.opening = OpeningState(at=T0, cash=D("10000"), positions={})

    def _with_fee(self, fee):
        return build_performance(
            opening=self.opening,
            end=day(2),
            fills=[
                Fill(
                    at=day(1),
                    ticker="AAPL",
                    side=TxType.BUY,
                    quantity=D("10"),
                    price=D("100"),
                    fee=fee,
                )
            ],
            marks=self.marks,
        ).as_dict()

    def test_a_recorded_fee_reduces_cash_and_equity(self):
        report = self._with_fee(D("5.00"))
        self.assertEqual(report["closing_cash_usd"], "8995.00")
        self.assertEqual(report["closing_equity_usd"], "9995.00")
        self.assertEqual(report["fees"]["recorded_fees_usd"], "5.00")
        self.assertTrue(report["fees"]["fees_complete"])
        self.assertFalse(report["approximate"])

    def test_a_zero_fee_is_a_measurement(self):
        report = self._with_fee(D("0"))
        self.assertEqual(report["closing_equity_usd"], "10000.00")
        self.assertTrue(report["fees"]["fees_complete"])
        self.assertEqual(report["fees"]["unknown_fee_fill_count"], 0)
        self.assertFalse(report["approximate"])

    def test_an_unknown_fee_is_flagged_and_visibly_different(self):
        recorded = self._with_fee(D("5.00"))
        unknown = self._with_fee(None)

        self.assertNotEqual(recorded["closing_equity_usd"], unknown["closing_equity_usd"])
        self.assertFalse(unknown["fees"]["fees_complete"])
        self.assertEqual(unknown["fees"]["recorded_fees_usd"], "0.00")
        self.assertEqual(unknown["fees"]["unknown_fee_fill_count"], 1)
        self.assertIn("fees_incomplete", unknown["flags"])
        self.assertTrue(unknown["approximate"])
        self.assertIn(
            "fees_unrecorded_for_some_fills", unknown["methodology"]["approximate_reasons"]
        )


class ExternalFlowTests(SimpleTestCase):
    """A deposit changes equity, not performance."""

    def _run(self, flow_type: str, amount: str):
        return build_performance(
            opening=OpeningState(at=T0, cash=D("10000"), positions={}),
            end=day(3),
            flows=[ExternalFlow(at=day(2), flow_type=flow_type, amount=D(amount))],
        ).as_dict()

    def test_a_deposit_is_not_a_return(self):
        report = self._run("DEPOSIT", "5000")
        self.assertEqual(report["opening_equity_usd"], "10000.00")
        self.assertEqual(report["closing_equity_usd"], "15000.00")
        self.assertEqual(report["cumulative_return_pct"], "50.0000")
        self.assertEqual(report["time_weighted_return_pct"], "0.0000")
        self.assertEqual(report["twr"]["flow_boundary_count"], 1)
        self.assertEqual(report["twr"]["method"], TWR_METHOD)
        self.assertFalse(report["twr"]["approximate"])
        self.assertEqual(report["trade_count"], 0)
        self.assertEqual(report["external_flows"][0]["flow_type"], "DEPOSIT")
        cash = report["cash_reconciliation"]
        self.assertEqual(cash["status"], "matched")
        self.assertEqual(cash["deposits_usd"], "5000.00")
        self.assertEqual(cash["expected_closing_cash_usd"], "15000.00")

    def test_a_withdrawal_is_not_a_loss(self):
        report = self._run("WITHDRAWAL", "2500")
        self.assertEqual(report["closing_equity_usd"], "7500.00")
        self.assertEqual(report["cumulative_return_pct"], "-25.0000")
        self.assertEqual(report["time_weighted_return_pct"], "0.0000")
        self.assertEqual(report["external_flows"][0]["signed_usd"], "-2500.00")

    def test_a_non_usd_flow_is_refused_rather_than_mixed_in(self):
        with self.assertRaises(AccountingError):
            ExternalFlow(at=day(2), flow_type="DEPOSIT", amount=D("1000"), currency="EUR")


class TwrApproximationTests(SimpleTestCase):
    """The approximation flag must respond to boundary data, not be constant."""

    def setUp(self):
        self.opening = OpeningState(at=T0, cash=D("1000"), positions={"AAPL": D("10")})
        self.flow = ExternalFlow(at=day(1), flow_type="DEPOSIT", amount=D("1000"))

    def test_a_fresh_boundary_valuation_is_exact(self):
        report = build_performance(
            opening=self.opening,
            end=day(2),
            flows=[self.flow],
            marks=marks_for("AAPL", [(0, "100"), (1, "101"), (2, "102")]),
        ).as_dict()
        self.assertEqual(report["twr"]["method"], TWR_METHOD)
        self.assertFalse(report["twr"]["approximate"])
        self.assertFalse(report["approximate"])
        self.assertTrue(report["flow_boundaries"][0]["valuation_exact"])

    def test_a_stale_boundary_valuation_falls_back_and_flags_approximate(self):
        report = build_performance(
            opening=self.opening,
            end=day(2),
            flows=[self.flow],
            # No mark at the flow timestamp: the boundary would have to be
            # valued from a stale observation, which is not exact boundary data.
            marks=marks_for("AAPL", [(0, "100"), (2, "102")]),
        ).as_dict()
        self.assertEqual(report["twr"]["method"], "modified_dietz.v1")
        self.assertTrue(report["twr"]["approximate"])
        self.assertTrue(report["approximate"])
        self.assertIn("flow_boundary_valuation_not_exact", report["twr"]["reasons"])
        self.assertFalse(report["flow_boundaries"][0]["valuation_exact"])


class LlmCostTests(SimpleTestCase):
    def test_a_complete_cost_record_produces_an_explicit_net_series(self):
        report = build_performance(
            opening=OpeningState(at=T0, cash=D("1000"), positions={}),
            end=day(1),
            llm_costs=[LlmCost(at=T0, amount=D("0.50"), source="api")],
            llm_cost_missing_count=0,
        ).as_dict()
        self.assertTrue(report["llm_costs"]["costs_complete"])
        self.assertEqual(report["llm_costs"]["recorded_cost_usd"], "0.500000")
        self.assertIsNotNone(report["net_of_llm_return_pct"])
        self.assertEqual(len(report["llm_costs"]["equity_series_net_of_llm"]), 2)
        self.assertFalse(report["llm_costs"]["double_counted"])
        self.assertFalse(report["llm_costs"]["deducted_from_portfolio_cash"])

    def test_an_unreported_llm_cost_makes_the_net_return_null_not_zero(self):
        report = build_performance(
            opening=OpeningState(at=T0, cash=D("1000"), positions={}),
            end=day(1),
            llm_costs=[LlmCost(at=T0, amount=D("0.50"), source="api")],
            llm_cost_missing_count=2,
        ).as_dict()
        self.assertFalse(report["llm_costs"]["costs_complete"])
        self.assertEqual(report["llm_costs"]["missing_cost_run_count"], 2)
        self.assertIsNone(report["net_of_llm_return_pct"])
        self.assertIsNone(report["net_twr"]["return_pct"])
        self.assertIsNotNone(report["net_twr"]["known_cost_only_return_pct"])
        self.assertFalse(report["llm_costs"]["net_return_established"])
        self.assertIsNotNone(report["llm_costs"]["known_cost_only_return_pct"])
        self.assertIn("cannot be established", report["llm_costs"]["net_return_reason"])
        # The explicit series still exists and is labelled with known costs only.
        self.assertEqual(len(report["llm_costs"]["equity_series_net_of_llm"]), 2)


class MissingPriceTests(SimpleTestCase):
    def test_an_unpriced_position_makes_equity_null_not_zero(self):
        report = build_performance(
            opening=OpeningState(at=T0, cash=D("500"), positions={"AAPL": D("10")}),
            end=day(1),
        ).as_dict()
        self.assertIsNone(report["opening_equity_usd"])
        self.assertIsNone(report["closing_equity_usd"])
        self.assertIsNone(report["time_weighted_return_pct"])
        self.assertEqual(report["twr"]["method"], "unavailable")
        self.assertIn("unpriced_equity_points", report["flags"])
        point = report["equity_series"][0]
        self.assertIsNone(point["positions_value_usd"])
        self.assertIsNone(point["equity_usd"])
        self.assertEqual(point["unpriced_tickers"], ["AAPL"])


class PriceLeakageTests(SimpleTestCase):
    def test_a_price_before_the_decision_is_rejected(self):
        book = PriceBook([PriceMark("AAPL", day(0), D("100"), "prior_close")])
        selection = select_fill_price(ticker="AAPL", decision_at=day(1), marks=book)
        self.assertIsNone(selection.price)
        self.assertEqual(selection.rejected_earlier_marks, 1)
        self.assertEqual(selection.reason, "no_price_at_or_after_decision")
        with self.assertRaises(ExecutionPriceUnavailable):
            require_fill_price(ticker="AAPL", decision_at=day(1), marks=book)

    def test_the_guard_itself_raises_for_a_backdated_fill(self):
        with self.assertRaises(PriceLeakageError):
            assert_price_not_before(price_at=day(0), decision_at=day(1))

    def test_future_knowledge_cannot_value_an_earlier_instant(self):
        mark = PriceMark("AAPL", at=T0, price=D("100"), observed_at=day(3))
        book = PriceBook([mark])
        self.assertIsNone(book.mark_at_or_before("AAPL", T0))
        self.assertIsNotNone(book.mark_at_or_before("AAPL", day(3)))

        report = build_performance(
            opening=OpeningState(at=T0, cash=D("0"), positions={"AAPL": D("10")}),
            end=day(1),
            marks=[mark],
        ).as_dict()
        self.assertIsNone(report["opening_equity_usd"])
        self.assertIn("unpriced_equity_points", report["flags"])

    def test_no_lookahead_selection_respects_as_of(self):
        book = PriceBook(
            [
                PriceMark("AAPL", at=day(1), price=D("100"), observed_at=day(3)),
                PriceMark("AAPL", at=day(2), price=D("110"), observed_at=day(4)),
            ]
        )
        selection = select_fill_price(ticker="AAPL", decision_at=day(1), marks=book, as_of=day(2))
        self.assertIsNone(selection.price)
        selection_later = select_fill_price(
            ticker="AAPL", decision_at=day(1), marks=book, as_of=day(4)
        )
        self.assertEqual(selection_later.price, D("100"))


class WeekendMaturityTests(SimpleTestCase):
    """A Saturday horizon uses the next session; a missing session is null."""

    def setUp(self):
        self.friday = datetime(2026, 2, 6, 21, 0, tzinfo=UTC)

    def test_a_weekend_target_rolls_to_the_next_session(self):
        marks = [
            PriceMark("AAPL", at=self.friday, price=D("100"), source="friday_close"),
            PriceMark(
                "AAPL",
                at=datetime(2026, 2, 9, 21, 0, tzinfo=UTC),
                price=D("105"),
                source="monday_close",
            ),
        ]
        result = evaluate_horizon(
            ticker="AAPL", decision_at=self.friday, horizon_days=1, marks=marks
        )
        self.assertEqual(result.maturity, OutcomeMaturity.EVALUATED)
        self.assertEqual(result.price_at_horizon, D("105"))
        self.assertEqual(result.forward_return_pct, D("5.0000"))
        self.assertEqual(result.session_shift_days, 2)
        self.assertEqual(result.price_observed_at, datetime(2026, 2, 9, 21, 0, tzinfo=UTC))

    def test_a_weekend_with_no_next_session_is_unavailable_not_zero(self):
        result = evaluate_horizon(
            ticker="AAPL",
            decision_at=self.friday,
            horizon_days=1,
            marks=[PriceMark("AAPL", at=self.friday, price=D("100"))],
        )
        self.assertEqual(result.maturity, OutcomeMaturity.UNAVAILABLE)
        self.assertIsNone(result.price_at_horizon)
        self.assertIsNone(result.forward_return_pct)
        self.assertEqual(result.reason, "no_session_at_or_after_target")

    def test_no_entry_price_at_or_after_the_decision_is_rejected(self):
        result = evaluate_horizon(
            ticker="AAPL",
            decision_at=self.friday,
            horizon_days=1,
            marks=[PriceMark("AAPL", at=self.friday - DAY, price=D("99"))],
        )
        self.assertEqual(result.maturity, OutcomeMaturity.UNAVAILABLE)
        self.assertEqual(result.rejected_earlier_marks, 1)

    def test_as_of_cannot_use_a_future_session(self):
        monday = datetime(2026, 2, 9, 21, 0, tzinfo=UTC)
        result = evaluate_horizon(
            ticker="AAPL",
            decision_at=self.friday,
            horizon_days=1,
            marks=[
                PriceMark("AAPL", at=self.friday, price=D("100")),
                PriceMark("AAPL", at=monday, price=D("105")),
            ],
            as_of=datetime(2026, 2, 8, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(result.maturity, OutcomeMaturity.UNAVAILABLE)
        self.assertIsNone(result.forward_return_pct)


class OverlappingDecisionTests(SimpleTestCase):
    """Per-decision scores are not additive and never become account return."""

    def test_a_per_decision_series_refuses_every_summing_form(self):
        series = PerDecisionScoreSeries(
            observations=(observation(run_id="a"), observation(run_id="b"))
        )
        with self.assertRaises(MetricFamilyError):
            series.total()
        with self.assertRaises(MetricFamilyError):
            series.sum()
        with self.assertRaises(MetricFamilyError):
            series + series
        with self.assertRaises(MetricFamilyError):
            sum([series])

    def test_the_aggregate_publishes_no_total(self):
        rows = (observation(run_id="a"), observation(run_id="b"))
        summary = aggregate_observations(rows, horizon_days=7)
        self.assertEqual(summary["mean_action_score_pct"], "10.0000")
        self.assertFalse(summary["summable"])
        self.assertIn("sum_rejected_reason", summary)
        self.assertNotIn("total_action_score_pct", summary)
        self.assertNotIn("sum_action_score_pct", summary)

    def test_overlapping_scores_do_not_equal_the_account_return(self):
        # Two overlapping BUYs, each +10% on the instrument, with a flat account:
        # summing the scores would claim +20%; the account earned 0%.
        fills = [
            Fill(at=day(1), ticker="AAPL", side=TxType.BUY, quantity=D("10"), price=D("100")),
            Fill(at=day(2), ticker="AAPL", side=TxType.BUY, quantity=D("10"), price=D("100")),
        ]
        report = build_performance(
            opening=OpeningState(at=T0, cash=D("10000"), positions={}),
            end=day(5),
            fills=fills,
            marks=marks_for("AAPL", [(0, "100"), (1, "100"), (2, "100"), (5, "100")]),
        ).as_dict()
        score_sum = D("10") + D("10")
        self.assertEqual(report["time_weighted_return_pct"], "0.0000")
        self.assertNotEqual(D(report["time_weighted_return_pct"]), score_sum)
        self.assertEqual(report["metric_family"], "portfolio_performance")


class MissingBenchmarkTests(SimpleTestCase):
    def test_unmatched_rows_are_excluded_from_the_pairwise_average(self):
        matched = observation(
            run_id="with-benchmark",
            forward_return_pct=D("5"),
            action_score_pct=D("5"),
            benchmark_return_pct=D("2"),
            relative_forward_return_pct=D("3"),
            action_relative_return_pct=D("3"),
        )
        unmatched = observation(
            run_id="without-benchmark",
            forward_return_pct=D("-4"),
            action_score_pct=D("-4"),
            directional_hit=False,
            benchmark_return_pct=None,
            benchmark_matched=False,
            relative_forward_return_pct=None,
            action_relative_return_pct=None,
        )
        summary = aggregate_observations([matched, unmatched], horizon_days=7)
        self.assertEqual(summary["evaluated"], 2)
        self.assertEqual(summary["matched_benchmark_pairs"], 1)
        self.assertEqual(summary["unmatched_benchmark_rows_excluded"], 1)
        self.assertEqual(summary["mean_benchmark_return_pct"], "2.0000")
        self.assertEqual(summary["mean_relative_forward_return_pct"], "3.0000")
        # One matched BUY hit and one unmatched BUY miss: the hit rate still uses
        # both directional calls; only the benchmark average is pairwise.
        self.assertEqual(summary["hit_rate_pct"], "50.0")


class CounterfactualTests(SimpleTestCase):
    def setUp(self):
        self.marks = marks_for("AAPL", [(0, "100"), (1, "103"), (7, "95"), (30, "120")])

    def test_a_rejected_trade_alone_establishes_nothing(self):
        result = evaluate_counterfactual(
            ticker="AAPL",
            decision_at=T0,
            action="BUY",
            quantity=D("10"),
            marks=self.marks,
        ).as_dict()
        self.assertFalse(result["established"])
        self.assertFalse(result["established_by_rejection_alone"])
        self.assertIsNone(result["hypothetical_pnl_usd"])
        self.assertEqual(result["reason"], "no_declared_holding_exit_rule")

    def test_a_declared_rule_yields_a_hypothetical_and_a_horizon_range(self):
        result = evaluate_counterfactual(
            ticker="AAPL",
            decision_at=T0,
            action="BUY",
            quantity=D("10"),
            marks=self.marks,
            rule=CounterfactualRule(holding_period_days=7, horizons_days=(1, 7, 30)),
        ).as_dict()
        self.assertTrue(result["established"])
        self.assertEqual(result["entry"]["price_usd"], "100")
        self.assertEqual(result["exit"]["holding_days"], 7)
        self.assertEqual(result["hypothetical_return_pct"], "-5.0000")
        self.assertEqual(result["uncertainty"]["method"], "declared_horizon_sensitivity.v1")
        self.assertIsNone(result["uncertainty"]["confidence_interval"])
        self.assertFalse(result["established_by_rejection_alone"])
        self.assertIn("not executed performance", result["note"])


class LedgerIntegrationTests(TestCase):
    """Stored rows map onto exact decimals, with fee NULL distinct from zero."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("ledger-user"), balance="10000.00")
        self.t1 = T0
        self.t2 = day(1)

    def _transaction(self, *, side, amount, price, fee, timestamp):
        transaction = Transaction.objects.create(
            portfolio=self.portfolio,
            ticker="AAPL",
            tx_type=side,
            amount=D(amount),
            price=D(price),
            fee=fee,
            executed_by=ExecutedBy.USER,
        )
        Transaction.objects.filter(pk=transaction.pk).update(timestamp=timestamp)
        return transaction

    def test_fee_null_and_zero_survive_the_ledger_round_trip(self):
        self._transaction(side=TxType.BUY, amount="1", price="100", fee=None, timestamp=self.t1)
        self._transaction(
            side=TxType.BUY, amount="1", price="100", fee=D("0"), timestamp=self.t2 + DAY
        )
        fills = load_fills(self.portfolio)
        self.assertEqual([fill.fee for fill in fills], [None, D("0")])
        self.assertEqual([fill.fee_recorded for fill in fills], [False, True])

    def test_opening_cash_is_reverse_reconstructed_from_the_current_balance(self):
        self._transaction(side=TxType.BUY, amount="10", price="100", fee=D("5"), timestamp=self.t1)
        # A later sale changes today's balance; reversing its cash effect is what
        # recovers the cash at the earlier start of the window.
        self._transaction(side=TxType.SELL, amount="2", price="120", fee=D("0"), timestamp=self.t2)
        self.portfolio.balance_usd = D("9235.00")
        self.portfolio.save(update_fields=["balance_usd"])
        opening = derive_opening_state(self.portfolio, start=self.t1 + timedelta(hours=1))
        self.assertEqual(opening.cash, D("8995.00"))
        self.assertEqual(opening.positions, {"AAPL": D("10")})
        self.assertIn("cash_anchored_to_current_balance", opening.flags)

    def test_positions_replay_is_ordered_and_refuses_oversell(self):
        self._transaction(side=TxType.BUY, amount="2", price="100", fee=None, timestamp=self.t1)
        fills = load_fills(self.portfolio)
        self.assertEqual(positions_at(fills, self.t2), {"AAPL": D("2")})
        with self.assertRaises(NegativePositionError):
            positions_at(
                [
                    Fill(
                        at=self.t1,
                        ticker="MSFT",
                        side=TxType.SELL,
                        quantity=D("1"),
                        price=D("10"),
                    )
                ],
                self.t2,
            )
