"""Phase 3 measurement tables: cash flows, experiment manifests and scheduled runs.

Why these live beside ``core.models`` rather than inside it
-----------------------------------------------------------
``core.models`` is the Phase 0 schema and its tables are a consumer contract
(DATA-04). The tables here are additive, and they exist for one reason: a report
must be able to say where every number came from.

* **``CashFlow`` records money entering or leaving the portfolio** that is not a
  trade. It is deliberately a separate table rather than new ``TxType`` values.
  ``services.ledger.replay_positions`` treats every non-BUY row as a SELL, so a
  ``DEPOSIT`` row in ``Transaction`` would silently be booked as a share sale and
  corrupt positions and realised P&L. It would also not fit: ``tx_type`` is
  ``varchar(4)``. Keeping every ``Transaction`` a trade preserves both
  invariants.
* **``Experiment`` / ``ExperimentVariant`` / ``ExperimentRun`` record a frozen
  manifest and every scheduled run**, including the ones that never produced a
  decision run and the ones that failed. A denominator that only counts
  successful runs is a completion rate of 100% by construction; storing the
  scheduled rows is what makes the failure rate checkable.
* **``Transaction.fee`` is nullable.** ``NULL`` means *no fee was recorded for
  this fill*; ``Decimal("0")`` means *a fee was recorded and it was zero*. The
  distinction is load-bearing: a report that quietly treats an unrecorded fee as
  zero overstates net performance, and the difference is invisible after the
  fact. No migration backfills existing rows - inventing a zero fee for a fill
  nobody measured would be exactly the fabrication the accounting layer exists
  to prevent.

Timestamps are UTC (``USE_TZ = True``) and money is ``Decimal`` (DATA-02).
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

__all__ = [
    "CashFlow",
    "CashFlowType",
    "Experiment",
    "ExperimentEvaluationMode",
    "ExperimentRun",
    "ExperimentRunStatus",
    "ExperimentStatus",
    "ExperimentVariant",
    "PrimaryMetricFamily",
]


# ---------------------------------------------------------------------------
# External cash flows
# ---------------------------------------------------------------------------
class CashFlowType(models.TextChoices):
    """Direction of money moving in or out of the portfolio."""

    DEPOSIT = "DEPOSIT", "Deposit"
    WITHDRAWAL = "WITHDRAWAL", "Withdrawal"


class CashFlow(models.Model):
    """Cash that enters or leaves the portfolio without being a trade.

    An external flow changes equity but is not investment performance. Time-
    weighted return uses these rows as sub-period boundaries so a deposit cannot
    be mistaken for a return. ``amount`` is always positive; ``flow_type`` gives
    the direction, so a withdrawal can never be recorded as a negative deposit
    that a reader might sum with the wrong sign.
    """

    id = models.BigAutoField(primary_key=True)
    portfolio = models.ForeignKey(
        "core.Portfolio",
        on_delete=models.CASCADE,
        related_name="cash_flows",
    )
    flow_type = models.CharField(max_length=12, choices=CashFlowType.choices)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    #: Where the row came from (``manual``, ``fixture``, a broker statement id).
    #: Blank means unrecorded, not verified.
    source = models.CharField(max_length=64, blank=True, default="")
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "External cash flow"
        verbose_name_plural = "External cash flows"
        ordering = ["occurred_at", "id"]
        indexes = [
            models.Index(fields=["portfolio", "occurred_at"], name="cashflow_portfolio_time_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="cashflow_amount_positive"),
            models.CheckConstraint(
                condition=Q(flow_type__in=["DEPOSIT", "WITHDRAWAL"]),
                name="cashflow_type_known",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.flow_type} {self.amount} {self.currency} @ {self.occurred_at:%Y-%m-%d}"

    @property
    def signed_amount(self):
        """Cash effect: positive for a deposit, negative for a withdrawal."""
        return self.amount if self.flow_type == CashFlowType.DEPOSIT else -self.amount


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------
class ExperimentStatus(models.TextChoices):
    """Whether a manifest has been resolved and a report built from it."""

    DECLARED = "declared", "Declared, report not built"
    READY = "ready", "Report built"
    INVALID = "invalid", "Manifest could not be resolved"


class ExperimentEvaluationMode(models.TextChoices):
    """How the experiment's claims are to be read.

    ``historical`` is the strict mode: every run must have a point-in-time
    snapshot and no evidence may have been available after its cutoff, or the
    experiment is ineligible for strict historical claims. ``forward`` is a
    pre-registered run in which the outcome is not yet known when the manifest is
    frozen.
    """

    HISTORICAL = "historical", "Historical reconstruction"
    FORWARD = "forward", "Pre-registered forward run"


class PrimaryMetricFamily(models.TextChoices):
    """The two metric families. A report must never blend them."""

    SIGNAL_DIAGNOSTICS = "signal_diagnostics", "Signal diagnostics (analytical)"
    PORTFOLIO_PERFORMANCE = "portfolio_performance", "Portfolio performance (accounting)"


class Experiment(models.Model):
    """A frozen, versioned comparison definition.

    The whole manifest is stored as JSON because it is a document: reviewers
    diff it, the command replays it, and the report quotes it. The relational
    columns exist for the things a query has to filter on - ownership, the
    window, the frozen status and the report version - not to normalise the
    document into fragments that would then drift.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio = models.ForeignKey(
        "core.Portfolio",
        on_delete=models.CASCADE,
        related_name="experiments",
    )
    #: Stable natural key from the manifest (``experiment_id``). Replaying the
    #: same manifest updates this experiment rather than creating a second one.
    experiment_key = models.CharField(max_length=96, db_index=True)
    label = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")

    manifest_version = models.PositiveSmallIntegerField()
    schema_version = models.CharField(max_length=16)
    manifest_checksum = models.CharField(max_length=64)
    declared_at = models.DateTimeField()
    frozen = models.BooleanField(default=True)

    observation_start = models.DateTimeField()
    observation_end = models.DateTimeField()
    evaluation_mode = models.CharField(
        max_length=16,
        choices=ExperimentEvaluationMode.choices,
        default=ExperimentEvaluationMode.HISTORICAL,
    )

    #: Pre-registered primary metric. Stored as an object because it carries the
    #: horizon and direction alongside its id; a string alone cannot be checked.
    primary_metric = models.JSONField(default=dict)
    sample_requirements = models.JSONField(default=dict)
    cost_ceiling_usd = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    acceptable_drawdown_pct = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )

    manifest = models.JSONField(default=dict)
    report = models.JSONField(default=dict, blank=True)
    report_version = models.PositiveIntegerField(default=0)
    report_generated_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=12,
        choices=ExperimentStatus.choices,
        default=ExperimentStatus.DECLARED,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Experiment"
        verbose_name_plural = "Experiments"
        ordering = ["-declared_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "experiment_key"],
                name="experiment_unique_key",
            ),
            models.CheckConstraint(
                condition=Q(observation_end__gte=models.F("observation_start")),
                name="experiment_window_ordered",
            ),
            models.CheckConstraint(
                condition=Q(cost_ceiling_usd__isnull=True) | Q(cost_ceiling_usd__gte=0),
                name="experiment_cost_ceiling_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(acceptable_drawdown_pct__isnull=True)
                | Q(acceptable_drawdown_pct__gte=0),
                name="experiment_drawdown_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["portfolio", "-declared_at"], name="experiment_portfolio_recent"),
        ]

    def __str__(self) -> str:
        return f"{self.experiment_key} (v{self.report_version}, {self.status})"

    @property
    def has_report(self) -> bool:
        return bool(self.report) and self.report_version > 0


class ExperimentVariant(models.Model):
    """One declared comparison arm (V0-V4).

    ``implemented`` is explicit rather than inferred from whether runs happen to
    exist. V4 (portfolio allocation) is defined and unimplemented; a report must
    say so instead of presenting an empty result as a measured zero.
    """

    id = models.BigAutoField(primary_key=True)
    experiment = models.ForeignKey(
        Experiment,
        on_delete=models.CASCADE,
        related_name="variants",
    )
    key = models.CharField(max_length=8)
    label = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")

    engine = models.CharField(max_length=48)
    protocol_version = models.CharField(max_length=64, blank=True, default="")
    requires_rebuttals = models.BooleanField(default=False)
    implemented = models.BooleanField(default=True)
    unimplemented_reason = models.TextField(blank=True, default="")

    #: Provider/model/parameters for an LLM variant (EXP-02). Empty for V0.
    model_configuration = models.JSONField(default=dict)
    #: Declared caps, for the equal-budget comparison.
    budget = models.JSONField(default=dict)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "Experiment variant"
        verbose_name_plural = "Experiment variants"
        ordering = ["experiment", "display_order", "key"]
        constraints = [
            models.UniqueConstraint(
                fields=["experiment", "key"], name="variant_unique_key_per_experiment"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.experiment.experiment_key}/{self.key}"


class ExperimentRunStatus(models.TextChoices):
    """The fate of one *scheduled* run.

    ``not_executed`` is not a synonym for success: it is a scheduled
    observation that produced no stored output. It stays in the denominator.
    """

    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    NOT_EXECUTED = "not_executed", "Scheduled but not executed"


class ExperimentRun(models.Model):
    """One scheduled observation in an experiment.

    A run is registered from the manifest even when no ``DecisionRun`` exists.
    That is the point: dropping an unexecuted or failed schedule from the table
    would make completion and failure rates unmeasurable and would let a broken
    variant look clean by omission.
    """

    id = models.BigAutoField(primary_key=True)
    experiment = models.ForeignKey(
        Experiment,
        on_delete=models.CASCADE,
        related_name="scheduled_runs",
    )
    variant = models.ForeignKey(
        ExperimentVariant,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    run_key = models.CharField(max_length=96)
    scheduled_at = models.DateTimeField()

    status = models.CharField(
        max_length=16,
        choices=ExperimentRunStatus.choices,
        default=ExperimentRunStatus.NOT_EXECUTED,
        db_index=True,
    )
    decision_run = models.ForeignKey(
        "core.DecisionRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="experiment_runs",
    )

    #: Why a schedule failed or never ran. Kept as a typed code plus a readable
    #: detail so the report can group failures without parsing prose.
    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_detail = models.TextField(blank=True, default="")

    #: Copied from the stored run at build time so a later edit to the run
    #: cannot silently change an already-published report. Null means the value
    #: was not reported; zero is a real reported value.
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)

    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Experiment run"
        verbose_name_plural = "Experiment runs"
        ordering = ["experiment", "scheduled_at", "run_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["experiment", "run_key"], name="scheduled_run_unique_key"
            ),
            models.CheckConstraint(
                condition=Q(cost_usd__isnull=True) | Q(cost_usd__gte=0),
                name="scheduled_run_cost_non_negative",
            ),
        ]
        indexes = [
            models.Index(
                fields=["experiment", "variant", "scheduled_at"],
                name="scheduled_run_variant_time_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run_key} [{self.status}]"

    @property
    def was_attempted(self) -> bool:
        return self.status != ExperimentRunStatus.NOT_EXECUTED
