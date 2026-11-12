"""Shared execution types: the guard decision and the execution error hierarchy.

This module has no dependency on any other service module. It exists so the
executor (:mod:`services.execution`), the decision lifecycle
(:mod:`services.decision_lifecycle`) and the audit writer
(:mod:`services.execution_audit`) can all name the same contract without
importing each other in a cycle. The executor re-exports every name here, so
``services.execution`` remains the public import path for callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.db import DatabaseError

__all__ = [
    "ZERO",
    "ExecutionConflict",
    "ExecutionError",
    "ExecutionRejected",
    "GuardDecision",
    "GuardError",
    "MaterialChangeError",
    "PolicyChangedError",
    "ValuationUnavailableError",
]

ZERO = Decimal("0")


class ExecutionError(DatabaseError):
    """An execution was refused for a reason the caller must handle.

    Subclasses ``DatabaseError`` deliberately: every existing caller already
    treats a database error during execution as "the fill did not happen" and
    never as "retry with the same size". A refusal is never a partial write;
    the enclosing atomic block rolls back.
    """


class GuardError(ExecutionError):
    """An execution was attempted with a decision the guard never approved.

    Raised instead of using ``assert``: assertions are silently stripped when
    Python runs with ``-O``/``PYTHONOPTIMIZE``, which would let an unvalidated
    decision reach the ledger in exactly the environment where the check matters
    most. `apply_trade` is the only path that mutates money, so its preconditions
    fail loudly and unconditionally.
    """


class ExecutionRejected(ExecutionError):
    """The policy refused the trade. Nothing was written.

    ``code`` is the stable machine-readable reason the review asked for:
    ``missing_quote``, ``stale_quote``, ``future_quote``, ``untimed_quote``,
    ``fallback_quote_disallowed``, ``autonomy_disabled``, ``material_change``,
    ``approval_expired`` or ``legacy_unknown_provenance``. The human message
    stays in ``reason`` and is never parsed by a caller.
    """

    code = "execution_rejected"

    def __init__(
        self,
        reason: str,
        *,
        decision: GuardDecision | None = None,
        code: str | None = None,
        revised_terms: dict | None = None,
    ) -> None:
        self.reason = reason
        self.decision = decision
        if code:
            self.code = code
        self.revised_terms = revised_terms or {}
        super().__init__(reason)


class MaterialChangeError(ExecutionRejected):
    """The decision changed between approval and execution.

    A different price, a different permitted quantity or a changed policy means
    the approval no longer describes what would be executed. The safe answer is
    to require a new approval, not to execute the new decision under the old one.
    """

    code = "material_change"

    def __init__(
        self,
        reason: str,
        *,
        decision: GuardDecision | None = None,
        code: str | None = None,
        revised_terms: dict | None = None,
    ) -> None:
        super().__init__(
            reason,
            decision=decision,
            code=code or self.code,
            revised_terms=revised_terms,
        )


class PolicyChangedError(MaterialChangeError):
    """The portfolio's policy changed after the decision was approved."""

    code = "policy_changed"


class ValuationUnavailableError(ExecutionRejected):
    """A current, finite, positive mark is missing for a material position.

    Substituting average cost for a missing mark would make an authoritative
    risk number out of a stale accounting figure. No new risk is taken when the
    portfolio cannot be valued; a risk-reducing sale may still be evaluated.
    """

    code = "missing_quote"

    def __init__(
        self,
        reason: str,
        *,
        code: str | None = None,
        decision: GuardDecision | None = None,
        revised_terms: dict | None = None,
    ) -> None:
        super().__init__(
            reason,
            decision=decision,
            code=code or self.code,
            revised_terms=revised_terms,
        )


class ExecutionConflict(ExecutionRejected):
    """An execution key was reused with a different request."""

    code = "execution_key_conflict"


@dataclass
class GuardDecision:
    """Outcome of validating an AI proposal against the portfolio guardrails."""

    approved: bool
    reason: str
    action: str
    amount: Decimal = ZERO
    price: Decimal | None = None
    notional: Decimal = ZERO
    clamped: bool = False
    notes: list[str] = field(default_factory=list)
    #: The amount the caller asked for, before any clamp. ``amount`` is what the
    #: guard will actually permit.
    requested_amount: Decimal = ZERO
    #: Provenance of the evaluation. Persisted on ``GuardRecord`` so a clamp can
    #: be read against the policy and the mark that produced it.
    policy_version: str = ""
    evaluated_portfolio_version: str = ""
    quote_time: datetime | None = None
    quote_source: str = ""
    quote_retrieved_at: datetime | None = None
    approved_price: Decimal | None = None
    approved_quantity: Decimal | None = None
    price_tolerance_pct: Decimal | None = None
    quantity_tolerance: Decimal | None = None
    approval_ref: str = ""
    binding_rule: str = ""
    reason_codes: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    revised_terms: dict = field(default_factory=dict)
    risk_exception: dict = field(default_factory=dict)
    material_marks: dict = field(default_factory=dict)

    def reference_terms(self) -> dict:
        """The approval terms and observations each check was run against."""
        return {
            "approval_ref": self.approval_ref or None,
            "approved_price": (
                str(self.approved_price) if self.approved_price is not None else None
            ),
            "approved_quantity": (
                str(self.approved_quantity) if self.approved_quantity is not None else None
            ),
            "price_tolerance_pct": (
                str(self.price_tolerance_pct) if self.price_tolerance_pct is not None else None
            ),
            "quantity_tolerance": (
                str(self.quantity_tolerance) if self.quantity_tolerance is not None else None
            ),
            "quote_observed_at": self.quote_time.isoformat() if self.quote_time else None,
            "quote_retrieved_at": (
                self.quote_retrieved_at.isoformat() if self.quote_retrieved_at else None
            ),
            "quote_source": self.quote_source or None,
            "risk_exception": dict(self.risk_exception or {}),
            "material_marks": dict(self.material_marks or {}),
        }
