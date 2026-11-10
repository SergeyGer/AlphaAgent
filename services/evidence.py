"""Evidence, claim and rebuttal contracts (EVID-01 / EVID-02 / EVID-03).

What "valid" means here
-----------------------
* Every evidence item has a source, a retrieval time and an *availability* time.
  If the publisher states a publication time, availability cannot precede it.
* A claim may only cite evidence that existed and was available at the run's
  ``analysis_cutoff``. A claim that cites tomorrow's news is not a weak argument,
  it is hindsight, and it must be refused rather than scored.
* A rebuttal may only target an *initial* claim held by the opposing side in the
  same run. A bear rebutting a bear is not a disagreement; it is a duplicate
  opinion wearing a debate's clothes.

Refusals raise the typed errors in ``core.decision_errors`` so the API can turn
them into a 400 with a message a reviewer can act on.

Records here are append-only. There is deliberately no update path for a claim,
a rebuttal or an evidence item: an audit trail that can be edited after the fact
is not an audit trail. Corrections are new records, and Phase 2's producer is the
place to add them.
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from core.decision_errors import (
    ClaimValidationError,
    EvidenceNotFound,
    EvidenceValidationError,
    RebuttalValidationError,
)
from core.decision_json import content_hash as hash_of
from core.decision_json import json_safe
from core.decision_models import (
    MAX_EXCERPT_CHARS,
    MAX_STATEMENT_CHARS,
    AgentRole,
    Claim,
    ClaimStance,
    ClaimType,
    EvidenceItem,
    EvidenceSourceType,
    InputSnapshot,
    Rebuttal,
    RebuttalResponseType,
)

logger = logging.getLogger("alphaagent.evidence")

__all__ = [
    "claims_by_stance",
    "claims_for_run",
    "create_claim",
    "create_evidence_item",
    "create_rebuttal",
    "create_snapshot",
    "evidence_for_run",
    "rebuttals_for_run",
]

#: Which side a responding agent argues for. A rebuttal only makes sense from an
#: adversarial role; ``cio`` and ``system`` adjudicate rather than rebut.
AGENT_STANCES: dict[str, str] = {
    AgentRole.BULL: ClaimStance.BULL,
    AgentRole.BEAR: ClaimStance.BEAR,
}

OPPOSING_STANCE: dict[str, str] = {
    ClaimStance.BULL: ClaimStance.BEAR,
    ClaimStance.BEAR: ClaimStance.BULL,
}


def _require_aware(value, *, field: str):
    if value is None:
        raise EvidenceValidationError(f"{field} is required.")
    if timezone.is_naive(value):
        raise EvidenceValidationError(f"{field} must be timezone-aware (UTC).")
    return value


def _validate_choice(value: str, choices, *, field: str) -> str:
    allowed = {choice for choice, _label in choices}
    if value not in allowed:
        raise EvidenceValidationError(
            f"Unknown {field} {value!r}; expected one of {', '.join(sorted(allowed))}."
        )
    return value


def _as_uuid(value, *, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise EvidenceValidationError(f"{field} {value!r} is not a UUID.") from exc


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
def create_snapshot(
    *,
    portfolio,
    portfolio_version: str,
    analysis_cutoff,
    holdings,
    cash,
    quotes,
    captured_at=None,
    expected_content_hash: str = "",
) -> InputSnapshot:
    """Capture the inputs a run will be judged against, and freeze them.

    ``expected_content_hash`` lets a caller (the fixture loader) assert the
    snapshot it read from disk is the one that was hashed when the manifest was
    written. A mismatch is a corrupted fixture, not a new decision.
    """
    version = (portfolio_version or "").strip()
    if not version:
        raise EvidenceValidationError("portfolio_version is required.")
    cutoff = _require_aware(analysis_cutoff, field="analysis_cutoff")
    captured = _require_aware(captured_at or timezone.now(), field="captured_at")

    if not isinstance(holdings, list):
        raise EvidenceValidationError("holdings must be a JSON array.")
    if not isinstance(cash, dict):
        raise EvidenceValidationError("cash must be a JSON object.")
    if not isinstance(quotes, dict):
        raise EvidenceValidationError("quotes must be a JSON object.")

    payload = {
        "portfolio_id": portfolio.id,
        "portfolio_version": version,
        "analysis_cutoff": cutoff,
        "captured_at": captured,
        "holdings": holdings,
        "cash": cash,
        "quotes": quotes,
    }
    digest = hash_of(payload)
    if expected_content_hash and expected_content_hash != digest:
        raise EvidenceValidationError(
            "Input snapshot content_hash does not match the hashed payload "
            f"(expected {expected_content_hash}, computed {digest})."
        )

    snapshot = InputSnapshot.objects.create(
        portfolio=portfolio,
        portfolio_version=version,
        analysis_cutoff=cutoff,
        captured_at=captured,
        holdings=json_safe(holdings),
        cash=json_safe(cash),
        quotes=json_safe(quotes),
        content_hash=digest,
    )
    logger.info("Captured input snapshot %s for portfolio %s", snapshot.id, portfolio.id)
    return snapshot


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
def create_evidence_item(
    *,
    snapshot: InputSnapshot,
    source_type: str,
    retrieved_at,
    source_uri: str = "",
    publisher: str = "",
    published_at=None,
    available_at=None,
    excerpt: str = "",
    observation: dict | None = None,
    units: str = "",
    currency: str = "",
    is_redistributable: bool = False,
    expected_content_hash: str = "",
) -> EvidenceItem:
    """Record one evidence item against a snapshot.

    ``available_at`` defaults to the later of publication and retrieval: the
    pipeline cannot have relied on something before it either existed or was
    fetched. When ``published_at`` is unknown it stays null and availability is
    the retrieval time - the earliest instant we can honestly defend.
    """
    _validate_choice(source_type, EvidenceSourceType.choices, field="source_type")
    retrieved = _require_aware(retrieved_at, field="retrieved_at")
    published = _require_aware(published_at, field="published_at") if published_at else None

    if published is not None and available_at is None:
        available = max(published, retrieved)
    elif available_at is not None:
        available = _require_aware(available_at, field="available_at")
    else:
        available = retrieved

    if published is not None and available < published:
        raise EvidenceValidationError(
            "available_at cannot precede published_at: the item did not exist yet."
        )

    text = excerpt or ""
    if len(text) > MAX_EXCERPT_CHARS:
        raise EvidenceValidationError(
            f"excerpt is {len(text)} characters; the bound is {MAX_EXCERPT_CHARS}."
        )
    if observation is not None and not isinstance(observation, dict):
        raise EvidenceValidationError("observation must be a JSON object of typed values.")
    if observation is not None:
        observation = json_safe(observation)

    payload = {
        "snapshot_id": snapshot.id,
        "source_type": source_type,
        "source_uri": (source_uri or "").strip(),
        "publisher": (publisher or "").strip(),
        "published_at": published,
        "retrieved_at": retrieved,
        "available_at": available,
        "excerpt": text,
        "observation": observation,
        "units": (units or "").strip(),
        "currency": (currency or "").strip(),
    }
    digest = hash_of(payload)
    if expected_content_hash and expected_content_hash != digest:
        raise EvidenceValidationError(
            "Evidence content_hash does not match the hashed payload "
            f"(expected {expected_content_hash}, computed {digest})."
        )

    item = EvidenceItem.objects.create(
        snapshot=snapshot,
        source_type=source_type,
        source_uri=payload["source_uri"],
        publisher=payload["publisher"],
        published_at=published,
        retrieved_at=retrieved,
        available_at=available,
        excerpt=text,
        observation=observation,
        units=payload["units"],
        currency=payload["currency"],
        content_hash=digest,
        is_redistributable=bool(is_redistributable),
    )
    logger.info("Recorded evidence %s (%s) for snapshot %s", item.id, source_type, snapshot.id)
    return item


def _resolve_evidence(run, evidence_ids, *, required: bool = True) -> list[EvidenceItem]:
    """Load the cited evidence and verify it was available at the cutoff.

    The check is against both ``available_at`` and ``published_at``. Either one
    after the cutoff means the agent could not have read it, and checking only
    ``available_at`` would let a mis-recorded availability hide a look-ahead.
    """
    raw_ids = list(evidence_ids or [])
    if not raw_ids:
        if required:
            raise ClaimValidationError("A claim must cite at least one evidence item.")
        return []

    wanted = [_as_uuid(value, field="evidence_id") for value in raw_ids]
    items = list(EvidenceItem.objects.filter(pk__in=wanted))
    found = {item.pk for item in items}
    missing = [str(value) for value in wanted if value not in found]
    if missing:
        raise EvidenceNotFound(f"Unknown evidence item(s): {', '.join(missing)}.")

    snapshot = getattr(run, "snapshot", None)
    if snapshot is None:
        raise ClaimValidationError(
            "The run has no captured input snapshot, so evidence availability "
            "cannot be verified against an analysis cutoff."
        )
    cutoff = snapshot.analysis_cutoff

    for item in items:
        if item.available_at > cutoff:
            raise ClaimValidationError(
                f"Evidence {item.id} became available at {item.available_at.isoformat()}, "
                f"after the run's analysis cutoff {cutoff.isoformat()}."
            )
        if item.published_at is not None and item.published_at > cutoff:
            raise ClaimValidationError(
                f"Evidence {item.id} was published at {item.published_at.isoformat()}, "
                f"after the run's analysis cutoff {cutoff.isoformat()}."
            )

    # Preserve the caller's order rather than the database's: a claim's evidence
    # list is part of how the argument reads.
    by_id = {item.pk: item for item in items}
    return [by_id[value] for value in wanted]


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------
def create_claim(
    *,
    run,
    agent_role: str,
    stance: str,
    statement: str,
    claim_type: str,
    evidence_ids,
    horizon: str = "",
    limitations: str = "",
    case_round: int = 1,
    created_at=None,
) -> Claim:
    """Record one assertion, after checking its evidence was knowable in time."""
    _validate_choice(agent_role, AgentRole.choices, field="agent_role")
    _validate_choice(stance, ClaimStance.choices, field="stance")
    _validate_choice(claim_type, ClaimType.choices, field="claim_type")

    text = (statement or "").strip()
    if not text:
        raise ClaimValidationError("A claim needs a statement.")
    if len(text) > MAX_STATEMENT_CHARS:
        raise ClaimValidationError(
            f"Claim statement is {len(text)} characters; the bound is {MAX_STATEMENT_CHARS}."
        )
    if int(case_round) < 1:
        raise ClaimValidationError("case_round is 1-based; 1 is the initial case.")

    items = _resolve_evidence(run, evidence_ids)

    with transaction.atomic():
        claim = Claim.objects.create(
            run=run,
            agent_role=agent_role,
            stance=stance,
            statement=text,
            claim_type=claim_type,
            horizon=(horizon or "").strip(),
            limitations=(limitations or "").strip(),
            case_round=int(case_round),
            **({"created_at": created_at} if created_at is not None else {}),
        )
        claim.evidence.set(items)

    logger.info("Recorded %s claim %s on run %s", stance, claim.claim_id, run.id)
    return claim


def claims_for_run(run, *, stance: str | None = None, initial_only: bool = False):
    """Claims for a run, evidence preloaded, in argument order."""
    queryset = Claim.objects.filter(run=run).prefetch_related("evidence")
    if stance:
        queryset = queryset.filter(stance=stance)
    if initial_only:
        queryset = queryset.filter(case_round=1)
    return queryset.order_by("case_round", "created_at", "id")


def claims_by_stance(run) -> dict[str, list[Claim]]:
    """The two cases side by side, keyed by stance, for the detail response."""
    grouped: dict[str, list[Claim]] = {stance: [] for stance in ClaimStance.values}
    for claim in claims_for_run(run):
        grouped.setdefault(claim.stance, []).append(claim)
    return grouped


# ---------------------------------------------------------------------------
# Rebuttals
# ---------------------------------------------------------------------------
def create_rebuttal(
    *,
    run,
    responding_agent: str,
    target_claim: Claim,
    response_type: str,
    explanation: str,
    evidence_ids,
    position_change: str = "",
    created_at=None,
) -> Rebuttal:
    """Record a rebuttal against an opposing initial claim in the same run.

    "Same run" and "opposing side" are both checked. The first prevents a
    rebuttal being attached to a different decision; the second is the EVID-03
    rule that makes a rebuttal a disagreement rather than a repetition.
    """
    _validate_choice(responding_agent, AgentRole.choices, field="responding_agent")
    _validate_choice(response_type, RebuttalResponseType.choices, field="response_type")

    if target_claim is None:
        raise RebuttalValidationError("A rebuttal needs a target claim.")
    if target_claim.run_id != run.id:
        raise RebuttalValidationError(
            f"Target claim {target_claim.claim_id} belongs to a different decision run."
        )
    if target_claim.case_round != 1:
        raise RebuttalValidationError(
            "A rebuttal may only target an initial claim (case_round 1); "
            "follow-up claims are not the initial case."
        )

    responding_stance = AGENT_STANCES.get(responding_agent)
    if responding_stance is None:
        raise RebuttalValidationError(
            f"{responding_agent!r} does not argue a side and cannot rebut; "
            f"expected one of {', '.join(sorted(AGENT_STANCES))}."
        )
    if target_claim.stance != OPPOSING_STANCE[responding_stance]:
        raise RebuttalValidationError(
            f"A {responding_agent} rebuttal must target the opposing "
            f"{OPPOSING_STANCE[responding_stance]} case, but the target claim is "
            f"{target_claim.stance!r}."
        )

    text = (explanation or "").strip()
    if not text:
        raise RebuttalValidationError("A rebuttal needs an explanation.")

    # "Insufficient evidence" is the one response that legitimately cites nothing:
    # the point of it is that the target has not been supported.
    required = response_type != RebuttalResponseType.INSUFFICIENT_EVIDENCE
    items = _resolve_evidence(run, evidence_ids, required=required)

    with transaction.atomic():
        rebuttal = Rebuttal.objects.create(
            run=run,
            responding_agent=responding_agent,
            target_claim=target_claim,
            response_type=response_type,
            explanation=text,
            position_change=(position_change or "").strip()[:280],
            **({"created_at": created_at} if created_at is not None else {}),
        )
        rebuttal.evidence.set(items)

    logger.info(
        "Recorded %s rebuttal on claim %s (run %s)",
        response_type,
        target_claim.claim_id,
        run.id,
    )
    return rebuttal


def rebuttals_for_run(run):
    """Rebuttals for a run, with target claims and evidence preloaded."""
    return (
        Rebuttal.objects.filter(run=run)
        .select_related("target_claim")
        .prefetch_related("evidence")
        .order_by("created_at", "id")
    )


def evidence_for_run(run):
    """Evidence belonging to the run's captured snapshot."""
    if run.snapshot_id is None:
        return EvidenceItem.objects.none()
    return EvidenceItem.objects.filter(snapshot_id=run.snapshot_id).order_by("available_at", "id")
