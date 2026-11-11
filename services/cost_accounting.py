"""Model-cost accounting with five quantities that are never substituted for one another.

A cost figure is only meaningful when a reader can tell which of these it is:

``measured tokens``
    Token counts the provider reported for the call. They are an observation.
``estimated tokens``
    Token counts inferred from character counts. They are a model, not an
    observation, and they are labelled as such.
``price-table cost``
    Dollars derived by multiplying tokens (measured *or* estimated) by the
    configured per-model price table. It is arithmetic on top of tokens; it is
    not a bill.
``actual billed cost``
    Dollars the provider reported as the amount charged. Only a provider can
    supply this.
``unknown``
    No value. It is ``None`` with a typed reason, never ``Decimal("0")``. A zero
    means "the charge was zero", which is a measurement; conflating it with
    "we did not find out" is the defect this module exists to prevent.

The monetary ceiling in :mod:`services.debate_protocol` is therefore enforced
against the best available *basis* (billed where reported, price-table estimate
otherwise) and the run metadata says which basis was used. It is **not** a
strict billed-dollar ceiling: a provider can report tokens but not dollars, a
timed-out call can still be billed after the run ends, and a price table can be
wrong. The metadata carries ``strict_billed_ceiling: false`` for that reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.conf import settings

__all__ = [
    "UNKNOWN_MODEL_PRICING",
    "UNKNOWN_NO_CALL",
    "UNKNOWN_PROVIDER_COST",
    "UNKNOWN_PROVIDER_TOKEN_USAGE",
    "UNKNOWN_UNRECONCILED_TIMEOUT",
    "CostBreakdown",
    "TokenUsage",
    "estimate_cost",
    "price_rates",
    "price_table_cost",
]

logger = logging.getLogger("alphaagent.cost_accounting")

MILLION = Decimal("1000000")
ZERO = Decimal("0")

#: Why a billed cost is null. Typed so a report can group reasons instead of
#: parsing prose.
UNKNOWN_PROVIDER_TOKEN_USAGE = "provider_token_usage_unavailable"  # noqa: S105 - a reason code, not a secret
UNKNOWN_PROVIDER_COST = "provider_reported_cost_unavailable"
UNKNOWN_MODEL_PRICING = "model_not_in_price_table"
UNKNOWN_NO_CALL = "no_model_call_recorded"
UNKNOWN_UNRECONCILED_TIMEOUT = "timed_out_call_not_reconciled"


@dataclass(frozen=True)
class TokenUsage:
    """The split behind one agent run's token count.

    ``input_tokens`` is the **full** prompt count and already includes the cached
    and cache-write portions - CrewAI's Anthropic provider folds those counters
    into ``input_tokens`` before they reach ``usage_metrics``. The cached figures
    are therefore separated out here for pricing, not added to the total.
    """

    total: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def fresh_input_tokens(self) -> int:
        """Prompt tokens billed at the full input rate."""
        return max(0, self.input_tokens - self.cached_read_tokens - self.cache_write_tokens)

    @property
    def has_split(self) -> bool:
        """True when the provider reported a real input/output breakdown."""
        return self.input_tokens > 0 or self.output_tokens > 0


@dataclass
class CostBreakdown:
    """One run's cost, with every quantity kept separate.

    ``measured_*`` and ``estimated_*`` token counts are summed separately, so a
    total can never hide which part was observed. ``price_table_cost_usd`` is
    ``None`` unless *every* priced call had a price-table entry; a partial sum is
    published separately as ``price_table_cost_partial_usd`` and the count of
    unpriced calls makes the gap visible. ``billed_cost_usd`` is ``None`` unless
    every call reported an actual charge.
    """

    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0

    price_table_cost_usd: Decimal = ZERO
    price_table_priced_calls: int = 0
    price_table_unpriced_calls: int = 0
    price_table_unknown_reason: str = ""

    billed_cost_usd: Decimal = ZERO
    billed_cost_calls: int = 0
    billed_cost_unknown_calls: int = 0
    billed_cost_unknown_reason: str = ""

    calls: int = 0
    timed_out_calls: int = 0
    reconciled_timeout_calls: int = 0
    unreconciled_timeout_calls: int = 0

    #: Per-call basis used for the monetary ceiling. ``billed`` when the provider
    #: reported the charge, ``price_table_estimate`` when only a table price was
    #: available, ``unknown`` when neither exists.
    budget_basis_counts: dict[str, int] = field(default_factory=dict)
    budget_spent_usd: Decimal = ZERO

    @property
    def input_tokens(self) -> int:
        return self.measured_input_tokens + self.estimated_input_tokens

    @property
    def output_tokens(self) -> int:
        return self.measured_output_tokens + self.estimated_output_tokens

    @property
    def token_accounting(self) -> str:
        """How the token totals were obtained, without pooling the two sources."""
        if not self.calls:
            return "no_calls_recorded"
        measured = self.measured_input_tokens + self.measured_output_tokens
        estimated = self.estimated_input_tokens + self.estimated_output_tokens
        if measured and estimated:
            return "mixed_measured_and_estimated"
        if measured:
            return "provider_reported"
        return "estimated_chars_per_4_tokens"

    @property
    def price_table_cost_complete(self) -> bool:
        return self.price_table_priced_calls > 0 and self.price_table_unpriced_calls == 0

    @property
    def billed_cost_complete(self) -> bool:
        return self.calls > 0 and self.billed_cost_unknown_calls == 0

    @property
    def price_table_cost_known(self) -> str | None:
        """The complete price-table cost, or ``None`` when any call is unpriced."""
        if not self.price_table_cost_complete:
            return None
        return str(self.price_table_cost_usd.quantize(Decimal("0.000001")))

    @property
    def price_table_cost_partial(self) -> str | None:
        if self.price_table_cost_complete or not self.price_table_priced_calls:
            return None
        return str(self.price_table_cost_usd.quantize(Decimal("0.000001")))

    @property
    def billed_cost(self) -> str | None:
        if not self.billed_cost_complete:
            return None
        return str(self.billed_cost_usd.quantize(Decimal("0.000001")))

    @property
    def billed_cost_unknown(self) -> bool:
        return not self.billed_cost_complete

    @property
    def budget_basis(self) -> str:
        bases = set(self.budget_basis_counts)
        if not bases:
            return "unknown"
        if len(bases) == 1:
            return next(iter(bases))
        return "mixed_billed_and_price_table_estimate"

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "measured_tokens": {
                "input": self.measured_input_tokens,
                "output": self.measured_output_tokens,
            },
            "estimated_tokens": {
                "input": self.estimated_input_tokens,
                "output": self.estimated_output_tokens,
            },
            # Kept for consumers that only need a total; the split above is the
            # authoritative record and ``token_accounting`` says which part it is.
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "token_accounting": self.token_accounting,
            "price_table_cost_usd": self.price_table_cost_known,
            "price_table_cost_partial_usd": self.price_table_cost_partial,
            "price_table_cost_complete": self.price_table_cost_complete,
            "price_table_cost_unknown_calls": self.price_table_unpriced_calls,
            "price_table_cost_unknown_reason": (
                self.price_table_unknown_reason or None if self.price_table_unpriced_calls else None
            ),
            "billed_cost_usd": self.billed_cost,
            "billed_cost_reported": self.billed_cost_complete,
            "billed_cost_unknown_calls": self.billed_cost_unknown_calls,
            "billed_cost_unknown_reason": (
                self.billed_cost_unknown_reason or None if self.billed_cost_unknown_calls else None
            ),
            "timed_out_calls": self.timed_out_calls,
            "reconciled_timeout_calls": self.reconciled_timeout_calls,
            "unreconciled_timeout_calls": self.unreconciled_timeout_calls,
            "unreconciled_charge_note": (
                "A timed-out call may still be billed by the provider after this run "
                "ended; its charge is not included in any figure here."
                if self.unreconciled_timeout_calls
                else None
            ),
            "monetary_ceiling_basis": self.budget_basis,
            "monetary_ceiling_spent_usd": str(self.budget_spent_usd.quantize(Decimal("0.000001"))),
            "strict_billed_ceiling": False,
        }


def price_rates(model: str) -> dict | None:
    """The price-table entry for ``model``, or ``None`` when it is unpriced.

    Exact id first, then the longest prefix, so a dated id such as
    ``claude-haiku-4-5-20251001`` resolves against ``claude-haiku-4-5``. Unlike
    :func:`estimate_cost`, this performs **no default substitution**: an unpriced
    model is unknown, and the caller must say so rather than pricing it at an
    arbitrary rate.
    """
    pricing = getattr(settings, "AI_MODEL_PRICING", {})
    key = (model or "").split("/")[-1]
    if not key:
        return None
    rates = pricing.get(key)
    if rates is None:
        candidates = [candidate for candidate in pricing if key.startswith(candidate)]
        if candidates:
            rates = pricing[max(candidates, key=len)]
    return rates


def _rates_to_decimals(rates: dict) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    try:
        rate_in = Decimal(str(rates.get("input", 0)))
        rate_out = Decimal(str(rates.get("output", 0)))
        rate_cached = Decimal(str(rates.get("cached_input", rates.get("input", 0))))
        rate_write = Decimal(str(rates.get("cache_write", rates.get("input", 0))))
    except (InvalidOperation, TypeError, ValueError):
        return ZERO, ZERO, ZERO, ZERO
    return rate_in, rate_out, rate_cached, rate_write


def price_table_cost(
    usage: TokenUsage | int,
    model: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> Decimal | None:
    """Price-table cost for ``usage``, or ``None`` when the model has no entry.

    A ``TokenUsage`` with a real input/output split is priced by component; a bare
    total falls back to the blended rate the project has always used for
    providers that report only a total. Both are *estimates derived from a price
    table*, whatever the tokens were; the caller must not report either as a
    billed charge.
    """
    rates = price_rates(model)
    if rates is None:
        return None
    rate_in, rate_out, rate_cached, rate_write = _rates_to_decimals(rates)
    if isinstance(usage, TokenUsage) and usage.has_split:
        cost = (
            Decimal(usage.fresh_input_tokens) * rate_in
            + Decimal(usage.cached_read_tokens) * rate_cached
            + Decimal(usage.cache_write_tokens) * rate_write
            + Decimal(usage.output_tokens) * rate_out
        ) / MILLION
    elif input_tokens is not None or output_tokens is not None:
        cost = (
            Decimal(int(input_tokens or 0)) * rate_in + Decimal(int(output_tokens or 0)) * rate_out
        ) / MILLION
    else:
        total = usage.total if isinstance(usage, TokenUsage) else int(usage)
        blended = rate_in * Decimal("0.7") + rate_out * Decimal("0.3")
        cost = Decimal(total) / MILLION * blended
    return cost.quantize(Decimal("0.00001"))


def estimate_cost(usage: TokenUsage | int, model: str) -> Decimal:
    """Approximate USD cost from the configured price table.

    Kept for the legacy sequential path and for callers that explicitly want an
    estimate: it substitutes the configured default rate when the model is
    unpriced, logging a warning. New code that needs to distinguish "priced" from
    "unknown" should call :func:`price_table_cost`, which returns ``None``.
    """
    priced = price_table_cost(usage, model)
    if priced is not None:
        return priced

    default = getattr(settings, "AI_DEFAULT_PRICING", {"input": 0.27, "output": 1.10})
    rate_in = Decimal(str(default.get("input", 0)))
    rate_out = Decimal(str(default.get("output", 0)))
    logger.warning(
        "No price entry for model %r - cost is approximate and MUST be labelled an "
        "estimate. Add it to AI_MODEL_PRICING in config/settings.py.",
        (model or "<unset>").split("/")[-1],
    )
    if isinstance(usage, TokenUsage) and usage.has_split:
        cost = (
            Decimal(usage.input_tokens) * rate_in + Decimal(usage.output_tokens) * rate_out
        ) / MILLION
    else:
        total = usage.total if isinstance(usage, TokenUsage) else int(usage)
        blended = rate_in * Decimal("0.7") + rate_out * Decimal("0.3")
        cost = Decimal(total) / MILLION * blended
    return cost.quantize(Decimal("0.00001"))
