"""FIX-02 acceptance: every trade is revalidated under the lock.

These are real interleavings, not two sequential calls. Each worker connection
performs its *pre-execution* phase (the read-only guard simulation a caller does
before asking to execute), meets at a barrier, and only then calls the execution
service. The portfolio row lock serialises the second worker, which must see the
first worker's committed state and refuse to add the second decision on top.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from core.models import Asset, Portfolio, Transaction, TxType
from core.tests.helpers import TEST_CACHES, make_portfolio
from services.execution import (
    ExecutionRequest,
    MaterialChangeError,
    PolicyChangedError,
    ValuationUnavailableError,
    apply_trade,
    evaluate_proposal,
    execute_trade,
    portfolio_policy_version,
)
from services.ledger import PositionState, realised_pnl_today
from services.market_data import PriceQuote
from services.portfolio_metrics import build_exposure

D = Decimal


def quote(price: str, *, as_of=None) -> PriceQuote:
    return PriceQuote(
        ticker="X",
        symbol="X",
        price=D(price),
        source="test",
        as_of=as_of or timezone.now(),
    )


def make_portfolio_with(**kwargs) -> Portfolio:
    kwargs.setdefault("autonomous", True)
    return make_portfolio(**kwargs)


def predecision(portfolio: Portfolio, ticker: str, quantity: str, price: str):
    """The read-only phase a caller runs before asking for execution."""
    quotes = {ticker: quote(price)}
    asset = portfolio.assets.filter(ticker=ticker).first()
    position = PositionState(
        ticker=ticker,
        amount=asset.amount if asset is not None else D("0"),
        avg_cost=asset.avg_purchase_price if asset is not None else D("0"),
    )
    proposal = type("P", (), {"action": "BUY", "amount": D(quantity)})()
    return evaluate_proposal(
        portfolio,
        proposal,
        D(price),
        position,
        realised_pnl_today(portfolio),
        exposure=build_exposure(portfolio, ticker, price_map=quotes),
    )


def request_for(
    portfolio: Portfolio,
    ticker: str,
    quantity: Decimal,
    price: str,
    *,
    key: str,
    policy_version: str | None = None,
    approved_price: str | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        portfolio_id=portfolio.id,
        ticker=ticker,
        side="BUY",
        quantity=quantity,
        quotes={ticker: quote(price)},
        execution_key=key,
        source="autonomous",
        approval_ref=key,
        approved_policy_version=policy_version,
        approved_price=D(approved_price or price),
    )


def run_worker(fn, results: list, connections_closed=None):  # pragma: no cover - helper
    close_old_connections()
    try:
        fn()
    except Exception as exc:
        results.append(("error", exc))
    finally:
        close_old_connections()
        connections.close_all()


@override_settings(CACHES=TEST_CACHES)
class ConcentrationRegressionTests(TransactionTestCase):
    """20% existing + two independently approved 10% additions."""

    barrier = 4.0

    def setUp(self):
        # 20 shares at 100 = 20% of a 10,000 equity portfolio.
        self.portfolio = make_portfolio_with(
            balance="8000.00", allocation_pct="99.99", loss_limit="500.00"
        )
        self.portfolio.max_portfolio_exposure_pct = D("100.00")
        self.portfolio.max_ticker_concentration_pct = D("30.00")
        self.portfolio.save(
            update_fields=["max_portfolio_exposure_pct", "max_ticker_concentration_pct"]
        )
        Asset.objects.create(
            portfolio=self.portfolio, ticker="AAPL", amount=D("20"), avg_purchase_price=D("100")
        )
        self.policy_version = portfolio_policy_version(self.portfolio)

    def test_20_plus_10_plus_10_cannot_reach_40_percent(self):
        barrier = threading.Barrier(2)
        results: list = []

        def worker(key: str):
            close_old_connections()
            try:
                portfolio = Portfolio.objects.get(pk=self.portfolio.pk)
                decision = predecision(portfolio, "AAPL", "10", "100")
                assert decision.approved, decision.reason
                barrier.wait(timeout=self.barrier)
                try:
                    result = execute_trade(
                        request_for(
                            portfolio,
                            "AAPL",
                            decision.amount,
                            "100",
                            key=key,
                            policy_version=self.policy_version,
                        )
                    )
                    results.append(("executed" if result.approved else "rejected", result))
                except Exception as exc:
                    results.append(("rejected", exc))
            finally:
                close_old_connections()
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(f"add-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "an execution thread did not finish")

        portfolio = Portfolio.objects.get(pk=self.portfolio.pk)
        asset = Asset.objects.get(portfolio=portfolio, ticker="AAPL")
        equity = portfolio.balance_usd + asset.amount * D("100")
        self.assertLessEqual(asset.amount * D("100"), equity * D("0.30"))
        self.assertGreaterEqual(portfolio.balance_usd, D("0"))
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 1)
        kinds = sorted(kind for kind, _ in results)
        self.assertEqual(kinds, ["executed", "rejected"])
        rejection = next(value for kind, value in results if kind == "rejected")
        self.assertFalse(getattr(rejection, "approved", True))


@override_settings(CACHES=TEST_CACHES)
class ExposureAndCashContentionTests(TransactionTestCase):
    barrier = 4.0

    def test_different_tickers_cannot_together_breach_total_exposure(self):
        portfolio = make_portfolio_with(balance="10000.00", allocation_pct="99.99")
        portfolio.max_portfolio_exposure_pct = D("30.00")
        portfolio.max_ticker_concentration_pct = D("30.00")
        portfolio.save(update_fields=["max_portfolio_exposure_pct", "max_ticker_concentration_pct"])
        policy_version = portfolio_policy_version(portfolio)
        barrier = threading.Barrier(2)
        results: list = []

        def worker(ticker: str, key: str):
            close_old_connections()
            try:
                local = Portfolio.objects.get(pk=portfolio.pk)
                decision = predecision(local, ticker, "20", "100")
                assert decision.approved, decision.reason
                barrier.wait(timeout=self.barrier)
                try:
                    result = execute_trade(
                        ExecutionRequest(
                            portfolio_id=local.id,
                            ticker=ticker,
                            side="BUY",
                            quantity=decision.amount,
                            quotes={ticker: quote("100")},
                            execution_key=key,
                            approved_policy_version=policy_version,
                            approved_price=D("100"),
                        )
                    )
                    results.append(("executed" if result.approved else "rejected", result))
                except Exception as exc:
                    results.append(("rejected", exc))
            finally:
                close_old_connections()
                connections.close_all()

        threads = [
            threading.Thread(target=worker, args=(ticker, f"exposure-{ticker}"))
            for ticker in ("MSFT", "JPM")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "an execution thread did not finish")

        portfolio.refresh_from_db()
        positions = sum((asset.amount * D("100") for asset in portfolio.assets.all()), D("0"))
        equity = portfolio.balance_usd + positions
        self.assertLessEqual(positions, equity * D("0.30"))
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 1)
        self.assertEqual(sorted(kind for kind, _ in results), ["executed", "rejected"])

    def test_two_approved_buys_cannot_spend_the_same_cash(self):
        portfolio = make_portfolio_with(balance="1000.00", allocation_pct="99.99")
        portfolio.max_portfolio_exposure_pct = D("100.00")
        portfolio.max_ticker_concentration_pct = D("100.00")
        portfolio.save(update_fields=["max_portfolio_exposure_pct", "max_ticker_concentration_pct"])
        policy_version = portfolio_policy_version(portfolio)
        barrier = threading.Barrier(2)
        results: list = []

        def worker(key: str):
            close_old_connections()
            try:
                local = Portfolio.objects.get(pk=portfolio.pk)
                decision = predecision(local, "AAPL", "8", "100")
                assert decision.approved, decision.reason
                barrier.wait(timeout=self.barrier)
                try:
                    result = execute_trade(
                        request_for(
                            local,
                            "AAPL",
                            decision.amount,
                            "100",
                            key=key,
                            policy_version=policy_version,
                        )
                    )
                    results.append(("executed" if result.approved else "rejected", result))
                except Exception as exc:
                    results.append(("rejected", exc))
            finally:
                close_old_connections()
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(f"cash-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "an execution thread did not finish")

        portfolio.refresh_from_db()
        self.assertGreaterEqual(portfolio.balance_usd, D("0"))
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 1)
        self.assertEqual(sorted(kind for kind, _ in results), ["executed", "rejected"])


@override_settings(CACHES=TEST_CACHES)
class ChangedInputBetweenApprovalAndExecutionTests(TransactionTestCase):
    def _approved(self, portfolio: Portfolio, quantity="5", price="100"):
        decision = predecision(portfolio, "AAPL", quantity, price)
        assert decision.approved, decision.reason
        return decision

    def test_policy_change_after_approval_requires_a_new_decision(self):
        portfolio = make_portfolio_with(balance="10000.00", allocation_pct="99.99")
        policy_version = portfolio_policy_version(portfolio)
        decision = self._approved(portfolio)
        approved_barrier = threading.Barrier(2)
        changed_barrier = threading.Barrier(2)
        results: list = []

        def worker():
            close_old_connections()
            try:
                approved_barrier.wait(timeout=5)
                changed_barrier.wait(timeout=5)
                try:
                    results.append(
                        (
                            "executed",
                            execute_trade(
                                request_for(
                                    Portfolio.objects.get(pk=portfolio.pk),
                                    "AAPL",
                                    decision.amount,
                                    "100",
                                    key="policy-change",
                                    policy_version=policy_version,
                                )
                            ),
                        )
                    )
                except Exception as exc:
                    results.append(("error", exc))
            finally:
                close_old_connections()
                connections.close_all()

        thread = threading.Thread(target=worker)
        thread.start()
        approved_barrier.wait(timeout=5)
        portfolio.max_ticker_concentration_pct = D("1.00")
        portfolio.save(update_fields=["max_ticker_concentration_pct"])
        changed_barrier.wait(timeout=5)
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive(), "the execution thread did not finish")

        self.assertEqual([kind for kind, _ in results], ["error"])
        self.assertIsInstance(results[0][1], PolicyChangedError)
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 0)

    def test_daily_loss_change_under_lock_blocks_new_risk(self):
        portfolio = make_portfolio_with(
            balance="10000.00", allocation_pct="99.99", loss_limit="100"
        )
        decision = self._approved(portfolio)
        Asset.objects.create(
            portfolio=portfolio, ticker="AAPL", amount=D("10"), avg_purchase_price=D("100")
        )
        Transaction.objects.create(
            portfolio=portfolio,
            ticker="AAPL",
            tx_type=TxType.BUY,
            amount=D("10"),
            price=D("100"),
            executed_by="AI",
        )
        approved_barrier = threading.Barrier(2)
        changed_barrier = threading.Barrier(2)
        results: list = []

        def worker():
            close_old_connections()
            try:
                approved_barrier.wait(timeout=5)
                changed_barrier.wait(timeout=5)
                try:
                    results.append(
                        (
                            "approved",
                            execute_trade(
                                request_for(
                                    Portfolio.objects.get(pk=portfolio.pk),
                                    "AAPL",
                                    decision.amount,
                                    "100",
                                    key="daily-loss",
                                )
                            ),
                        )
                    )
                except Exception as exc:
                    results.append(("error", exc))
            finally:
                close_old_connections()
                connections.close_all()

        thread = threading.Thread(target=worker)
        thread.start()
        approved_barrier.wait(timeout=5)
        # A realised loss larger than the daily ceiling, written after approval.
        Transaction.objects.create(
            portfolio=portfolio,
            ticker="AAPL",
            tx_type=TxType.SELL,
            amount=D("10"),
            price=D("50"),
            executed_by="AI",
        )
        changed_barrier.wait(timeout=5)
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive(), "the execution thread did not finish")

        self.assertEqual([kind for kind, _ in results], ["approved"])
        result = results[0][1]
        self.assertFalse(result.approved)
        self.assertIn("daily loss", result.reason.lower())
        self.assertEqual(
            Transaction.objects.filter(portfolio=portfolio, tx_type=TxType.BUY).count(), 1
        )

    def test_missing_full_valuation_refuses_new_risk(self):
        portfolio = make_portfolio_with(balance="10000.00", allocation_pct="99.99")
        Asset.objects.create(
            portfolio=portfolio, ticker="AAPL", amount=D("10"), avg_purchase_price=D("100")
        )
        with self.assertRaises(ValuationUnavailableError):
            execute_trade(
                ExecutionRequest(
                    portfolio_id=portfolio.id,
                    ticker="MSFT",
                    side="BUY",
                    quantity=D("1"),
                    quotes={"MSFT": quote("100")},
                    execution_key="unpriced",
                    approved_price=D("100"),
                )
            )
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 0)

    def test_a_risk_reducing_sale_is_allowed_when_another_holding_is_unpriced(self):
        portfolio = make_portfolio_with(balance="1000.00", allocation_pct="99.99")
        Asset.objects.create(
            portfolio=portfolio, ticker="AAPL", amount=D("10"), avg_purchase_price=D("100")
        )
        Asset.objects.create(
            portfolio=portfolio, ticker="MSFT", amount=D("1"), avg_purchase_price=D("200")
        )
        decision = evaluate_proposal(
            portfolio,
            type("P", (), {"action": "SELL", "amount": D("5")})(),
            D("100"),
            PositionState(ticker="AAPL", amount=D("10"), avg_cost=D("100")),
            realised_pnl_today(portfolio),
            exposure=None,
        )
        self.assertTrue(decision.approved, decision.reason)
        result = execute_trade(
            ExecutionRequest(
                portfolio_id=portfolio.id,
                ticker="AAPL",
                side="SELL",
                quantity=D("5"),
                quotes={"AAPL": quote("100")},
                execution_key="risk-reducing",
                approved_price=D("100"),
                require_full_valuation=False,
            )
        )
        self.assertTrue(result.approved)
        self.assertEqual(Asset.objects.get(portfolio=portfolio, ticker="AAPL").amount, D("5"))
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 1)

    def test_a_constructed_guard_decision_cannot_bypass_revalidation(self):
        portfolio = make_portfolio_with(balance="10000.00", allocation_pct="1.00")
        constructed = type(
            "Decision",
            (),
            {
                "approved": True,
                "action": "BUY",
                "amount": D("100"),
                "price": D("100"),
                "notional": D("10000"),
                "clamped": False,
                "notes": [],
                "reason": "hand-built",
                "policy_version": "",
                "quote_time": timezone.now(),
                "requested_amount": D("100"),
            },
        )()
        with self.assertRaises(MaterialChangeError):
            apply_trade(portfolio.id, "AAPL", constructed)
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 0)


@override_settings(CACHES=TEST_CACHES)
class DuplicateDeliveryTests(TransactionTestCase):
    def test_duplicate_celery_delivery_writes_exactly_one_ledger_row(self):
        portfolio = make_portfolio_with(balance="10000.00", allocation_pct="99.99")
        decision = predecision(portfolio, "AAPL", "5", "100")
        barrier = threading.Barrier(2)
        results: list = []

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                request = request_for(
                    Portfolio.objects.get(pk=portfolio.pk),
                    "AAPL",
                    decision.amount,
                    "100",
                    key="celery:task-123",
                )
                results.append(("ok", execute_trade(request)))
            except Exception as exc:
                results.append(("error", exc))
            finally:
                close_old_connections()
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "an execution thread did not finish")

        self.assertEqual([kind for kind, _ in results], ["ok", "ok"])
        executed = [value for _kind, value in results]
        self.assertEqual(sum(1 for value in executed if value.already_executed), 1)
        first = executed[0].transaction
        self.assertTrue(all(value.transaction.id == first.id for value in executed))
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 1)

    def test_same_key_with_a_different_request_is_a_conflict(self):
        from services.execution import ExecutionConflict

        portfolio = make_portfolio_with(balance="10000.00", allocation_pct="99.99")
        decision = predecision(portfolio, "AAPL", "5", "100")
        execute_trade(request_for(portfolio, "AAPL", decision.amount, "100", key="celery:conflict"))
        with self.assertRaises(ExecutionConflict):
            execute_trade(request_for(portfolio, "AAPL", D("4"), "100", key="celery:conflict"))
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 1)


@override_settings(CACHES=TEST_CACHES)
class ExecutionKeyConstraintTests(TestCase):
    def test_the_database_refuses_two_rows_with_the_same_execution_key(self):
        from django.db import IntegrityError

        portfolio = make_portfolio_with(balance="10000.00")
        for _ in range(2):
            pass
        Transaction.objects.create(
            portfolio=portfolio,
            ticker="AAPL",
            tx_type=TxType.BUY,
            amount=D("1"),
            price=D("100"),
            executed_by="AI",
            execution_key="one-key",
        )
        with self.assertRaises(IntegrityError):
            Transaction.objects.create(
                portfolio=portfolio,
                ticker="AAPL",
                tx_type=TxType.BUY,
                amount=D("1"),
                price=D("100"),
                executed_by="AI",
                execution_key="one-key",
            )

    def test_legacy_rows_without_a_key_are_allowed(self):
        portfolio = make_portfolio_with(balance="10000.00")
        for _ in range(2):
            Transaction.objects.create(
                portfolio=portfolio,
                ticker="AAPL",
                tx_type=TxType.BUY,
                amount=D("1"),
                price=D("100"),
                executed_by="AI",
            )
        self.assertEqual(Transaction.objects.filter(portfolio=portfolio).count(), 2)
