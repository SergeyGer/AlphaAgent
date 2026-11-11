"""Run lifecycle: creation, legal transitions and the ordered event log.

Two rules shape this module.

**A run only moves where the protocol allows.** ``LEGAL_TRANSITIONS`` is the one
table every caller goes through. It is deliberately not a free-form status
update: a run that jumps from ``queued`` to ``completed`` has no evidence, no
verdict and no guard comparison, and the dashboard would have to render that
hole as if it were a decision. ``analysing`` may go straight to ``adjudicating``
because the current pipeline has no rebuttal stage; ``rebutting`` is only
reachable when a producer actually records rebuttals (Phase 2). A fixture that
needs the stage uses it, and is labelled ``synthetic``.

**Sequence allocation is transactional.** ``append_event`` locks the run row,
reads the current maximum sequence and inserts the next number in the same
transaction. The event is therefore persisted before anything is published
(API-03): a live consumer may see an event twice, but it can never see
``sequence=7`` before ``sequence=6`` exists. The notification is persisted in the
same transaction as an outbox row; delivery is retried, and the event log remains
the authority even when Redis is down. A realtime outage never fails a decision
run.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.decision_errors import DecisionRunError, IllegalTransition
from core.decision_json import json_safe, utc_iso
from core.decision_models import (
    EVENT_SCHEMA_VERSION,
    RUN_EVENT_TYPES,
    RUN_STAGE_ORDER,
    AgentRole,
    DecisionRun,
    DecisionStatus,
    InputSnapshot,
    RunEvent,
    RunEventType,
    RunMode,
    RunSource,
    RunStage,
)
from services.event_outbox import deliver_now, enqueue_event

logger = logging.getLogger("alphaagent.decision_runs")

__all__ = [
    "LEGAL_TRANSITIONS",
    "append_event",
    "can_transition",
    "create_run",
    "legal_targets",
    "link_legacy_log",
    "record_failure",
    "stage_states",
    "transition_status",
    "update_protocol_metadata",
]

TERMINAL_STATUSES = frozenset(
    {DecisionStatus.COMPLETED, DecisionStatus.FAILED, DecisionStatus.CANCELLED}
)

#: The one table that decides whether a status change is legal. Terminal states
#: have no outgoing edges: a completed decision is not edited afterwards.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    DecisionStatus.QUEUED: frozenset(
        {DecisionStatus.COLLECTING, DecisionStatus.FAILED, DecisionStatus.CANCELLED}
    ),
    DecisionStatus.COLLECTING: frozenset(
        {DecisionStatus.ANALYSING, DecisionStatus.FAILED, DecisionStatus.CANCELLED}
    ),
    # No rebutting edge from analysing is *not* required: the current pipeline
    # goes straight to adjudication, and pretending otherwise would fabricate the
    # stage in the event log.
    DecisionStatus.ANALYSING: frozenset(
        {
            DecisionStatus.REBUTTING,
            DecisionStatus.ADJUDICATING,
            DecisionStatus.FAILED,
            DecisionStatus.CANCELLED,
        }
    ),
    DecisionStatus.REBUTTING: frozenset(
        {DecisionStatus.ADJUDICATING, DecisionStatus.FAILED, DecisionStatus.CANCELLED}
    ),
    DecisionStatus.ADJUDICATING: frozenset(
        {DecisionStatus.VALIDATING, DecisionStatus.FAILED, DecisionStatus.CANCELLED}
    ),
    # A validated proposal either needs a human (awaiting approval) or is
    # complete. It may not jump back to collecting.
    DecisionStatus.VALIDATING: frozenset(
        {
            DecisionStatus.AWAITING_APPROVAL,
            DecisionStatus.COMPLETED,
            DecisionStatus.FAILED,
            DecisionStatus.CANCELLED,
        }
    ),
    DecisionStatus.AWAITING_APPROVAL: frozenset(
        {DecisionStatus.COMPLETED, DecisionStatus.FAILED, DecisionStatus.CANCELLED}
    ),
    DecisionStatus.COMPLETED: frozenset(),
    DecisionStatus.FAILED: frozenset(),
    DecisionStatus.CANCELLED: frozenset(),
}

#: WebSocket event type for the persisted run stream. The event log is the
#: record; this only tells a connected dashboard to fetch the new rows.
RUN_EVENT_STREAM = "decision_run.event"


def can_transition(current: str, target: str) -> bool:
    """Whether ``current -> target`` is a legal run transition."""
    return target in LEGAL_TRANSITIONS.get(current, frozenset())


def legal_targets(current: str) -> list[str]:
    """Sorted target statuses from ``current`` - useful in error messages."""
    return sorted(LEGAL_TRANSITIONS.get(current, frozenset()))


def _decimal_or_none(value, *, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DecisionRunError(f"{field} must be a decimal number, got {value!r}.") from exc
    if amount < 0:
        raise DecisionRunError(f"{field} must not be negative.")
    return amount


def _int_or_none(value, *, field: str) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise DecisionRunError(f"{field} must be an integer, got {value!r}.") from exc
    if number < 0:
        raise DecisionRunError(f"{field} must not be negative.")
    return number


def _validate_choice(value: str, choices, *, field: str) -> str:
    allowed = {choice for choice, _label in choices}
    if value not in allowed:
        raise DecisionRunError(
            f"Unknown {field} {value!r}; expected one of {', '.join(sorted(allowed))}."
        )
    return value


def _queue_event(user_id: int | None, event: RunEvent) -> object | None:
    """Persist the notification for a committed-or-committing event.

    Called *inside* the event transaction so the queue row and the event row
    commit together. The live delivery is attempted after commit; if the process
    dies in between, the pending row survives and the retry task delivers it.
    """
    return enqueue_event(
        user_id,
        RUN_EVENT_STREAM,
        "decision_run.event",
        {
            "run_id": str(event.run_id),
            "sequence": event.sequence,
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "stage": event.stage,
            "agent_role": event.agent_role,
            "occurred_at": utc_iso(event.occurred_at),
            "detail_path": f"/api/decision-runs/{event.run_id}/",
        },
        dedupe_key=f"run_event:{event.event_id}",
    )


def _dispatch_after_commit(outbox_row) -> None:
    if outbox_row is None:
        return
    transaction.on_commit(lambda: deliver_now(outbox_row.id))


def _next_sequence(run_id) -> int:
    """Next sequence for a locked run. Caller must hold the row lock."""
    current = RunEvent.objects.filter(run_id=run_id).aggregate(last=Max("sequence"))["last"]
    return (current or 0) + 1


def _create_event(
    run: DecisionRun,
    *,
    event_type: str,
    stage: str,
    agent_role: str = "",
    payload: dict | None = None,
    occurred_at=None,
    schema_version: str = EVENT_SCHEMA_VERSION,
) -> RunEvent:
    """Insert one event. Caller must hold the run row lock inside a transaction."""
    if event_type not in RUN_EVENT_TYPES:
        raise DecisionRunError(
            f"Unknown event_type {event_type!r}; add it to RunEventType and "
            "RUN_EVENT_TYPES before writing it."
        )
    if stage not in RunStage.values:
        raise DecisionRunError(f"Unknown stage {stage!r}.")
    if agent_role and agent_role not in AgentRole.values:
        raise DecisionRunError(f"Unknown agent_role {agent_role!r}.")
    if payload is not None and not isinstance(payload, dict):
        raise DecisionRunError("An event payload must be a JSON object.")

    return RunEvent.objects.create(
        run=run,
        sequence=_next_sequence(run.pk),
        schema_version=schema_version,
        event_type=event_type,
        stage=stage,
        agent_role=agent_role,
        occurred_at=occurred_at or timezone.now(),
        payload=json_safe(payload or {}),
    )


def append_event(
    run: DecisionRun,
    event_type: str,
    *,
    stage: str,
    agent_role: str = "",
    payload: dict | None = None,
    occurred_at=None,
    schema_version: str = EVENT_SCHEMA_VERSION,
    dedupe_key: str = "",
) -> RunEvent:
    """Append one event to a run's ordered log.

    The run row is locked for the duration of sequence allocation, so two
    producers appending concurrently get consecutive numbers rather than the
    same one. The event and its queued notification are committed together.
    A non-empty ``dedupe_key`` makes a retried handler return the first event
    instead of appending a duplicate.
    """
    key = (dedupe_key or "").strip()[:160]
    with transaction.atomic():
        locked = DecisionRun.objects.select_for_update().select_related("portfolio").get(pk=run.pk)
        if key:
            existing = RunEvent.objects.filter(run=locked, dedupe_key=key).first()
            if existing is not None:
                return existing
        event = _create_event(
            locked,
            event_type=event_type,
            stage=stage,
            agent_role=agent_role,
            payload=payload,
            occurred_at=occurred_at,
            schema_version=schema_version,
        )
        if key:
            RunEvent.objects.filter(pk=event.pk).update(dedupe_key=key)
            event.dedupe_key = key
        outbox_row = _queue_event(locked.portfolio.user_id, event)

    _dispatch_after_commit(outbox_row)
    return event


def create_run(
    *,
    portfolio,
    ticker: str,
    protocol_version: str,
    mode: str,
    source: str,
    snapshot: InputSnapshot | None = None,
    legacy_log=None,
    started_at=None,
    fixture_id: str = "",
    fixture_checksum: str = "",
    model_hash: str | None = None,
    config_hash: str | None = None,
    prompt_hash: str | None = None,
    total_cost_usd=None,
    input_tokens=None,
    output_tokens=None,
    cached_input_tokens=None,
    protocol_metadata: dict | None = None,
) -> DecisionRun:
    """Create a run in ``queued`` and write its ``run.created`` event.

    Creation deliberately does not accept an arbitrary starting status. A run
    that is born ``completed`` has no transitions to audit; every fixture and
    every producer starts at ``queued`` and walks the table, so the event log is
    always a complete account of how the status was reached.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        raise DecisionRunError("A run needs a ticker.")
    protocol = (protocol_version or "").strip()
    if not protocol:
        raise DecisionRunError("A run needs a protocol_version.")
    _validate_choice(mode, RunMode.choices, field="mode")
    _validate_choice(source, RunSource.choices, field="source")

    if snapshot is not None and snapshot.portfolio_id != portfolio.id:
        raise DecisionRunError("The input snapshot belongs to a different portfolio.")
    if legacy_log is not None and legacy_log.portfolio_id != portfolio.id:
        raise DecisionRunError("The legacy decision log belongs to a different portfolio.")

    cost = _decimal_or_none(total_cost_usd, field="total_cost_usd")
    tokens_in = _int_or_none(input_tokens, field="input_tokens")
    tokens_out = _int_or_none(output_tokens, field="output_tokens")
    tokens_cached = _int_or_none(cached_input_tokens, field="cached_input_tokens")

    started = started_at or timezone.now()

    with transaction.atomic():
        if fixture_id and DecisionRun.objects.filter(fixture_id=fixture_id).exists():
            raise DecisionRunError(f"A decision run for fixture {fixture_id!r} already exists.")

        run = DecisionRun.objects.create(
            portfolio=portfolio,
            ticker=symbol,
            protocol_version=protocol,
            status=DecisionStatus.QUEUED,
            mode=mode,
            source=source,
            snapshot=snapshot,
            model_hash=model_hash,
            config_hash=config_hash,
            prompt_hash=prompt_hash,
            started_at=started,
            total_cost_usd=cost,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cached_input_tokens=tokens_cached,
            legacy_log=legacy_log,
            fixture_id=fixture_id,
            fixture_checksum=fixture_checksum,
            protocol_metadata=json_safe(protocol_metadata or {}),
        )
        event = _create_event(
            run,
            event_type=RunEventType.RUN_CREATED,
            stage=RunStage.INTAKE,
            agent_role=AgentRole.SYSTEM,
            occurred_at=started,
            payload={
                "ticker": symbol,
                "protocol_version": protocol,
                "mode": mode,
                "source": source,
                "snapshot_id": str(snapshot.id) if snapshot is not None else None,
                "fixture_id": fixture_id or None,
            },
        )
        outbox_row = _queue_event(portfolio.user_id, event)

    _dispatch_after_commit(outbox_row)
    logger.info("Created decision run %s for %s (%s, %s)", run.id, symbol, mode, source)
    return run


def transition_status(
    run: DecisionRun,
    new_status: str,
    *,
    error_code: str = "",
    error_detail: str = "",
    occurred_at=None,
    payload: dict | None = None,
) -> RunEvent:
    """Move a run to ``new_status``, enforcing the legal-transition table.

    Terminal transitions stamp ``completed_at`` and ``latency_ms``. The status
    change and its audit event are written in one transaction, then the event is
    published; a failure to publish cannot undo a decision.
    """
    _validate_choice(new_status, DecisionStatus.choices, field="status")
    at = occurred_at or timezone.now()

    with transaction.atomic():
        locked = DecisionRun.objects.select_for_update().select_related("portfolio").get(pk=run.pk)
        previous = locked.status
        if not can_transition(previous, new_status):
            targets = legal_targets(previous)
            raise IllegalTransition(
                f"Cannot move decision run {locked.id} from {previous!r} to "
                f"{new_status!r}; legal targets are "
                f"{', '.join(targets) if targets else 'none (terminal state)'}."
            )

        completed_at = locked.completed_at
        latency_ms = locked.latency_ms
        update_fields = ["status", "updated_at"]

        if new_status in TERMINAL_STATUSES:
            completed_at = locked.completed_at or at
            update_fields.append("completed_at")
            if latency_ms is None:
                elapsed = (completed_at - locked.started_at).total_seconds() * 1000
                latency_ms = int(max(elapsed, 0))
                update_fields.append("latency_ms")

        if new_status == DecisionStatus.FAILED:
            locked.error_code = (error_code or "").strip()[:64]
            locked.error_detail = (error_detail or "").strip()
            update_fields.extend(["error_code", "error_detail"])

        locked.status = new_status
        locked.completed_at = completed_at
        locked.latency_ms = latency_ms
        locked.save(update_fields=update_fields)

        event_payload = {
            "from": previous,
            "to": new_status,
            "stage": previous if previous in RUN_STAGE_ORDER else new_status,
        }
        if error_code:
            event_payload["error_code"] = error_code
        if error_detail:
            event_payload["error_detail"] = error_detail
        if payload:
            event_payload.update(payload)

        event = _create_event(
            locked,
            event_type=RunEventType.STATUS_CHANGED,
            stage=new_status,
            agent_role=AgentRole.SYSTEM,
            occurred_at=at,
            payload=event_payload,
        )
        outbox_row = _queue_event(locked.portfolio.user_id, event)

    # Keep the caller's instance readable without a refetch. This is the only
    # mutation of a model instance outside the database layer, and it mirrors
    # exactly what was committed.
    run.status = new_status
    run.completed_at = completed_at
    run.latency_ms = latency_ms
    if new_status == DecisionStatus.FAILED:
        run.error_code = locked.error_code
        run.error_detail = locked.error_detail

    _dispatch_after_commit(outbox_row)
    logger.info("Decision run %s: %s -> %s", run.id, previous, new_status)
    return event


def record_failure(
    run: DecisionRun,
    *,
    error_code: str,
    error_detail: str = "",
    occurred_at=None,
    payload: dict | None = None,
) -> RunEvent:
    """Transition to ``failed`` with a machine-readable code and detail."""
    return transition_status(
        run,
        DecisionStatus.FAILED,
        error_code=error_code,
        error_detail=error_detail,
        occurred_at=occurred_at,
        payload=payload,
    )


def update_protocol_metadata(run: DecisionRun, **values) -> dict:
    """Merge provenance into ``run.protocol_metadata`` and persist it.

    Written once at the end of a protocol (and again on the failed path), so the
    run row carries the budgets, usage and raw validated outputs without a
    second table. ``None`` values are dropped rather than stored, so a missing
    measurement stays missing instead of becoming a JSON ``null`` that reads
    like a recorded absence.
    """
    merged = dict(run.protocol_metadata or {})
    merged.update({key: value for key, value in values.items() if value is not None})
    merged = json_safe(merged)
    DecisionRun.objects.filter(pk=run.pk).update(
        protocol_metadata=merged, updated_at=timezone.now()
    )
    run.protocol_metadata = merged
    return merged


def link_legacy_log(run: DecisionRun, legacy_log) -> None:
    """Point a run at the ``AgentDecisionLog`` row it explains (DATA-04).

    The Phase 0 audit row is still written by ``tasks.py`` after the guard runs,
    so the link is established after the protocol has finished. Only an unlinked
    row can be attached: the unique constraint means two runs must not both claim
    to explain one decision, and updating it again would silently repoint the
    record.
    """
    if legacy_log is None:
        return
    if run.legacy_log_id is not None and run.legacy_log_id != legacy_log.id:
        raise DecisionRunError(
            f"Decision run {run.id} already explains AgentDecisionLog {run.legacy_log_id}."
        )
    if legacy_log.portfolio_id != run.portfolio_id:
        raise DecisionRunError("The legacy decision log belongs to a different portfolio.")
    DecisionRun.objects.filter(pk=run.pk, legacy_log__isnull=True).update(legacy_log=legacy_log)
    run.legacy_log_id = legacy_log.id


def stage_states(run: DecisionRun, events=None) -> dict:
    """Which stages ran, which are pending, and which were honestly skipped.

    Derived from the persisted ``status.changed`` events rather than from the
    numeric order of the status enum. That distinction matters: ``analysing``
    can go straight to ``adjudicating`` in the current pipeline, and a
    position-based renderer would paint ``rebutting`` as complete when it never
    happened. Unreached stages read ``not_run`` on a finished run and ``pending``
    on one that is still moving.

    ``stage.failed`` events do not abort a run by themselves. They mark a stage
    ``degraded`` when the run continued, so a decision made on partial data is
    visibly made on partial data.
    """
    if events is None:
        events = run.events.order_by("sequence")
    events = sorted(events, key=lambda event: event.sequence)

    entered: dict[str, object] = {DecisionStatus.QUEUED: run.started_at}
    left: dict[str, object] = {}
    degraded: dict[str, str] = {}
    failure_stage: str | None = None

    for event in events:
        payload = event.payload or {}
        if event.event_type == RunEventType.STATUS_CHANGED:
            source = payload.get("from")
            target = payload.get("to")
            if target in RUN_STAGE_ORDER:
                entered.setdefault(target, event.occurred_at)
            if source in RUN_STAGE_ORDER:
                left.setdefault(source, event.occurred_at)
            if target in {DecisionStatus.FAILED, DecisionStatus.CANCELLED}:
                failure_stage = payload.get("stage") or source
        elif event.event_type == RunEventType.STAGE_FAILED:
            degraded[event.stage] = (
                payload.get("reason") or payload.get("detail") or "stage reported an error"
            )

    current_index = (
        RUN_STAGE_ORDER.index(run.status) if run.status in RUN_STAGE_ORDER else len(RUN_STAGE_ORDER)
    )
    stages = []
    for index, stage in enumerate(RUN_STAGE_ORDER):
        if stage == run.status and not run.is_terminal:
            state = "active"
        elif stage == failure_stage:
            state = "failed"
        elif stage in entered or stage in left:
            state = "degraded" if stage in degraded else "complete"
        else:
            # A stage the run has already moved past was skipped, not pending:
            # a baseline in ``validating`` will never run ``rebutting``. Only a
            # stage ahead of the current one can still happen.
            state = "not_run" if run.is_terminal or index < current_index else "pending"

        stages.append(
            {
                "stage": stage,
                "state": state,
                "entered_at": utc_iso(entered.get(stage)),
                "left_at": utc_iso(left.get(stage)),
                "limitation": degraded.get(stage),
            }
        )

    return {
        "current": failure_stage
        if run.status in {DecisionStatus.FAILED, DecisionStatus.CANCELLED}
        else run.status,
        "stages": stages,
    }
