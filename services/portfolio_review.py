"""The advisory plan review payload (FIX-08).

A reviewer must be able to answer, without reading the database or the prompt:

* what is held now, and what the model requested versus what the deterministic
  layer allowed;
* what happens to cash and to each sector, including the unclassified part;
* where the money comes from, what it is expected to cost, and that the cost is
  an estimate while ``Transaction.fee`` stays ``NULL``;
* which alternatives the model rejected, what it says remains uncertain, and
  what the input data could not support;
* which exact plan version and permission hash an approval would cover, whether
  one exists, and when it expires;
* what (if anything) was executed, through which leg and ledger row.

All of it comes from the stored plan and its brief; nothing is recomputed from
today's portfolio, because that would answer a different question from the one
the reviewer approved.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from django.utils import timezone

from core.allocation_models import EXECUTION_MODE_PAPER, PlanStatus, PortfolioPlan
from core.decision_json import content_hash, decimal_string, utc_iso
from services.portfolio_approval import approval_state

__all__ = ["build_plan_review", "plan_summary"]

ZERO = Decimal("0")
WEIGHT_QUANTUM = Decimal("0.000001")


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _weight(value: Any) -> str | None:
    parsed = _dec(value)
    return decimal_string(parsed.quantize(WEIGHT_QUANTUM)) if parsed is not None else None


def _delta(a: Any, b: Any) -> str | None:
    left, right = _dec(a), _dec(b)
    if left is None or right is None:
        return None
    return decimal_string((left - right).quantize(WEIGHT_QUANTUM))


def _action_for(plan: PortfolioPlan, ticker: str) -> dict | None:
    for row in plan.actions or []:
        if str(row.get("ticker") or "").upper() == ticker:
            return row
    return None


def _weights_table(plan: PortfolioPlan) -> list[dict]:
    brief = plan.brief or {}
    current = dict(brief.get("weights") or {})
    requested = dict(plan.requested_target_weights or {})
    constrained = dict(plan.target_weights or {})
    executed = dict(plan.executed_target_weights or {})
    tickers = sorted(
        {str(ticker).upper() for ticker in set(current) | set(requested) | set(constrained)}
    )
    rows: list[dict] = []
    for ticker in tickers:
        if ticker == "CASH" and ticker not in requested and ticker not in constrained:
            continue
        action = _action_for(plan, ticker)
        rows.append(
            {
                "ticker": ticker,
                "is_cash": ticker == "CASH",
                "current_weight": _weight(current.get(ticker)),
                "requested_weight": _weight(requested.get(ticker)),
                "constrained_weight": _weight(constrained.get(ticker)),
                "change_from_current": _delta(constrained.get(ticker), current.get(ticker)),
                "requested_vs_constrained": _delta(constrained.get(ticker), requested.get(ticker)),
                "executed_weight": _weight(executed.get(ticker)),
                "action": (action or {}).get("action"),
                "rationale": (action or {}).get("rationale"),
                "claim_ids": list((action or {}).get("claim_ids") or []),
            }
        )
    return rows


def _cash_effect(plan: PortfolioPlan) -> dict:
    brief = plan.brief or {}
    cash_now = _dec((brief.get("cash") or {}).get("weight"))
    requested = _dec((plan.requested_target_weights or {}).get("CASH"))
    constrained = _dec((plan.target_weights or {}).get("CASH"))
    equity = plan.equity_usd
    current_usd = plan.cash_usd
    target_usd = None
    if equity is not None and constrained is not None:
        target_usd = (equity * constrained).quantize(Decimal("0.01"))
    min_cash = plan.min_cash_usd
    buffer_status = "unknown"
    if target_usd is not None and min_cash is not None:
        buffer_status = "within_policy" if target_usd + Decimal("0.01") >= min_cash else "breach"
    return {
        "current_cash_weight": _weight(cash_now),
        "requested_cash_weight": _weight(requested),
        "constrained_cash_weight": _weight(constrained),
        "current_cash_usd": str(current_usd) if current_usd is not None else None,
        "target_cash_usd": str(target_usd) if target_usd is not None else None,
        "min_cash_usd": str(min_cash) if min_cash is not None else None,
        "change_usd": (
            str((target_usd - current_usd).quantize(Decimal("0.01")))
            if target_usd is not None and current_usd is not None
            else None
        ),
        "buffer_status": buffer_status,
        "note": (
            "Cash is a deliberate allocation, not a failure. The target is the "
            "deterministic projection's cash weight after limits and rounding."
        ),
    }


def _sector_effects(plan: PortfolioPlan) -> dict:
    brief = plan.brief or {}
    exposure = dict(brief.get("exposure") or {})
    classifications = dict(brief.get("classifications") or {})
    current_rows = {
        str(row.get("sector")): _dec(row.get("weight"))
        for row in exposure.get("sector_exposure") or []
        if row.get("sector")
    }
    target: dict[str, Decimal] = {}
    asset_class_target: dict[str, Decimal] = {}
    unclassified_target = ZERO
    unclassified_tickers: list[str] = []
    for ticker, value in (plan.target_weights or {}).items():
        symbol = str(ticker).upper()
        if symbol == "CASH":
            continue
        weight = _dec(value) or ZERO
        if weight <= ZERO:
            continue
        info = classifications.get(symbol) or {}
        sector = str(info.get("sector") or "").strip()
        asset_class = str(info.get("asset_class") or "").strip()
        if sector:
            target[sector] = target.get(sector, ZERO) + weight
        else:
            unclassified_target += weight
            unclassified_tickers.append(symbol)
        if asset_class:
            asset_class_target[asset_class] = asset_class_target.get(asset_class, ZERO) + weight
    labels = sorted(set(current_rows) | set(target))
    rows = []
    for sector in labels:
        current = current_rows.get(sector)
        projected = target.get(sector)
        rows.append(
            {
                "sector": sector,
                "current_weight": _weight(current),
                "target_weight": _weight(projected),
                "change": (
                    _weight(projected - current)
                    if current is not None and projected is not None
                    else None
                ),
            }
        )
    coverage = dict(exposure.get("coverage") or {})
    return {
        "rows": rows,
        "asset_class_target": {
            label: _weight(weight) for label, weight in sorted(asset_class_target.items())
        },
        "unclassified_target_weight": _weight(unclassified_target),
        "unclassified_target_tickers": sorted(unclassified_tickers),
        "current_coverage": coverage,
        "target_coverage_status": "incomplete" if unclassified_target > ZERO else "complete",
        "note": (
            "Sector weights come only from the explicit InstrumentClassification lookup. "
            "Unclassified target weight is not assigned to a default sector."
        ),
    }


def _costs(plan: PortfolioPlan) -> dict:
    legs = list(plan.legs.order_by("sequence"))
    return {
        "estimated_cost_usd": str(plan.estimated_cost_usd),
        "estimated_turnover_pct": (
            str(plan.estimated_turnover_pct) if plan.estimated_turnover_pct is not None else None
        ),
        "cost_model": plan.cost_model,
        "per_leg_estimated_cost_usd": {leg.ticker: str(leg.estimated_cost_usd) for leg in legs},
        "recorded_fee_note": (
            "These are planning estimates. Transaction.fee is NULL because no broker fee "
            "was recorded; NULL means no fee was recorded, not a zero fee."
        ),
        "fees_recorded_in_ledger": False,
    }


def _data_quality(plan: PortfolioPlan, submission_quality: dict | None) -> dict:
    brief = plan.brief or {}
    quality = dict(plan.data_quality or brief.get("data_quality") or {})
    risk = dict(brief.get("risk_statistics") or {})
    limitations: list[str] = []
    for flag in quality.get("flags") or []:
        detail = str(flag.get("detail") or "").strip()
        if detail and detail not in limitations:
            limitations.append(detail)
    if risk:
        status = risk.get("status")
        limitations.append(
            f"Correlation/covariance status: {status}. "
            f"History source: {risk.get('history_source')}. "
            + (
                "Missing pairs are null with reasons."
                if risk.get("missing_pairs")
                else "No pair was silently assumed to be zero."
            )
        )
        quality_flags = risk.get("quality_flags") or []
        if quality_flags:
            limitations.append("Risk statistics quality flags: " + ", ".join(quality_flags))
    if submission_quality:
        for text in submission_quality.get("limitations") or []:
            if text not in limitations:
                limitations.append(text)
    return {
        **quality,
        "risk_statistics": {
            "status": risk.get("status"),
            "method_version": risk.get("method_version"),
            "history_source": risk.get("history_source"),
            "observation_window": risk.get("observation_window"),
            "min_observations": risk.get("min_observations"),
            "missing_pairs": risk.get("missing_pairs") or [],
            "quality_flags": risk.get("quality_flags") or [],
            "annualisation": risk.get("annualisation"),
            "note": risk.get("note"),
        },
        "submission": submission_quality or {},
        "input": (submission_quality or {}).get("input", {}),
        "limitations": limitations,
        "declared_policies": quality.get("policy_actions") or [],
        "sector_coverage": quality.get("sector_coverage") or {},
        "source": quality.get("source") or {},
    }


def _approval_history(plan: PortfolioPlan) -> list[dict]:
    rows = []
    for approval in plan.approvals.order_by("-decided_at", "-id"):
        rows.append(
            {
                "approval_id": approval.id,
                "decision": approval.decision,
                "plan_version": approval.plan_version,
                "approval_hash": approval.approval_hash or None,
                "decided_at": utc_iso(approval.decided_at),
                "decided_by": approval.decided_by.username if approval.decided_by_id else None,
                "expires_at": utc_iso(approval.expires_at),
                "note": approval.note,
            }
        )
    return rows


def _legs(plan: PortfolioPlan) -> list[dict]:
    rows = []
    for leg in plan.legs.order_by("sequence"):
        rows.append(
            {
                "leg_id": leg.id,
                "sequence": leg.sequence,
                "ticker": leg.ticker,
                "side": leg.side,
                "quantity": str(leg.quantity),
                "price": str(leg.price),
                "notional_usd": str(leg.notional_usd),
                "estimated_cost_usd": str(leg.estimated_cost_usd),
                "priority": leg.priority,
                "sector": leg.sector or None,
                "asset_class": leg.asset_class or None,
                "status": leg.status,
                "transaction_id": leg.transaction_id,
                "applied_at": utc_iso(leg.applied_at),
                "reason": leg.reason,
                "signed_cash_effect_usd": str(leg.signed_cash_effect_usd),
            }
        )
    return rows


def _submission_for(plan: PortfolioPlan):
    return plan.submissions.order_by("-created_at", "-id").first() if plan.pk else None


def _provenance(plan: PortfolioPlan, submission) -> dict:
    metadata = dict(plan.model_metadata or {})
    synthetic = bool(metadata.get("synthetic_response"))
    if submission is not None and submission.synthetic_response:
        synthetic = True
    provider = str(metadata.get("provider") or (submission.provider if submission else "") or "")
    model = str(metadata.get("model") or (submission.model if submission else "") or "")
    mode = str(
        metadata.get("analysis_mode")
        or (submission.mode if submission is not None else "")
        or "unknown"
    )
    if synthetic:
        label = "Stub decision (no model call)"
    elif provider and provider not in {"stub", "fixture"}:
        label = f"Live model decision ({provider}/{model})"
    else:
        label = "Producer not recorded"
    return {
        "provider": provider or None,
        "model": model or None,
        "mode": mode,
        "synthetic_response": synthetic,
        "label": label,
        "prompt_template_version": metadata.get("prompt_template_version"),
        "protocol_version": metadata.get("protocol_version") or plan.protocol_version,
        "prompt_hash": metadata.get("prompt_hash"),
        "parameters": metadata.get("parameters") or {},
        "input_tokens": metadata.get("input_tokens"),
        "output_tokens": metadata.get("output_tokens"),
        "cost_usd": metadata.get("cost_usd"),
        "cost_reported": metadata.get("cost_reported"),
        "token_accounting": metadata.get("token_accounting"),
        "adapter": metadata.get("adapter"),
        "calls": metadata.get("calls") or [],
        "note": metadata.get("note"),
    }


def build_plan_review(plan: PortfolioPlan, *, now: datetime | None = None) -> dict:
    """The complete review payload for one plan version."""
    now = now or timezone.now()
    submission = _submission_for(plan)
    state = approval_state(plan, now=now)
    requested = dict(plan.requested_target_weights or {})
    constrained = dict(plan.target_weights or {})
    fingerprint_payload = {
        "plan_id": str(plan.id),
        "plan_version": plan.plan_version,
        "brief_hash": plan.brief_hash,
        "approval_hash": plan.approval_hash,
        "requested": {key: str(requested[key]) for key in sorted(requested)},
        "constrained": {key: str(constrained[key]) for key in sorted(constrained)},
        "legs": [
            {
                "sequence": leg.sequence,
                "ticker": leg.ticker,
                "side": leg.side,
                "quantity": str(leg.quantity),
                "price": str(leg.price),
            }
            for leg in plan.legs.order_by("sequence")
        ],
    }
    legs = _legs(plan)
    reviewable = plan.status == PlanStatus.PROPOSED
    return {
        "plan_id": str(plan.id),
        "portfolio_id": plan.portfolio_id,
        "status": plan.status,
        "reviewable": reviewable,
        "plan_version": plan.plan_version,
        "review_fingerprint": content_hash(fingerprint_payload),
        "created_at": utc_iso(plan.created_at),
        "updated_at": utc_iso(plan.updated_at),
        "applied_at": utc_iso(plan.applied_at),
        "execution_mode": plan.execution_mode,
        "protocol_version": plan.protocol_version,
        "policy_version": plan.policy_version,
        "proposal_portfolio_version": plan.proposal_portfolio_version,
        "evaluated_portfolio_version": plan.evaluated_portfolio_version,
        "brief_hash": plan.brief_hash or None,
        "approval_hash": plan.approval_hash or None,
        "data_sufficiency": plan.data_sufficiency or None,
        "objective": plan.objective,
        "rationale": plan.rationale,
        "actions": plan.actions,
        "holdings": list((plan.brief or {}).get("holdings") or []),
        "weights": _weights_table(plan),
        "requested_target_weights": requested,
        "constrained_target_weights": constrained,
        "cash_effect": _cash_effect(plan),
        "sector_effects": _sector_effects(plan),
        "funding": plan.funding,
        "costs": _costs(plan),
        "estimated_turnover_pct": (
            str(plan.estimated_turnover_pct) if plan.estimated_turnover_pct is not None else None
        ),
        "rejected_alternatives": plan.rejected_alternatives,
        "unresolved_uncertainties": plan.unresolved_uncertainties,
        "deviations": plan.deviations,
        "constraints": plan.constraints,
        "data_quality": _data_quality(
            plan, submission.data_quality if submission is not None else None
        ),
        "limits": (plan.brief or {}).get("limits") or (plan.brief or {}).get("policy") or {},
        "legs": legs,
        "provenance": _provenance(plan, submission),
        "submission": (
            {
                "run_id": str(submission.id),
                "mode": submission.mode,
                "status": submission.status,
                "synthetic_response": submission.synthetic_response,
                "provider": submission.provider or None,
                "model": submission.model or None,
                "request_hash": submission.request_hash,
            }
            if submission is not None
            else None
        ),
        "approval": state,
        "approval_history": _approval_history(plan),
        "execution": {
            "status": plan.status,
            "applied_at": utc_iso(plan.applied_at),
            "applied_by": plan.applied_by.username if plan.applied_by_id else None,
            "applied_approval_version": plan.applied_approval_version or None,
            "executed_target_weights": plan.executed_target_weights,
            "reasons": plan.execution_reasons,
            "transaction_ids": [leg["transaction_id"] for leg in legs if leg["transaction_id"]],
            "failure_code": plan.failure_code or None,
            "failure_detail": plan.failure_detail or None,
        },
        "revalidation": {
            "required": plan.revalidation_required,
            "count": plan.revalidation_count,
            "revalidated_at": utc_iso(plan.revalidated_at),
        },
        "review_actions": {
            "can_approve": reviewable
            and not plan.revalidation_required
            and plan.status == PlanStatus.PROPOSED,
            "can_reject": reviewable,
            "can_cancel": reviewable,
            "can_apply": reviewable and state["state"] == "approved",
            "can_revalidate": reviewable,
            "default_apply_mode": (
                EXECUTION_MODE_PAPER if plan.execution_mode == EXECUTION_MODE_PAPER else "live"
            ),
        },
        "immutability": {
            "plan_version": plan.plan_version,
            "approval_version": plan.approval_version or None,
            "approval_required": True,
            "requires_expiry": True,
            "note": (
                "An approval names this plan_version and approval_hash and carries an "
                "expiry. A revalidation increments plan_version and clears the approval; "
                "the old audit rows remain."
            ),
        },
    }


def plan_summary(plan: PortfolioPlan, *, now: datetime | None = None) -> dict:
    """One row for the plan list; deliberately not the full review."""
    state = approval_state(plan, now=now)
    return {
        "plan_id": str(plan.id),
        "portfolio_id": plan.portfolio_id,
        "status": plan.status,
        "plan_version": plan.plan_version,
        "protocol_version": plan.protocol_version,
        "provider": (plan.model_metadata or {}).get("provider"),
        "model": (plan.model_metadata or {}).get("model"),
        "synthetic_response": bool((plan.model_metadata or {}).get("synthetic_response")),
        "approval_state": state["state"],
        "approval_expires_at": state["expires_at"],
        "objective": plan.objective,
        "requested_weights": plan.requested_target_weights,
        "constrained_weights": plan.target_weights,
        "estimated_cost_usd": str(plan.estimated_cost_usd),
        "data_sufficiency": plan.data_sufficiency or None,
        "revalidation_required": plan.revalidation_required,
        "created_at": utc_iso(plan.created_at),
        "applied_at": utc_iso(plan.applied_at),
    }
