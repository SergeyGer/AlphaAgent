"""Phase 1 decision-run tests: schema contracts, services, API and fixtures.

The style follows the rest of ``core/tests``: plain ``TestCase`` for the model
layer, ``APITestCase`` plus ``auth_client`` for the read API, and typed-error
assertions rather than ``ValueError``/``assertRaises(Exception)``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from core.decision_errors import (
    ClaimValidationError,
    EvidenceNotFound,
    IllegalTransition,
    RebuttalValidationError,
    SnapshotImmutableError,
)
from core.decision_json import content_hash
from core.decision_models import (
    ClaimStance,
    ClaimType,
    DecisionRecord,
    DecisionRun,
    DecisionStatus,
    EvidenceItem,
    GuardRecord,
    InputSnapshot,
    OutcomeMaturity,
    OutcomeRecord,
    RunEvent,
    RunEventType,
    RunMode,
    RunSource,
    RunStage,
)
from core.tests.helpers import (
    TEST_CACHES,
    auth_client,
    make_portfolio,
    make_user,
)
from services.decision_runs import (
    append_event,
    create_run,
    record_failure,
    stage_states,
    transition_status,
)
from services.evidence import (
    create_claim,
    create_evidence_item,
    create_rebuttal,
    create_snapshot,
)

# A fixed instant keeps every fixture in this module independent of the clock.
CUTOFF = datetime(2026, 2, 3, 14, 30, tzinfo=UTC)
NOW = datetime(2026, 2, 3, 14, 31, tzinfo=UTC)


def make_run(portfolio=None, *, mode=RunMode.SYNTHETIC, source=RunSource.FIXTURE, fixture_id=""):
    portfolio = portfolio or make_portfolio()
    snapshot = create_snapshot(
        portfolio=portfolio,
        portfolio_version="v1",
        analysis_cutoff=CUTOFF,
        captured_at=CUTOFF - timedelta(seconds=5),
        holdings=[],
        cash={"balance_usd": "10000.00", "currency": "USD"},
        quotes={"AAPL": {"price_usd": "225.00", "currency": "USD"}},
    )
    run = create_run(
        portfolio=portfolio,
        ticker="AAPL",
        protocol_version="test.v1",
        mode=mode,
        source=source,
        snapshot=snapshot,
        started_at=CUTOFF,
        fixture_id=fixture_id,
    )
    return run, snapshot


def add_evidence(
    snapshot, *, published_at=None, retrieved_at=None, excerpt="evidence", **overrides
):
    return create_evidence_item(
        snapshot=snapshot,
        source_type=overrides.pop("source_type", "news_article"),
        retrieved_at=retrieved_at or (CUTOFF - timedelta(hours=2)),
        published_at=published_at,
        excerpt=excerpt,
        is_redistributable=overrides.pop("is_redistributable", False),
        **overrides,
    )


def add_claim(
    run, evidence, *, key="claim", agent_role="bull", stance=ClaimStance.BULL, **overrides
):
    return create_claim(
        run=run,
        agent_role=agent_role,
        stance=stance,
        statement=overrides.pop("statement", f"{key} statement"),
        claim_type=overrides.pop("claim_type", ClaimType.OBSERVED_FACT),
        evidence_ids=[evidence.id],
        **overrides,
    )


class RunEventModelTests(TestCase):
    def setUp(self):
        self.run, self.snapshot = make_run()

    def test_sequence_is_unique_per_run(self):
        self.assertEqual(RunEvent.objects.filter(run=self.run).count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RunEvent.objects.create(
                run=self.run,
                sequence=1,
                event_type=RunEventType.STATUS_CHANGED,
                stage=RunStage.QUEUED,
                occurred_at=NOW,
            )

    def test_event_id_is_unique_across_runs(self):
        other_run, _ = make_run()
        event_id = uuid4()
        RunEvent.objects.create(
            run=other_run,
            sequence=2,
            event_id=event_id,
            event_type=RunEventType.STAGE_STARTED,
            stage=RunStage.COLLECTING,
            occurred_at=NOW,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            RunEvent.objects.create(
                run=self.run,
                sequence=99,
                event_id=event_id,
                event_type=RunEventType.STAGE_STARTED,
                stage=RunStage.COLLECTING,
                occurred_at=NOW,
            )

    def test_append_event_allocates_contiguous_sequences(self):
        first = append_event(
            self.run,
            RunEventType.STAGE_STARTED,
            stage=RunStage.COLLECTING,
            occurred_at=NOW,
        )
        second = append_event(
            self.run,
            RunEventType.STAGE_COMPLETED,
            stage=RunStage.COLLECTING,
            occurred_at=NOW,
        )
        self.assertEqual([first.sequence, second.sequence], [2, 3])
        self.assertIsNotNone(first.persisted_at)
        self.assertEqual(list(self.run.events.values_list("sequence", flat=True)), [1, 2, 3])

    def test_illegal_transition_is_refused(self):
        with self.assertRaises(IllegalTransition):
            transition_status(self.run, DecisionStatus.COMPLETED)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, DecisionStatus.QUEUED)

    def test_legal_transition_stamps_completion_and_latency(self):
        for target in (
            DecisionStatus.COLLECTING,
            DecisionStatus.ANALYSING,
            DecisionStatus.ADJUDICATING,
            DecisionStatus.VALIDATING,
        ):
            transition_status(self.run, target, occurred_at=NOW)

        transition_status(
            self.run, DecisionStatus.COMPLETED, occurred_at=NOW + timedelta(minutes=2)
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, DecisionStatus.COMPLETED)
        self.assertIsNotNone(self.run.completed_at)
        self.assertEqual(self.run.latency_ms, 180_000)
        self.assertTrue(self.run.is_terminal)

    def test_stage_states_mark_skipped_rebuttal_and_degraded_analysis(self):
        for target in (
            DecisionStatus.COLLECTING,
            DecisionStatus.ANALYSING,
            DecisionStatus.ADJUDICATING,
            DecisionStatus.VALIDATING,
            DecisionStatus.COMPLETED,
        ):
            transition_status(self.run, target, occurred_at=NOW)
        append_event(
            self.run,
            RunEventType.STAGE_FAILED,
            stage=RunStage.ANALYSING,
            occurred_at=NOW,
            payload={"reason": "analyst estimate feed unavailable"},
        )

        states = {item["stage"]: item for item in stage_states(self.run)["stages"]}
        self.assertEqual(states["rebutting"]["state"], "not_run")
        self.assertEqual(states["analysing"]["state"], "degraded")
        self.assertEqual(states["analysing"]["limitation"], "analyst estimate feed unavailable")
        self.assertEqual(states["awaiting_approval"]["state"], "not_run")


class InputSnapshotTests(TestCase):
    def test_snapshot_is_immutable_after_creation(self):
        portfolio = make_portfolio()
        snapshot = create_snapshot(
            portfolio=portfolio,
            portfolio_version="v1",
            analysis_cutoff=CUTOFF,
            captured_at=CUTOFF - timedelta(seconds=5),
            holdings=[{"ticker": "AAPL", "quantity": "1.0"}],
            cash={"balance_usd": "1000.00"},
            quotes={"AAPL": {"price_usd": "225.00"}},
        )
        original_hash = snapshot.content_hash

        snapshot.holdings = [{"ticker": "AAPL", "quantity": "999.0"}]
        with self.assertRaises(SnapshotImmutableError):
            snapshot.save()

        stored = InputSnapshot.objects.get(pk=snapshot.pk)
        self.assertEqual(stored.holdings, [{"ticker": "AAPL", "quantity": "1.0"}])
        self.assertEqual(stored.content_hash, original_hash)

    def test_snapshot_hash_is_stable_for_the_same_payload(self):
        portfolio = make_portfolio()
        kwargs = {
            "portfolio": portfolio,
            "portfolio_version": "v1",
            "analysis_cutoff": CUTOFF,
            "captured_at": CUTOFF - timedelta(seconds=5),
            "holdings": [],
            "cash": {"balance_usd": "1000.00"},
            "quotes": {},
        }
        first = create_snapshot(**kwargs)
        second = create_snapshot(**kwargs)
        self.assertEqual(first.content_hash, second.content_hash)


class EvidenceContractTests(TestCase):
    def setUp(self):
        self.run, self.snapshot = make_run()
        self.available = add_evidence(
            self.snapshot,
            published_at=CUTOFF - timedelta(hours=3),
            retrieved_at=CUTOFF - timedelta(hours=1),
        )

    def test_unknown_publication_time_is_preserved_as_unknown(self):
        item = add_evidence(
            self.snapshot,
            published_at=None,
            retrieved_at=CUTOFF - timedelta(hours=4),
        )
        self.assertIsNone(item.published_at)
        self.assertFalse(item.is_published)
        self.assertEqual(item.available_at, item.retrieved_at)

    def test_claim_citing_future_evidence_is_rejected(self):
        future = add_evidence(
            self.snapshot,
            published_at=CUTOFF + timedelta(days=1),
            retrieved_at=CUTOFF - timedelta(hours=1),
        )
        with self.assertRaises(ClaimValidationError):
            create_claim(
                run=self.run,
                agent_role="bull",
                stance=ClaimStance.BULL,
                statement="Tomorrow's news is available today.",
                claim_type=ClaimType.OBSERVED_FACT,
                evidence_ids=[future.id],
            )

    def test_claim_citing_unknown_evidence_is_rejected(self):
        with self.assertRaises(EvidenceNotFound):
            create_claim(
                run=self.run,
                agent_role="bull",
                stance=ClaimStance.BULL,
                statement="A claim with a missing source.",
                claim_type=ClaimType.OBSERVED_FACT,
                evidence_ids=[uuid4()],
            )

    def test_claim_statement_length_is_bounded(self):
        with self.assertRaises(ClaimValidationError):
            create_claim(
                run=self.run,
                agent_role="bull",
                stance=ClaimStance.BULL,
                statement="x" * 281,
                claim_type=ClaimType.OBSERVED_FACT,
                evidence_ids=[self.available.id],
            )

    def test_rebuttal_target_must_be_the_opposing_initial_case(self):
        bull_claim = add_claim(
            self.run, self.available, key="bull-claim", agent_role="bull", stance=ClaimStance.BULL
        )
        bear_claim = add_claim(
            self.run,
            self.available,
            key="bear-claim",
            agent_role="bear",
            stance=ClaimStance.BEAR,
        )

        # A bear cannot rebut its own side.
        with self.assertRaises(RebuttalValidationError):
            create_rebuttal(
                run=self.run,
                responding_agent="bear",
                target_claim=bear_claim,
                response_type="dispute",
                explanation="I disagree with myself.",
                evidence_ids=[self.available.id],
            )

        # A follow-up claim is not the initial case.
        follow_up = add_claim(
            self.run,
            self.available,
            key="follow-up",
            agent_role="bull",
            stance=ClaimStance.BULL,
            case_round=2,
        )
        with self.assertRaises(RebuttalValidationError):
            create_rebuttal(
                run=self.run,
                responding_agent="bear",
                target_claim=follow_up,
                response_type="dispute",
                explanation="Rebuttal of a follow-up.",
                evidence_ids=[self.available.id],
            )

        # The opposing initial claim is accepted.
        rebuttal = create_rebuttal(
            run=self.run,
            responding_agent="bear",
            target_claim=bull_claim,
            response_type="dispute",
            explanation="The bull claim overstates the evidence.",
            evidence_ids=[self.available.id],
        )
        self.assertEqual(rebuttal.target_claim_id, bull_claim.id)
        self.assertEqual(rebuttal.run_id, self.run.id)

    def test_rebuttal_target_must_belong_to_the_same_run(self):
        other_run, _ = make_run()
        other_evidence = add_evidence(
            other_run.snapshot,
            published_at=CUTOFF - timedelta(hours=3),
            retrieved_at=CUTOFF - timedelta(hours=1),
        )
        foreign_claim = add_claim(other_run, other_evidence, key="foreign")
        with self.assertRaises(RebuttalValidationError):
            create_rebuttal(
                run=self.run,
                responding_agent="bear",
                target_claim=foreign_claim,
                response_type="dispute",
                explanation="Wrong run.",
                evidence_ids=[self.available.id],
            )


@override_settings(CACHES=TEST_CACHES)
class DecisionRunApiTests(APITestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = make_user("api-owner")
        self.portfolio = make_portfolio(self.user)
        self.run, self.snapshot = make_run(self.portfolio)
        self.evidence = add_evidence(
            self.snapshot,
            published_at=CUTOFF - timedelta(hours=3),
            retrieved_at=CUTOFF - timedelta(hours=1),
            excerpt="Public synthetic evidence.",
            is_redistributable=True,
        )
        self.bull_claim = add_claim(
            self.run,
            self.evidence,
            key="bull",
            agent_role="bull",
            stance=ClaimStance.BULL,
        )
        self.bear_claim = add_claim(
            self.run,
            self.evidence,
            key="bear",
            agent_role="bear",
            stance=ClaimStance.BEAR,
        )
        for target in (
            DecisionStatus.COLLECTING,
            DecisionStatus.ANALYSING,
            DecisionStatus.REBUTTING,
            DecisionStatus.ADJUDICATING,
            DecisionStatus.VALIDATING,
        ):
            transition_status(self.run, target, occurred_at=NOW)
        self.decision = DecisionRecord.objects.create(
            run=self.run,
            action="BUY",
            requested_quantity=Decimal("4.00000000"),
            concise_rationale="Measured rationale.",
            decisive_claim_ids=[str(self.bull_claim.claim_id)],
            strongest_objection="The bear disagrees.",
            thesis="Own it for the growth.",
            invalidation_condition="Growth below 10%.",
            engine_source=RunSource.FIXTURE,
        )
        GuardRecord.objects.create(
            proposal=self.decision,
            policy_version="guard.v1",
            evaluated_portfolio_version="v1",
            quote_time=CUTOFF,
            status="clamped",
            requested_quantity=Decimal("10.00000000"),
            allowed_quantity=Decimal("4.00000000"),
            binding_rule="max_trade_allocation_pct",
            reason_codes=["quantity_clamped_to_budget"],
            numerical_checks={
                "requested_notional_usd": "2250.00",
                "checks": [
                    {
                        "rule_code": "max_trade_allocation_pct",
                        "outcome": "fail",
                        "threshold": "1000.00",
                        "actual": "2250.00",
                        "unit": "USD",
                    },
                    {
                        "rule_code": "daily_loss_limit_usd",
                        "outcome": "not_evaluated",
                        "threshold": "500.00",
                        "actual": None,
                        "unit": "USD",
                    },
                ],
            },
        )
        OutcomeRecord.objects.create(
            decision=self.decision,
            horizon_days=7,
            method_version="attribution.v1",
            maturity=OutcomeMaturity.PENDING,
        )
        transition_status(
            self.run, DecisionStatus.COMPLETED, occurred_at=NOW + timedelta(minutes=2)
        )
        self.client = auth_client(self.user)

    def test_list_is_scoped_to_the_callers_portfolios(self):
        other = make_user("other-owner")
        other_run, _ = make_run(make_portfolio(other))

        response = self.client.get(reverse("core:decision-run-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["results"]
        ids = {item["id"] for item in results}
        self.assertEqual(ids, {str(self.run.id)})
        self.assertNotIn(str(other_run.id), ids)
        self.assertIn("error_code", results[0])
        self.assertIsNone(results[0]["error_code"])

    def test_another_users_run_is_a_404(self):
        other = make_user("other-owner")
        other_run, _ = make_run(make_portfolio(other))
        other_client = auth_client(other)

        for name in (
            "core:decision-run-detail",
            "core:decision-run-detail-rich",
            "core:decision-run-events",
            "core:decision-run-export",
        ):
            response = self.client.get(reverse(name, args=[str(other_run.id)]))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, name)
            self.assertEqual(response.json()["detail"], "Decision run not found.")
            self.assertTrue(response.json()["error"])

        # And the rightful owner can read it.
        self.assertEqual(
            other_client.get(
                reverse("core:decision-run-detail", args=[str(other_run.id)])
            ).status_code,
            status.HTTP_200_OK,
        )

    def test_metadata_exposes_stage_states_source_cost_and_failure(self):
        response = self.client.get(reverse("core:decision-run-detail", args=[str(self.run.id)]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["status"], DecisionStatus.COMPLETED)
        self.assertEqual(body["mode"], RunMode.SYNTHETIC)
        self.assertIn("error_code", body)
        self.assertIsNone(body["error_code"])
        self.assertTrue(body["is_synthetic"])
        self.assertEqual(body["source"], RunSource.FIXTURE)
        self.assertIsNone(body["cost"]["total_cost_usd"])
        self.assertIsNone(body["failure"])
        states = {item["stage"]: item["state"] for item in body["stage_states"]["stages"]}
        self.assertEqual(states["rebutting"], "complete")
        self.assertEqual(states["awaiting_approval"], "not_run")

    def test_failed_run_exposes_failure_details(self):
        failed_run, _ = make_run(self.portfolio)
        transition_status(failed_run, DecisionStatus.COLLECTING, occurred_at=NOW)
        record_failure(
            failed_run,
            error_code="quote_unavailable",
            error_detail="The quote feed returned no price for the cutoff.",
            occurred_at=NOW,
        )

        response = self.client.get(reverse("core:decision-run-detail", args=[str(failed_run.id)]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["status"], DecisionStatus.FAILED)
        self.assertEqual(body["failure"]["error_code"], "quote_unavailable")
        self.assertIn("quote feed", body["failure"]["detail"])
        self.assertEqual(body["error_code"], "quote_unavailable")
        self.assertIn("quote feed", body["error_detail"])
        states = {item["stage"]: item["state"] for item in body["stage_states"]["stages"]}
        self.assertEqual(states["collecting"], "failed")
        self.assertEqual(states["completed"], "not_run")
        self.assertEqual(body["error_code"], "quote_unavailable")

    def test_detail_exposes_cases_verdict_guard_and_outcomes(self):
        response = self.client.get(
            reverse("core:decision-run-detail-rich", args=[str(self.run.id)])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(len(body["cases"]["bull"]), 1)
        self.assertEqual(len(body["cases"]["bear"]), 1)
        self.assertEqual(body["verdict"]["action"], "BUY")
        self.assertEqual(
            body["verdict"]["decisive_claims"][0]["claim_id"], str(self.bull_claim.claim_id)
        )
        self.assertEqual(body["guard_comparison"]["requested_quantity"], "10.00000000")
        self.assertEqual(body["guard_comparison"]["allowed_quantity"], "4.00000000")
        self.assertEqual(body["guard_comparison"]["binding_rule"], "max_trade_allocation_pct")
        self.assertEqual(body["outcomes"][0]["maturity"], OutcomeMaturity.PENDING)
        self.assertTrue(body["run"]["provenance"]["is_synthetic"])

    def test_events_window_after_seq_is_exact(self):
        for index in range(5):
            append_event(
                self.run,
                RunEventType.STAGE_STARTED,
                stage=RunStage.ANALYSING,
                occurred_at=NOW,
                payload={"index": index},
            )

        url = reverse("core:decision-run-events", args=[str(self.run.id)])
        response = self.client.get(url, {"after_seq": 3, "limit": 4})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertIn("events", body)
        self.assertNotIn("results", body)
        self.assertEqual([item["seq"] for item in body["events"]], [4, 5, 6, 7])
        self.assertIn("event_id", body["events"][0])
        self.assertIn("type", body["events"][0])
        self.assertNotIn("event_type", body["events"][0])
        self.assertEqual(body["count"], 4)
        self.assertEqual(body["last_sequence"], 7)
        self.assertEqual(body["latest_sequence"], 12)
        self.assertIn("after_seq=7", body["next"])

        last = self.client.get(url, {"after_seq": 11, "limit": 4}).json()
        self.assertEqual([item["seq"] for item in last["events"]], [12])
        self.assertIsNone(last["next"])

    def test_events_rejects_a_bad_cursor(self):
        url = reverse("core:decision-run-events", args=[str(self.run.id)])
        response = self.client.get(url, {"after_seq": "not-a-number"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(response.json()["error"])

    def test_export_has_no_credentials_account_ids_or_unlicensed_bodies(self):
        secret_evidence = add_evidence(
            self.snapshot,
            published_at=CUTOFF - timedelta(hours=2),
            retrieved_at=CUTOFF - timedelta(hours=1),
            excerpt="PROPRIETARY-SOURCE-BODY-DO-NOT-EXPORT",
            is_redistributable=False,
        )
        add_claim(
            self.run,
            secret_evidence,
            key="third",
            agent_role="bear",
            stance=ClaimStance.BEAR,
        )
        token = Token.objects.get(user=self.user)

        response = self.client.get(reverse("core:decision-run-export", args=[str(self.run.id)]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        bundle = response.json()
        blob = response.content.decode()

        self.assertEqual(bundle["bundle_version"], "1.0")
        self.assertNotIn(token.key, blob)
        self.assertNotIn(self.user.username, blob)
        self.assertNotIn("portfolio_id", blob)
        self.assertNotIn("user_id", blob)
        self.assertNotIn("chat_id", blob)
        self.assertNotIn("password", blob)
        self.assertNotIn("authorization", blob)
        self.assertNotIn("PROPRIETARY-SOURCE-BODY-DO-NOT-EXPORT", blob)

        exported = {item["id"]: item for item in bundle["evidence"]}
        omitted = exported[str(secret_evidence.id)]
        self.assertIsNone(omitted["excerpt"])
        self.assertTrue(omitted["excerpt_omitted"])
        self.assertGreater(omitted["excerpt_chars"], 0)

        checksum = bundle.pop("checksum")
        self.assertEqual(checksum["algorithm"], "sha256")
        self.assertEqual(checksum["value"], content_hash(bundle))

    def test_requires_authentication(self):
        response = APIClient().get(reverse("core:decision-run-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class FixtureLoaderTests(TestCase):
    def _load(self):
        out = StringIO()
        call_command("load_decision_fixtures", stdout=out)
        return out.getvalue()

    def test_loader_is_idempotent_and_fixtures_are_honest(self):
        first_output = self._load()
        first_ids = set(DecisionRun.objects.values_list("fixture_id", "id"))
        self.assertEqual(len(first_ids), 3)
        self.assertIn("3 created, 0 skipped", first_output)

        second_output = self._load()
        second_ids = set(DecisionRun.objects.values_list("fixture_id", "id"))
        self.assertEqual(second_ids, first_ids)
        self.assertEqual(DecisionRun.objects.count(), 3)
        self.assertIn("0 created, 3 skipped", second_output)

        runs = {run.fixture_id: run for run in DecisionRun.objects.all()}
        self.assertEqual(
            set(runs),
            {
                "risk-intervention-aapl-2026-02-03",
                "disagreement-tsla-2026-02-04",
                "uncertainty-msft-2026-02-05",
            },
        )
        for run in runs.values():
            self.assertEqual(run.mode, RunMode.SYNTHETIC)
            self.assertTrue(run.is_synthetic)
            self.assertEqual(run.status, DecisionStatus.COMPLETED)

        risk = runs["risk-intervention-aapl-2026-02-03"]
        guard = GuardRecord.objects.get(proposal__run=risk)
        self.assertEqual(guard.status, "clamped")
        self.assertEqual(guard.requested_quantity, Decimal("40.00000000"))
        self.assertEqual(guard.allowed_quantity, Decimal("2.00000000"))
        self.assertEqual(guard.binding_rule, "max_trade_allocation_pct")

        disagreement = runs["disagreement-tsla-2026-02-04"]
        self.assertEqual(disagreement.rebuttals.count(), 2)
        self.assertEqual(disagreement.claims.count(), 4)

        uncertainty = runs["uncertainty-msft-2026-02-05"]
        states = {item["stage"]: item for item in stage_states(uncertainty)["stages"]}
        self.assertEqual(states["analysing"]["state"], "degraded")
        outcome = OutcomeRecord.objects.get(decision__run=uncertainty)
        self.assertEqual(outcome.maturity, OutcomeMaturity.UNAVAILABLE)
        self.assertTrue(outcome.unavailable_reason)
        unknown_date = EvidenceItem.objects.get(
            snapshot=uncertainty.snapshot, source_type="fundamentals"
        )
        self.assertIsNone(unknown_date.published_at)

    def test_loaded_fixtures_are_readable_through_the_api(self):
        self._load()
        # The loader creates the fixture account; a token is enough to read it.
        from django.contrib.auth.models import User

        user = User.objects.get(username="demo")
        client = auth_client(user)
        listing = client.get(reverse("core:decision-run-list")).json()
        self.assertEqual(listing["count"], 3)
        self.assertTrue(all(item["is_synthetic"] for item in listing["results"]))

        disagreement = DecisionRun.objects.get(fixture_id="disagreement-tsla-2026-02-04")
        detail = client.get(
            reverse("core:decision-run-detail-rich", args=[str(disagreement.id)])
        ).json()
        self.assertEqual(len(detail["rebuttals"]), 2)
        self.assertTrue(detail["has_rebuttals"])
        self.assertEqual(detail["verdict"]["action"], "HOLD")
        self.assertEqual(detail["rationale"], detail["verdict"]["concise_rationale"])
        self.assertEqual(len(detail["claims"]), 4)
        self.assertEqual(detail["rebuttals"][0]["rebuttal_id"], detail["rebuttals"][0]["id"])
        self.assertIsNone(detail["guard"])
        self.assertEqual(detail["run"]["mode"], RunMode.SYNTHETIC)
        self.assertEqual(detail["run"]["provenance"]["is_synthetic"], True)

        events = client.get(
            reverse("core:decision-run-events", args=[str(disagreement.id)]),
            {"after_seq": 0, "limit": 3},
        ).json()
        self.assertEqual([item["seq"] for item in events["events"]], [1, 2, 3])
        self.assertTrue(all("type" in item for item in events["events"]))
        self.assertIsNotNone(events["next"])

        risk = DecisionRun.objects.get(fixture_id="risk-intervention-aapl-2026-02-03")
        risk_detail = client.get(reverse("core:decision-run-detail", args=[str(risk.id)])).json()
        self.assertEqual(risk_detail["status"], DecisionStatus.COMPLETED)
        self.assertIsNone(risk_detail["failure"])
        guard = client.get(reverse("core:decision-run-detail-rich", args=[str(risk.id)])).json()[
            "guard_comparison"
        ]
        self.assertEqual(guard["status"], "clamped")
        self.assertEqual(guard["difference_quantity"], "38.00000000")
        self.assertEqual(guard["binding_rule"], "max_trade_allocation_pct")
