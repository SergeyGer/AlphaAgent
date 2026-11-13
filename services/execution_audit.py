"""Durable execution audit: the fill, the guard and the run resolution in one transaction.

The defect this module closes is a committed fill with no terminal decision
record. The order used to be ``execute_trade`` (commit) then ``complete_run``
(second transaction). A worker that died between them left a ledger row the
retry path returned as "already executed" while the run stayed
``awaiting_approval`` forever.

The rule now is:

* a fill, its exact final guard evaluation, the execution-attempt row, the run's
  execution state and the durable event are written in the caller's transaction;
* model calls and external notifications are outside it;
* a repeated execution key re-reads the attempt and repairs a missing link
  before returning, and
* a no-trade terminal outcome is persisted under its own stable identity, so a
  replay returns the original resolution.

Normal states: ``executed`` (fill), ``held``, ``blocked``, ``rejected``,
``expired``, ``cancelled``, ``failed`` and ``suppressed`` (duplicate advisory
proposal). Recovery states are a missing attempt, an attempt without a terminal
event, and a run that is still non-terminal while a fill exists. The
``ExecutionAttempt`` row identifies the attempt; the API returns its id, the
transaction id, the run id and the terminal event id.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.decision_json import json_safe, utc_iso
from core.decision_models import (
    DecisionExecutionState,
    DecisionRun,
    ExecutionAttempt,
    GuardRecord,
    RunEvent,
    RunEventType,
)
from core.models import TradeRecommendation, Transaction
from services.decision_lifecycle import complete_run
from services.event_outbox import deliver_now, enqueue_event
from services.events import EventType

logger = logging.getLogger("alphaagent.execution_audit")

__all__ = [
    "ExecutionAudit",
    "ensure_fill_attempt",
    "record_fill_outcome",
    "record_terminal_outcome",
    "resume_execution_audit",
    "terminal_event_for",
]

TERMINAL_STATES = frozenset(
    {
        DecisionExecutionState.EXECUTED,
        DecisionExecutionState.HELD,
        DecisionExecutionState.BLOCKED,
        DecisionExecutionState.REJECTED,
        DecisionExecutionState.EXPIRED,
        DecisionExecutionState.CANCELLED,
        DecisionExecutionState.FAILED,
        DecisionExecutionState.SUPPRESSED,
    }
)


class ExecutionAuditError(Exception):
    """The audit context is not sufficient to complete the execution record."""


class LegacyExecutionProvenance(ExecutionAuditError):
    """A fill exists with no attempt record and no terms that can be validated."""

    code = "legacy_unknown_provenance"


class ExecutionAudit:
    """What the caller knows about the decision this fill resolves.

    It is deliberately plain data: no model call, no notification and no query
    happens while it is being built.
    """

    def __init__(
        self,
        *,
        run: DecisionRun | None = None,
        recommendation: TradeRecommendation | None = None,
        outcome: str = DecisionExecutionState.EXECUTED,
        reason: str = "",
        via: str = "",
        user_id: int | None = None,
        approved_terms: dict | None = None,
        duplicate_recommendation: TradeRecommendation | None = None,
        execution_key: str = "",
    ) -> None:
        self.run = run
        self.recommendation = recommendation
        self.outcome = outcome
        self.reason = reason
        self.via = via
        self.user_id = user_id
        self.approved_terms = approved_terms or {}
        self.duplicate_recommendation = duplicate_recommendation
        self.execution_key = execution_key

    @property
    def resolved_key(self) -> str:
        if self.execution_key:
            return self.execution_key
        if self.recommendation is not None:
            return f"recommendation:{self.recommendation.id}"
        if self.run is not None:
            return f"run:{self.run.id}:{self.outcome}"
        return ""


def _attempt_for(key: str) -> ExecutionAttempt | None:
    if not key:
        return None
    return ExecutionAttempt.objects.filter(execution_key=key).first()


def _guard_for(run: DecisionRun | None, guard: GuardRecord | None = None) -> GuardRecord | None:
    if guard is not None:
        return guard
    if run is None:
        return None
    return GuardRecord.objects.filter(proposal__run=run).order_by("-created_at", "-id").first()


def _latest_terminal_event(run: DecisionRun, *, outcome: str) -> RunEvent | None:
    key = f"decision_completed:{outcome}"
    event = (
        run.events.filter(event_type=RunEventType.DECISION_COMPLETED, dedupe_key__startswith=key)
        .order_by("-sequence")
        .first()
    )
    if event is not None:
        return event
    return (
        run.events.filter(event_type=RunEventType.DECISION_COMPLETED).order_by("-sequence").first()
    )


def terminal_event_for(run: DecisionRun, *, outcome: str) -> RunEvent | None:
    return _latest_terminal_event(run, outcome=outcome)


def _decision_detail(decision: Any, reason: str = "") -> dict:
    if decision is None:
        return {"reason": reason or None}
    reference = {}
    if hasattr(decision, "reference_terms"):
        try:
            reference = decision.reference_terms()
        except Exception:  # pragma: no cover - presentation helper must not fail a write
            reference = {}
    return json_safe(
        {
            "reason": getattr(decision, "reason", "") or reason or None,
            "reason_codes": list(getattr(decision, "reason_codes", []) or []),
            "checks": list(getattr(decision, "checks", []) or []),
            "binding_rule": getattr(decision, "binding_rule", "") or None,
            "reference_terms": reference,
            "quote_time": utc_iso(getattr(decision, "quote_time", None)),
        }
    )


def _approved_terms(decision: Any, audit: ExecutionAudit, request=None) -> dict:
    terms = dict(audit.approved_terms or {})
    if hasattr(decision, "reference_terms"):
        try:
            terms.setdefault("decision_reference", decision.reference_terms())
        except Exception as exc:  # pragma: no cover - presentation helper
            logger.debug("Could not build decision reference terms: %s", exc)
    if request is not None:
        terms.setdefault(
            "request",
            {
                "ticker": request.ticker,
                "side": request.side,
                "quantity": str(request.quantity),
                "approved_price": (
                    str(request.approved_price) if request.approved_price is not None else None
                ),
                "approved_quantity": (
                    str(request.approved_quantity)
                    if request.approved_quantity is not None
                    else None
                ),
                "price_tolerance_pct": (
                    str(request.price_tolerance_pct)
                    if request.price_tolerance_pct is not None
                    else None
                ),
                "mode": request.mode,
                "source": request.source,
                "approval_ref": request.approval_ref or None,
            },
        )
    return json_safe(terms)


def _enqueue_execution_event(
    *,
    audit: ExecutionAudit,
    attempt: ExecutionAttempt,
    transaction_row: Transaction | None,
    run: DecisionRun | None,
    outcome: str,
) -> None:
    """Write the durable notification in the same transaction as the record."""
    user_id = audit.user_id
    if user_id is None and audit.recommendation is not None:
        user_id = audit.recommendation.portfolio.user_id
    if user_id is None and run is not None:
        user_id = run.portfolio.user_id
    if user_id is None:
        return
    payload = {
        "execution_attempt_id": str(attempt.id),
        "execution_key": attempt.execution_key,
        "state": outcome,
        "transaction_id": getattr(transaction_row, "id", None),
        "run_id": str(run.id) if run is not None else None,
        "recommendation_id": (
            audit.recommendation.id if audit.recommendation is not None else None
        ),
        "terminal_event_id": getattr(attempt.terminal_event, "id", None),
        "reason": audit.reason or None,
        "occurred_at": utc_iso(timezone.now()),
    }
    row = enqueue_event(
        user_id,
        "portfolio",
        EventType.DECISION_EXECUTION,
        payload,
        dedupe_key=f"execution-attempt:{attempt.id}",
    )
    if row is not None:
        transaction.on_commit(lambda row_id=row.id: deliver_now(row_id))


def _upsert_attempt(
    *,
    key: str,
    audit: ExecutionAudit,
    state: str,
    transaction_row: Transaction | None,
    guard: GuardRecord | None,
    terminal_event: RunEvent | None,
    request=None,
    decision: Any = None,
    recovered: bool = False,
) -> ExecutionAttempt:
    run = audit.run
    recommendation = audit.recommendation
    if not key:
        raise ExecutionAuditError("An execution attempt requires a stable key.")
    defaults = {
        "source": getattr(request, "source", "") if request is not None else "",
        "approval_ref": getattr(request, "approval_ref", "") if request is not None else "",
        "state": state,
        "transaction": transaction_row,
        "decision_run": run,
        "recommendation": recommendation,
        "guard_record": guard,
        "terminal_event": terminal_event,
        "approved_terms": _approved_terms(decision, audit, request),
        "decision_detail": _decision_detail(decision, audit.reason),
        "quote_observed_at": (
            getattr(request, "quote_time", None) if request is not None else None
        ),
        "quote_retrieved_at": (
            getattr(request, "quote_retrieved_at", None) if request is not None else None
        ),
        "quote_source": getattr(request, "quote_source", "") if request is not None else "",
        "completed_at": timezone.now() if state in TERMINAL_STATES else None,
        "detail": audit.reason or "",
        "recovered": recovered,
    }
    attempt, created = ExecutionAttempt.objects.get_or_create(execution_key=key, defaults=defaults)
    if not created:
        # Never rewrite the immutable identity of an attempt; only fill links a
        # worker or a repair has since established.
        update_fields: list[str] = []
        for field, value in defaults.items():
            if field in {"approved_terms", "decision_detail", "detail"}:
                if value and getattr(attempt, field) != value:
                    setattr(attempt, field, value)
                    update_fields.append(field)
                continue
            if (getattr(attempt, field) is None or getattr(attempt, field) == "") and value not in (
                None,
                "",
            ):
                setattr(attempt, field, value)
                update_fields.append(field)
        if update_fields:
            attempt.save(update_fields=sorted(set(update_fields)))
    return attempt


def ensure_fill_attempt(
    *,
    transaction_row: Transaction,
    decision: Any,
    request,
    audit: ExecutionAudit | None,
    recovered: bool = False,
) -> ExecutionAttempt | None:
    """Create or repair the attempt row for a committed fill.

    Called inside the ledger transaction. A direct caller with no audit context
    still gets an attempt, because the repeated-key path must be able to explain
    the fill it is returning.
    """
    key = (getattr(request, "execution_key", "") or "").strip()
    if not key:
        return None
    if audit is None:
        audit = ExecutionAudit(
            outcome=DecisionExecutionState.EXECUTED,
            user_id=getattr(transaction_row.portfolio, "user_id", None),
            approved_terms={},
        )
    run = audit.run
    guard = None
    terminal_event = None
    if run is not None:
        guard = _guard_for(run)
        terminal_event = _latest_terminal_event(run, outcome=audit.outcome)
    attempt = _upsert_attempt(
        key=key,
        audit=audit,
        state=audit.outcome,
        transaction_row=transaction_row,
        guard=guard,
        terminal_event=terminal_event,
        request=request,
        decision=decision,
        recovered=recovered,
    )
    _enqueue_execution_event(
        audit=audit,
        attempt=attempt,
        transaction_row=transaction_row,
        run=run,
        outcome=audit.outcome,
    )
    return attempt


def record_fill_outcome(
    *,
    run: DecisionRun | None,
    transaction_row: Transaction,
    decision: Any,
    request,
    recommendation: TradeRecommendation | None = None,
    reason: str = "",
    via: str = "",
    user_id: int | None = None,
    duplicate_recommendation: TradeRecommendation | None = None,
    approved_terms: dict | None = None,
) -> ExecutionAttempt | None:
    """Commit the fill's run resolution and attempt row in this transaction.

    ``approved_terms`` carries the exact displayed terms (including the
    server-issued reference) from the caller's ``ExecutionAudit``. Without it
    the attempt would record only the request shape, and the reference the
    operator saw would be lost at the moment the fill is written.
    """
    if run is None:
        return ensure_fill_attempt(
            transaction_row=transaction_row,
            decision=decision,
            request=request,
            audit=None,
        )
    run = DecisionRun.objects.select_for_update().get(pk=run.pk)
    audit = ExecutionAudit(
        run=run,
        recommendation=recommendation,
        outcome=DecisionExecutionState.EXECUTED,
        reason=reason or getattr(decision, "reason", ""),
        via=via,
        user_id=user_id,
        execution_key=(getattr(request, "execution_key", "") or "").strip(),
        approved_terms=approved_terms or {},
    )
    complete_run(
        run,
        outcome=DecisionExecutionState.EXECUTED,
        guard_decision=decision,
        transaction_row=transaction_row,
        recommendation=recommendation,
        reason=audit.reason,
        via=via,
    )
    run.refresh_from_db()
    return ensure_fill_attempt(
        transaction_row=transaction_row,
        decision=decision,
        request=request,
        audit=audit,
    )


def record_terminal_outcome(
    *,
    run: DecisionRun | None,
    outcome: str,
    recommendation: TradeRecommendation | None = None,
    guard_decision: Any = None,
    transaction_row: Transaction | None = None,
    reason: str = "",
    via: str = "",
    user_id: int | None = None,
    duplicate_recommendation: TradeRecommendation | None = None,
    execution_key: str = "",
) -> ExecutionAttempt | None:
    """Persist a terminal no-trade (or executed) resolution idempotently."""
    if outcome not in TERMINAL_STATES:
        raise ExecutionAuditError(f"{outcome!r} is not a terminal execution state.")
    if run is None and recommendation is None:
        return None
    if run is not None:
        run = DecisionRun.objects.select_for_update().get(pk=run.pk)
    key = execution_key or (
        f"recommendation:{recommendation.id}:{outcome}"
        if recommendation is not None
        else f"run:{run.id}:{outcome}"
    )
    existing = _attempt_for(key)
    if existing is not None:
        return existing

    if duplicate_recommendation is not None and run is not None:
        DecisionRun.objects.filter(pk=run.pk).update(
            duplicate_recommendation=duplicate_recommendation,
            updated_at=timezone.now(),
        )
        run.duplicate_recommendation = duplicate_recommendation

    if run is not None:
        complete_run(
            run,
            outcome=outcome,
            guard_decision=guard_decision if outcome != DecisionExecutionState.HELD else None,
            transaction_row=transaction_row,
            recommendation=recommendation,
            reason=reason,
            via=via,
        )
        run.refresh_from_db()
        guard = _guard_for(run)
        terminal_event = _latest_terminal_event(run, outcome=outcome)
    else:
        guard = None
        terminal_event = None

    audit = ExecutionAudit(
        run=run,
        recommendation=recommendation,
        outcome=outcome,
        reason=reason,
        via=via,
        user_id=user_id,
        duplicate_recommendation=duplicate_recommendation,
        execution_key=key,
    )
    attempt = _upsert_attempt(
        key=key,
        audit=audit,
        state=outcome,
        transaction_row=transaction_row,
        guard=guard,
        terminal_event=terminal_event,
        request=None,
        decision=guard_decision,
    )
    _enqueue_execution_event(
        audit=audit,
        attempt=attempt,
        transaction_row=transaction_row,
        run=run,
        outcome=outcome,
    )
    return attempt


def resume_execution_audit(
    *,
    transaction_row: Transaction,
    request,
    audit: ExecutionAudit | None,
) -> ExecutionAttempt | None:
    """Repair or return the audit for an already-committed fill.

    This runs before a repeated execution key is reported as complete. If the
    attempt is missing and the request carries no terms that can be validated,
    the fill is legacy provenance and a fresh approval is required rather than
    silently returning an unexplained ledger row.
    """
    key = (getattr(request, "execution_key", "") or "").strip()
    if not key:
        return None
    attempt = _attempt_for(key)
    if attempt is None:
        if audit is None or (
            audit.run is None and audit.recommendation is None and not (audit.approved_terms or {})
        ):
            raise LegacyExecutionProvenance(
                f"Execution key {key!r} already has a ledger row with no attempt record or "
                "approval terms. Its provenance cannot be validated; a fresh approval is "
                "required."
            )
        attempt = ensure_fill_attempt(
            transaction_row=transaction_row,
            decision=None,
            request=request,
            audit=audit,
            recovered=True,
        )
        return attempt

    run = attempt.decision_run
    if run is not None:
        run.refresh_from_db()
        if run.execution_state not in TERMINAL_STATES:
            complete_run(
                run,
                outcome=attempt.state or DecisionExecutionState.EXECUTED,
                transaction_row=transaction_row,
                recommendation=attempt.recommendation,
                reason=attempt.detail or "Recovered the terminal state for an existing fill.",
                via="recovery",
            )
            run.refresh_from_db()
        if attempt.terminal_event_id is None:
            event = _latest_terminal_event(run, outcome=attempt.state)
            if event is not None:
                attempt.terminal_event = event
                attempt.recovered = True
                attempt.save(update_fields=["terminal_event", "recovered"])
    return attempt
