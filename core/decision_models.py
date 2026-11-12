"""Phase 1 decision-run tables (DATA-01 / DATA-02 / DATA-03).

Why these live beside ``core.models`` rather than inside it
-----------------------------------------------------------
``core.models`` is the Phase 0 schema and its 16 tables are a stable consumer
contract (DATA-04): ``AgentDecisionLog``, ``DecisionOutcome``, the trade ledger
and the dashboard all read it. The tables here are additive and are the record
of *how a decision was reached* - the run, its captured inputs, the evidence and
claims the agents argued from, the deterministic guard's comparison, and the
outcome. Keeping them in a separate module makes it obvious at review time that
nothing in the Phase 0 schema was edited.

``core/models.py`` imports this module at the bottom of the file so Django's app
loader registers the models. The foreign keys are declared with string labels
(``"core.Portfolio"``) rather than object imports, so there is no circular import
between the two modules.

Design rules applied throughout
-------------------------------
* **Money, quantities and weights are ``Decimal``** (DATA-02). Fields that can
  genuinely be unknown are nullable; they are never defaulted to zero, because
  "we did not measure this" and "we measured zero" are different claims and the
  dashboard must be able to tell them apart.
* **Publish time is nullable.** ``EvidenceItem.published_at`` is null when the
  publisher does not state one. The loader preserves that as unknown instead of
  guessing it from ``retrieved_at``.
* **Unknown hashes stay null.** A legacy run predates model/config/prompt
  fingerprinting, so those columns are null rather than empty strings that would
  read like a recorded-but-blank hash.
* **Timestamps are UTC** (``USE_TZ = True``); the API serialises them as ISO-8601.
* ``InputSnapshot`` is immutable after creation and enforces that in ``save()``.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.decision_errors import SnapshotImmutableError

#: The models, plus the small vocabulary constants the decision services import
#: from here instead of duplicating. The constants below are a public contract:
#: ``RUN_STAGE_ORDER``/``RUN_EVENT_TYPES`` drive statusing and validation in
#: services/decision_runs.py, and the bounds are enforced by services/evidence.py.
__all__ = [
    "EVENT_SCHEMA_VERSION",
    "MAX_EXCERPT_CHARS",
    "MAX_STATEMENT_CHARS",
    "RUN_EVENT_TYPES",
    "RUN_STAGE_ORDER",
    "AgentRole",
    "Claim",
    "ClaimStance",
    "ClaimType",
    "DecisionAction",
    "DecisionExecutionState",
    "DecisionRecord",
    "DecisionRun",
    "DecisionStatus",
    "EvidenceItem",
    "EvidenceSourceType",
    "GuardRecord",
    "GuardStatus",
    "InputSnapshot",
    "OutcomeMaturity",
    "OutcomeRecord",
    "Rebuttal",
    "RebuttalResponseType",
    "RunEvent",
    "RunEventType",
    "RunMode",
    "RunSource",
    "RunStage",
]


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class DecisionStatus(models.TextChoices):
    """The DATA-03 run lifecycle.

    The order is meaningful and ``RUN_STAGE_ORDER`` below uses it. "awaiting
    approval" sits between validation and completion because a validated
    proposal is not yet a decision.
    """

    QUEUED = "queued", "Queued"
    COLLECTING = "collecting", "Collecting evidence"
    ANALYSING = "analysing", "Analysing"
    REBUTTING = "rebutting", "Rebutting"
    ADJUDICATING = "adjudicating", "Adjudicating"
    VALIDATING = "validating", "Validating"
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


#: Stages a run moves through, in order. Terminal statuses are not stages.
RUN_STAGE_ORDER: tuple[str, ...] = (
    DecisionStatus.QUEUED,
    DecisionStatus.COLLECTING,
    DecisionStatus.ANALYSING,
    DecisionStatus.REBUTTING,
    DecisionStatus.ADJUDICATING,
    DecisionStatus.VALIDATING,
    DecisionStatus.AWAITING_APPROVAL,
    DecisionStatus.COMPLETED,
)


class RunMode(models.TextChoices):
    """Where the run's content came from, and therefore how far to trust it.

    ``synthetic`` is a first-class value rather than a debug flag. Every Phase 1
    fixture is synthetic - there was no rebuttal stage in the current pipeline to
    record - and the UI has to be able to say so on the decision itself.
    """

    LIVE = "live", "Live"
    RECORDED = "recorded", "Recorded"
    HEURISTIC = "heuristic", "Heuristic fallback"
    LEGACY = "legacy", "Legacy import"
    SYNTHETIC = "synthetic", "Synthetic fixture"
    #: A real protocol execution through the injectable model seam with a
    #: *scripted* provider. The code path, snapshot, budgets, accounting and
    #: event log are real; the model output is not a sample from an LLM. It is
    #: labelled separately from both "live" and "synthetic" because calling it
    #: either would overstate what was measured.
    SIMULATED = "simulated", "Simulated (scripted provider)"


class RunSource(models.TextChoices):
    """Which producer wrote the run."""

    CREWAI = "crewai", "CrewAI"
    HEURISTIC_FALLBACK = "heuristic_fallback", "Heuristic fallback"
    FIXTURE = "fixture", "Fixture manifest"
    HISTORICAL_IMPORT = "historical_import", "Historical import"
    STUB = "stub", "Scripted stub provider"


class RunStage(models.TextChoices):
    """Stages an individual event belongs to.

    Deliberately the same strings as the statuses, plus ``intake`` for the
    ``run.created`` event, so an event's stage and the run's status vocabulary
    cannot drift apart.
    """

    INTAKE = "intake", "Intake"
    QUEUED = "queued", "Queued"
    COLLECTING = "collecting", "Collecting evidence"
    ANALYSING = "analysing", "Analysing"
    REBUTTING = "rebutting", "Rebutting"
    ADJUDICATING = "adjudicating", "Adjudicating"
    VALIDATING = "validating", "Validating"
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class AgentRole(models.TextChoices):
    """The role that produced a claim, rebuttal, event or decision."""

    BULL = "bull", "Bull researcher"
    BEAR = "bear", "Bear researcher"
    CIO = "cio", "Chief investment officer"
    RISK = "risk", "Risk manager"
    RESEARCH = "research", "Research analyst"
    SYSTEM = "system", "System"


class ClaimStance(models.TextChoices):
    BULL = "bull", "Bullish"
    BEAR = "bear", "Bearish"
    NEUTRAL = "neutral", "Neutral"


class ClaimType(models.TextChoices):
    """EVID-02: what kind of assertion a claim is.

    The distinction is what lets a reader weigh it: an observed fact can be
    checked against its evidence, an interpretation can be argued with, and a
    forecast is a bet that has not resolved yet.
    """

    OBSERVED_FACT = "observed_fact", "Observed fact"
    INTERPRETATION = "interpretation", "Interpretation"
    FORECAST = "forecast", "Forecast"


class RebuttalResponseType(models.TextChoices):
    AGREE = "agree", "Agree"
    DISPUTE = "dispute", "Dispute"
    QUALIFY = "qualify", "Qualify"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence", "Insufficient evidence"


class DecisionAction(models.TextChoices):
    """The validated proposal's action.

    Same wire values as ``TxType`` for the executable actions, plus ``HOLD`` -
    which is a real decision that produces no transaction and must not be
    collapsed into "no decision".
    """

    BUY = "BUY", "Buy"
    SELL = "SELL", "Sell"
    HOLD = "HOLD", "Hold"


class GuardStatus(models.TextChoices):
    APPROVED = "approved", "Approved as requested"
    CLAMPED = "clamped", "Clamped to policy"
    REJECTED = "rejected", "Rejected"


class DecisionExecutionState(models.TextChoices):
    """What happened to the proposal *after* it was validated.

    ``DecisionStatus`` describes where the analysis is; this describes the
    decision's execution outcome, which is a different question. A proposal that
    is awaiting a human is not a fill, and a rejected execution is not a
    completed decision with no trade: conflating those is exactly the defect
    this field separates. The empty value is a historical run that predates the
    distinction and is not relabelled.
    """

    VALIDATING = "validating", "Awaiting guard evaluation"
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
    APPROVED = "approved", "Approved, execution pending"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"
    EXECUTED = "executed", "Executed"
    HELD = "held", "Hold, no trade"
    BLOCKED = "blocked", "Blocked by the guard"
    FAILED = "failed", "Execution failed"
    #: A duplicate advisory proposal was suppressed because an actionable
    #: recommendation already exists. This is a terminal no-trade outcome, not
    #: a second approval request: leaving the new run ``awaiting_approval``
    #: would wait for a decision that can never arrive.
    SUPPRESSED = "suppressed", "Suppressed as a duplicate"


class EvidenceSourceType(models.TextChoices):
    """EVID-01 source classes.

    Chosen to be broad enough for the existing pipeline's inputs (news, price
    history, fundamentals) without letting free text stand in for a source type.
    """

    NEWS_ARTICLE = "news_article", "News article"
    FILING = "filing", "Regulatory or company filing"
    EARNINGS_TRANSCRIPT = "earnings_transcript", "Earnings transcript"
    PRICE_QUOTE = "price_quote", "Price quote"
    FUNDAMENTALS = "fundamentals", "Fundamentals"
    MACRO_RELEASE = "macro_release", "Macroeconomic release"
    ANALYST_NOTE = "analyst_note", "Analyst note"
    DERIVED_METRIC = "derived_metric", "Derived metric"
    OTHER = "other", "Other"


class OutcomeMaturity(models.TextChoices):
    """Whether an outcome can be measured yet, and if not, why not.

    ``evaluated`` is the only state that carries a score; ``unavailable`` is a
    measured absence (there is no price to score against), not a zero.
    """

    PENDING = "pending", "Pending"
    MATURED = "matured", "Matured, not yet evaluated"
    EVALUATED = "evaluated", "Evaluated"
    UNAVAILABLE = "unavailable", "Unavailable"


class RunEventType:
    """Event type vocabulary.

    Plain constants rather than ``TextChoices``: the event log is append-only and
    a new event type is not a schema change worth a migration, but the service
    validates against this set so a typo cannot silently enter the log.
    """

    RUN_CREATED = "run.created"
    STATUS_CHANGED = "status.changed"
    STAGE_STARTED = "stage.started"
    STAGE_COMPLETED = "stage.completed"
    STAGE_FAILED = "stage.failed"
    EVIDENCE_RECORDED = "evidence.recorded"
    CLAIM_CREATED = "claim.created"
    REBUTTAL_CREATED = "rebuttal.created"
    DECISION_PROPOSED = "decision.proposed"
    GUARD_EVALUATED = "guard.evaluated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    EXECUTION_RECORDED = "execution.recorded"
    EXECUTION_REJECTED = "execution.rejected"
    EXECUTION_FAILED = "execution.failed"
    DECISION_COMPLETED = "decision.completed"
    OUTCOME_RECORDED = "outcome.recorded"
    LIMITATION_RECORDED = "limitation.recorded"


#: Every value ``RunEvent.event_type`` may hold. Validated in the service.
RUN_EVENT_TYPES: frozenset[str] = frozenset(
    value for name, value in vars(RunEventType).items() if not name.startswith("_")
)

#: Event schema version, stored on every row so a consumer can branch rather
#: than guess. Bumped only when a payload's meaning changes incompatibly.
EVENT_SCHEMA_VERSION = "1.0"

#: Hard ceiling on a stored excerpt, so "bounded excerpt" is enforceable.
MAX_EXCERPT_CHARS = 2000

#: EVID-02 bound on a claim statement.
MAX_STATEMENT_CHARS = 280


# ---------------------------------------------------------------------------
# Run + event log
# ---------------------------------------------------------------------------
class DecisionRun(models.Model):
    """One end-to-end decision attempt for one instrument and one portfolio.

    This is the join key the dashboard needs: a decision is not just the final
    ``AgentDecisionLog`` row, it is the captured snapshot, the debate, the guard
    comparison and the measured outcome. ``legacy_log`` links that new record to
    the Phase 0 audit row so existing consumers keep working (DATA-04).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio = models.ForeignKey(
        "core.Portfolio",
        on_delete=models.CASCADE,
        related_name="decision_runs",
    )
    ticker = models.CharField(max_length=32, db_index=True)

    #: Protocol identity, e.g. ``legacy_sequential`` or ``synthetic.v1``. Also
    #: lets a reader tell that a run predates the Phase 2 rebuttal protocol.
    protocol_version = models.CharField(max_length=64)
    status = models.CharField(
        max_length=20,
        choices=DecisionStatus.choices,
        default=DecisionStatus.QUEUED,
        db_index=True,
    )
    mode = models.CharField(max_length=16, choices=RunMode.choices, db_index=True)
    source = models.CharField(max_length=20, choices=RunSource.choices, db_index=True)

    #: The immutable inputs the agents saw. Null for a legacy run imported from
    #: an AgentDecisionLog: no snapshot was captured, and inventing one would be
    #: the fabrication this whole table exists to prevent.
    snapshot = models.ForeignKey(
        "core.InputSnapshot",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="runs",
    )

    #: Fingerprints of the three things that decide reproducibility. Null means
    #: "not recorded"; see the module docstring.
    model_hash = models.CharField(max_length=64, null=True, blank=True)
    config_hash = models.CharField(max_length=64, null=True, blank=True)
    prompt_hash = models.CharField(max_length=64, null=True, blank=True)

    #: Phase 2 provenance: provider, model id, parameters, prompt-template
    #: version, tool versions, budget limits actually applied, per-stage usage
    #: and the raw validated stage outputs (DEB-05). Empty for a Phase 1 fixture
    #: or a legacy run, which recorded no protocol of this kind.
    protocol_metadata = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    #: Wall-clock duration, stored rather than derived so it survives a clock
    #: change and can be compared across runs without re-reading both stamps.
    latency_ms = models.PositiveIntegerField(null=True, blank=True)

    #: Cost totals. Nullable on purpose: a synthetic or historical run spent no
    #: money, but it also has no measured cost, and reporting 0.000000 would read
    #: like a measurement. Live runs record the real figures.
    total_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    #: The price-table arithmetic over tokens, kept in its own column. It is an
    #: estimate even when the tokens were provider-reported, and it is never
    #: written into ``total_cost_usd``.
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    #: ``billed_provider_reported`` | ``price_table_estimate`` | ``unknown``.
    #: Blank on runs written before the distinction existed; blank means the
    #: basis was not recorded, not that a measurement exists.
    cost_basis = models.CharField(max_length=32, blank=True, default="")
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    cached_input_tokens = models.IntegerField(null=True, blank=True)

    error_code = models.CharField(max_length=64, blank=True, default="")
    error_detail = models.TextField(blank=True, default="")

    #: The post-validation outcome. Kept separate from ``status`` so an
    #: ``awaiting_approval`` proposal is never presented as a fill and a hold is
    #: never presented as an execution. Empty for historical runs.
    execution_state = models.CharField(
        max_length=24,
        choices=DecisionExecutionState.choices,
        blank=True,
        default="",
        db_index=True,
    )

    #: Phase 0 audit row this run explains, when one exists. One run per legacy
    #: log: two explanations of one decision would be two conflicting records.
    legacy_log = models.ForeignKey(
        "core.AgentDecisionLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decision_runs",
    )

    #: The actionable recommendation a suppressed duplicate run was linked to
    #: for context. The recommendation's own lifecycle is never rewritten by a
    #: suppressed sweep.
    duplicate_recommendation = models.ForeignKey(
        "core.TradeRecommendation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suppressed_duplicate_runs",
    )

    #: Set only by the fixture loader. It is what makes ``load_decision_fixtures``
    #: idempotent: the natural key of a fixture run is its stable fixture id.
    fixture_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    fixture_checksum = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Decision run"
        verbose_name_plural = "Decision runs"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["portfolio", "-started_at"], name="run_portfolio_recent_idx"),
            models.Index(fields=["portfolio", "status"], name="run_portfolio_status_idx"),
            models.Index(fields=["ticker", "-started_at"], name="run_ticker_recent_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(ticker=""), name="decision_run_ticker_not_blank"),
            models.CheckConstraint(
                condition=Q(total_cost_usd__isnull=True) | Q(total_cost_usd__gte=0),
                name="decision_run_cost_non_negative",
            ),
            models.UniqueConstraint(
                fields=["legacy_log"],
                condition=Q(legacy_log__isnull=False),
                name="one_run_per_legacy_decision_log",
            ),
            models.UniqueConstraint(
                fields=["fixture_id"],
                condition=~Q(fixture_id=""),
                name="one_run_per_fixture_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.ticker} {self.status} ({self.mode}) {self.id}"

    def save(self, *args, **kwargs):
        self.ticker = (self.ticker or "").strip().upper()
        super().save(*args, **kwargs)

    # -- Derived helpers ---------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in {
            DecisionStatus.COMPLETED,
            DecisionStatus.FAILED,
            DecisionStatus.CANCELLED,
        }

    @property
    def is_synthetic(self) -> bool:
        """True when the content was constructed, not observed.

        The API exposes this next to ``mode`` so a UI cannot accidentally present
        a fixture as a live decision.
        """
        return self.mode == RunMode.SYNTHETIC

    @property
    def failure(self) -> dict | None:
        """Failure details in the shape the API returns, or ``None``."""
        if self.status not in {DecisionStatus.FAILED, DecisionStatus.CANCELLED}:
            return None
        return {
            "status": self.status,
            "error_code": self.error_code or None,
            "detail": self.error_detail or None,
            "at": self.completed_at,
        }


class RunEvent(models.Model):
    """Append-only, ordered event for one run.

    ``sequence`` is allocated inside a transaction that locks the run row, so two
    workers cannot both claim the same number and no consumer can observe a gap
    followed by a lower number (API-03). ``event_id`` is a separate UUID so a
    consumer can de-duplicate a redelivered live event against the persisted one.
    """

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(
        DecisionRun,
        on_delete=models.CASCADE,
        related_name="events",
    )
    sequence = models.PositiveIntegerField()
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    schema_version = models.CharField(max_length=16, default=EVENT_SCHEMA_VERSION)
    event_type = models.CharField(max_length=64, db_index=True)
    stage = models.CharField(max_length=20, choices=RunStage.choices, db_index=True)
    agent_role = models.CharField(max_length=16, choices=AgentRole.choices, blank=True, default="")
    occurred_at = models.DateTimeField(db_index=True)
    persisted_at = models.DateTimeField(auto_now_add=True)
    #: Typed JSON payload. Values that are Decimal/datetime are stored as strings
    #: so a JSON round trip is lossless and a reviewer can read the numbers.
    payload = models.JSONField(default=dict)
    #: Idempotency key for a completion handler. Two retries of the same handler
    #: produce one event, not two. Empty means the event makes no such claim.
    dedupe_key = models.CharField(max_length=160, blank=True, default="", db_index=True)

    class Meta:
        verbose_name = "Run event"
        verbose_name_plural = "Run events"
        ordering = ["run_id", "sequence"]
        indexes = [
            models.Index(fields=["run", "sequence"], name="run_event_seq_idx"),
            models.Index(fields=["run", "event_type"], name="run_event_type_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["run", "sequence"], name="run_event_unique_sequence"),
            models.UniqueConstraint(
                fields=["run", "dedupe_key"],
                condition=~Q(dedupe_key=""),
                name="one_run_event_per_dedupe_key",
            ),
            models.CheckConstraint(condition=Q(sequence__gt=0), name="run_event_sequence_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} #{self.sequence} {self.event_type}"


# ---------------------------------------------------------------------------
# Captured inputs
# ---------------------------------------------------------------------------
class InputSnapshot(models.Model):
    """What the agents could see, frozen at capture time.

    Immutable after creation. ``content_hash`` is the canonical hash of the
    payload so two snapshots can be compared without diffing every field, and a
    run can be re-checked against the exact inputs it claims to have used.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio = models.ForeignKey(
        "core.Portfolio",
        on_delete=models.CASCADE,
        related_name="input_snapshots",
    )
    #: Opaque version of the portfolio state this snapshot represents. A string
    #: rather than an integer because the producer may use a ledger digest.
    portfolio_version = models.CharField(max_length=64)
    #: The instant after which no evidence may have become available to this run.
    analysis_cutoff = models.DateTimeField(db_index=True)
    captured_at = models.DateTimeField(default=timezone.now)

    holdings = models.JSONField(default=list)
    cash = models.JSONField(default=dict)
    quotes = models.JSONField(default=dict)

    content_hash = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Input snapshot"
        verbose_name_plural = "Input snapshots"
        ordering = ["-captured_at"]
        indexes = [
            models.Index(
                fields=["portfolio", "-captured_at"], name="snapshot_portfolio_recent_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(portfolio_version=""), name="snapshot_portfolio_version_not_blank"
            ),
            models.CheckConstraint(condition=~Q(content_hash=""), name="snapshot_hash_not_blank"),
        ]

    def __str__(self) -> str:
        return f"snapshot {self.id} v{self.portfolio_version} @ {self.captured_at:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        """Refuse every write after the row exists.

        ``QuerySet.update`` can still bypass this; the guarantee is that no code
        path can load a snapshot, change a field and save it. The database-level
        alternative (a trigger) is deliberately not used: it would also block
        legitimate migrations and is invisible in review.
        """
        if not self._state.adding or (
            self.pk is not None and type(self).objects.filter(pk=self.pk).exists()
        ):
            raise SnapshotImmutableError(
                "An input snapshot is immutable once captured; "
                "capture a new snapshot instead of editing this one."
            )
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Evidence, claims, rebuttals (EVID-01 / EVID-02 / EVID-03)
# ---------------------------------------------------------------------------
class EvidenceItem(models.Model):
    """One citable observation behind a claim.

    The contract is that a reviewer can check it: it names its source, when the
    source published it, when it was retrieved, and the earliest time it could
    have been available to the decision. ``published_at`` stays null when the
    publisher does not state a time - filling it in from ``retrieved_at`` would
    turn "unknown" into a false precision.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot = models.ForeignKey(
        InputSnapshot,
        on_delete=models.CASCADE,
        related_name="evidence_items",
    )
    source_type = models.CharField(max_length=24, choices=EvidenceSourceType.choices)
    source_uri = models.CharField(max_length=512, blank=True, default="")
    publisher = models.CharField(max_length=200, blank=True, default="")

    published_at = models.DateTimeField(null=True, blank=True)
    retrieved_at = models.DateTimeField()
    available_at = models.DateTimeField(db_index=True)

    #: Bounded quote/citation, or ``observation`` for a typed numerical datum.
    #: Either may be empty: a source can be cited without reproducing its text.
    excerpt = models.TextField(blank=True, default="")
    observation = models.JSONField(null=True, blank=True)
    units = models.CharField(max_length=32, blank=True, default="")
    currency = models.CharField(max_length=8, blank=True, default="")

    content_hash = models.CharField(max_length=64, db_index=True)
    #: Whether the excerpt may be redistributed in an export. Defaults to False:
    #: a source is not redistributable until someone says it is.
    is_redistributable = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Evidence item"
        verbose_name_plural = "Evidence items"
        ordering = ["available_at", "id"]
        indexes = [
            models.Index(fields=["snapshot", "available_at"], name="evidence_snapshot_time_idx"),
            models.Index(fields=["source_type"], name="evidence_source_type_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(published_at__isnull=True)
                | Q(available_at__gte=models.F("published_at")),
                name="evidence_available_after_publish",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source_type} {self.publisher or self.source_uri or self.id}"

    @property
    def is_published(self) -> bool:
        """False when the publication time is genuinely unknown."""
        return self.published_at is not None


class Claim(models.Model):
    """One assertion in a case, with the evidence it stands on.

    ``statement`` is capped at 280 characters in the column, the service and a
    database check. A claim that needs a paragraph to state is not a claim; the
    cap is what forces the decisive assertion into the open where it can be
    attacked.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: Stable, externally referenced id. ``decisive_claim_ids`` on the decision
    #: stores these, not the primary key, so a fixture or a Phase 2 producer can
    #: address a claim without knowing the database id.
    claim_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    run = models.ForeignKey(
        DecisionRun,
        on_delete=models.CASCADE,
        related_name="claims",
    )
    agent_role = models.CharField(max_length=16, choices=AgentRole.choices)
    stance = models.CharField(max_length=8, choices=ClaimStance.choices, db_index=True)
    statement = models.CharField(max_length=MAX_STATEMENT_CHARS)
    claim_type = models.CharField(max_length=16, choices=ClaimType.choices)
    evidence = models.ManyToManyField(
        EvidenceItem,
        related_name="claims",
        blank=True,
    )
    #: "30d", "through the next earnings call" - free text because a horizon can
    #: be event-relative, and normalising it would lose that.
    horizon = models.CharField(max_length=64, blank=True, default="")
    limitations = models.TextField(blank=True, default="")

    #: 1 = the initial case. A rebuttal may only target an initial claim; this is
    #: how "the opposing initial case" is represented without inventing a
    #: conversation model. Phase 2 may raise it for follow-up rounds.
    case_round = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Claim"
        verbose_name_plural = "Claims"
        ordering = ["run_id", "agent_role", "created_at"]
        indexes = [
            models.Index(fields=["run", "stance"], name="claim_run_stance_idx"),
            models.Index(fields=["run", "agent_role"], name="claim_run_role_idx"),
        ]
        constraints = [
            # The 280-character bound is enforced twice: ``max_length`` makes the
            # column ``varchar(280)``, which PostgreSQL refuses to overflow, and
            # ``services.evidence.create_claim`` raises a typed error first so a
            # client gets a message instead of a driver exception.
            models.CheckConstraint(
                condition=Q(case_round__gte=1), name="claim_case_round_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"[{self.stance}] {self.statement[:60]}"


class Rebuttal(models.Model):
    """A recorded response to a claim from the opposing side.

    Phase 1 has no rebuttal producer: no row is written by the live pipeline. The
    table exists so the consumer contract is real, and the one Phase 1 fixture
    that fills it is labelled ``mode="synthetic"``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: Denormalised from ``target_claim.run`` so rebuttals can be listed for a
    #: run in one query. The service keeps the two in step and rejects a mismatch.
    run = models.ForeignKey(
        DecisionRun,
        on_delete=models.CASCADE,
        related_name="rebuttals",
    )
    responding_agent = models.CharField(max_length=16, choices=AgentRole.choices)
    target_claim = models.ForeignKey(
        Claim,
        on_delete=models.CASCADE,
        related_name="rebuttals",
    )
    response_type = models.CharField(max_length=24, choices=RebuttalResponseType.choices)
    explanation = models.TextField()
    evidence = models.ManyToManyField(
        EvidenceItem,
        related_name="rebuttals",
        blank=True,
    )
    #: What the responding agent now believes, when this rebuttal changed its
    #: position. Blank means the position is unchanged - not that it is unknown.
    position_change = models.CharField(max_length=280, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Rebuttal"
        verbose_name_plural = "Rebuttals"
        ordering = ["run_id", "created_at"]
        indexes = [
            models.Index(fields=["run", "responding_agent"], name="rebuttal_run_agent_idx"),
            models.Index(fields=["target_claim"], name="rebuttal_target_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.responding_agent} {self.response_type} -> {self.target_claim_id}"


# ---------------------------------------------------------------------------
# Proposal, guard, outcome
# ---------------------------------------------------------------------------
class DecisionRecord(models.Model):
    """The validated proposal the run produced, with its reasoning on the record.

    One per run. ``concise_rationale`` is what the UI shows first; the full
    reasoning text stays on ``AgentDecisionLog`` where Phase 0 consumers already
    read it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.OneToOneField(
        DecisionRun,
        on_delete=models.CASCADE,
        related_name="decision",
    )
    action = models.CharField(max_length=4, choices=DecisionAction.choices)
    #: Null for a HOLD: no quantity was requested, which is different from
    #: requesting zero (which the guard would reject as a malformed proposal).
    requested_quantity = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    concise_rationale = models.TextField()
    decisive_claim_ids = models.JSONField(default=list)
    strongest_objection = models.TextField(blank=True, default="")
    thesis = models.TextField(blank=True, default="")
    invalidation_condition = models.TextField(blank=True, default="")
    engine_source = models.CharField(max_length=20, choices=RunSource.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Decision record"
        verbose_name_plural = "Decision records"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(requested_quantity__isnull=True) | Q(requested_quantity__gt=0),
                name="decision_requested_quantity_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.requested_quantity or ''} ({self.run_id})"


class GuardRecord(models.Model):
    """The deterministic guard's evaluation of one proposal.

    ``requested_quantity`` and ``allowed_quantity`` are both stored because the
    interesting number is the difference. A UI that only shows the executed
    quantity cannot demonstrate that the risk layer did anything.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(
        DecisionRecord,
        on_delete=models.CASCADE,
        related_name="guards",
    )
    policy_version = models.CharField(max_length=64)
    #: The portfolio state the guard evaluated. A clamp only means anything
    #: relative to the limits and balance in force at that moment.
    evaluated_portfolio_version = models.CharField(max_length=64)
    #: The market observation instant. Null is an explicit "unknown": a guard
    #: evaluation of an untimed estimate must not be presented as a current mark.
    quote_time = models.DateTimeField(null=True, blank=True)
    quote_source = models.CharField(max_length=64, blank=True, default="")
    quote_retrieved_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=GuardStatus.choices, db_index=True)
    requested_quantity = models.DecimalField(max_digits=20, decimal_places=8)
    allowed_quantity = models.DecimalField(max_digits=20, decimal_places=8)
    #: Stable identity of this evaluation. A retried completion handler
    #: re-evaluates the same proposal against the same state and produces the
    #: same key, so it updates/returns one row instead of appending a duplicate.
    evaluation_key = models.CharField(max_length=200, null=True, blank=True, unique=True)

    reason_codes = models.JSONField(default=list)
    #: The single rule that produced the allowed quantity. Kept separate from the
    #: reason codes so a reader can always name the binding constraint.
    binding_rule = models.CharField(max_length=64, blank=True, default="")
    #: The arithmetic behind the decision, as decimal strings.
    numerical_checks = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Guard record"
        verbose_name_plural = "Guard records"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["proposal", "status"], name="guard_proposal_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(requested_quantity__gt=0),
                name="guard_requested_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(allowed_quantity__gte=0),
                name="guard_allowed_quantity_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(allowed_quantity__lte=models.F("requested_quantity")),
                name="guard_allowed_lte_requested",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.status} {self.allowed_quantity}/{self.requested_quantity}"

    @property
    def difference_quantity(self):
        """How much the guard removed. Zero for an unmodified approval."""
        return self.requested_quantity - self.allowed_quantity


class OutcomeRecord(models.Model):
    """What happened after the decision, at one horizon.

    Separate from ``DecisionOutcome`` (which remains the Phase 0 attribution
    record) because it carries the maturity state and the identity of the method
    that measured it. An outcome is only comparable to another one when both say
    how they were produced.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.ForeignKey(
        DecisionRecord,
        on_delete=models.CASCADE,
        related_name="outcomes",
    )
    horizon_days = models.PositiveSmallIntegerField()
    #: Which definition of "return" scored this row. Frozen before any comparative
    #: claim is made, per the specification's Phase 3 precondition.
    method_version = models.CharField(max_length=32)
    maturity = models.CharField(
        max_length=16,
        choices=OutcomeMaturity.choices,
        default=OutcomeMaturity.PENDING,
        db_index=True,
    )

    price_at_decision = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)
    price_at_horizon = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)
    #: Market observation instants (session-date precision), separate from
    #: ``evaluated_at``. Comparing a benchmark window against an *evaluation*
    #: timestamp would be a false interval check.
    price_observed_at_decision = models.DateTimeField(null=True, blank=True)
    price_observed_at = models.DateTimeField(null=True, blank=True)
    price_source = models.CharField(max_length=64, blank=True, default="")

    forward_return_pct = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    decision_return_pct = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )

    benchmark_ticker = models.CharField(max_length=32, blank=True, default="")
    benchmark_return_pct = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    #: Benchmark provenance. A return without the observations behind it cannot be
    #: interval-checked against the instrument's window; these columns make the
    #: interval verifiable at session-date precision for rows written after the
    #: provenance change. Null means the row predates it (or the producer could
    #: not price the benchmark), and the report says so rather than assuming.
    benchmark_price_at_decision = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    benchmark_price_at_horizon = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    benchmark_observed_at_decision = models.DateTimeField(null=True, blank=True)
    benchmark_price_observed_at = models.DateTimeField(null=True, blank=True)
    benchmark_price_source = models.CharField(max_length=64, blank=True, default="")

    #: Signal diagnostics (volatility, drawdown, sample size) as typed JSON. The
    #: point is to stop a single return number from looking more certain than it
    #: is; an empty object means none were computed.
    signal_diagnostics = models.JSONField(default=dict)
    unavailable_reason = models.TextField(blank=True, default="")
    evaluated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Outcome record"
        verbose_name_plural = "Outcome records"
        ordering = ["decision", "horizon_days"]
        indexes = [
            models.Index(fields=["decision", "horizon_days"], name="outcome_decision_horizon_idx"),
            models.Index(fields=["maturity"], name="outcome_maturity_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["decision", "horizon_days", "method_version"],
                name="outcome_unique_per_method_horizon",
            ),
            models.CheckConstraint(
                condition=Q(horizon_days__gt=0), name="outcome_horizon_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.decision_id} +{self.horizon_days}d {self.maturity}"

    @property
    def is_scored(self) -> bool:
        return self.maturity == OutcomeMaturity.EVALUATED and self.decision_return_pct is not None


class ExecutionAttempt(models.Model):
    """One durable attempt to resolve a decision, keyed by execution identity.

    A ``Transaction`` answers "what fill exists?". This row answers the other
    half: which run and recommendation that fill belongs to, which final guard
    evaluation authorised it, which terminal event records the outcome, and
    what terms the approval named. It is written in the same transaction as the
    ledger row, so a crash cannot leave a fill whose audit was never recorded.

    For a no-trade decision the row exists too, keyed by a stable terminal
    identity. A replayed request therefore returns the original resolution
    instead of inventing a second one.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: The stable identity of the execution request. Empty is not allowed: an
    #: attempt with no key cannot be replayed and cannot be repaired by key.
    execution_key = models.CharField(max_length=200, unique=True)
    request_hash = models.CharField(max_length=64, blank=True, default="")
    source = models.CharField(max_length=32, blank=True, default="")
    approval_ref = models.CharField(max_length=200, blank=True, default="")
    state = models.CharField(
        max_length=24,
        choices=DecisionExecutionState.choices,
        db_index=True,
        default=DecisionExecutionState.VALIDATING,
    )
    transaction = models.OneToOneField(
        "core.Transaction",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="execution_attempt",
    )
    decision_run = models.ForeignKey(
        DecisionRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="execution_attempts",
    )
    recommendation = models.ForeignKey(
        "core.TradeRecommendation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="execution_attempts",
    )
    guard_record = models.ForeignKey(
        GuardRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="execution_attempts",
    )
    terminal_event = models.OneToOneField(
        "core.RunEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="execution_attempt",
    )
    #: The exact terms the check was run under (approved side/quantity/reference
    #: price/tolerance and observation provenance), as JSON-safe strings.
    approved_terms = models.JSONField(default=dict, blank=True)
    #: The final numerical checks, including the observation time each one used.
    decision_detail = models.JSONField(default=dict, blank=True)
    quote_observed_at = models.DateTimeField(null=True, blank=True)
    quote_retrieved_at = models.DateTimeField(null=True, blank=True)
    quote_source = models.CharField(max_length=64, blank=True, default="")
    #: True when this attempt was created by a repair, so a reader never mistakes
    #: a reconstructed row for one written by the original worker.
    recovered = models.BooleanField(default=False)
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Execution attempt"
        verbose_name_plural = "Execution attempts"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["decision_run", "state"], name="attempt_run_state_idx"),
            models.Index(fields=["recommendation", "state"], name="attempt_reco_state_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.execution_key} [{self.state}]"
