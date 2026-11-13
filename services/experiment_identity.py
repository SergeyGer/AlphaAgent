"""The resolved attempt identity of an experiment execution (steps 7-8).

Why this module exists
----------------------
The first version of the execution namespace identified an execution by the
portfolio, the opportunity-set content hash, the provider *mode* and the
injected client's class name. Those four values are not the configuration that
produces a decision: a different model, a different sampling parameter or a
different budget ran in the same namespace and reused the earlier arm's runs.
The report then quoted one configuration while the DB row had been produced by
another.

This module resolves the values the run **actually uses** - the provider and
model reported by the client, the protocol version, the client's parameters, the
protocol budget the protocol applies and the declared execution policy - and
hashes them. An exact repeat resolves to the same identity and reuses its own
attempt; any changed value is a distinct identity.

The identity is not the namespace. The namespace answers "which experiment is
this?"; the identity answers "which configuration produced this attempt?".
``fixture_id`` carries both, plus the attempt ordinal, so:

* a forced rerun creates the next attempt instead of deleting the earlier rows;
* an exact repeat of an earlier attempt reuses it rather than double-spending;
* a changed model or budget cannot resolve to the other configuration's run.

What is deliberately not in it
------------------------------
* Wall-clock time. A replay at a different moment is the same attempt.
* The database row ids. Two independent replays of the same configuration are
  the same attempt, and the report publishes the run ids beside the identity
  rather than inside it.
* The requested provider mode when the resolved one differs. ``--provider auto``
  with no configured provider resolves to ``auto`` with no client; a scripted
  run resolves to ``stub``. The resolved value is recorded and hashed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from core.decision_json import content_hash, json_safe

__all__ = [
    "ATTEMPT_IDENTITY_VERSION",
    "AttemptIdentity",
    "attempt_prefix",
    "fixture_id_for_attempt",
    "parse_attempt",
    "resolve_attempt_identity",
]

#: Version of the identity payload. A reader that computes the hash itself must
#: use this version; a changed field set is a new version, not a silent change.
ATTEMPT_IDENTITY_VERSION = "experiment_attempt.v1"

#: The fixture-id shape. It is bounded by ``DecisionRun.fixture_id`` (64 chars):
#: ``x.<namespace 16>.<arm>.<opportunity digest 8>.<identity hash 16>.a<attempt>``
#: is at most 53 characters for a three-digit attempt.
_FIXTURE_PREFIX = "x"
_OPPORTUNITY_DIGEST_LENGTH = 8
_IDENTITY_HASH_LENGTH = 16


def _decimal_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(Decimal(str(value)).normalize())
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


@dataclass(frozen=True)
class AttemptIdentity:
    """The resolved configuration one attempt ran under."""

    identity_hash: str
    arm: str
    protocol_version: str
    provider: str
    model: str | None
    parameters: dict
    budget: dict
    execution_policy: dict
    resolved_from: str = "runtime_configuration"
    version: str = ATTEMPT_IDENTITY_VERSION

    @classmethod
    def from_dict(cls, payload: dict) -> AttemptIdentity:
        """Reconstruct an identity recorded in an archive (rebuild path)."""
        return cls(
            identity_hash=str(payload.get("identity_hash") or ""),
            arm=str(payload.get("arm") or ""),
            protocol_version=str(payload.get("protocol_version") or ""),
            provider=str(payload.get("provider") or ""),
            model=(None if payload.get("model") is None else str(payload.get("model"))),
            parameters=dict(payload.get("parameters") or {}),
            budget=dict(payload.get("budget") or {}),
            execution_policy=dict(payload.get("execution_policy") or {}),
            resolved_from=str(payload.get("resolved_from") or "archived_record"),
            version=str(payload.get("version") or ATTEMPT_IDENTITY_VERSION),
        )

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "identity_hash": self.identity_hash,
            "arm": self.arm,
            "protocol_version": self.protocol_version,
            "provider": self.provider,
            "model": self.model,
            "parameters": json_safe(self.parameters),
            "budget": json_safe(self.budget),
            "execution_policy": json_safe(self.execution_policy),
            "resolved_from": self.resolved_from,
            "note": (
                "Resolved from the configuration the run used, not from the requested "
                "labels. A changed provider, model, parameter, budget or execution policy "
                "is a distinct identity and cannot reuse another configuration's runs."
            ),
        }


def _budget_payload(budget) -> dict:
    """The budget the protocol will apply, with every ceiling named."""
    if budget is None:
        return {"applicable": False, "reason": "deterministic_no_model_call"}
    return {
        "applicable": True,
        **{key: (None if value is None else str(value)) for key, value in budget.as_dict().items()},
    }


def resolve_attempt_identity(
    *,
    arm: str,
    protocol_version: str,
    client,
    provider_mode: str,
    budget,
    execution_assumptions: dict,
    simulation_method_version: str,
    fill_timing_rule: str,
    trading_day_policy: str,
    cost_basis_policy: str,
    fee_policy: str,
) -> AttemptIdentity:
    """Resolve the identity for one arm from the runtime configuration.

    ``provider``, ``model`` and ``parameters`` are read from the client exactly
    as ``DebateProtocol`` reads them, with the same ``"unknown"`` fallback. A
    deterministic arm made no model call, so its model is ``None`` with the
    reason recorded in the policy block rather than a made-up model id.
    """
    assumptions = dict(execution_assumptions or {})
    provider_mode = str(provider_mode or "")
    if arm == "V0":
        provider = "deterministic"
        model = None
        parameters: dict = {}
        budget_payload = {"applicable": False, "reason": "deterministic_no_model_call"}
    else:
        provider = str(getattr(client, "provider", "") or "unknown").strip()
        model_value = str(getattr(client, "model", "") or "unknown").strip()
        model = model_value or None
        parameters = dict(getattr(client, "parameters", {}) or {})
        budget_payload = _budget_payload(budget)
    execution_policy = {
        "provider_mode_resolved": provider_mode,
        "fill_rule": str(
            assumptions.get("fill_rule") or "first_executable_mark_at_or_after_decision.v1"
        ),
        "fill_timing_rule": fill_timing_rule,
        "simulation_method": simulation_method_version,
        "guard_policy_version": str(
            assumptions.get("guard_policy_version") or "services.execution.evaluate_proposal"
        ),
        "fee_bps": _decimal_string(assumptions.get("fee_bps")),
        "slippage_bps": _decimal_string(assumptions.get("slippage_bps")),
        "trading_day": trading_day_policy,
        "cost_basis": cost_basis_policy,
        "fee_policy": fee_policy,
    }
    payload = {
        "identity_version": ATTEMPT_IDENTITY_VERSION,
        "arm": arm,
        "protocol_version": protocol_version,
        "provider": provider,
        "model": model,
        "parameters": json_safe(parameters),
        "budget": budget_payload,
        "execution_policy": execution_policy,
    }
    return AttemptIdentity(
        identity_hash=content_hash(payload)[:_IDENTITY_HASH_LENGTH],
        arm=arm,
        protocol_version=protocol_version,
        provider=provider,
        model=model,
        parameters=json_safe(parameters),
        budget=budget_payload,
        execution_policy=execution_policy,
    )


def attempt_prefix(*, namespace: str, arm: str, opportunity_key: str) -> str:
    """The stable, identity-free prefix for one (namespace, arm, opportunity)."""
    digest = content_hash(str(opportunity_key))[:_OPPORTUNITY_DIGEST_LENGTH]
    return f"{_FIXTURE_PREFIX}.{namespace}.{arm}.{digest}"


def fixture_id_for_attempt(*, prefix: str, identity_hash: str, attempt: int) -> str:
    """The bounded natural key of one attempt."""
    if attempt <= 0:
        raise ValueError("An attempt ordinal must be positive.")
    return f"{prefix}.{identity_hash[:_IDENTITY_HASH_LENGTH]}.a{int(attempt)}"


def parse_attempt(fixture_id: str, *, prefix: str) -> tuple[str, int] | None:
    """Return ``(identity_hash, attempt)`` for a namespaced fixture id.

    Returns ``None`` for a row that does not belong to this run key (a legacy
    fixture id, or another arm). The parser is deliberately strict: an
    unparsable suffix is not guessed into an attempt number.
    """
    marker = prefix + "."
    if not fixture_id or not fixture_id.startswith(marker):
        return None
    parts = fixture_id[len(marker) :].split(".")
    if len(parts) != 2:
        return None
    identity_hash, attempt_token = parts
    if not identity_hash or not attempt_token.startswith("a"):
        return None
    try:
        attempt = int(attempt_token[1:])
    except ValueError:
        return None
    return (identity_hash, attempt) if attempt > 0 else None
