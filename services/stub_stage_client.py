"""A scripted stage client for the experiment execution path (FIX-07B).

This client exists so the V0-V3 execution path can be exercised **end to end**
without an API key: the same ``DebateProtocol`` code, the same snapshot, the same
budgets, the same guard and the same accounting. What it is not is a language
model. Its replies are deterministic functions of the prompt, so the runs it
produces are recorded as ``mode="simulated"`` / ``source="stub"`` and every
report marks them ``engineering_validation_only``. They must never be presented
as a protocol comparison.

The replies are deliberately differentiated by stage so the arms are not
identical by construction: the one-analyst baseline answers from the brief, the
no-rebuttal configuration answers after reading both cases, and the rebuttal
configuration answers after reading a dispute. A scripted client cannot prove
that a real model would behave differently; it can only prove that the arms,
their records, their snapshots, their guard outcomes and their accounting are
distinct and independently bound.

Token counts are **estimated** from the exact prompt and reply text at four
characters per token. No provider tokenizer was used, so they are reported as
estimated tokens, not as measured/provider-reported ones. The billed cost is
``None``: no provider was called and therefore no charge exists. The price-table
cost is published separately as an estimate.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

from services.cost_accounting import TokenUsage, price_table_cost
from services.debate_protocol import ModelUsage

__all__ = ["OfflineStageClient"]

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CATALOG_ID_RE = re.compile(rf'"id": "({UUID})"')
CLAIM_ID_RE = re.compile(rf'"claim_id": "({UUID})"')
CASE_CLAIM_ID_RE = re.compile(rf"\(claim_id ({UUID});")
VALID_TARGET_RE = re.compile(rf"^- ({UUID})$", re.MULTILINE)


def _catalog_ids(prompt: str) -> list[str]:
    return list(dict.fromkeys(CATALOG_ID_RE.findall(prompt)))


def _opposing_claim_ids(prompt: str) -> list[str]:
    found = CLAIM_ID_RE.findall(prompt) or VALID_TARGET_RE.findall(prompt)
    return list(dict.fromkeys(found))


def _case_claim_ids(prompt: str) -> list[str]:
    return list(dict.fromkeys(CASE_CLAIM_ID_RE.findall(prompt)))


class OfflineStageClient:
    """Deterministic, offline implementation of the protocol's model seam."""

    provider = "stub"
    #: The price table is applied to this configured model id so the estimate is
    #: meaningful. ``provider="stub"`` is what says no model was actually called.
    model = "deepseek-reasoner"
    parameters: ClassVar[dict] = {
        "temperature": 0.0,
        "max_output_tokens": 4096,
        "scripted": True,
    }

    def __init__(self, *, model: str | None = None) -> None:
        if model:
            self.model = model
        self.last_usage: ModelUsage | None = None
        self.calls: list[tuple[str, str]] = []

    # -- replies ----------------------------------------------------------
    def _bull_case(self, prompt: str) -> dict:
        ids = _catalog_ids(prompt)
        return {
            "summary": (
                "The available evidence is mildly constructive, but the case is a "
                "mechanical read of the catalog and not a strong conviction."
            ),
            "claims": [
                {
                    "statement": "The catalog evidence leans positive for the instrument",
                    "claim_type": "interpretation",
                    "evidence_ids": ids[:1],
                    "horizon": "30d",
                    "limitations": "A scripted reading of evidence carries no judgement.",
                }
            ],
        }

    def _bear_case(self, prompt: str) -> dict:
        ids = _catalog_ids(prompt)
        return {
            "summary": (
                "The catalog also contains material that argues against the position; "
                "it is not sufficient to reject the bull case outright."
            ),
            "claims": [
                {
                    "statement": "The catalog evidence contains a credible offsetting risk",
                    "claim_type": "observed_fact",
                    "evidence_ids": ids[-1:],
                    "horizon": "30d",
                    "limitations": "One catalog item is not the whole picture.",
                }
            ],
        }

    def _rebuttal(self, prompt: str) -> dict:
        targets = _opposing_claim_ids(prompt)
        return {
            "responses": [
                {
                    "target_claim_id": targets[0],
                    "response_type": "dispute",
                    "explanation": (
                        "The opposing claim is not settled by the evidence it cites; "
                        "the same catalog item supports a smaller conclusion."
                    ),
                    "evidence_ids": _catalog_ids(prompt)[-1:],
                    "position_change": (
                        "Unchanged: the claim did not move this position, and the "
                        "unresolved uncertainty is recorded."
                    ),
                }
            ]
        }

    def _adjudication(self, prompt: str, *, baseline: bool = False) -> dict:
        # The three configurations read different prompts, and answer differently:
        # the single analyst proposes; the no-rebuttal configuration follows the
        # stronger case; the rebuttal configuration records the dispute and holds.
        disputed = _case_claim_ids(prompt)
        rebuttal_present = "No rebuttals were recorded." not in prompt
        if baseline:
            action, amount, sentiment = "HOLD", 0, "NEUTRAL"
            reasoning = (
                "Single analyst baseline: one case, no adversarial round. The scripted "
                "analyst holds because the evidence on its own does not support size."
            )
            decisive: list[str] = []
        elif rebuttal_present:
            action, amount, sentiment = "HOLD", 0, "NEUTRAL"
            reasoning = (
                "The scripted rebuttal disputed the bear claim and left the dispute open, "
                "so the CIO holds rather than treating the matter as settled."
            )
            decisive = disputed[:1]
        else:
            action, amount, sentiment = "BUY", 1, "BULLISH"
            reasoning = (
                "No rebuttal round was executed in this configuration; the scripted CIO "
                "follows the constructive case and buys a small position."
            )
            decisive = disputed[:1]
        return {
            "action": action,
            "amount": amount,
            "sentiment": sentiment,
            "reasoning": reasoning,
            "decisive_claim_ids": decisive,
            "strongest_objection": (
                "The offsetting risk in the catalog could be material at this horizon."
            ),
            "unresolved_uncertainty": (
                "Whether the offsetting evidence outweighs the constructive case."
            ),
            "thesis": "" if action == "HOLD" else "Scripted constructive case.",
            "invalidation_condition": "" if action == "HOLD" else "The risk item materialises.",
        }

    def _reply(self, stage: str, prompt: str) -> dict:
        if stage == "bull_case":
            return self._bull_case(prompt)
        if stage == "bear_case":
            return self._bear_case(prompt)
        if stage in {"bull_rebuttal", "bear_rebuttal"}:
            return self._rebuttal(prompt)
        if stage == "baseline_analysis":
            return self._adjudication(prompt, baseline=True)
        if stage == "adjudication":
            return self._adjudication(prompt)
        raise ValueError(f"OfflineStageClient has no script for stage {stage!r}")

    # -- seam -------------------------------------------------------------
    def complete(self, stage: str, prompt: str) -> str:
        payload = self._reply(stage, prompt)
        text = json.dumps(payload, sort_keys=True)
        self.calls.append((stage, prompt))
        input_tokens = max(1, len(prompt) // 4)
        output_tokens = max(1, len(text) // 4)
        priced = price_table_cost(
            TokenUsage(
                total=input_tokens + output_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            self.model,
        )
        self.last_usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # No provider was called, so no charge exists. This is an unknown
            # billed cost, not a zero one.
            cost_usd=None,
            reported=False,
            tokens_measured=False,
            price_table_cost_usd=priced,
            cost_unknown_reason="scripted_provider_made_no_charge",
        )
        return text
