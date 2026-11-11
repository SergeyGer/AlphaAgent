"""Serializers and payload builders for the Phase 1 decision-run API (API-01).

Two presentation rules are enforced here rather than left to the UI.

**Synthetic is visible.** Every payload that carries a run carries ``mode`` and
``is_synthetic``. The Phase 1 disagreement fixture has a rebuttal the current
pipeline cannot produce; if the API did not say ``synthetic`` next to it, a UI
could render fabricated debate as a recorded decision. ``provenance`` repeats it
in one object so a client cannot pick up ``mode`` and forget the rest.

**Decimals are strings and times are UTC.** Money, quantities and weights are
``Decimal`` in the database (DATA-02) and are serialised as strings. A float
would round a price on the way to a chart and nobody would notice.

The export bundle is built from a whitelist, not by deleting fields from a model
dump. Redaction that works by subtraction fails open the day someone adds a
field; a whitelist fails closed. Evidence excerpts are only included when the
source is flagged redistributable, and event payload keys that could carry a
credential or an account identifier are dropped recursively.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from core.decision_json import content_hash as hash_of
from core.decision_json import utc_iso
from core.decision_models import (
    ClaimStance,
    DecisionRun,
    EvidenceItem,
    GuardRecord,
    OutcomeMaturity,
    OutcomeRecord,
    Rebuttal,
    RunEvent,
)
from services.decision_runs import stage_states
from services.evidence import claims_for_run, evidence_for_run, rebuttals_for_run

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "DecisionRunSerializer",
    "RunEventSerializer",
    "build_export_bundle",
    "build_run_detail",
    "build_run_metadata",
    "claim_payload",
    "evidence_payload",
    "export_evidence_payload",
    "guard_payload",
    "outcome_payload",
    "protocol_summary",
    "rebuttal_payload",
]

BUNDLE_SCHEMA_VERSION = "1.0"

#: Keys dropped recursively from anything that reaches an export bundle. Account
#: identifiers are on the list because a bundle is a document that leaves the
#: server, not because the owner is untrusted.
SENSITIVE_KEYS = frozenset(
    {
        "account",
        "account_id",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "chat_id",
        "credential",
        "credentials",
        "email",
        "legacy_log_id",
        "password",
        "passwd",
        "portfolio",
        "portfolio_id",
        "secret",
        "telegram",
        "telegram_username",
        "token",
        "user",
        "user_id",
        "username",
    }
)


# ---------------------------------------------------------------------------
# Model serializers (list + event windows)
# ---------------------------------------------------------------------------
class DecisionRunSerializer(serializers.ModelSerializer):
    """Metadata for the list endpoint; the detail endpoints add the reasoning."""

    is_synthetic = serializers.BooleanField(read_only=True)
    snapshot_id = serializers.UUIDField(read_only=True, allow_null=True)
    cost = serializers.SerializerMethodField()
    failure = serializers.SerializerMethodField()
    #: Empty string in the column means "no failure"; the API contract says null.
    error_code = serializers.SerializerMethodField()
    error_detail = serializers.SerializerMethodField()

    class Meta:
        model = DecisionRun
        fields = [
            "id",
            "ticker",
            "protocol_version",
            "status",
            "mode",
            "is_synthetic",
            "source",
            "snapshot_id",
            "started_at",
            "completed_at",
            "latency_ms",
            "cost",
            "failure",
            "error_code",
            "error_detail",
            "fixture_id",
            "created_at",
        ]
        read_only_fields = fields

    def get_cost(self, obj: DecisionRun) -> dict:
        return {
            "total_cost_usd": (str(obj.total_cost_usd) if obj.total_cost_usd is not None else None),
            "input_tokens": obj.input_tokens,
            "output_tokens": obj.output_tokens,
            "cached_input_tokens": obj.cached_input_tokens,
        }

    def get_failure(self, obj: DecisionRun) -> dict | None:
        failure = obj.failure
        if failure is None:
            return None
        return {**failure, "at": utc_iso(failure["at"])}

    def get_error_code(self, obj: DecisionRun) -> str | None:
        return obj.error_code or None

    def get_error_detail(self, obj: DecisionRun) -> str | None:
        return obj.error_detail or None


class RunEventSerializer(serializers.ModelSerializer):
    """One ordered event in the event-stream contract.

    ``seq`` and ``type`` are the wire names the replay UI reads. The database
    columns stay ``sequence``/``event_type`` because those are the domain names;
    the mapping belongs in the API boundary.
    """

    event_id = serializers.UUIDField(read_only=True)
    seq = serializers.IntegerField(source="sequence", read_only=True)
    type = serializers.CharField(source="event_type", read_only=True)
    agent_role = serializers.SerializerMethodField()

    class Meta:
        model = RunEvent
        fields = [
            "event_id",
            "seq",
            "type",
            "stage",
            "agent_role",
            "occurred_at",
            "persisted_at",
            "schema_version",
            "payload",
        ]
        read_only_fields = fields

    def get_agent_role(self, obj: RunEvent) -> str | None:
        """Blank means "no agent" on the wire; null is how the event contract says it."""
        return obj.agent_role or None


# ---------------------------------------------------------------------------
# Shared payload pieces
# ---------------------------------------------------------------------------
def _money(value) -> str | None:
    return str(value) if value is not None else None


def evidence_payload(item: EvidenceItem, *, include_body: bool = True) -> dict:
    """One evidence item. ``include_body=False`` is the export/redacted form."""
    payload = {
        "id": str(item.id),
        "snapshot_id": str(item.snapshot_id),
        "source_type": item.source_type,
        "source_uri": item.source_uri or None,
        "publisher": item.publisher or None,
        "published_at": utc_iso(item.published_at),
        "published_at_known": item.published_at is not None,
        "retrieved_at": utc_iso(item.retrieved_at),
        "available_at": utc_iso(item.available_at),
        "units": item.units or None,
        "currency": item.currency or None,
        "observation": item.observation,
        "content_hash": item.content_hash,
        "is_redistributable": item.is_redistributable,
    }
    if include_body:
        payload["excerpt"] = item.excerpt
    else:
        payload["excerpt"] = None
        payload["excerpt_omitted"] = bool(item.excerpt)
        payload["excerpt_chars"] = len(item.excerpt)
    return payload


def claim_payload(claim) -> dict:
    evidence = list(claim.evidence.all())
    return {
        "claim_id": str(claim.claim_id),
        "agent_role": claim.agent_role,
        "stance": claim.stance,
        "statement": claim.statement,
        "claim_type": claim.claim_type,
        "horizon": claim.horizon or None,
        "limitations": claim.limitations or None,
        "case_round": claim.case_round,
        "evidence_ids": [str(item.id) for item in evidence],
        "evidence": [evidence_payload(item) for item in evidence],
        "created_at": utc_iso(claim.created_at),
    }


def rebuttal_payload(rebuttal: Rebuttal) -> dict:
    evidence = list(rebuttal.evidence.all())
    return {
        "id": str(rebuttal.id),
        "rebuttal_id": str(rebuttal.id),
        "responding_agent": rebuttal.responding_agent,
        "response_type": rebuttal.response_type,
        "explanation": rebuttal.explanation,
        "target_claim_id": str(rebuttal.target_claim.claim_id),
        "target_claim_statement": rebuttal.target_claim.statement,
        "evidence_ids": [str(item.id) for item in evidence],
        "evidence": [evidence_payload(item) for item in evidence],
        "position_change": rebuttal.position_change or None,
        "created_at": utc_iso(rebuttal.created_at),
    }


GUARD_CHECK_OUTCOMES = frozenset({"pass", "fail", "not_evaluated"})


def _guard_checks(guard: GuardRecord) -> list[dict]:
    """The typed check array, alongside the free-form numerical evidence.

    ``not_evaluated`` is a first-class outcome, not a missing ``fail``: a check
    the guard never ran says nothing about whether the order was safe, and a UI
    that painted it red would report a refusal that did not happen.
    """
    raw = guard.numerical_checks or {}
    items = raw.get("checks") if isinstance(raw, dict) else None
    checks = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        outcome = str(item.get("outcome") or "not_evaluated").strip().lower()
        if outcome not in GUARD_CHECK_OUTCOMES:
            outcome = "not_evaluated"
        checks.append(
            {
                "rule_code": str(item.get("rule_code") or "unknown"),
                "outcome": outcome,
                "threshold": (
                    str(item["threshold"]) if item.get("threshold") is not None else None
                ),
                "actual": str(item["actual"]) if item.get("actual") is not None else None,
                "unit": str(item["unit"]) if item.get("unit") else None,
            }
        )
    return checks


def guard_payload(guard: GuardRecord) -> dict:
    numerical = guard.numerical_checks or {}
    if isinstance(numerical, dict):
        numerical = {key: value for key, value in numerical.items() if key != "checks"}
    return {
        "id": str(guard.id),
        "status": guard.status,
        "policy_version": guard.policy_version,
        "evaluated_portfolio_version": guard.evaluated_portfolio_version,
        "quote_time": utc_iso(guard.quote_time),
        "requested_quantity": str(guard.requested_quantity),
        "allowed_quantity": str(guard.allowed_quantity),
        "difference_quantity": str(guard.difference_quantity),
        "binding_rule": guard.binding_rule or None,
        "reason_codes": list(guard.reason_codes or []),
        "checks": _guard_checks(guard),
        "numerical_checks": numerical,
        "created_at": utc_iso(guard.created_at),
    }


def outcome_payload(outcome: OutcomeRecord) -> dict:
    """One horizon, in both the consumer contract and the audit form.

    ``portfolio_impact`` is deliberately ``null`` throughout Phase 1. Portfolio
    accounting does not reconcile yet, so any number there would be a guess. The
    UI shows "Not available"; it is not shown a zero.

    ``signal_score`` prefers an explicit diagnostic supplied by the producer and
    otherwise falls back to the action's measured return - which is a score, not
    a portfolio impact, and is reported as such.
    """
    diagnostics = outcome.signal_diagnostics or {}
    raw_signal = diagnostics.get("signal_score") if isinstance(diagnostics, dict) else None
    if raw_signal is not None:
        signal_score = str(raw_signal)
    elif outcome.maturity == OutcomeMaturity.EVALUATED and outcome.decision_return_pct is not None:
        signal_score = str(outcome.decision_return_pct)
    else:
        signal_score = None

    return {
        "id": str(outcome.id),
        "horizon_days": outcome.horizon_days,
        "method_version": outcome.method_version,
        "maturity": outcome.maturity,
        "is_scored": outcome.is_scored,
        # -- OutcomeView contract -----------------------------------------
        "signal_score": signal_score,
        # Benchmark identity, not a nested return object. The return itself is
        # reported separately below for a reader who wants to check the score.
        "benchmark": outcome.benchmark_ticker or None,
        "portfolio_impact": None,
        # -- audit detail ---------------------------------------------------
        "price_at_decision": _money(outcome.price_at_decision),
        "price_at_horizon": _money(outcome.price_at_horizon),
        "price_observed_at": utc_iso(outcome.price_observed_at),
        "price_source": outcome.price_source or None,
        "forward_return_pct": _money(outcome.forward_return_pct),
        "decision_return_pct": _money(outcome.decision_return_pct),
        "benchmark_return_pct": _money(outcome.benchmark_return_pct),
        "signal_diagnostics": diagnostics,
        "unavailable_reason": outcome.unavailable_reason or None,
        "evaluated_at": utc_iso(outcome.evaluated_at),
        "created_at": utc_iso(outcome.created_at),
    }


def _snapshot_payload(run: DecisionRun) -> dict | None:
    snapshot = run.snapshot
    if snapshot is None:
        return None
    return {
        "id": str(snapshot.id),
        "portfolio_version": snapshot.portfolio_version,
        "analysis_cutoff": utc_iso(snapshot.analysis_cutoff),
        "captured_at": utc_iso(snapshot.captured_at),
        "content_hash": snapshot.content_hash,
        "holdings": snapshot.holdings,
        "cash": snapshot.cash,
        "quotes": snapshot.quotes,
    }


def _verdict_payload(run: DecisionRun, claims_by_id: dict[str, dict]) -> dict | None:
    decision = getattr(run, "decision", None)
    if decision is None:
        return None
    decisive_ids = [str(value) for value in (decision.decisive_claim_ids or [])]
    return {
        "action": decision.action,
        "requested_quantity": _money(decision.requested_quantity),
        "concise_rationale": decision.concise_rationale,
        "decisive_claim_ids": decisive_ids,
        "decisive_claims": [
            claims_by_id[claim_id] for claim_id in decisive_ids if claim_id in claims_by_id
        ],
        "strongest_objection": decision.strongest_objection or None,
        "thesis": decision.thesis or None,
        "invalidation_condition": decision.invalidation_condition or None,
        "engine_source": decision.engine_source,
        "created_at": utc_iso(decision.created_at),
    }


def _limitations(run: DecisionRun, claims, outcomes, events) -> list[dict]:
    """Every honest caveat attached to a run, in one place.

    A reviewer should not have to open four sections to discover that a claim was
    unverifiable, a stage degraded, and an outcome could not be priced. The list
    is additive: an empty list means no recorded limitation, not that there were
    none.
    """
    limitations = []
    for claim in claims:
        if claim.limitations:
            limitations.append({"source": f"claim:{claim.claim_id}", "text": claim.limitations})
    for outcome in outcomes:
        if outcome.unavailable_reason:
            limitations.append(
                {
                    "source": f"outcome:+{outcome.horizon_days}d",
                    "text": outcome.unavailable_reason,
                }
            )
    for event in events:
        if event.event_type == "stage.failed":
            payload = event.payload or {}
            text = payload.get("reason") or payload.get("detail")
            if text:
                limitations.append({"source": f"stage:{event.stage}", "text": str(text)})
        elif event.event_type == "limitation.recorded":
            # Phase 2 records excluded claims, skipped rebuttal stages and
            # fallback attempts as limitation events. Lifting them into this
            # list is what keeps "flagged, never silently dropped" visible to
            # the reviewer rather than only to someone reading the raw stream.
            text = (event.payload or {}).get("limitation")
            if text:
                limitations.append({"source": f"event:{event.sequence}", "text": str(text)})
    return limitations


def protocol_summary(run: DecisionRun) -> dict | None:
    """The Phase 2 provenance summary recorded on a run, or ``None``.

    A summary rather than the whole ``protocol_metadata`` blob: the raw validated
    stage outputs live in the database (they can be large and are not part of the
    read contract), while everything a reviewer needs to judge reproducibility -
    provider, model, parameters, prompt-template version, tool versions,
    snapshot hash, limits, usage and the fallback label - is exposed here. A
    Phase 1 fixture recorded no protocol of this kind and returns ``None``, so
    the key cannot be mistaken for "a protocol ran and recorded nothing".
    """
    metadata = run.protocol_metadata or {}
    if not metadata:
        return None
    protocol = metadata.get("protocol") or {}
    return {
        "name": protocol.get("name"),
        "prompt_template_version": protocol.get("prompt_template_version"),
        "provider": protocol.get("provider"),
        "model": protocol.get("model"),
        "parameters": protocol.get("parameters"),
        "tool_versions": protocol.get("tool_versions"),
        "snapshot_hash": metadata.get("snapshot_hash") or protocol.get("snapshot_hash"),
        "limits": metadata.get("limits") or protocol.get("limits"),
        "rounds": protocol.get("rounds"),
        "usage": metadata.get("usage"),
        "counts": metadata.get("counts"),
        "analyst_consensus": metadata.get("analyst_consensus"),
        "fallback": metadata.get("fallback"),
        "deterministic_replay": metadata.get("deterministic_replay"),
    }


def build_run_metadata(run: DecisionRun, *, events=None) -> dict:
    """Run metadata + stage states + source + cost + failure details."""
    events = list(events) if events is not None else list(run.events.order_by("sequence"))
    failure = run.failure
    return {
        "id": str(run.id),
        "ticker": run.ticker,
        "protocol_version": run.protocol_version,
        "status": run.status,
        "status_label": run.get_status_display(),
        "mode": run.mode,
        "is_synthetic": run.is_synthetic,
        "source": run.source,
        "execution_state": run.execution_state or None,
        "execution": {
            "state": run.execution_state or None,
            "executed": run.execution_state == "executed",
            "awaiting_approval": run.execution_state == "awaiting_approval",
            "no_trade": run.execution_state in {"held", "rejected", "expired", "blocked"},
        },
        "provenance": {
            "mode": run.mode,
            "is_synthetic": run.is_synthetic,
            "source": run.source,
            "protocol_version": run.protocol_version,
            "fixture_id": run.fixture_id or None,
        },
        "protocol": protocol_summary(run),
        "snapshot_id": str(run.snapshot_id) if run.snapshot_id else None,
        "started_at": utc_iso(run.started_at),
        "completed_at": utc_iso(run.completed_at),
        "latency_ms": run.latency_ms,
        "model_hash": run.model_hash,
        "config_hash": run.config_hash,
        "prompt_hash": run.prompt_hash,
        "cost": {
            "total_cost_usd": _money(run.total_cost_usd),
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "cached_input_tokens": run.cached_input_tokens,
        },
        "failure": ({**failure, "at": utc_iso(failure["at"])} if failure is not None else None),
        # Also at the top level: the run summary contract reads `error_code`
        # directly, without unpacking the failure envelope.
        "error_code": run.error_code or None,
        "error_detail": run.error_detail or None,
        "legacy_log_id": run.legacy_log_id,
        "fixture_id": run.fixture_id or None,
        "created_at": utc_iso(run.created_at),
        "stage_states": stage_states(run, events),
    }


def build_run_detail(run: DecisionRun, *, events=None) -> dict:
    """The evidence-backed account of one decision."""
    events = list(events) if events is not None else list(run.events.order_by("sequence"))
    claims = list(claims_for_run(run))
    claim_payloads = [claim_payload(claim) for claim in claims]
    claims_by_id = {payload["claim_id"]: payload for payload in claim_payloads}

    cases: dict[str, list[dict]] = {stance: [] for stance in ClaimStance.values}
    for payload in claim_payloads:
        cases.setdefault(payload["stance"], []).append(payload)

    rebuttals = [rebuttal_payload(rebuttal) for rebuttal in rebuttals_for_run(run)]
    guards = list(GuardRecord.objects.filter(proposal__run=run).order_by("-created_at", "-id"))
    guard_payloads = [guard_payload(guard) for guard in guards]
    outcomes = list(
        OutcomeRecord.objects.filter(decision__run=run).order_by("horizon_days", "method_version")
    )
    outcome_payloads = [outcome_payload(outcome) for outcome in outcomes]

    verdict = _verdict_payload(run, claims_by_id)
    guard = guard_payloads[0] if guard_payloads else None

    detail = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        # -- flat consumer contract (API-01) ------------------------------
        "run": build_run_metadata(run, events=events),
        "rationale": verdict["concise_rationale"] if verdict else None,
        "decisive_claim_ids": verdict["decisive_claim_ids"] if verdict else [],
        "strongest_objection": verdict["strongest_objection"] if verdict else None,
        "claims": claim_payloads,
        "rebuttals": rebuttals,
        "evidence": [evidence_payload(item) for item in evidence_for_run(run)],
        "guard": guard,
        "outcomes": outcome_payloads,
        # Only true when a producer actually recorded a rebuttal. A legacy or
        # sequential run has an empty list and the UI removes the section
        # entirely rather than rendering an empty debate.
        "has_rebuttals": bool(rebuttals),
        # -- nested form kept for the Phase 2 view and richer audit --------
        "snapshot": _snapshot_payload(run),
        "cases": cases,
        "verdict": verdict,
        "guard_comparison": guard,
        "guards": guard_payloads,
        "limitations": _limitations(run, claims, outcomes, events),
    }
    return detail


# ---------------------------------------------------------------------------
# Redacted export
# ---------------------------------------------------------------------------
def _redact(value):
    """Recursively drop sensitive keys. Used only for the export bundle."""
    if isinstance(value, dict):
        return {
            key: _redact(item)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def export_evidence_payload(item: EvidenceItem) -> dict:
    """Evidence with the body only when redistribution is permitted."""
    include_body = bool(item.is_redistributable)
    return evidence_payload(item, include_body=include_body)


def _redact_evidence_bodies(value):
    """Strip non-redistributable excerpts anywhere in the bundle.

    Evidence payloads are embedded more than once - under ``evidence``, under
    every claim's ``evidence`` list, and under any decisive claim in the verdict.
    Redacting only the top-level list would leak the same body through the cases,
    so the rule follows the payload wherever it appears.
    """
    if isinstance(value, dict):
        if value.get("is_redistributable") is False and value.get("excerpt"):
            text = value["excerpt"]
            value = {
                **value,
                "excerpt": None,
                "excerpt_omitted": True,
                "excerpt_chars": len(text),
            }
        return {key: _redact_evidence_bodies(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_evidence_bodies(item) for item in value]
    return value


def build_export_bundle(run: DecisionRun, *, events=None) -> dict:
    """Versioned, redacted JSON bundle for one decision run.

    Whitelisted on the way in. The only free-form fields are the ones an audit
    needs (explanations, rationales, event payloads); event payloads and the
    failure block are recursively stripped of credential/account keys, and
    evidence bodies are included only when the source is redistributable.
    """
    events = list(events) if events is not None else list(run.events.order_by("sequence"))
    detail = build_run_detail(run, events=events)

    run_payload = detail["run"]
    # The failure detail can carry a driver message with a connection string or
    # an account name; the export keeps the code and drops the prose. The
    # authenticated detail endpoint still shows it, because the owner needs it.
    failure = run_payload.get("failure")
    if failure:
        failure = {"status": failure.get("status"), "error_code": failure.get("error_code")}
    run_payload = {
        **run_payload,
        "failure": failure,
        # The detail endpoint keeps the prose because the owner needs it to
        # diagnose a failure; a document that leaves the server keeps the code.
        "error_detail": None,
        "legacy_log_id": None,
        "fixture_id": run_payload.get("fixture_id"),
    }

    evidence = [export_evidence_payload(item) for item in evidence_for_run(run)]
    omitted = sum(1 for item in evidence if item.get("excerpt_omitted"))

    bundle = {
        "bundle_version": BUNDLE_SCHEMA_VERSION,
        "bundle_kind": "alphaagent.decision-run",
        "exported_at": utc_iso(timezone.now()),
        "run": run_payload,
        "snapshot": detail["snapshot"],
        "evidence": evidence,
        "cases": detail["cases"],
        "rebuttals": detail["rebuttals"],
        "verdict": detail["verdict"],
        "guard_comparison": detail["guard_comparison"],
        "guards": detail["guards"],
        "outcomes": detail["outcomes"],
        "limitations": detail["limitations"],
        "events": [
            {
                "sequence": event.sequence,
                "event_id": str(event.event_id),
                "schema_version": event.schema_version,
                "event_type": event.event_type,
                "stage": event.stage,
                "agent_role": event.agent_role or None,
                "occurred_at": utc_iso(event.occurred_at),
                "persisted_at": utc_iso(event.persisted_at),
                "payload": event.payload,
            }
            for event in events
        ],
        "redaction": {
            "version": "1.0",
            "dropped_key_count": len(SENSITIVE_KEYS),
            "evidence_bodies_omitted": omitted,
            "evidence_body_policy": (
                "An excerpt is exported only when the source is marked "
                "redistributable; otherwise its length is reported and the text "
                "is omitted."
            ),
        },
    }

    bundle = _redact(bundle)
    bundle = _redact_evidence_bodies(bundle)
    bundle["checksum"] = {
        "algorithm": "sha256",
        "scope": "this object with the checksum member removed",
        "value": hash_of(bundle),
    }
    return bundle
