"""P1 steps 3A-3C — the scheduler bounds work, not candidate decisions.

The acceptance review reproduced the starvation on the pre-fix tree: with a
batch limit of three, three newer decisions whose horizons were already scored
consumed every candidate slot, so an older decision with day 1 present and days
7 and 30 due was never selected. Observed before the repair::

    counts {'scored': 0, 'skipped': 9, 'pending': 0, 'unpriced': 0,
            'failed': 0, 'decisions': 3}
    older horizons: [1]

The scheduler now selects due ``(decision, horizon, method)`` work from the
indexed work ledger before applying ``limit``. Evaluated rows, terminal
unavailable rows and deferred retries are not candidates at all.

Money is ``Decimal``, timestamps are UTC, and an unavailable result stays a
measured absence rather than a zero.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from core.decision_models import (
    DecisionRecord,
    OutcomeEvaluation,
    OutcomeEvaluationState,
    OutcomeMaturity,
    OutcomeRecord,
    RunEvent,
    RunEventType,
)
from core.event_models import EventOutbox
from core.models import AgentDecisionLog, DecisionOutcome
from core.tests.helpers import TEST_CACHES, make_portfolio, make_user
from services.attribution import OUTCOME_METHOD_VERSION, evaluate_pending
from services.decision_runs import create_run

D = Decimal
HORIZONS = (1, 7, 30)


def price_history(points_by_ticker):
    from services.fundamentals import PriceHistory

    def loader(ticker, period="1y"):
        points = points_by_ticker.get(ticker, [])
        return PriceHistory(
            ticker=ticker,
            period=period,
            points=[(stamp, D(price)) for stamp, price in points],
            latest=D(points[-1][1]) if points else None,
        )

    return loader


def points(base, offsets_prices):
    return [
        ((base + timedelta(days=days)).date().isoformat(), price) for days, price in offsets_prices
    ]


def make_decision(portfolio, *, ticker="AAPL", created_at, action=None):
    decision = AgentDecisionLog.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        action_taken=action or f"BUY 1 {ticker}",
        reasoning="recorded reasoning",
    )
    AgentDecisionLog.objects.filter(pk=decision.pk).update(created_at=created_at)
    decision.refresh_from_db()
    run = create_run(
        portfolio=portfolio,
        ticker=ticker,
        protocol_version="test.v1",
        mode="recorded",
        source="fixture",
        legacy_log=decision,
        started_at=created_at,
    )
    DecisionRecord.objects.create(
        run=run,
        action="BUY",
        requested_quantity=D("1"),
        concise_rationale="Measured rationale.",
        engine_source="fixture",
    )
    return decision, run


def completed_outcome(decision, horizon, *, price_at_decision="100", price_at_horizon="101"):
    return DecisionOutcome.objects.create(
        decision=decision,
        horizon_days=horizon,
        ticker=decision.ticker,
        action="BUY",
        price_at_decision=D(price_at_decision),
        price_at_horizon=D(price_at_horizon),
        forward_return_pct=D("1"),
        decision_return_pct=D("1"),
        method_version="attribution.v1",
    )


@override_settings(CACHES=TEST_CACHES)
class FairBatchSchedulingTests(TestCase):
    """3A — newer completed decisions cannot starve an older due horizon."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("fair-batch"))
        self.now = timezone.now()
        self.older_base = self.now - timedelta(days=40)
        self.older, self.older_run = make_decision(
            self.portfolio, ticker="AAPL", created_at=self.older_base
        )
        completed_outcome(self.older, 1, price_at_decision="100", price_at_horizon="110")
        self.newer = []
        self.newer_runs = []
        for index in range(5):
            created_at = self.now - timedelta(days=10 - index)
            decision, run = make_decision(self.portfolio, ticker="MSFT", created_at=created_at)
            for horizon in HORIZONS:
                completed_outcome(decision, horizon)
            self.newer.append(decision)
            self.newer_runs.append(run)
        self.history = price_history(
            {
                "AAPL": points(
                    self.older_base,
                    [(0, "100"), (1, "110"), (7, "120"), (30, "150")],
                ),
                "SPY": points(
                    self.older_base,
                    [(0, "400"), (1, "401"), (7, "404"), (30, "420")],
                ),
                "MSFT": points(
                    self.older_base,
                    [(0, "100"), (1, "101"), (7, "107"), (30, "130")],
                ),
            }
        )

    def test_3a_the_older_decisions_due_horizons_are_scheduled(self):
        with patch("services.fundamentals.get_price_history", side_effect=self.history):
            counts = evaluate_pending(now=self.now, limit=3)

        self.assertEqual(counts["scored"], 2)
        self.assertEqual(
            sorted(
                DecisionOutcome.objects.filter(decision=self.older).values_list(
                    "horizon_days", flat=True
                )
            ),
            [1, 7, 30],
        )
        # The newer decisions were already complete; the batch did not "work"
        # on them, so their rows did not change and no event was appended.
        self.assertEqual(
            DecisionOutcome.objects.filter(decision__in=self.newer).count(),
            len(self.newer) * len(HORIZONS),
        )
        self.assertEqual(
            RunEvent.objects.filter(
                run__in=[self.older_run], event_type=RunEventType.OUTCOME_RECORDED
            )
            .values("payload__horizon_days")
            .distinct()
            .count(),
            2,
        )
        self.assertFalse(
            RunEvent.objects.filter(
                run__in=self.newer_runs, event_type=RunEventType.OUTCOME_RECORDED
            ).exists()
        )


@override_settings(CACHES=TEST_CACHES)
class IneligiblePairBatchTests(TestCase):
    """3B — completed, deferred and terminal pairs cannot consume a work slot."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("ineligible-batch"))
        self.now = timezone.now()

    def test_3b_deferred_evaluated_and_unavailable_pairs_do_not_consume_slots(self):
        now = self.now
        # One eligible decision, older than every ineligible one. The pre-fix
        # newest-first decision limit selected an ineligible newer decision.
        eligible, _run = make_decision(
            self.portfolio, ticker="AAPL", created_at=now - timedelta(days=30)
        )
        mature_at = eligible.created_at + timedelta(days=1)
        OutcomeEvaluation.objects.create(
            decision=eligible,
            horizon_days=1,
            method_version=OUTCOME_METHOD_VERSION,
            state=OutcomeEvaluationState.PENDING,
            next_attempt_at=mature_at,
            max_attempts=3,
            reference_basis="proposal_close",
            reference_at=eligible.created_at,
        )

        ineligible = []
        for ticker, state, next_attempt_at in (
            ("DEFER", OutcomeEvaluationState.MATURED, now + timedelta(hours=2)),
            ("DONE", OutcomeEvaluationState.EVALUATED, None),
            ("TERM", OutcomeEvaluationState.UNAVAILABLE, None),
        ):
            decision, _run = make_decision(
                self.portfolio, ticker=ticker, created_at=now - timedelta(days=2)
            )
            ineligible.append(decision)
            for horizon in HORIZONS:
                OutcomeEvaluation.objects.create(
                    decision=decision,
                    horizon_days=horizon,
                    method_version=OUTCOME_METHOD_VERSION,
                    state=state,
                    next_attempt_at=next_attempt_at,
                    max_attempts=3,
                    reference_basis="proposal_close",
                    reference_at=decision.created_at,
                )
        with patch(
            "services.fundamentals.get_price_history",
            side_effect=price_history(
                {
                    "AAPL": points(
                        eligible.created_at,
                        [(0, "100"), (1, "110"), (2, "111")],
                    ),
                    "SPY": points(
                        eligible.created_at,
                        [(0, "400"), (1, "401"), (2, "402")],
                    ),
                }
            ),
        ):
            counts = evaluate_pending(now=now, limit=1)

        self.assertEqual(counts["scored"], 1)
        self.assertTrue(DecisionOutcome.objects.filter(decision=eligible, horizon_days=1).exists())
        self.assertFalse(DecisionOutcome.objects.filter(decision__in=ineligible).exists())
        deferred = OutcomeEvaluation.objects.get(decision=ineligible[0], horizon_days=1)
        self.assertEqual(deferred.next_attempt_at, now + timedelta(hours=2))
        self.assertEqual(
            OutcomeEvaluation.objects.get(decision=ineligible[1], horizon_days=1).state,
            OutcomeEvaluationState.EVALUATED,
        )
        self.assertEqual(
            OutcomeEvaluation.objects.get(decision=ineligible[2], horizon_days=1).state,
            OutcomeEvaluationState.UNAVAILABLE,
        )


@override_settings(CACHES=TEST_CACHES)
class ProjectionAlreadyPresentTests(TestCase):
    """3A — a completed detail projection is a current day-one result."""

    def test_3a_a_completed_projection_is_not_re_measured(self):
        portfolio = make_portfolio(make_user("projection-present"))
        now = timezone.now()
        base = now - timedelta(days=40)
        decision, run = make_decision(portfolio, ticker="AAPL", created_at=base)
        OutcomeRecord.objects.create(
            decision=run.decision,
            horizon_days=1,
            method_version=OUTCOME_METHOD_VERSION,
            maturity=OutcomeMaturity.EVALUATED,
            price_at_decision=D("100"),
            price_at_horizon=D("110"),
            forward_return_pct=D("10"),
            decision_return_pct=D("10"),
            measurement_basis="proposal_close",
            reference_at=base,
            evaluated_at=base + timedelta(days=1),
        )
        history = price_history(
            {
                "AAPL": points(base, [(0, "100"), (1, "110"), (7, "120"), (30, "150")]),
                "SPY": points(base, [(0, "400"), (1, "401"), (7, "404"), (30, "420")]),
            }
        )
        with patch("services.fundamentals.get_price_history", side_effect=history):
            counts = evaluate_pending(now=now, limit=1)

        self.assertEqual(counts["scored"], 1)
        self.assertEqual(
            sorted(
                DecisionOutcome.objects.filter(decision=decision).values_list(
                    "horizon_days", flat=True
                )
            ),
            [7],
        )
        evaluation = OutcomeEvaluation.objects.get(decision=decision, horizon_days=1)
        self.assertEqual(evaluation.state, OutcomeEvaluationState.EVALUATED)
        self.assertIsNone(evaluation.outcome_id)


@override_settings(CACHES=TEST_CACHES)
class OverlappingTickAndBackfillTests(TransactionTestCase):
    """3C — overlapping ticks and a repeated backfill leave one current result."""

    def setUp(self):
        self.portfolio = make_portfolio(make_user("overlap"))
        self.now = timezone.now()
        self.base = self.now - timedelta(days=40)
        self.decision, self.run = make_decision(self.portfolio, ticker="AAPL", created_at=self.base)
        self.history = price_history(
            {
                "AAPL": points(
                    self.base,
                    [(0, "100"), (1, "110"), (7, "120"), (30, "150")],
                ),
                "SPY": points(
                    self.base,
                    [(0, "400"), (1, "401"), (7, "404"), (30, "420")],
                ),
            }
        )

    def _tick(self, barrier, errors):
        try:
            barrier.wait(timeout=10)
            evaluate_pending(now=self.now)
        except Exception as exc:  # pragma: no cover - only a failing assertion needs it
            errors.append(exc)
        finally:
            connection.close()

    def test_3c_overlapping_ticks_and_a_repeated_backfill_are_idempotent(self):
        errors = []
        barrier = threading.Barrier(2)
        threads = [
            threading.Thread(target=self._tick, args=(barrier, errors), daemon=True)
            for _ in range(2)
        ]
        with patch("services.fundamentals.get_price_history", side_effect=self.history):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            self.assertTrue(
                all(not thread.is_alive() for thread in threads),
                "an overlapping tick did not finish",
            )

            self.assertEqual(DecisionOutcome.objects.filter(decision=self.decision).count(), 3)
            current = OutcomeRecord.objects.filter(
                decision=self.run.decision, method_version=OUTCOME_METHOD_VERSION
            )
            self.assertEqual(sorted(current.values_list("horizon_days", flat=True)), [1, 7, 30])
            self.assertEqual(current.values("horizon_days").distinct().count(), 3)
            events = RunEvent.objects.filter(run=self.run, event_type=RunEventType.OUTCOME_RECORDED)
            self.assertEqual(events.count(), 3)
            self.assertEqual(
                EventOutbox.objects.filter(
                    payload__event_type=RunEventType.OUTCOME_RECORDED
                ).count(),
                3,
            )
            revisions = {row.horizon_days: row.revision for row in current}
            event_count = events.count()
            outcome_count = DecisionOutcome.objects.filter(decision=self.decision).count()

            out = StringIO()
            call_command("backfill_outcome_records", "--apply", "--json", stdout=out)
            second = StringIO()
            call_command("backfill_outcome_records", "--apply", "--json", stdout=second)

        self.assertEqual(errors, [])
        self.assertEqual(
            DecisionOutcome.objects.filter(decision=self.decision).count(), outcome_count
        )
        self.assertEqual(
            RunEvent.objects.filter(run=self.run, event_type=RunEventType.OUTCOME_RECORDED).count(),
            event_count,
        )
        self.assertEqual(
            {
                row.horizon_days: row.revision
                for row in OutcomeRecord.objects.filter(
                    decision=self.run.decision, method_version=OUTCOME_METHOD_VERSION
                )
            },
            revisions,
        )
