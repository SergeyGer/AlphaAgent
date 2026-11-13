"""Phase 4 allocation tables: instrument classification, plans and plan legs.

Why these live beside ``core.models`` rather than inside it
-----------------------------------------------------------
``core.models`` is the Phase 0 schema and its tables are a stable consumer
contract (DATA-04). The tables here are additive, and they exist for one reason:
a portfolio allocation decision must be inspectable after the fact.

* **``InstrumentClassification`` is an explicit lookup, not an inference.**
  Nothing in the pre-Phase-4 schema recorded a sector or an asset class, so the
  brief could not report sector exposure at all. Classification is stored per
  portfolio and per ticker, and a missing row means *unclassified*. A ticker
  string is never parsed, mapped or guessed into a sector: a wrong sector is
  worse than an absent one, because it makes the sector limit look enforced when
  it is not. Existing databases have no rows, so their sector exposure is
  reported as incomplete rather than as 0%.
* **``PortfolioPlan`` is one portfolio-level decision made against one
  snapshot.** It stores the brief the CIO saw, the weights it requested, the
  weights the deterministic layer could actually support, every deviation, and
  the model provenance. ``target_weights`` there are the constrained weights the
  plan will execute, not the raw model request; ``requested_target_weights``
  keeps the request so a reader can see what was declined.
* **``PortfolioPlanLeg`` is the executable form of the weights.** Sells carry a
  ``funding`` or ``risk_reducing`` priority and are sequenced before buys. A leg
  has at most one transaction: the one-to-one link is what makes applying the
  same plan twice idempotent at the ledger level, not just at the API level.

Money, quantities and weights are ``Decimal`` (DATA-02). Decimal values are
stored as JSON strings inside the JSON columns and serialised back as strings.
Timestamps are UTC (``USE_TZ = True``).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

__all__ = [
    "EXECUTION_MODE_LIVE",
    "EXECUTION_MODE_PAPER",
    "AllocationSubmission",
    "AllocationSubmissionStatus",
    "InstrumentClassification",
    "InstrumentClassificationSource",
    "PlanApproval",
    "PlanApprovalDecision",
    "PlanExecutionAttempt",
    "PlanLegPriority",
    "PlanLegStatus",
    "PlanSide",
    "PlanStatus",
    "PortfolioPlan",
    "PortfolioPlanLeg",
]

#: Is this plan allowed to execute at its own planned marks?
#:
#: ``paper`` is the only mode in which a missing quote may fall back to the
#: price the plan was sized at, and a paper fill is labelled as such on the plan.
#: ``live`` requires a current, timestamped mark for every material holding.
EXECUTION_MODE_LIVE = "live"
EXECUTION_MODE_PAPER = "paper"


# ---------------------------------------------------------------------------
# Explicit instrument classification
# ---------------------------------------------------------------------------
class InstrumentClassificationSource(models.TextChoices):
    """Where a classification came from.

    ``manual`` is a person or an operator-supplied import; ``provider`` is a data
    vendor that states the sector itself. The distinction matters when two
    sources disagree: neither is silently overwritten by a guess.
    """

    MANUAL = "manual", "Manually assigned"
    PROVIDER = "provider", "Data provider"
    FIXTURE = "fixture", "Fixture / demonstration data"


class InstrumentClassification(models.Model):
    """An explicit sector / asset-class label for one ticker in one portfolio.

    The row is a deliberate statement. A missing row means the classification is
    **unknown**, which the brief reports as incomplete coverage - not as a zero
    exposure and not as a default bucket. The unique constraint is per portfolio
    because two portfolios may legitimately classify the same instrument
    differently, and this table is not an attempted canonical security master.
    """

    id = models.BigAutoField(primary_key=True)
    portfolio = models.ForeignKey(
        "core.Portfolio",
        on_delete=models.CASCADE,
        related_name="instrument_classifications",
    )
    ticker = models.CharField(max_length=32, db_index=True)
    #: A label, not an enum. Sector taxonomies differ (GICS, ICB, a broker's own
    #: buckets); storing the label lets the deployment use one consistently
    #: instead of forcing a mapping that would silently merge unlike things.
    sector = models.CharField(max_length=64)
    asset_class = models.CharField(max_length=32)
    source = models.CharField(
        max_length=16,
        choices=InstrumentClassificationSource.choices,
        default=InstrumentClassificationSource.MANUAL,
    )
    source_detail = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Instrument classification"
        verbose_name_plural = "Instrument classifications"
        ordering = ["ticker"]
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "ticker"],
                name="classification_unique_ticker_per_portfolio",
            ),
            models.CheckConstraint(
                condition=~Q(ticker="") & ~Q(sector="") & ~Q(asset_class=""),
                name="classification_fields_not_blank",
            ),
        ]
        indexes = [
            models.Index(fields=["portfolio", "sector"], name="classif_portfolio_sector_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.ticker} [{self.sector}/{self.asset_class}]"


# ---------------------------------------------------------------------------
# Portfolio plans
# ---------------------------------------------------------------------------
class PlanStatus(models.TextChoices):
    """Lifecycle of one portfolio allocation plan.

    ``no_change`` is a first-class outcome. The specification explicitly allows
    maintaining cash and making no change, and collapsing that into ``applied``
    would make a plan that traded nothing indistinguishable from one that did.
    ``rejected`` is a human review decision and is distinct from ``cancelled``
    (a plan withdrawn before a decision) and ``failed`` (an execution failure).
    """

    PROPOSED = "proposed", "Proposed"
    APPLIED = "applied", "Applied"
    NO_CHANGE = "no_change", "No change"
    FAILED = "failed", "Failed"
    SUPERSEDED = "superseded", "Superseded"
    CANCELLED = "cancelled", "Cancelled"
    REJECTED = "rejected", "Rejected"


class PlanSide(models.TextChoices):
    BUY = "BUY", "Buy"
    SELL = "SELL", "Sell"


class PlanLegPriority(models.TextChoices):
    """Execution priority. Funding and risk-reducing legs run before new risk.

    The order is the point: a plan that buys first and hopes to sell later is
    assuming proceeds that do not exist yet.
    """

    FUNDING = "funding", "Funding sale"
    RISK_REDUCING = "risk_reducing", "Risk-reducing sale"
    REBALANCE = "rebalance", "Rebalance"
    NEW_RISK = "new_risk", "New risk"


class PlanLegStatus(models.TextChoices):
    PLANNED = "planned", "Planned"
    APPLIED = "applied", "Applied"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class PortfolioPlan(models.Model):
    """One portfolio-level allocation decision, frozen against one snapshot.

    ``brief`` is the exact JSON handed to the model. Storing it rather than
    pointing at live tables is what makes the decision reviewable later: the
    portfolio will have moved, and a rationale read against today's holdings is
    a different claim.

    ``submission_key`` is the idempotency key. A duplicate submission returns the
    existing plan; it does not create a second plan, a second set of legs or a
    second ledger row. It is nullable-in-effect via the blank string rather than
    a nullable column so the unique constraint can ignore unkeyed plans.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio = models.ForeignKey(
        "core.Portfolio",
        on_delete=models.CASCADE,
        related_name="allocation_plans",
    )
    snapshot = models.ForeignKey(
        "core.InputSnapshot",
        on_delete=models.CASCADE,
        related_name="allocation_plans",
    )
    submission_key = models.CharField(max_length=128, blank=True, default="", db_index=True)
    #: Canonical hash of the request that created the plan. A submission key is
    #: an idempotency claim about *one request*: reusing it with a different body
    #: is a conflict, not a duplicate to be silently answered with the old plan.
    submission_request_hash = models.CharField(max_length=64, blank=True, default="")
    execution_mode = models.CharField(max_length=8, default=EXECUTION_MODE_LIVE)
    status = models.CharField(
        max_length=16, choices=PlanStatus.choices, default=PlanStatus.PROPOSED, db_index=True
    )

    #: Versions that decide whether this plan may still be applied. The proposal
    #: version is what the model saw; the evaluated version is the portfolio state
    #: the deterministic sizing ran against. They differ when the plan was built
    #: from a stale snapshot, which is recorded rather than hidden.
    protocol_version = models.CharField(max_length=64)
    policy_version = models.CharField(max_length=64)
    proposal_portfolio_version = models.CharField(max_length=64)
    evaluated_portfolio_version = models.CharField(max_length=64)
    investment_horizon_days = models.PositiveSmallIntegerField(default=30)

    #: Immutable review version. It changes only when the plan's material
    #: decision changes (``revalidate_plan`` re-derives the legs and binding).
    #: An approval names ``(plan_id, plan_version, approval_hash)`` and an
    #: expiry, so a later material change cannot inherit the old approval.
    plan_version = models.PositiveIntegerField(default=1)
    #: The validated proposal's own statement of whether the evidence was
    #: sufficient. Stored because the review surface must not infer it from the
    #: existence of a plan.
    data_sufficiency = models.CharField(max_length=16, blank=True, default="")

    objective = models.TextField(blank=True, default="")

    #: The model's request and the plan's constrained target. Both include CASH
    #: and both sum to one (within the recorded tolerance for the request; the
    #: constrained target is normalised to exactly one at six decimals).
    requested_target_weights = models.JSONField(default=dict)
    target_weights = models.JSONField(default=dict)
    actions = models.JSONField(default=list)
    funding_source = models.CharField(max_length=16, blank=True, default="")
    funding = models.JSONField(default=dict)
    rationale = models.TextField(blank=True, default="")
    rejected_alternatives = models.JSONField(default=list)
    unresolved_uncertainties = models.JSONField(default=list)

    #: Deterministic output. ``deviations`` is the difference between what the
    #: model asked for and what the limits allowed; an empty list is a claim that
    #: the request was already feasible, not that no check ran.
    deviations = models.JSONField(default=list)
    constraints = models.JSONField(default=dict)
    data_quality = models.JSONField(default=dict)

    brief = models.JSONField(default=dict)
    brief_hash = models.CharField(max_length=64, blank=True, default="")
    cost_model = models.JSONField(default=dict)
    model_metadata = models.JSONField(default=dict)
    #: The observation provenance of every mark the plan was sized from:
    #: ``{ticker: {price, source, as_of, retrieved_at, currency}}``. A missing
    #: entry means the producer did not record it; the approval then records
    #: that absence rather than inventing a time.
    quote_observations = models.JSONField(default=dict, blank=True)

    #: What the plan was approved against: the limits, classifications, cost
    #: assumptions and allocation that decide whether it is still permitted.
    #: ``approval_hash`` is the canonical hash of ``approval_binding``; execution
    #: recomputes it under the lock and refuses a plan whose permissions moved.
    approval_binding = models.JSONField(default=dict)
    approval_hash = models.CharField(max_length=64, blank=True, default="")

    #: The human approval currently in force. ``approval_version`` is the plan
    #: version the decision named; it is cleared whenever the plan materially
    #: changes. ``approval_expires_at`` is mandatory for an approval: an
    #: approval without an expiry would be an unbounded permission.
    approval_version = models.PositiveIntegerField(default=0)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_portfolio_plans",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_expires_at = models.DateTimeField(null=True, blank=True)

    #: The approval that was in force when the plan executed. Execution is an
    #: explicit second act; this records which permission it used.
    applied_approval_version = models.PositiveIntegerField(default=0)
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_portfolio_plans",
    )

    #: The weights actually achieved after application, with the per-leg reason.
    #: Requested and projected weights were already stored; without this the plan
    #: could not answer "what was really executed?" after a clamp or a rounding.
    executed_target_weights = models.JSONField(default=dict)
    execution_reasons = models.JSONField(default=list)

    equity_usd = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    cash_usd = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    min_cash_usd = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    estimated_cost_usd = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00")
    )
    estimated_turnover_pct = models.DecimalField(
        max_digits=9, decimal_places=4, null=True, blank=True
    )

    #: Revalidation is an explicit act after the portfolio changed. The plan is
    #: not applied against a different portfolio version by accident.
    revalidation_required = models.BooleanField(default=False)
    revalidation_count = models.PositiveIntegerField(default=0)
    revalidated_at = models.DateTimeField(null=True, blank=True)

    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_detail = models.TextField(blank=True, default="")

    #: Identity of the most recent application attempt. Failure recording uses a
    #: conditional update on this token, so a delayed handler from an older
    #: attempt cannot overwrite a plan another attempt has already applied.
    execution_attempt_id = models.UUIDField(null=True, blank=True, db_index=True)
    execution_attempt_number = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Portfolio plan"
        verbose_name_plural = "Portfolio plans"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["portfolio", "-created_at"], name="plan_portfolio_recent_idx"),
            models.Index(fields=["portfolio", "status"], name="plan_portfolio_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "submission_key"],
                condition=~Q(submission_key=""),
                name="one_plan_per_submission_key",
            ),
            models.CheckConstraint(
                condition=Q(estimated_cost_usd__gte=0),
                name="plan_estimated_cost_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(revalidation_count__gte=0),
                name="plan_revalidation_count_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"plan {self.id} [{self.status}] {self.portfolio_id}"

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            PlanStatus.APPLIED,
            PlanStatus.NO_CHANGE,
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
            PlanStatus.REJECTED,
        }

    @property
    def is_applicable(self) -> bool:
        return self.status == PlanStatus.PROPOSED and not self.revalidation_required

    @property
    def is_reviewable(self) -> bool:
        return self.status == PlanStatus.PROPOSED


class PortfolioPlanLeg(models.Model):
    """One executable order in a plan.

    ``quantity`` is fixed at plan time from a snapshot price, then revalidated at
    apply time against the current mark and the live guard. A leg whose
    revalidation fails stops the plan; the remaining legs are not attempted, and
    a failed sale's proceeds are never treated as available cash.
    """

    id = models.BigAutoField(primary_key=True)
    plan = models.ForeignKey(
        PortfolioPlan,
        on_delete=models.CASCADE,
        related_name="legs",
    )
    sequence = models.PositiveIntegerField()
    ticker = models.CharField(max_length=32, db_index=True)
    side = models.CharField(max_length=4, choices=PlanSide.choices)
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    price = models.DecimalField(max_digits=18, decimal_places=8)
    notional_usd = models.DecimalField(max_digits=18, decimal_places=2)
    estimated_cost_usd = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00")
    )
    priority = models.CharField(
        max_length=16, choices=PlanLegPriority.choices, default=PlanLegPriority.REBALANCE
    )
    sector = models.CharField(max_length=64, blank=True, default="")
    asset_class = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=PlanLegStatus.choices, default=PlanLegStatus.PLANNED
    )
    reason = models.TextField(blank=True, default="")

    #: The ledger row this leg produced. One-to-one: the database itself refuses
    #: a second row for the same leg, so a retried apply cannot duplicate a fill
    #: even if an application-level check were bypassed.
    transaction = models.OneToOneField(
        "core.Transaction",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="allocation_leg",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Portfolio plan leg"
        verbose_name_plural = "Portfolio plan legs"
        ordering = ["plan", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "sequence"], name="leg_unique_sequence"),
            models.UniqueConstraint(
                fields=["plan", "ticker", "side"], name="leg_unique_ticker_side"
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="leg_quantity_positive"),
            models.CheckConstraint(
                condition=Q(notional_usd__gte=0), name="leg_notional_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(estimated_cost_usd__gte=0),
                name="leg_estimated_cost_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["plan", "status"], name="leg_plan_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.sequence}. {self.side} {self.quantity} {self.ticker} @ {self.price}"

    @property
    def signed_cash_effect_usd(self) -> Decimal:
        """Cash used (negative) or released (positive) by this leg, costs included."""
        if self.side == PlanSide.BUY:
            return -(self.notional_usd + self.estimated_cost_usd)
        return self.notional_usd - self.estimated_cost_usd

    @property
    def was_applied(self) -> bool:
        return self.status == PlanLegStatus.APPLIED and self.transaction_id is not None


# ---------------------------------------------------------------------------
# Analysis submissions
# ---------------------------------------------------------------------------
class AllocationSubmissionStatus(models.TextChoices):
    """Lifecycle of one idempotent portfolio-analysis request.

    ``queued`` is the durable row written before any work. A worker claims it
    and moves it to ``running``; the response returns as soon as the row is
    committed, so a slow provider never blocks the request and a process death
    leaves a state a repair command can see.

    ``succeeded``, ``failed``, ``cancelled`` and ``timed_out`` are terminal.
    Every transition is monotonic: a terminal row is never reopened, and a
    cancelled or timed-out submission is refused at the final, locked step that
    would create and attach a plan. A failure keeps the row so a retry of the
    same idempotency key returns the same outcome rather than paying for a
    second provider call.
    """

    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    TIMED_OUT = "timed_out", "Timed out"


class AllocationSubmission(models.Model):
    """One authenticated, idempotent request for a portfolio allocation plan.

    Why this is not a ``DecisionRun``
    ---------------------------------
    ``DecisionRun`` is per instrument: one ticker, one debate, one guard record.
    A portfolio allocation is one decision over the whole portfolio, sized
    against one snapshot and producing one plan. Recording it as N decision runs
    would either duplicate the portfolio call per ticker or invent a ticker that
    does not exist. This table is the submission record, and the status URL the
    ``POST`` returns points at it.

    ``request_hash`` covers the *submission body*, not the model response. That
    is what makes the retry idempotent before the model is called: the same key
    with a different body is a conflict even though the model would produce a
    different answer anyway.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio = models.ForeignKey(
        "core.Portfolio",
        on_delete=models.CASCADE,
        related_name="allocation_submissions",
    )
    idempotency_key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    #: The canonical, validated request body. Stored verbatim so a conflict
    #: decision can be audited rather than argued about.
    request = models.JSONField(default=dict)
    tickers = models.JSONField(default=list)
    mode = models.CharField(max_length=16)
    protocol_version = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=AllocationSubmissionStatus.choices,
        default=AllocationSubmissionStatus.QUEUED,
        db_index=True,
    )

    #: The plan this analysis produced, when it completed. SET_NULL keeps the
    #: submission's own record if an operator removes a plan.
    plan = models.ForeignKey(
        "core.PortfolioPlan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submissions",
    )

    #: Model provenance copied from the plan's metadata. Recorded here too so a
    #: failed submission can say which provider answered without a plan row.
    provider = models.CharField(max_length=64, blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="")
    #: True when the response was produced by an explicit stub, never by a
    #: provider. The boolean exists so no consumer has to infer it from a name.
    synthetic_response = models.BooleanField(default=False)
    model_metadata = models.JSONField(default=dict, blank=True)

    #: Input provenance and declared data-quality limitations gathered while
    #: building the brief. Never contains invented sectors or correlations.
    data_quality = models.JSONField(default=dict, blank=True)

    error_code = models.CharField(max_length=64, blank=True, default="")
    error_detail = models.TextField(blank=True, default="")

    #: Explicit ownership and attempt accounting. ``attempt_count`` increases
    #: only when a worker claims the row; a redelivered task that does not own
    #: a live lease does nothing. The lease is what tells the repair command
    #: that the worker that claimed this row is gone.
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    claimed_by = models.CharField(max_length=128, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    #: The absolute deadline for the whole submission. Set when the durable row
    #: is written, so a submission with no worker at all is still visibly past
    #: its deadline rather than queued forever with no reported reason.
    deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    queued_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_dispatched_at = models.DateTimeField(null=True, blank=True)
    #: Every broker publish attempt is counted, and a failure leaves a stable
    #: code rather than free-form exception text. A queued row with a non-empty
    #: error (or with no attempt at all) is what the recovery sweep looks for:
    #: the database commit succeeded but the broker never accepted the task.
    dispatch_attempt_count = models.PositiveIntegerField(default=0)
    last_dispatch_error = models.CharField(max_length=64, blank=True, default="")

    #: The provider call is a paid side effect, so it gets its own checkpoint.
    #: ``provider_call_started_at`` is written before the call and
    #: ``provider_completed_at`` with ``provider_result`` after it. A retry that
    #: finds a start without a result does not call the provider again; it
    #: records ``provider_call_unreconciled`` and stops.
    provider_call_started_at = models.DateTimeField(null=True, blank=True)
    provider_completed_at = models.DateTimeField(null=True, blank=True)
    provider_result = models.JSONField(default=dict, blank=True)
    #: The durable identity of the billable attempt, written before the first
    #: reservation. The daily-spend ledger is keyed by this, not by the user's
    #: idempotency key: two portfolios may use the same key, and a retried
    #: worker must replay its own reservation rather than hold a second one.
    paid_attempt = models.PositiveIntegerField(default=0)
    reservation_key = models.CharField(max_length=255, blank=True, default="")
    #: The immutable input the persisted provider result answered, plus the
    #: declared values needed to rebuild the plan without reading today's market:
    #: the brief payload, policy, cost model, quotes and classifications.
    input_snapshot = models.ForeignKey(
        "core.InputSnapshot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="allocation_submissions",
    )
    checkpoint_context = models.JSONField(default=dict, blank=True)
    #: Provider, model, parameters and budget ceilings in force for the call.
    #: Kept with the result so a resume can say what produced it without
    #: reconstructing the client from a possibly different configuration.
    provider_config = models.JSONField(default=dict, blank=True)
    #: The validated proposal is persisted before any plan row is written, so a
    #: retry can resume without a second provider spend and the plan creation is
    #: a separate, atomic step.
    worker_metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Allocation submission"
        verbose_name_plural = "Allocation submissions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["portfolio", "-created_at"], name="subm_portfolio_recent_idx"),
            models.Index(fields=["portfolio", "status"], name="subm_portfolio_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "idempotency_key"],
                name="one_submission_per_idempotency_key",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key="") & ~Q(request_hash=""),
                name="submission_key_and_hash_not_blank",
            ),
        ]

    def __str__(self) -> str:
        return f"submission {self.id} [{self.status}] {self.portfolio_id}"

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            AllocationSubmissionStatus.SUCCEEDED,
            AllocationSubmissionStatus.FAILED,
            AllocationSubmissionStatus.CANCELLED,
            AllocationSubmissionStatus.TIMED_OUT,
        }

    @property
    def terminal_at(self):
        """The terminal timestamp, or ``None`` while the submission is open."""
        return self.completed_at if self.is_terminal else None


# ---------------------------------------------------------------------------
# Plan approvals
# ---------------------------------------------------------------------------
class PlanApprovalDecision(models.TextChoices):
    """A human review decision, kept as an immutable audit row.

    ``cancelled`` is recorded here as well as on the plan because withdrawal is
    an act with a person, a time and usually a reason; leaving it only as a
    mutable status column would lose all three.
    """

    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"


class PlanApproval(models.Model):
    """One audit row for one human decision about one plan version.

    The approval binds ``plan_version`` **and** ``approval_hash``: a decision is
    about the exact requested allocation, limits, classifications and costs the
    reviewer saw, not about the plan id. ``expires_at`` is mandatory for an
    approval and null for a rejection/cancellation. A materially changed plan
    gets a new ``plan_version`` and a new permission hash, so the old row can
    never authorise the new plan.
    """

    id = models.BigAutoField(primary_key=True)
    plan = models.ForeignKey(
        "core.PortfolioPlan",
        on_delete=models.CASCADE,
        related_name="approvals",
    )
    plan_version = models.PositiveIntegerField()
    #: The ``PortfolioPlan.approval_hash`` at decision time. Empty only for a
    #: cancellation recorded before a binding existed.
    approval_hash = models.CharField(max_length=64, blank=True, default="")
    decision = models.CharField(max_length=16, choices=PlanApprovalDecision.choices)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="portfolio_plan_approvals",
    )
    decided_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    #: The evaluator's portfolio version at decision time. A reviewer should be
    #: able to tell whether the plan already looked stale when it was approved.
    evaluated_portfolio_version = models.CharField(max_length=64, blank=True, default="")
    #: The exact terms the reviewer approved. ``approved_legs`` is a list of
    #: ``{sequence, ticker, side, quantity, reference_price, quote_observed_at,
    #: quote_retrieved_at, quote_source}``. ``terms_hash`` is the canonical hash
    #: of that list plus the tolerances and risk versions below, so a plan that
    #: changed without a new version is detectable.
    approved_legs = models.JSONField(default=list, blank=True)
    terms_hash = models.CharField(max_length=64, blank=True, default="")
    price_tolerance_pct = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    quantity_tolerance = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    max_quote_age_seconds = models.PositiveIntegerField(null=True, blank=True)
    policy_version = models.CharField(max_length=64, blank=True, default="")
    classification_version = models.CharField(max_length=64, blank=True, default="")
    allocation_policy_version = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "Plan approval"
        verbose_name_plural = "Plan approvals"
        ordering = ["-decided_at", "-id"]
        indexes = [
            models.Index(fields=["plan", "-decided_at"], name="approval_plan_recent_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(plan_version__gte=1), name="approval_plan_version_positive"
            ),
            models.CheckConstraint(
                condition=(~Q(decision="approved") | Q(expires_at__isnull=False)),
                name="approval_requires_expiry",
            ),
        ]

    def __str__(self) -> str:
        return f"approval {self.plan_id} v{self.plan_version} [{self.decision}]"


class PlanExecutionAttempt(models.Model):
    """One attempt to apply a plan, successful or not.

    The plan row records the outcome of the newest attempt. This row keeps every
    attempt, so a failed attempt that was overtaken by a successful one remains
    visible without either row pretending to be the other. ``attempt_id`` is the
    identity the delayed failure handler conditions on; ``attempt_number`` is
    for humans and may repeat after a rollback.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        PortfolioPlan,
        on_delete=models.CASCADE,
        related_name="execution_attempts",
    )
    attempt_id = models.UUIDField(unique=True)
    attempt_number = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, default="running")
    mode = models.CharField(max_length=8, blank=True, default="")
    expected_plan_version = models.PositiveIntegerField(default=1)
    expected_portfolio_version = models.CharField(max_length=64, blank=True, default="")
    price_tolerance_pct = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    transaction_ids = models.JSONField(default=list, blank=True)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_detail = models.TextField(blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Plan execution attempt"
        verbose_name_plural = "Plan execution attempts"
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["plan", "-started_at"], name="plan_attempt_recent_idx"),
        ]

    def __str__(self) -> str:
        return f"plan {self.plan_id} attempt {self.attempt_number} [{self.status}]"
