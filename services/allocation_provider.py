"""The provider-backed CIO adapter, with the protocol's budgets applied (FIX-08).

``PortfolioCIO`` (in ``services.portfolio_allocation``) only needs
``complete(stage, prompt) -> str``. This module supplies two implementations:

* :class:`BudgetedAllocationClient` wraps the **real model seam** -
  ``services.debate_protocol.StageClient``, which ``ai_agent.CrewAIStageClient``
  implements for the configured provider - and applies the same constraints the
  debate protocol applies before every attempt: stage timeout bounded by the
  remaining run deadline, input/output token ceilings, retries with backoff that
  must fit the deadline, and a monetary ceiling. Its usage accounting keeps
  measured tokens, estimated tokens, price-table cost, billed cost and unknown
  separate, exactly as FIX-07A requires.
* :class:`StaticAllocationClient` (imported from the allocation module) is the
  explicit stub. It is never selected implicitly: ``build_allocation_client``
  requires the caller to say ``mode="stub"``, and every plan built from it
  records ``provider: "stub"``.

``build_allocation_client`` is the only place a provider SDK is imported. It is
lazy, so importing the allocation path still needs no key and no network.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from django.conf import settings

from services.cost_accounting import (
    UNKNOWN_MODEL_PRICING,
    UNKNOWN_PROVIDER_COST,
    UNKNOWN_UNRECONCILED_TIMEOUT,
    price_table_cost,
    pricing_version,
)
from services.debate_protocol import (
    BudgetExhausted,
    DeadlineExceeded,
    ModelUsage,
    ProtocolBudget,
    ProtocolUsage,
    ProviderError,
    ProviderTransientError,
    StageFailure,
    StageTimeout,
    _call_with_timeout,
    _estimate_tokens,
)
from services.portfolio_allocation import AllocationClient, StaticAllocationClient

logger = logging.getLogger("alphaagent.allocation_provider")

__all__ = [
    "AllocationProviderUnavailable",
    "BudgetedAllocationClient",
    "allocation_budget",
    "build_allocation_client",
]


class AllocationProviderUnavailable(RuntimeError):
    """The live adapter was requested but no usable provider is configured."""


def allocation_budget(config: dict | None = None) -> ProtocolBudget:
    """The ceilings a portfolio allocation call runs under.

    ``ALLOCATION_CONFIG`` overrides ``DEBATE_CONFIG`` where it sets a value, so
    the allocation path has its own knobs without inventing a second budget
    vocabulary. Every value is a ceiling the adapter stops at, not a target.
    """
    base = dict(getattr(settings, "DEBATE_CONFIG", {}) or {})
    override = dict(config if config is not None else getattr(settings, "ALLOCATION_CONFIG", {}))
    # The allocation settings use a ``SUBMISSION_`` prefix so they cannot be
    # mistaken for the debate protocol's own ceilings; map them onto the shared
    # vocabulary rather than maintaining a second budget class.
    mapping = {
        "STAGE_TIMEOUT_SECONDS": "SUBMISSION_STAGE_TIMEOUT_SECONDS",
        "RUN_DEADLINE_SECONDS": "SUBMISSION_RUN_DEADLINE_SECONDS",
        "MAX_INPUT_TOKENS": "SUBMISSION_MAX_INPUT_TOKENS",
        "MAX_OUTPUT_TOKENS": "SUBMISSION_MAX_OUTPUT_TOKENS",
        "PROVIDER_RETRIES": "SUBMISSION_PROVIDER_RETRIES",
        "MONETARY_BUDGET_USD": "SUBMISSION_MONETARY_BUDGET_USD",
    }
    for target, source in mapping.items():
        if source in override and override[source] not in (None, ""):
            base[target] = override[source]
    return ProtocolBudget.from_settings(base)


class BudgetedAllocationClient:
    """A ``StageClient`` wrapped in the protocol's budget and usage accounting.

    The class is an ``AllocationClient`` (provider/model/parameters/last_usage and
    ``complete``). It contains no provider-specific import: the wrapped client is
    the model seam, so tests can substitute a scripted ``StageClient`` and this
    class still enforces the real budget path.
    """

    def __init__(
        self,
        client: Any,
        *,
        budget: ProtocolBudget | None = None,
    ) -> None:
        self.client = client
        self.budget = budget or allocation_budget()
        self.usage = ProtocolUsage()
        self.last_usage: ModelUsage | None = None
        #: The last usage object already accounted, so a failed retry is not
        #: counted twice when the client leaves ``last_usage`` unchanged.
        self._accounted_client_usage: Any = None
        self._started_monotonic = time.monotonic()
        self.calls: list[dict] = []
        self.notes: list[str] = []

    # -- provenance -------------------------------------------------------
    @property
    def provider(self) -> str:
        return str(getattr(self.client, "provider", "") or "unknown")

    @property
    def model(self) -> str:
        return str(getattr(self.client, "model", "") or "unknown")

    @property
    def parameters(self) -> dict:
        return dict(getattr(self.client, "parameters", {}) or {})

    # -- budget helpers ---------------------------------------------------
    def _elapsed(self) -> float:
        return time.monotonic() - self._started_monotonic

    def _remaining_deadline(self) -> float | None:
        if not self.budget.run_deadline_seconds:
            return None
        return self.budget.run_deadline_seconds - self._elapsed()

    def _effective_timeout(self, stage: str) -> tuple[float, str]:
        stage_timeout = self.budget.stage_timeout_seconds or 0.0
        remaining = self._remaining_deadline()
        if remaining is None:
            return stage_timeout, "stage_timeout"
        if remaining <= 0:
            raise DeadlineExceeded(
                f"run deadline of {self.budget.run_deadline_seconds:g}s elapsed before "
                f"stage {stage!r} could start.",
                budget="run_deadline",
                stage=stage,
            )
        if stage_timeout and stage_timeout <= remaining:
            return stage_timeout, "stage_timeout"
        return remaining, "remaining_deadline"

    def _check_budgets(self, stage: str, prompt: str) -> None:
        remaining = self._remaining_deadline()
        if remaining is not None and remaining <= 0:
            raise DeadlineExceeded(
                f"run deadline of {self.budget.run_deadline_seconds:g}s elapsed before "
                f"stage {stage!r}.",
                budget="run_deadline",
                stage=stage,
            )
        if (
            self.budget.max_input_tokens
            and self.usage.input_tokens + _estimate_tokens(prompt) > self.budget.max_input_tokens
        ):
            raise BudgetExhausted(
                f"input token budget of {self.budget.max_input_tokens} would be exceeded "
                f"before stage {stage!r}.",
                budget="input_tokens",
                stage=stage,
            )
        if (
            self.budget.max_output_tokens
            and self.usage.output_tokens >= self.budget.max_output_tokens
        ):
            raise BudgetExhausted(
                f"output token budget of {self.budget.max_output_tokens} was exhausted "
                f"before stage {stage!r}.",
                budget="output_tokens",
                stage=stage,
            )
        if (
            self.budget.monetary_budget_usd is not None
            and self.usage.budget_spent_usd >= self.budget.monetary_budget_usd
        ):
            raise BudgetExhausted(
                f"monetary budget of {self.budget.monetary_budget_usd} USD was "
                f"exhausted before stage {stage!r} (basis: {self.usage.budget_basis}; "
                f"{self.usage.unreconciled_timeout_calls} unreconciled call(s)). This "
                "ceiling is not a strict billed-dollar cap.",
                budget="monetary",
                stage=stage,
            )

    # -- usage accounting -------------------------------------------------
    def _read_usage(self, prompt: str, text: str) -> ModelUsage:
        reported = getattr(self.client, "last_usage", None)
        if isinstance(reported, ModelUsage):
            if reported.price_table_cost_usd is None and reported.cost_usd is None:
                priced = price_table_cost(
                    reported.input_tokens + reported.output_tokens,
                    self.model,
                    input_tokens=reported.input_tokens,
                    output_tokens=reported.output_tokens,
                )
                reported = ModelUsage(
                    input_tokens=reported.input_tokens,
                    output_tokens=reported.output_tokens,
                    cost_usd=reported.cost_usd,
                    reported=reported.reported,
                    tokens_measured=reported.tokens_measured,
                    price_table_cost_usd=priced,
                    pricing_version=reported.pricing_version or pricing_version(),
                    cost_unknown_reason=reported.cost_unknown_reason,
                )
            return reported
        input_tokens = _estimate_tokens(prompt)
        output_tokens = _estimate_tokens(text)
        priced = price_table_cost(
            input_tokens + output_tokens,
            self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=None,
            reported=False,
            tokens_measured=False,
            price_table_cost_usd=priced,
            pricing_version=pricing_version() if priced is not None else "",
            cost_unknown_reason=UNKNOWN_PROVIDER_COST,
        )

    def _account(self, stage: str, usage: ModelUsage, *, elapsed_ms: int) -> None:
        self.usage.calls += 1
        if usage.measured_tokens:
            self.usage.measured_input_tokens += usage.input_tokens
            self.usage.measured_output_tokens += usage.output_tokens
            accounting_basis = "provider_reported"
        else:
            self.usage.estimated_input_tokens += usage.input_tokens
            self.usage.estimated_output_tokens += usage.output_tokens
            accounting_basis = "estimated_chars_per_4_tokens"

        if usage.cost_usd is not None:
            self.usage.billed_cost_calls += 1
            self.usage.billed_cost_usd += usage.cost_usd
            self.usage.budget_basis_counts["billed"] = (
                self.usage.budget_basis_counts.get("billed", 0) + 1
            )
            self.usage.budget_spent_usd += usage.cost_usd
        else:
            self.usage.billed_cost_unknown_calls += 1
            if not self.usage.billed_cost_unknown_reason:
                self.usage.billed_cost_unknown_reason = (
                    usage.cost_unknown_reason or UNKNOWN_PROVIDER_COST
                )

        if usage.price_table_cost_usd is not None:
            self.usage.price_table_priced_calls += 1
            self.usage.price_table_cost_usd += usage.price_table_cost_usd
            if usage.cost_usd is None:
                self.usage.budget_basis_counts["price_table_estimate"] = (
                    self.usage.budget_basis_counts.get("price_table_estimate", 0) + 1
                )
                self.usage.budget_spent_usd += usage.price_table_cost_usd
        else:
            self.usage.price_table_unpriced_calls += 1
            if not self.usage.price_table_unknown_reason:
                self.usage.price_table_unknown_reason = UNKNOWN_MODEL_PRICING
            if usage.cost_usd is None:
                self.usage.budget_basis_counts["unknown"] = (
                    self.usage.budget_basis_counts.get("unknown", 0) + 1
                )

        self.calls.append(
            {
                "stage": stage,
                "kind": "model_call",
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "token_accounting": accounting_basis,
                "cost_usd": str(usage.cost_usd) if usage.cost_usd is not None else None,
                "price_table_cost_usd": (
                    str(usage.price_table_cost_usd)
                    if usage.price_table_cost_usd is not None
                    else None
                ),
                "cost_unknown_reason": usage.cost_unknown_reason or None,
                "elapsed_ms": elapsed_ms,
            }
        )
        if (
            self.budget.max_output_tokens
            and self.usage.output_tokens > self.budget.max_output_tokens
        ):
            raise BudgetExhausted(
                f"output token budget of {self.budget.max_output_tokens} was exceeded at "
                f"stage {stage!r}.",
                budget="output_tokens",
                stage=stage,
            )

    def _reconcile_failed_call(self, stage: str, exc: StageFailure) -> None:
        reported = getattr(self.client, "last_usage", None)
        if not isinstance(reported, ModelUsage) or reported is self._accounted_client_usage:
            self.calls.append(
                {
                    "stage": stage,
                    "kind": "failed_attempt_usage",
                    "error": exc.detail,
                    "usage_reported": False,
                    "note": (
                        "The failed attempt reported no usage; any charge it incurred is "
                        "unknown and is not included in this run's cost."
                    ),
                }
            )
            return
        self._accounted_client_usage = reported
        self._account(stage, reported, elapsed_ms=0)
        self.calls.append(
            {
                "stage": stage,
                "kind": "failed_attempt_usage",
                "error": exc.detail,
                "usage_reported": True,
                "note": "Usage reported for a failed attempt is counted.",
            }
        )

    def _reconcile_timed_out_call(self, stage: str) -> None:
        self.usage.timed_out_calls += 1
        reconciled: ModelUsage | None = None
        reconciler = getattr(self.client, "reconcile_timed_out_call", None)
        if callable(reconciler):
            try:
                reconciled = reconciler(stage=stage)
            except Exception as reconcile_error:  # pragma: no cover - defensive
                logger.warning("Could not reconcile timed-out stage %s: %s", stage, reconcile_error)
                reconciled = None
        if isinstance(reconciled, ModelUsage):
            self.usage.reconciled_timeout_calls += 1
            self._account(stage, reconciled, elapsed_ms=0)
            self._accounted_client_usage = getattr(self.client, "last_usage", None)
            self.calls.append(
                {
                    "stage": stage,
                    "kind": "timeout_reconciled",
                    "detail": (
                        "The abandoned call was attributed from provider counters after the "
                        "stage timeout. Its billed cost is still whatever the provider reports."
                    ),
                }
            )
            return
        self.usage.unreconciled_timeout_calls += 1
        if not self.usage.billed_cost_unknown_reason:
            self.usage.billed_cost_unknown_reason = UNKNOWN_UNRECONCILED_TIMEOUT
        self.calls.append(
            {
                "stage": stage,
                "kind": "timeout_unreconciled",
                "note": (
                    "The provider may still bill this abandoned call; it is excluded from "
                    "every cost figure on this run."
                ),
            }
        )

    # -- the model seam ---------------------------------------------------
    def complete(self, stage: str, prompt: str) -> str:
        """One model call with budgets, timeout, retries and usage accounting."""
        retries = 0
        delay = self.budget.provider_retry_backoff_seconds
        while True:
            self._check_budgets(stage, prompt)
            timeout, basis = self._effective_timeout(stage)
            started = time.monotonic()
            try:
                text = _call_with_timeout(
                    lambda: self.client.complete(stage, prompt),
                    timeout,
                    stage=stage,
                    timeout_basis=basis,
                )
            except StageTimeout as exc:
                self._reconcile_timed_out_call(stage)
                if basis == "remaining_deadline":
                    raise DeadlineExceeded(
                        f"the remaining run deadline bounded stage {stage!r} to {timeout:g}s, "
                        "which elapsed. The stage was abandoned and the allocation stopped.",
                        budget="run_deadline",
                        stage=stage,
                    ) from exc
                raise
            except ProviderTransientError as exc:
                self._reconcile_failed_call(stage, exc)
                retries += 1
                self.usage.retries += 1
                if retries > self.budget.provider_retries:
                    raise ProviderError(
                        f"stage {stage!r} still failing after {retries} attempt(s): {exc.detail}",
                        stage=stage,
                    ) from exc
                if delay:
                    remaining = self._remaining_deadline()
                    if remaining is not None and remaining <= delay:
                        raise DeadlineExceeded(
                            f"stage {stage!r} needs a retry after {delay:g}s of backoff, but "
                            f"only {remaining:.2f}s remain.",
                            budget="run_deadline",
                            stage=stage,
                        ) from exc
                    time.sleep(delay)
                delay *= 2
                continue
            except StageFailure:
                raise
            except Exception as exc:
                raise ProviderError(
                    f"stage {stage!r} raised {type(exc).__name__}: {exc}",
                    stage=stage,
                ) from exc

            usage = self._read_usage(prompt, text)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._account(stage, usage, elapsed_ms=elapsed_ms)
            self.last_usage = usage
            self._accounted_client_usage = getattr(self.client, "last_usage", None)
            return text

    # -- reporting --------------------------------------------------------
    def report(self) -> dict:
        """Budget and usage provenance recorded on the plan's model metadata."""
        return {
            "adapter": "budgeted_provider_client",
            "provider": self.provider,
            "model": self.model,
            "parameters": self.parameters,
            "budget": self.budget.as_dict(),
            "usage": {
                **self.usage.as_dict(),
                "monetary_ceiling": {
                    "limit_usd": (
                        str(self.budget.monetary_budget_usd)
                        if self.budget.monetary_budget_usd is not None
                        else None
                    ),
                    "enforced_on": self.usage.budget_basis,
                    "strict_billed_ceiling": False,
                    "late_charges_possible": True,
                },
            },
            "calls": list(self.calls),
            "notes": list(self.notes),
        }


def build_allocation_client(
    *,
    mode: str,
    stub_response: Any = None,
    budget: ProtocolBudget | None = None,
) -> AllocationClient:
    """Return the client for an explicit mode.

    ``stub`` is never a fallback: it must be named by the caller, and the
    resulting plan says ``provider: "stub"``. ``live`` requires a configured
    provider credential; without one this raises
    :class:`AllocationProviderUnavailable` rather than silently substituting a
    deterministic answer.
    """
    normalized = str(mode or "").strip().lower()
    if normalized == "stub":
        if stub_response is None:
            raise AllocationProviderUnavailable(
                "stub mode was selected but no stub response was supplied."
            )
        return StaticAllocationClient(stub_response)
    if normalized != "live":
        raise AllocationProviderUnavailable(f"Unsupported allocation mode {mode!r}.")

    try:
        from ai_agent import AlphaAgentOrchestrator, CrewAIStageClient
    except Exception as exc:  # pragma: no cover - optional provider dependency
        raise AllocationProviderUnavailable(
            f"The provider-backed CIO could not be imported: {type(exc).__name__}: {exc}"
        ) from exc

    orchestrator = AlphaAgentOrchestrator()
    if not orchestrator.is_configured:
        raise AllocationProviderUnavailable(
            "No LLM credential is configured (AI_LLM_API_KEY / AI_LLM_PROVIDER); "
            "the live allocation adapter will not run without one."
        )
    stage_client = CrewAIStageClient(orchestrator)
    return BudgetedAllocationClient(stage_client, budget=budget or allocation_budget())


def usage_decimal(value: Any) -> Decimal | None:
    """Small helper for callers that need a Decimal money field from metadata."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
