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

from django.db import models
from django.db.models import Q

__all__ = [
    "EXECUTION_MODE_LIVE",
    "EXECUTION_MODE_PAPER",
    "InstrumentClassification",
    "InstrumentClassificationSource",
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
    """

    PROPOSED = "proposed", "Proposed"
    APPLIED = "applied", "Applied"
    NO_CHANGE = "no_change", "No change"
    FAILED = "failed", "Failed"
    SUPERSEDED = "superseded", "Superseded"
    CANCELLED = "cancelled", "Cancelled"


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

    #: What the plan was approved against: the limits, classifications, cost
    #: assumptions and allocation that decide whether it is still permitted.
    #: ``approval_hash`` is the canonical hash of ``approval_binding``; execution
    #: recomputes it under the lock and refuses a plan whose permissions moved.
    approval_binding = models.JSONField(default=dict)
    approval_hash = models.CharField(max_length=64, blank=True, default="")

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
        }

    @property
    def is_applicable(self) -> bool:
        return self.status == PlanStatus.PROPOSED and not self.revalidation_required


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
