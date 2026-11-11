"""The real decision lifecycle (FIX-01).

Analysis completion and decision completion are different events. The debate
protocol now stops at ``validating``; this module is what moves a run past it,
and it only does so once a guard result actually exists.

Guarantees:

* **A proposal is never a fill.** ``DecisionRun.status`` says where the analysis
  is; ``execution_state`` says what happened afterwards. Awaiting approval,
  rejected, expired, held and executed are distinct values.
* **The guard record is the evaluation that was executed**, not a read-only
  simulation: ``execute_trade`` returns the under-lock decision and this module
  persists it.
* **Handlers are idempotent.** The guard row and every completion event carry a
  stable key, so retrying a handler returns the first row instead of appending a
  second one.
* **HOLD has an explicit no-trade completion.** No ``GuardRecord`` is fabricated
  for a proposal that requested no quantity; the event says ``executed: false``
  and names the reason.
"""

from __future__ import annotations

import logging
from contextlib import suppress

from django.db import transaction
from django.utils import timezone

from core.decision_json import content_hash, json_safe, utc_iso
from core.decision_models import (
    AgentRole,
    DecisionExecutionState,
    DecisionRecord,
    DecisionRun,
    DecisionStatus,
    GuardRecord,
    GuardStatus,
    RunEventType,
    RunStage,
)
from core.models import TradeRecommendation
from services.decision_runs import append_event, can_transition, transition_status
from services.execution import GuardDecision

logger = logging.getLogger("alphaagent.decision_lifecycle")

__all__ = [
    "complete_run",
    "record_guard_evaluation",
]

#: Which terminal execution states mean "a fill happened".
_EXECUTED_STATES = {DecisionExecutionState.EXECUTED}
#: Which execution states are a rejection rather than a no-trade decision.
_REJECTED_STATES = {
    DecisionExecutionState.REJECTED,
    DecisionExecutionState.BLOCKED,
    DecisionExecutionState.EXPIRED,
    DecisionExecutionState.FAILED,
}


def _guard_status(decision: GuardDecision) -> str:
    if not decision.approved:
        return GuardStatus.REJECTED
    if decision.clamped or decision.amount != decision.requested_amount:
        return GuardStatus.CLAMPED
    return GuardStatus.APPROVED


def record_guard_evaluation(
    run: DecisionRun,
    guard_decision: GuardDecision,
    *,
    policy_version: str = "",
    evaluated_portfolio_version: str = "",
    quote_time=None,
    decision_price=None,
) -> GuardRecord | None:
    """Persist the typed guard result for the exact evaluation used.

    The key is derived from the run, the proposal and the numeric outcome. A
    retried handler re-evaluates the same proposal against the same state and
    produces the same key; a genuinely new evaluation (for example the advisory
    approval rerun) produces a second row, which is correct: both were real.
    """
    if guard_decision is None:
        return None
    try:
        decision_record = run.decision
    except DecisionRecord.DoesNotExist:
        return None
    requested = guard_decision.requested_amount or guard_decision.amount
    if requested <= 0:
        # A HOLD (or an unparseable amount) has no quantity to compare. No guard
        # record is fabricated; the no-trade completion event is the record.
        return None
    # A rejected evaluation permitted nothing. Keeping a non-zero "allowed" on a
    # rejection would read like a partial approval in the detail screen.
    allowed = guard_decision.amount if guard_decision.approved else 0
    key_payload = {
        "run_id": str(run.id),
        "proposal_id": str(decision_record.id),
        "status": _guard_status(guard_decision),
        "requested": str(requested),
        "allowed": str(allowed),
        "policy_version": policy_version,
        "portfolio_version": evaluated_portfolio_version,
        "quote_time": utc_iso(quote_time or guard_decision.quote_time),
        "price": str(decision_price) if decision_price is not None else None,
    }
    evaluation_key = f"guard:{run.id}:{content_hash(key_payload)[:32]}"
    defaults = {
        "policy_version": policy_version or guard_decision.policy_version or "unknown",
        "evaluated_portfolio_version": (
            evaluated_portfolio_version or guard_decision.evaluated_portfolio_version or "unknown"
        ),
        "quote_time": quote_time or guard_decision.quote_time or timezone.now(),
        "status": _guard_status(guard_decision),
        "requested_quantity": requested,
        "allowed_quantity": allowed,
        "reason_codes": list(guard_decision.reason_codes or []),
        "binding_rule": guard_decision.binding_rule or "",
        "numerical_checks": json_safe(
            {
                "checks": list(guard_decision.checks or []),
                "reason": guard_decision.reason,
                "notes": list(guard_decision.notes or []),
                "quote_price": str(decision_price) if decision_price is not None else None,
            }
        ),
    }
    record, created = GuardRecord.objects.get_or_create(
        proposal=decision_record, evaluation_key=evaluation_key, defaults=defaults
    )
    if not created:
        # Never rewrite an evaluation that produced an execution. The key already
        # guarantees it describes the same numbers; leaving the row intact keeps
        # the historical record honest.
        return record
    return record


def _set_execution_state(run: DecisionRun, state: str) -> None:
    if run.execution_state == state:
        return
    DecisionRun.objects.filter(pk=run.pk).update(execution_state=state, updated_at=timezone.now())
    run.execution_state = state


def _ensure_status(run: DecisionRun, target: str) -> None:
    current = DecisionRun.objects.filter(pk=run.pk).values_list("status", flat=True).first()
    if current is None or current == target:
        return
    if not can_transition(current, target):
        # A retried handler may find the run already past this point. That is a
        # completed lifecycle, not an error to raise into a task.
        if current in {
            DecisionStatus.COMPLETED,
            DecisionStatus.FAILED,
            DecisionStatus.CANCELLED,
        }:
            return
        raise IllegalTransitionGuard(run.id, current, target)
    transition_status(run, target, payload={"execution_state": run.execution_state})


class IllegalTransitionGuard(Exception):
    """Internal guard: a completion handler tried to move a run backwards."""

    def __init__(self, run_id, current: str, target: str) -> None:
        self.run_id = run_id
        self.current = current
        self.target = target
        super().__init__(f"Decision run {run_id} is {current!r} and cannot complete as {target!r}.")


@transaction.atomic
def complete_run(
    run: DecisionRun | None,
    *,
    outcome: str,
    guard_decision: GuardDecision | None = None,
    transaction_row=None,
    recommendation: TradeRecommendation | None = None,
    reason: str = "",
    via: str = "",
    error_code: str = "",
    error_detail: str = "",
    requires_approval: bool = False,
    occurred_at=None,
) -> DecisionRun | None:
    """Record the terminal (or awaiting-approval) state for one run.

    ``outcome`` is a :class:`DecisionExecutionState` value. The function is safe
    to call from a retried task or a repeated approval: every write is keyed.
    """
    if run is None:
        return None
    at = occurred_at or timezone.now()
    decision_record = None
    with suppress(DecisionRecord.DoesNotExist):
        decision_record = run.decision

    guard = None
    if guard_decision is not None and decision_record is not None:
        guard = record_guard_evaluation(
            run,
            guard_decision,
            policy_version=guard_decision.policy_version,
            evaluated_portfolio_version=guard_decision.evaluated_portfolio_version,
            quote_time=guard_decision.quote_time,
            decision_price=guard_decision.price,
        )
    if guard is not None:
        append_event(
            run,
            RunEventType.GUARD_EVALUATED,
            stage=RunStage.VALIDATING,
            agent_role=AgentRole.SYSTEM,
            payload={
                "guard_id": str(guard.id),
                "status": guard.status,
                "requested_quantity": str(guard.requested_quantity),
                "allowed_quantity": str(guard.allowed_quantity),
                "binding_rule": guard.binding_rule,
                "policy_version": guard.policy_version,
                "quote_time": utc_iso(guard.quote_time),
            },
            occurred_at=at,
            dedupe_key=f"guard_evaluated:{guard.id}",
        )

    base_payload = {
        "decision_id": str(decision_record.id) if decision_record is not None else None,
        "guard_id": str(guard.id) if guard is not None else None,
        "transaction_id": getattr(transaction_row, "id", None),
        "recommendation_id": recommendation.id if recommendation is not None else None,
        "execution_state": outcome,
        "executed": transaction_row is not None,
        "reason": reason,
        "via": via or None,
    }

    if requires_approval:
        _set_execution_state(run, DecisionExecutionState.AWAITING_APPROVAL)
        append_event(
            run,
            RunEventType.APPROVAL_REQUESTED,
            stage=RunStage.AWAITING_APPROVAL,
            agent_role=AgentRole.SYSTEM,
            payload={
                **base_payload,
                "expires_at": utc_iso(recommendation.expires_at)
                if recommendation is not None
                else None,
            },
            occurred_at=at,
            dedupe_key=f"approval_requested:{recommendation.id if recommendation else 'none'}",
        )
        _ensure_status(run, DecisionStatus.AWAITING_APPROVAL)
        return run

    _set_execution_state(run, outcome)

    if recommendation is not None:
        # The human decision that resolved the pending proposal. Its key includes
        # the outcome, so a retried expiry/rejection/approval handler writes once.
        append_event(
            run,
            RunEventType.APPROVAL_DECIDED,
            stage=RunStage.AWAITING_APPROVAL,
            agent_role=AgentRole.SYSTEM,
            payload={**base_payload, "outcome": outcome, "via": via or None},
            occurred_at=at,
            dedupe_key=f"approval_decided:{recommendation.id}:{outcome}",
        )

    if outcome in _EXECUTED_STATES:
        event_type = RunEventType.EXECUTION_RECORDED
        stage = RunStage.COMPLETED
    elif outcome == DecisionExecutionState.FAILED:
        event_type = RunEventType.EXECUTION_FAILED
        stage = RunStage.FAILED
    elif outcome in _REJECTED_STATES:
        event_type = RunEventType.EXECUTION_REJECTED
        stage = RunStage.COMPLETED
    else:
        event_type = RunEventType.DECISION_COMPLETED
        stage = RunStage.COMPLETED

    event_key = (
        f"execution:{outcome}:{getattr(transaction_row, 'id', None)}:"
        f"{getattr(recommendation, 'id', None)}"
    )
    if event_type != RunEventType.DECISION_COMPLETED:
        append_event(
            run,
            event_type,
            stage=stage,
            agent_role=AgentRole.SYSTEM,
            payload={**base_payload, "no_trade": transaction_row is None},
            occurred_at=at,
            dedupe_key=event_key,
        )
    append_event(
        run,
        RunEventType.DECISION_COMPLETED,
        stage=RunStage.COMPLETED,
        agent_role=AgentRole.SYSTEM,
        payload={**base_payload, "no_trade": transaction_row is None},
        occurred_at=at,
        dedupe_key=f"decision_completed:{outcome}:{getattr(transaction_row, 'id', None)}",
    )

    # A failed execution is a failed decision; every other terminal state keeps
    # ``completed`` so the analysis and the outcome are both on the record.
    target = (
        DecisionStatus.FAILED
        if outcome == DecisionExecutionState.FAILED
        else DecisionStatus.COMPLETED
    )
    current = DecisionRun.objects.filter(pk=run.pk).values_list("status", flat=True).first()
    if target == DecisionStatus.FAILED:
        if current != DecisionStatus.FAILED:
            transition_status(
                run,
                target,
                error_code=error_code or "execution_failed",
                error_detail=error_detail or reason,
                occurred_at=at,
            )
    else:
        _ensure_status(run, target)

    logger.info(
        "Decision run %s completed as %s (executed=%s)",
        run.id,
        outcome,
        transaction_row is not None,
    )
    return run
