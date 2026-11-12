"""Authenticated, idempotent portfolio analysis submission (FIX-08).

The proposal under review was:

    ``POST /api/decision-runs/`` — authenticated; body carries ``portfolio_id``,
    ``tickers``, ``mode``, ``protocol_version`` and an idempotency key. Returns
    **202** with ``run_id`` and a status URL. Validate owner, protocol, payload
    and request hash; reusing a key with a different payload returns **409**.

This module implements the service half. It deliberately writes the submission
row **before** calling the model, and keys the idempotency check on a canonical
hash of the *request body*, not the model's answer. A retry with the same key and
body therefore returns the first submission even when the model would answer
differently; a retry with a different body is a conflict even though the model
would produce a different plan anyway.

Two things are never inferred:

* **Prices.** Current quotes come from ``services.market_data``. A held name
  without a quote falls back to its recorded average cost **and says so**; a
  candidate without a quote is left unpriced and the deterministic layer sends
  its target to cash with a deviation. Nothing is marked at a fabricated price.
* **Classifications and correlations.** Classifications come only from the
  explicit ``InstrumentClassification`` lookup. Price history is not fetched by
  this path, so ``risk_statistics`` is ``not_available`` with a declared policy
  rather than an invented correlation. (The brief can already compute
  correlations when a caller supplies history; see ``ALLOCATION.md``.)

Submission never places a trade. It creates a ``PortfolioPlan`` in ``proposed``
state; execution requires a separate approval and an explicit apply.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from core.allocation_models import (
    EXECUTION_MODE_LIVE,
    AllocationSubmission,
    AllocationSubmissionStatus,
    PortfolioPlan,
)
from core.decision_json import content_hash, json_safe, utc_iso
from core.log_safety import log_safe
from core.models import Asset, Portfolio
from services.allocation_provider import allocation_budget, build_allocation_client
from services.budget import (
    check_and_reserve,
    logical_call_key,
    mark_unknown,
    reconcile_report,
    reserve_bound_for_run,
)
from services.execution import Mark
from services.market_data import get_latest_quote
from services.portfolio_allocation import (
    PROTOCOL_VERSION,
    PortfolioCIO,
    StaticAllocationClient,
    build_hold_proposal,
    create_plan,
)
from services.portfolio_brief import (
    CandidateCase,
    build_allocation_brief,
    capture_allocation_snapshot,
    load_candidate_cases,
    load_classifications,
)
from services.portfolio_policy import AllocationPolicy, CostModel
from services.portfolio_targets import AllocationProposal, TargetAction

logger = logging.getLogger("alphaagent.allocation_submission")

__all__ = [
    "ANALYSIS_PROTOCOL_VERSION",
    "TERMINAL_SUBMISSION_STATUSES",
    "AllocationSubmissionConflict",
    "AllocationSubmissionError",
    "QuoteCollection",
    "SubmissionRequest",
    "analysis_is_terminal",
    "cancel_submission",
    "claim_submission",
    "collect_quotes",
    "dispatch_submission",
    "process_submission",
    "recover_stuck_submissions",
    "submission_as_dict",
    "submission_plan_or_none",
    "submit_analysis",
]

#: The only protocol this endpoint accepts. A request may not select an
#: arbitrary protocol string: the value ends up on the plan and on every review
#: surface, and a typo would read like a different producer.
ANALYSIS_PROTOCOL_VERSION = PROTOCOL_VERSION
SUPPORTED_MODES: frozenset[str] = frozenset({"live", "stub"})
DEFAULT_MAX_TICKERS = 25
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")
KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
ZERO = Decimal("0")

#: The statuses after which no further work may happen. Every transition guard
#: in this module and every repair decision is expressed against this set.
TERMINAL_SUBMISSION_STATUSES = frozenset(
    {
        AllocationSubmissionStatus.SUCCEEDED,
        AllocationSubmissionStatus.FAILED,
        AllocationSubmissionStatus.CANCELLED,
        AllocationSubmissionStatus.TIMED_OUT,
    }
)

DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 300
DEFAULT_ANALYSIS_LEASE_SECONDS = 120
DEFAULT_ANALYSIS_MAX_ATTEMPTS = 3
DEFAULT_ANALYSIS_STALE_SECONDS = 600


class AllocationSubmissionError(ValueError):
    """A submission body is invalid. ``code`` is a stable client-facing string."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class AllocationSubmissionConflict(Exception):
    """An idempotency key was reused with a materially different request body."""


@dataclass(frozen=True)
class SubmissionRequest:
    """A validated, canonical submission body."""

    portfolio_id: int
    tickers: tuple[str, ...]
    mode: str
    protocol_version: str
    idempotency_key: str
    horizon_days: int
    stub_response: dict | None
    request_hash: str
    canonical: dict

    @classmethod
    def build(cls, portfolio: Portfolio, data: Mapping[str, Any]) -> SubmissionRequest:
        raw = dict(data or {})
        raw_portfolio = raw.get("portfolio_id")
        try:
            portfolio_id = int(raw_portfolio)
        except (TypeError, ValueError):
            raise AllocationSubmissionError(
                "invalid_portfolio_id", "portfolio_id must be an integer."
            ) from None
        if portfolio_id != portfolio.id:
            raise AllocationSubmissionError(
                "portfolio_mismatch",
                f"portfolio_id {portfolio_id} does not match the resolved portfolio.",
            )

        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in SUPPORTED_MODES:
            raise AllocationSubmissionError(
                "invalid_mode",
                f"mode must be one of {sorted(SUPPORTED_MODES)}; got {mode!r}. A stub run is "
                "explicit and is recorded as such on the plan.",
            )

        protocol_version = str(raw.get("protocol_version") or "").strip()
        if protocol_version != ANALYSIS_PROTOCOL_VERSION:
            raise AllocationSubmissionError(
                "unsupported_protocol",
                f"protocol_version must be {ANALYSIS_PROTOCOL_VERSION!r}; got "
                f"{protocol_version!r}.",
            )

        key_text = raw.get("idempotency_key")
        if key_text in (None, "") and raw.get("idempotency-key") is not None:
            key_text = raw.get("idempotency-key")
        key = str(key_text or "").strip()
        if not KEY_RE.match(key):
            raise AllocationSubmissionError(
                "invalid_idempotency_key",
                "idempotency_key is required: 1-128 characters from A-Z a-z 0-9 . _ : -.",
            )

        raw_tickers = raw.get("tickers")
        if not isinstance(raw_tickers, Sequence) or isinstance(raw_tickers, (str, bytes)):
            raise AllocationSubmissionError("invalid_tickers", "tickers must be a list.")
        limit = int(
            getattr(settings, "ALLOCATION_CONFIG", {}).get(
                "MAX_TICKERS_PER_ANALYSIS", DEFAULT_MAX_TICKERS
            )
        )
        tickers: list[str] = []
        seen: set[str] = set()
        for item in raw_tickers:
            ticker = str(item or "").strip().upper()
            if not TICKER_RE.match(ticker):
                raise AllocationSubmissionError(
                    "invalid_ticker",
                    f"{item!r} is not a usable ticker symbol.",
                )
            if ticker not in seen:
                seen.add(ticker)
                tickers.append(ticker)
        if not tickers:
            raise AllocationSubmissionError(
                "empty_tickers", "At least one candidate ticker is required."
            )
        if len(tickers) > limit:
            raise AllocationSubmissionError(
                "too_many_tickers",
                f"At most {limit} tickers are accepted per analysis; got {len(tickers)}.",
            )

        horizon_raw = raw.get("horizon_days")
        policy_horizon = int(
            getattr(settings, "ALLOCATION_CONFIG", {}).get("INVESTMENT_HORIZON_DAYS", 30)
        )
        if horizon_raw in (None, ""):
            horizon_days = policy_horizon
        else:
            try:
                horizon_days = int(horizon_raw)
            except (TypeError, ValueError):
                raise AllocationSubmissionError(
                    "invalid_horizon", "horizon_days must be an integer."
                ) from None
            if not 1 <= horizon_days <= 365:
                raise AllocationSubmissionError(
                    "invalid_horizon", "horizon_days must be between 1 and 365."
                )

        stub_response = raw.get("stub_response")
        if stub_response is not None and not isinstance(stub_response, Mapping):
            raise AllocationSubmissionError(
                "invalid_stub_response", "stub_response must be a JSON object."
            )
        if mode == "live" and stub_response is not None:
            raise AllocationSubmissionError(
                "stub_response_in_live_mode",
                "stub_response is only accepted in explicit stub mode; a live request must "
                "come from the provider.",
            )
        if mode == "stub" and stub_response is not None:
            # A stub proposal is still validated by the same target contract,
            # and its portfolio_version must match the brief. It is supplied so
            # tests and demonstrations can exercise real trade-bearing plans
            # without a network call.
            stub_response = dict(stub_response)

        canonical = {
            "schema_version": "portfolio_analysis.request.v1",
            "portfolio_id": portfolio.id,
            "tickers": sorted(tickers),
            "mode": mode,
            "protocol_version": protocol_version,
            "horizon_days": horizon_days,
            "stub_response": json_safe(stub_response) if stub_response is not None else None,
        }
        return cls(
            portfolio_id=portfolio.id,
            tickers=tuple(tickers),
            mode=mode,
            protocol_version=protocol_version,
            idempotency_key=key,
            horizon_days=horizon_days,
            stub_response=stub_response,
            request_hash=content_hash(canonical),
            canonical=canonical,
        )


# ---------------------------------------------------------------------------
# Explicit data setup
# ---------------------------------------------------------------------------
@dataclass
class QuoteCollection:
    """Prices gathered for one submission, with provenance for each source."""

    quotes: dict[str, Decimal] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    #: The full observation for each price, including the instant it was made.
    #: A price in ``quotes`` without an entry here has unknown provenance.
    observations: dict[str, Mark] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    fallbacks: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def add(
        self,
        ticker: str,
        price: Decimal,
        source: str,
        *,
        as_of=None,
        retrieved_at=None,
        currency: str = "USD",
    ) -> None:
        if price is None or price <= ZERO:
            return
        symbol = str(ticker).upper()
        self.quotes[symbol] = price
        self.sources[symbol] = source
        self.observations[symbol] = Mark(
            price=price,
            as_of=as_of,
            source=source,
            ticker=symbol,
            currency=currency or "USD",
            retrieved_at=retrieved_at,
        )

    def as_dict(self) -> dict:
        return {
            "quote_source": "market_data_with_declared_cost_fallback",
            "quotes": {
                ticker: {
                    "price": str(price),
                    "source": self.sources.get(ticker, "unknown"),
                    "as_of": (
                        self.observations[ticker].as_of.isoformat()
                        if ticker in self.observations
                        and self.observations[ticker].as_of is not None
                        else None
                    ),
                    "retrieved_at": (
                        self.observations[ticker].retrieved_at.isoformat()
                        if ticker in self.observations
                        and self.observations[ticker].retrieved_at is not None
                        else None
                    ),
                    "currency": (
                        self.observations[ticker].currency if ticker in self.observations else "USD"
                    ),
                }
                for ticker, price in sorted(self.quotes.items())
            },
            "missing_tickers": sorted(self.missing),
            "cost_fallbacks": list(self.fallbacks),
            "quote_errors": list(self.errors),
            "note": (
                "A missing quote is never replaced by a fabricated market price. A held "
                "position may fall back to its recorded average cost, which the brief "
                "labels as a non-market mark."
            ),
        }


def collect_quotes(
    portfolio: Portfolio,
    tickers: Sequence[str],
    *,
    provided: Mapping[str, Any] | None = None,
    allow_cost_fallback: bool = True,
) -> QuoteCollection:
    """Current quotes for every held and candidate name, with explicit fallbacks.

    ``provided`` is for the recording command and tests: an operator-supplied
    price is labelled ``operator_supplied`` and is never presented as a market
    quote. The HTTP path does not accept client-supplied prices.
    """
    collection = QuoteCollection()
    provided_prices = {str(ticker).upper(): price for ticker, price in (provided or {}).items()}
    assets = {
        asset.ticker.upper(): asset
        for asset in Asset.objects.filter(portfolio=portfolio, amount__gt=ZERO).order_by("ticker")
    }
    wanted = sorted(set(assets) | {str(ticker).upper() for ticker in tickers})
    for ticker in wanted:
        if ticker in provided_prices:
            try:
                price = Decimal(str(provided_prices[ticker]))
            except Exception:
                collection.errors.append(
                    {"ticker": ticker, "error": "provided price is not a decimal"}
                )
                collection.missing.append(ticker)
                continue
            collection.add(ticker, price, "operator_supplied")
            continue
        try:
            quote = get_latest_quote(ticker)
        except Exception as exc:
            logger.warning("Quote lookup failed for %s: %s", ticker, exc)
            quote = None
            collection.errors.append({"ticker": ticker, "error": type(exc).__name__})
        if quote is not None:
            try:
                price = Decimal(str(quote.price))
            except Exception:
                price = None
            if price is not None and price > ZERO:
                collection.add(
                    ticker,
                    price,
                    str(getattr(quote, "source", "") or "market_data"),
                    as_of=getattr(quote, "as_of", None),
                    retrieved_at=getattr(quote, "retrieved_at", None),
                    currency=str(getattr(quote, "currency", "") or "USD"),
                )
                continue
        asset = assets.get(ticker)
        if (
            allow_cost_fallback
            and asset is not None
            and asset.avg_purchase_price
            and asset.avg_purchase_price > ZERO
        ):
            collection.add(
                ticker,
                asset.avg_purchase_price,
                "average_cost_fallback",
                as_of=None,
                retrieved_at=timezone.now(),
            )
            collection.fallbacks.append(
                {
                    "ticker": ticker,
                    "source": "average_cost",
                    "detail": (
                        "No market quote was available; the recorded average cost is used "
                        "only as an explicitly labelled valuation crutch."
                    ),
                }
            )
            continue
        collection.missing.append(ticker)
    return collection


def classification_provenance(portfolio: Portfolio, tickers: Sequence[str]) -> dict:
    """Report which names have an explicit classification; never infer a sector."""
    wanted = sorted({str(ticker).upper() for ticker in tickers})
    rows = load_classifications(portfolio, wanted)
    classified = {ticker: rows[ticker] for ticker in wanted if ticker in rows}
    missing = [ticker for ticker in wanted if ticker not in rows]
    return {
        "source": "explicit_portfolio_classification_lookup",
        "status": "complete" if not missing else "incomplete",
        "classified": {
            ticker: {
                "sector": info.get("sector"),
                "asset_class": info.get("asset_class"),
                "source": info.get("source"),
            }
            for ticker, info in classified.items()
        },
        "unclassified": missing,
        "note": (
            "A missing classification is unknown, not a default sector. No sector is "
            "inferred from a ticker string."
        ),
    }


def _candidate_cases(
    portfolio: Portfolio,
    tickers: Sequence[str],
    *,
    limit: int,
    max_claims: int,
    as_of,
) -> tuple[list[CandidateCase], dict]:
    """Stored cases plus an explicit declaration for every requested ticker.

    A requested ticker with no completed decision run is **not** silently
    dropped: it becomes a candidate case with no claims and a rationale saying
    so. That keeps it visible to the CIO and in ``permitted_tickers`` while
    making ``insufficient_evidence`` true. No claim, evidence or rationale is
    invented for it.
    """
    stored = load_candidate_cases(
        portfolio, tickers=tickers, as_of=as_of, limit=limit, max_claims=max_claims
    )
    by_ticker = {case.ticker.upper() for case in stored}
    held = {
        asset.ticker.upper() for asset in Asset.objects.filter(portfolio=portfolio, amount__gt=ZERO)
    }
    requested = {str(ticker).upper() for ticker in tickers}
    missing = sorted(requested - by_ticker - held)
    extras = [
        CandidateCase(
            ticker=ticker,
            action="HOLD",
            rationale=(
                "No completed decision run exists for this requested ticker. The brief "
                "carries no claim and no evidence for it; that absence is the record."
            ),
            strongest_objection="No validated evidence-backed case was recorded.",
            run_id="",
            mode="no_recorded_case",
            decided_at=None,
            claims=[],
        )
        for ticker in missing
    ]
    info = {
        "source": "stored_decision_runs_plus_declared_absence",
        "requested": sorted(requested),
        "held": sorted(held),
        "stored_cases": sorted(by_ticker),
        "declared_without_case": missing,
        "note": (
            "A requested ticker with no completed run is declared as having no case; "
            "no claim or evidence is fabricated for it."
        ),
    }
    return [*stored, *extras], info


def _data_quality(
    *,
    quotes: QuoteCollection,
    classification_info: dict,
    candidate_info: dict | None = None,
    brief=None,
) -> dict:
    limitations: list[str] = []
    if quotes.missing:
        limitations.append(
            "No price was available for: " + ", ".join(quotes.missing) + ". A target in a "
            "name without a price is sent to cash by the deterministic layer; it is never "
            "priced at an invented market value."
        )
    if quotes.fallbacks:
        limitations.append(
            "Average cost was used as a valuation crutch for: "
            + ", ".join(row["ticker"] for row in quotes.fallbacks)
            + ". It is not a market price."
        )
    if classification_info.get("unclassified"):
        limitations.append(
            "Sector classification is missing for: "
            + ", ".join(classification_info["unclassified"])
            + ". Sector exposure is incomplete, not zero."
        )
    limitations.append(
        "Price history is not fetched by the submission endpoint, so correlation and "
        "covariance are not computed. The declared policy is label_only; the model may "
        "not invent them."
    )
    brief_quality: dict = {}
    if brief is not None:
        brief_quality = dict(brief.data_quality or {})
        for flag in brief_quality.get("flags", []):
            text = str(flag.get("detail") or "").strip()
            if text and text not in limitations:
                limitations.append(text)
        risk = dict(getattr(brief, "risk_statistics", {}) or {})
        if risk.get("status") not in {"computed", "insufficient_instruments"}:
            limitations.append(
                f"Correlations: {risk.get('status', 'not_available')}. No correlation or "
                "covariance is asserted."
            )
    return {
        "input": {
            "prices": quotes.as_dict(),
            "classifications": classification_info,
            "candidates": candidate_info or {},
            "price_history": {
                "status": "not_fetched",
                "source": "none",
                "declared_policy": "label_only",
                "note": (
                    "The submission endpoint does not fetch history. A caller that needs "
                    "correlations supplies history to the brief builder explicitly."
                ),
            },
        },
        "brief": brief_quality,
        "declared_policies": list(brief_quality.get("policy_actions") or []),
        "limitations": limitations,
    }


# ---------------------------------------------------------------------------
# Submission lifecycle
# ---------------------------------------------------------------------------
def _existing_submission(portfolio: Portfolio, key: str) -> AllocationSubmission | None:
    return AllocationSubmission.objects.filter(portfolio=portfolio, idempotency_key=key).first()


# ---------------------------------------------------------------------------
# Durable lifecycle: claim, provider checkpoint, finalise
# ---------------------------------------------------------------------------
def _config_int(key: str, default: int) -> int:
    value = getattr(settings, "ALLOCATION_CONFIG", {}).get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _analysis_timeout_seconds() -> int:
    return max(1, _config_int("ANALYSIS_TIMEOUT_SECONDS", DEFAULT_ANALYSIS_TIMEOUT_SECONDS))


def _analysis_lease_seconds() -> int:
    """How long a claim is trusted before another worker may take it.

    The lease must outlast a legitimate provider call, otherwise a slow-but-alive
    worker would look dead and a second worker would mark the call
    unreconciled. The provider's own run deadline is the bound we know, so the
    lease is at least that plus a margin for persisting the result.
    """
    configured = _config_int("ANALYSIS_LEASE_SECONDS", DEFAULT_ANALYSIS_LEASE_SECONDS)
    provider_deadline = _config_int("SUBMISSION_RUN_DEADLINE_SECONDS", 180)
    return max(1, configured, provider_deadline + 60)


def _analysis_max_attempts() -> int:
    return max(1, _config_int("ANALYSIS_MAX_ATTEMPTS", DEFAULT_ANALYSIS_MAX_ATTEMPTS))


def _analysis_stale_seconds() -> int:
    return max(1, _config_int("ANALYSIS_STALE_SECONDS", DEFAULT_ANALYSIS_STALE_SECONDS))


def _owned_submission(submission_id) -> AllocationSubmission:
    # No select_related here: ``plan`` is nullable and PostgreSQL refuses
    # FOR UPDATE on the nullable side of an outer join. The caller reads the
    # relationships after the lock, which is one extra query on a rare path.
    return AllocationSubmission.objects.select_for_update().get(pk=submission_id)


def _finish_locked(
    submission: AllocationSubmission,
    *,
    status: str,
    code: str,
    detail: str,
    now,
) -> AllocationSubmission:
    """Write a terminal state on a row already locked by the caller."""
    submission.status = status
    submission.completed_at = now
    submission.error_code = (code or "")[:64]
    submission.error_detail = (detail or "")[:4000]
    submission.claimed_by = ""
    submission.heartbeat_at = now
    submission.lease_expires_at = None
    submission.save(
        update_fields=[
            "status",
            "completed_at",
            "error_code",
            "error_detail",
            "claimed_by",
            "heartbeat_at",
            "lease_expires_at",
            "updated_at",
        ]
    )
    return submission


def _finish_submission(
    submission_id,
    *,
    status: str,
    code: str,
    detail: str,
    now=None,
    owner: str | None = None,
    data_quality: dict | None = None,
    worker_metadata: dict | None = None,
) -> AllocationSubmission:
    """Terminal transition guarded by terminality and, when given, ownership.

    ``owner=None`` is the repair/cancel path: it may stop any row that is still
    open. A worker passes its owner so a stale attempt cannot overwrite a
    terminal state written after its lease expired.
    """
    moment = now or timezone.now()
    with transaction.atomic():
        submission = _owned_submission(submission_id)
        if submission.is_terminal:
            return submission
        if owner is not None and submission.claimed_by != owner:
            return submission
        if data_quality is not None:
            submission.data_quality = json_safe(data_quality)
        if worker_metadata is not None:
            submission.worker_metadata = json_safe(worker_metadata)
        fields = []
        if data_quality is not None:
            fields.append("data_quality")
        if worker_metadata is not None:
            fields.append("worker_metadata")
        finished = _finish_locked(
            submission,
            status=status,
            code=code,
            detail=detail,
            now=moment,
        )
        if fields:
            finished.save(update_fields=[*fields, "updated_at"])
        return finished


def claim_submission(
    submission_id,
    *,
    owner: str,
    now=None,
) -> tuple[AllocationSubmission, bool]:
    """Atomically take ownership of one submission.

    Returns ``(submission, claimed)``. A live lease is not stolen; an expired
    lease is, which is how a new worker resumes after a process death. The
    attempt counter increases only on a successful claim. The deadline is
    checked before any work, so a queued row with no worker still reaches
    ``timed_out`` with a reason instead of waiting forever.
    """
    moment = now or timezone.now()
    with transaction.atomic():
        submission = _owned_submission(submission_id)
        if submission.is_terminal:
            return submission, False
        if (
            submission.status == AllocationSubmissionStatus.RUNNING
            and submission.lease_expires_at is not None
            and submission.lease_expires_at > moment
        ):
            return submission, False
        if submission.deadline_at is not None and moment >= submission.deadline_at:
            return (
                _finish_locked(
                    submission,
                    status=AllocationSubmissionStatus.TIMED_OUT,
                    code="analysis_timeout",
                    detail=(
                        "The submission reached its analysis deadline before a worker "
                        "finished it. No plan was created and no trade was applied."
                    ),
                    now=moment,
                ),
                False,
            )
        if submission.attempt_count >= submission.max_attempts:
            return (
                _finish_locked(
                    submission,
                    status=AllocationSubmissionStatus.FAILED,
                    code="attempts_exhausted",
                    detail=(
                        "The submission exhausted its bounded attempt count without a "
                        "validated plan. A new idempotency key is required to try again."
                    ),
                    now=moment,
                ),
                False,
            )
        if submission.deadline_at is None:
            # Rows written before the deadline column existed still get a
            # bounded life the first time a worker touches them.
            submission.deadline_at = moment + timedelta(seconds=_analysis_timeout_seconds())
        submission.status = AllocationSubmissionStatus.RUNNING
        submission.attempt_count += 1
        submission.claimed_by = owner[:128]
        submission.claimed_at = moment
        submission.heartbeat_at = moment
        submission.lease_expires_at = moment + timedelta(seconds=_analysis_lease_seconds())
        if submission.started_at is None:
            submission.started_at = moment
        submission.worker_metadata = json_safe(
            {
                **(submission.worker_metadata or {}),
                "last_checkpoint": "claimed",
                "claimed_by": owner[:128],
                "attempt": submission.attempt_count,
            }
        )
        submission.save(
            update_fields=[
                "status",
                "attempt_count",
                "claimed_by",
                "claimed_at",
                "heartbeat_at",
                "lease_expires_at",
                "started_at",
                "deadline_at",
                "worker_metadata",
                "updated_at",
            ]
        )
        return submission, True


def _begin_provider_call(submission_id, *, owner: str, now) -> AllocationSubmission | None:
    """Checkpoint the paid call, or return ``None`` when work must not start."""
    with transaction.atomic():
        submission = _owned_submission(submission_id)
        if submission.is_terminal or submission.claimed_by != owner:
            return None
        if submission.deadline_at is not None and now >= submission.deadline_at:
            _finish_locked(
                submission,
                status=AllocationSubmissionStatus.TIMED_OUT,
                code="analysis_timeout",
                detail=(
                    "The provider call was not started because the analysis deadline "
                    "had passed. No provider spend and no plan."
                ),
                now=now,
            )
            return None
        submission.provider_call_started_at = now
        submission.heartbeat_at = now
        submission.lease_expires_at = now + timedelta(seconds=_analysis_lease_seconds())
        submission.worker_metadata = json_safe(
            {
                **(submission.worker_metadata or {}),
                "last_checkpoint": "provider_call_started",
            }
        )
        submission.save(
            update_fields=[
                "provider_call_started_at",
                "heartbeat_at",
                "lease_expires_at",
                "worker_metadata",
                "updated_at",
            ]
        )
        return submission


def _checkpoint_provider_result(
    submission_id,
    *,
    owner: str,
    proposal: AllocationProposal,
    metadata: dict,
    now,
) -> AllocationSubmission | None:
    """Persist the validated provider result before any plan side effect."""
    with transaction.atomic():
        submission = _owned_submission(submission_id)
        if submission.is_terminal or submission.claimed_by != owner:
            return None
        submission.provider_result = json_safe(proposal.as_dict())
        submission.model_metadata = json_safe(metadata)
        submission.provider = str(metadata.get("provider") or "")[:64]
        submission.model = str(metadata.get("model") or "")[:128]
        submission.synthetic_response = bool(metadata.get("synthetic_response"))
        submission.provider_completed_at = now
        submission.heartbeat_at = now
        submission.lease_expires_at = now + timedelta(seconds=_analysis_lease_seconds())
        submission.worker_metadata = json_safe(
            {
                **(submission.worker_metadata or {}),
                "last_checkpoint": "provider_result_persisted",
            }
        )
        submission.save(
            update_fields=[
                "provider_result",
                "model_metadata",
                "provider",
                "model",
                "synthetic_response",
                "provider_completed_at",
                "heartbeat_at",
                "lease_expires_at",
                "worker_metadata",
                "updated_at",
            ]
        )
        return submission


def _proposal_from_result(payload: Mapping[str, Any], model_metadata: dict) -> AllocationProposal:
    """Rebuild a validated proposal from the persisted checkpoint.

    The stored document is exactly `AllocationProposal.as_dict()`, so rebuilding
    it reproduces the plan request hash. It is not re-validated against a fresh
    brief: it was validated before it was persisted, and re-validating against
    changed prices would silently substitute a different answer.
    """
    raw_actions = payload.get("actions") or []
    actions = [
        TargetAction(
            ticker=str(action.get("ticker") or ""),
            action=str(action.get("action") or ""),
            rationale=str(action.get("rationale") or ""),
            claim_ids=[str(value) for value in (action.get("claim_ids") or [])],
            target_weight=Decimal(str(action.get("target_weight") or "0")),
        )
        for action in raw_actions
        if isinstance(action, Mapping)
    ]
    weights = {
        str(ticker): Decimal(str(value))
        for ticker, value in dict(payload.get("target_weights") or {}).items()
    }
    return AllocationProposal(
        schema_version=str(payload.get("schema_version") or ""),
        portfolio_version=str(payload.get("portfolio_version") or ""),
        target_weights=weights,
        actions=actions,
        funding_source=str(payload.get("funding_source") or ""),
        rationale=str(payload.get("rationale") or ""),
        rejected_alternatives=list(payload.get("rejected_alternatives") or []),
        unresolved_uncertainties=[
            str(value) for value in (payload.get("unresolved_uncertainties") or [])
        ],
        objective=str(payload.get("objective") or ""),
        data_sufficiency=str(payload.get("data_sufficiency") or ""),
        model_metadata=dict(model_metadata or {}),
        raw=dict(payload),
    )


def _build_analysis_context(submission: AllocationSubmission) -> dict[str, Any]:
    """Gather the deterministic inputs for one attempt.

    This runs on every attempt, including a resume after the provider result was
    persisted. It is not a paid side effect; the plan records the requested
    proposal's `portfolio_version` separately and flags `revalidation_required`
    when the freshly evaluated portfolio version differs.
    """
    portfolio = submission.portfolio
    request = dict(submission.request or {})
    tickers = [str(ticker).upper() for ticker in (submission.tickers or [])]
    horizon_days = int(request.get("horizon_days") or 0) or None
    held = set(
        Asset.objects.filter(portfolio=portfolio, amount__gt=ZERO).values_list("ticker", flat=True)
    )
    classification_info = classification_provenance(
        portfolio, sorted(set(tickers) | {str(ticker).upper() for ticker in held})
    )
    quotes = collect_quotes(portfolio, tickers)
    if not quotes.quotes:
        raise AllocationSubmissionError(
            "no_prices",
            "No usable price was available for any held or candidate name. The "
            "submission was not run against an invented valuation.",
        )
    policy = AllocationPolicy.from_portfolio(portfolio)
    cost_model = CostModel.from_config()
    now = timezone.now()
    snapshot = capture_allocation_snapshot(portfolio, quotes=quotes.quotes, as_of=now)
    candidates, candidate_info = _candidate_cases(
        portfolio,
        tickers,
        as_of=now,
        limit=policy.max_candidate_cases,
        max_claims=policy.max_claims_per_case,
    )
    brief = build_allocation_brief(
        portfolio,
        snapshot,
        quotes=quotes.quotes,
        price_history=None,
        candidates=candidates,
        policy=policy,
        cost_model=cost_model,
        as_of=now,
        horizon_days=horizon_days,
        history_source="not_fetched",
        candidate_source="stored_decision_runs_plus_declared_absence",
    )
    return {
        "quotes": quotes,
        "classification_info": classification_info,
        "candidate_info": candidate_info,
        "brief": brief,
        "policy": policy,
        "cost_model": cost_model,
    }


def _provider_metadata(
    submission: AllocationSubmission, proposal: AllocationProposal, context: Mapping[str, Any]
) -> dict:
    metadata = dict(proposal.model_metadata or {})
    metadata.update(
        {
            "submission_id": str(submission.id),
            "analysis_mode": submission.mode,
            "synthetic_response": bool(
                metadata.get("synthetic_response") or submission.mode == "stub"
            ),
            "input_provenance": {
                "prices": context["quotes"].as_dict(),
                "classifications": context["classification_info"],
                "candidate_source": "stored_decision_runs_plus_declared_absence",
                "price_history": "not_fetched",
            },
        }
    )
    return json_safe(metadata)


def _client_for(submission: AllocationSubmission, brief):
    request = dict(submission.request or {})
    stub_response = request.get("stub_response")
    if submission.mode == "stub" and stub_response is None:
        hold = build_hold_proposal(
            brief,
            "Explicit stub mode: deterministic no-change proposal; no model was called.",
        )
        client = StaticAllocationClient(hold.as_dict())
        max_repairs = 0
    else:
        if submission.mode == "stub" and isinstance(stub_response, Mapping):
            stub_response = dict(stub_response)
            stub_response["portfolio_version"] = brief.portfolio_version
        client = build_allocation_client(
            mode=submission.mode,
            stub_response=stub_response,
            budget=allocation_budget(),
        )
        max_repairs = 1 if submission.mode == "live" else 0
    return client, max_repairs


def _finalize_success(
    submission_id,
    *,
    owner: str,
    proposal: AllocationProposal,
    context: Mapping[str, Any],
    now,
) -> AllocationSubmission:
    """Create and attach the plan and mark success in one locked transaction.

    Cancellation and timeout are re-checked under the row lock. If either won
    the race, no plan row is created and nothing can later apply a trade. The
    plan's own idempotency key makes a resumed attempt return the same plan
    instead of a duplicate.
    """
    quotes: QuoteCollection = context["quotes"]
    classification_info = context["classification_info"]
    candidate_info = context["candidate_info"]
    brief = context["brief"]
    policy = context["policy"]
    cost_model = context["cost_model"]
    with transaction.atomic():
        submission = _owned_submission(submission_id)
        if (
            submission.is_terminal
            or submission.status != AllocationSubmissionStatus.RUNNING
            or submission.claimed_by != owner
        ):
            return submission
        if submission.deadline_at is not None and now >= submission.deadline_at:
            return _finish_locked(
                submission,
                status=AllocationSubmissionStatus.TIMED_OUT,
                code="analysis_timeout",
                detail=(
                    "The analysis deadline passed before the plan could be attached. "
                    "No plan was created and no trade was applied."
                ),
                now=now,
            )
        plan, _created = create_plan(
            submission.portfolio,
            brief=brief,
            proposal=proposal,
            submission_key=f"analysis:{submission.id}",
            policy=policy,
            cost_model=cost_model,
            execution_mode=EXECUTION_MODE_LIVE,
            quote_observations={
                ticker: mark.as_dict()
                if hasattr(mark, "as_dict")
                else {
                    "price": str(mark.price),
                    "source": mark.source,
                    "as_of": mark.as_of.isoformat() if mark.as_of else None,
                    "retrieved_at": mark.retrieved_at.isoformat() if mark.retrieved_at else None,
                    "currency": mark.currency,
                }
                for ticker, mark in quotes.observations.items()
            },
        )
        submission.plan = plan
        submission.provider = str((submission.model_metadata or {}).get("provider") or "")[:64]
        submission.model = str((submission.model_metadata or {}).get("model") or "")[:128]
        submission.synthetic_response = bool(
            (submission.model_metadata or {}).get("synthetic_response")
        )
        submission.data_quality = json_safe(
            _data_quality(
                quotes=quotes,
                classification_info=classification_info,
                candidate_info=candidate_info,
                brief=brief,
            )
        )
        submission.status = AllocationSubmissionStatus.SUCCEEDED
        submission.completed_at = now
        submission.error_code = ""
        submission.error_detail = ""
        submission.claimed_by = ""
        submission.heartbeat_at = now
        submission.lease_expires_at = None
        submission.worker_metadata = json_safe(
            {
                **(submission.worker_metadata or {}),
                "last_checkpoint": "succeeded",
                "plan_id": str(plan.id),
            }
        )
        submission.save(
            update_fields=[
                "plan",
                "provider",
                "model",
                "synthetic_response",
                "data_quality",
                "status",
                "completed_at",
                "error_code",
                "error_detail",
                "claimed_by",
                "heartbeat_at",
                "lease_expires_at",
                "worker_metadata",
                "updated_at",
            ]
        )
        logger.info("Allocation submission %s succeeded with plan %s", submission.id, plan.id)
        return submission


def submit_analysis(
    portfolio: Portfolio,
    *,
    data: Mapping[str, Any],
    user=None,
) -> tuple[AllocationSubmission, bool]:
    """Record one durable, idempotent submission and return immediately.

    The 202 response is written from this row. No model call happens here: the
    caller dispatches a worker task, and the status URL is the authority. The
    row is committed with a deadline before dispatch, so a process death at any
    point leaves a state the repair command can name and act on.

    Returns ``(submission, created)``.
    """
    request = SubmissionRequest.build(portfolio, data)
    created = False
    moment = timezone.now()
    try:
        with transaction.atomic():
            submission, created = AllocationSubmission.objects.get_or_create(
                portfolio=portfolio,
                idempotency_key=request.idempotency_key,
                defaults={
                    "request_hash": request.request_hash,
                    "request": json_safe(request.canonical),
                    "tickers": list(request.tickers),
                    "mode": request.mode,
                    "protocol_version": request.protocol_version,
                    "status": AllocationSubmissionStatus.QUEUED,
                    "queued_at": moment,
                    "deadline_at": moment + timedelta(seconds=_analysis_timeout_seconds()),
                    "max_attempts": _analysis_max_attempts(),
                },
            )
            if not created and submission.request_hash != request.request_hash:
                raise AllocationSubmissionConflict(
                    f"Idempotency key {request.idempotency_key!r} was already used for a "
                    f"different request ({submission.id}). Use a new key for a different "
                    "body."
                )
    except IntegrityError:
        # A concurrent request won the unique constraint. The first row is the
        # authority; re-check its hash rather than creating a second run.
        existing = _existing_submission(portfolio, request.idempotency_key)
        if existing is None:
            raise
        if existing.request_hash != request.request_hash:
            raise AllocationSubmissionConflict(
                f"Idempotency key {request.idempotency_key!r} was already used for a "
                f"different request ({existing.id})."
            ) from None
        return existing, False

    _ = user
    return submission, created


def dispatch_submission(submission: AllocationSubmission) -> bool:
    """Publish the worker task without waiting for the analysis.

    A broker failure is not allowed to turn a durable submission into an HTTP
    error: the row remains ``queued`` with its deadline, and the repair command
    (or a later GET that re-dispatches) can pick it up. No exception text is
    logged; the submission id and the fact are enough to find the cause.
    """
    key = str(submission.pk)
    AllocationSubmission.objects.filter(
        pk=submission.pk, status=AllocationSubmissionStatus.QUEUED
    ).update(last_dispatched_at=timezone.now())
    try:
        # Local import: tasks imports this module for the worker entry point.
        from tasks import run_allocation_submission_task

        run_allocation_submission_task.delay(key)
        return True
    except Exception:
        logger.warning(
            "Allocation submission %s could not be dispatched; it remains queued for repair",
            key,
        )
        return False


def process_submission(
    submission: AllocationSubmission | str,
    *,
    owner: str = "",
    now=None,
) -> AllocationSubmission:
    """Run one claimed attempt: build, persist the provider result, create a plan.

    The order is deliberate and is the recovery contract:

    1. claim the row atomically (attempt count, owner, lease);
    2. build the deterministic brief;
    3. write ``provider_call_started_at`` before the paid call;
    4. persist the validated provider result *before* any plan row;
    5. create and attach the plan under the row lock, re-checking cancellation
       and the deadline.

    A retry that finds a persisted result resumes at step 5 without a second
    provider call. A retry that finds a started call with no result records
    ``provider_call_unreconciled`` and spends nothing; the provider may already
    have billed that attempt, and guessing otherwise would be worse.
    """
    if isinstance(submission, str):
        try:
            submission = AllocationSubmission.objects.get(pk=submission)
        except (AllocationSubmission.DoesNotExist, ValueError):
            raise AllocationSubmissionError(
                "unknown_submission", "No allocation submission exists for that id."
            ) from None
    moment = now or timezone.now()
    owner = (owner or f"pid:{os.getpid()}")[:128]
    submission, claimed = claim_submission(submission.pk, owner=owner, now=moment)
    if not claimed:
        return submission

    context: dict[str, Any] = {}
    try:
        if not submission.provider_result:
            if submission.provider_call_started_at is not None:
                return _finish_submission(
                    submission.pk,
                    status=AllocationSubmissionStatus.FAILED,
                    code="provider_call_unreconciled",
                    detail=(
                        "A previous attempt started a provider call but no result was "
                        "recorded. The retry stopped without a second provider spend, "
                        "because the first call may already have been billed."
                    ),
                    owner=owner,
                    now=timezone.now(),
                )
            context = _build_analysis_context(submission)
            client, max_repairs = _client_for(submission, context["brief"])
            # A live analysis is a paid call with an unknown final price. Reserve
            # the configured upper bound before the provider is called; a refused
            # submission writes no provider_started checkpoint and makes no call.
            ledger_key = None
            if submission.mode == "live":
                ledger_key = logical_call_key("allocation", submission.idempotency_key)
                reserve = check_and_reserve(
                    call_key=ledger_key,
                    upper_bound_usd=reserve_bound_for_run("allocation"),
                    provider=getattr(client, "provider", "") or "",
                    model=getattr(client, "model", "") or "",
                    stage="portfolio_analysis",
                    metadata={"submission_id": str(submission.id)},
                )
                if not reserve.allowed:
                    return _finish_submission(
                        submission.pk,
                        status=AllocationSubmissionStatus.FAILED,
                        code=reserve.reason,
                        detail=(
                            f"The daily AI spend ceiling would be exceeded: committed "
                            f"${reserve.committed_usd} plus a reserved upper bound of "
                            f"${reserve.upper_bound_usd} against a ${reserve.limit_usd} "
                            f"ceiling (billed ${reserve.spent_usd}, estimated "
                            f"${reserve.estimated_spend_usd}, reserved "
                            f"${reserve.reserved_usd}, unknown-cost runs "
                            f"{reserve.unknown_cost_runs}). The provider was not called."
                        ),
                        owner=owner,
                        now=timezone.now(),
                    )
            started = _begin_provider_call(submission.pk, owner=owner, now=moment)
            if started is None:
                return AllocationSubmission.objects.get(pk=submission.pk)
            proposal = PortfolioCIO(
                client=client,
                policy=context["policy"],
                max_repairs=max_repairs,
            ).decide(context["brief"])
            if ledger_key is not None:
                try:
                    reconcile_report(ledger_key, client.report())
                except Exception as ledger_exc:  # pragma: no cover - accounting is best effort
                    logger.exception(
                        "Could not reconcile allocation usage %s: %s",
                        log_safe(ledger_key),
                        log_safe(ledger_exc),
                    )
                    mark_unknown(
                        call_key=ledger_key,
                        reason="allocation_usage_reconciliation_failed",
                        metadata={"error_type": type(ledger_exc).__name__},
                    )
            metadata = _provider_metadata(submission, proposal, context)
            checkpoint = _checkpoint_provider_result(
                submission.pk,
                owner=owner,
                proposal=proposal,
                metadata=metadata,
                now=timezone.now(),
            )
            if checkpoint is None:
                return AllocationSubmission.objects.get(pk=submission.pk)
            submission = checkpoint
        else:
            context = _build_analysis_context(submission)

        proposal = _proposal_from_result(
            submission.provider_result, submission.model_metadata or {}
        )
        return _finalize_success(
            submission.pk,
            owner=owner,
            proposal=proposal,
            context=context,
            now=timezone.now(),
        )
    except Exception as exc:
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        detail = str(getattr(exc, "detail", "") or exc)
        if "ledger_key" in locals() and ledger_key is not None:
            try:
                mark_unknown(
                    call_key=ledger_key,
                    reason=f"allocation_failed_{code}"[:64],
                    metadata={"error_code": code},
                )
            except Exception:  # pragma: no cover - never mask the original failure
                logger.exception("Could not mark allocation usage %s unknown", log_safe(ledger_key))
        timed_out = code in {"run_deadline_exceeded", "stage_timeout", "analysis_timeout"}
        data_quality = None
        if context:
            try:
                data_quality = _data_quality(
                    quotes=context.get("quotes") or QuoteCollection(),
                    classification_info=context.get("classification_info") or {},
                    candidate_info=context.get("candidate_info"),
                    brief=context.get("brief"),
                )
            except Exception:  # pragma: no cover - failure recording must not mask the cause
                data_quality = None
        logger.warning(
            "Allocation submission %s failed with code %s", submission.id, log_safe(code)
        )
        return _finish_submission(
            submission.pk,
            status=(
                AllocationSubmissionStatus.TIMED_OUT
                if timed_out
                else AllocationSubmissionStatus.FAILED
            ),
            code=code,
            detail=detail,
            owner=owner,
            now=timezone.now(),
            data_quality=data_quality,
            worker_metadata={
                **(submission.worker_metadata or {}),
                "last_checkpoint": "failed",
                "failure_code": code,
            },
        )


def cancel_submission(
    submission: AllocationSubmission,
    *,
    reason: str = "cancelled_by_user",
    now=None,
) -> tuple[AllocationSubmission, bool]:
    """Cancel an open submission. A terminal row is returned unchanged."""
    code = (reason or "cancelled_by_user").strip()[:64] or "cancelled_by_user"
    moment = now or timezone.now()
    with transaction.atomic():
        locked = _owned_submission(submission.pk)
        if locked.is_terminal:
            return locked, False
        _finish_locked(
            locked,
            status=AllocationSubmissionStatus.CANCELLED,
            code=code,
            detail=(
                "The submission was cancelled before a plan was attached. No plan "
                "was created and no trade was applied."
            ),
            now=moment,
        )
        logger.info("Allocation submission %s cancelled", locked.id)
        return locked, True


def _stuck_queryset(*, now, stale_after_seconds: int):
    threshold = now - timedelta(seconds=stale_after_seconds)
    return (
        AllocationSubmission.objects.filter(
            status__in=[
                AllocationSubmissionStatus.QUEUED,
                AllocationSubmissionStatus.RUNNING,
            ]
        )
        .filter(
            Q(deadline_at__isnull=False, deadline_at__lte=now)
            | Q(status=AllocationSubmissionStatus.QUEUED, created_at__lte=threshold)
            | Q(
                status=AllocationSubmissionStatus.RUNNING,
                heartbeat_at__lte=threshold,
            )
            # A row claimed before the heartbeat column existed has no value to
            # compare; its age is the next-best evidence that no worker owns it.
            | Q(
                status=AllocationSubmissionStatus.RUNNING,
                heartbeat_at__isnull=True,
                created_at__lte=threshold,
            )
            | Q(
                status=AllocationSubmissionStatus.RUNNING,
                lease_expires_at__isnull=False,
                lease_expires_at__lte=now,
            )
        )
        .select_related("plan")
        .order_by("created_at")
    )


def _repair_action(submission: AllocationSubmission, now) -> tuple[str, str, str]:
    if submission.deadline_at is not None and submission.deadline_at <= now:
        return (
            "time_out",
            "analysis_timeout",
            "The submission passed its analysis deadline. It is marked timed out and "
            "no plan or trade will be created.",
        )
    if (
        submission.status == AllocationSubmissionStatus.RUNNING
        and submission.provider_call_started_at is not None
        and not submission.provider_result
    ):
        return (
            "fail",
            "provider_call_unreconciled",
            "A provider call was started and its result was never recorded. A retry "
            "would spend a second time, so the submission is failed instead.",
        )
    if submission.status == AllocationSubmissionStatus.RUNNING:
        return (
            "requeue",
            "worker_lost",
            "The worker lease expired before a plan was attached. The submission is "
            "returned to the queue for another attempt.",
        )
    return (
        "requeue",
        "not_claimed",
        "The submission was queued but no worker claimed it before the stale "
        "threshold. It is re-dispatched.",
    )


def recover_stuck_submissions(
    *,
    dry_run: bool = True,
    now=None,
    stale_after_seconds: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Find and optionally repair submissions a worker will not finish.

    Dry run is the default and performs no write. With ``dry_run=False`` a
    past-deadline or unreconciled row becomes ``timed_out``/``failed``, and a
    row whose worker merely died before the paid call is returned to ``queued``
    and dispatched again. Cancelled and timed-out rows are never reopened.
    """
    moment = now or timezone.now()
    stale = _analysis_stale_seconds() if stale_after_seconds is None else stale_after_seconds
    rows = list(_stuck_queryset(now=moment, stale_after_seconds=stale)[: max(1, int(limit))])
    report: list[dict[str, Any]] = []
    for row in rows:
        action, code, detail = _repair_action(row, moment)
        entry = {
            "run_id": str(row.id),
            "status": row.status,
            "attempt_count": row.attempt_count,
            "claimed_by": row.claimed_by or None,
            "deadline_at": utc_iso(row.deadline_at),
            "heartbeat_at": utc_iso(row.heartbeat_at),
            "provider_call_started_at": utc_iso(row.provider_call_started_at),
            "provider_result_persisted": bool(row.provider_result),
            "action": action,
            "reason": code,
            "detail": detail,
            "applied": False,
        }
        if not dry_run:
            if action == "requeue":
                requeued = False
                with transaction.atomic():
                    locked = _owned_submission(row.pk)
                    if not locked.is_terminal:
                        locked.status = AllocationSubmissionStatus.QUEUED
                        locked.claimed_by = ""
                        locked.claimed_at = None
                        locked.heartbeat_at = None
                        locked.lease_expires_at = None
                        locked.worker_metadata = json_safe(
                            {
                                **(locked.worker_metadata or {}),
                                "last_checkpoint": "requeued",
                                "requeue_reason": code,
                            }
                        )
                        locked.save(
                            update_fields=[
                                "status",
                                "claimed_by",
                                "claimed_at",
                                "heartbeat_at",
                                "lease_expires_at",
                                "worker_metadata",
                                "updated_at",
                            ]
                        )
                        requeued = True
                if requeued:
                    dispatch_submission(row)
                entry["applied"] = requeued
            else:
                status = (
                    AllocationSubmissionStatus.TIMED_OUT
                    if action == "time_out"
                    else AllocationSubmissionStatus.FAILED
                )
                repaired = _finish_submission(
                    row.pk,
                    status=status,
                    code=code,
                    detail=detail,
                    owner=None,
                    now=moment,
                    worker_metadata={
                        **(row.worker_metadata or {}),
                        "last_checkpoint": "repaired",
                        "repair_action": action,
                    },
                )
                entry["applied"] = repaired.is_terminal and repaired.status == status
        report.append(entry)
    return report


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def submission_as_dict(submission: AllocationSubmission) -> dict:
    plan = submission.plan
    return {
        "run_id": str(submission.id),
        "portfolio_id": submission.portfolio_id,
        "status": submission.status,
        "terminal": submission.is_terminal,
        "terminal_at": utc_iso(submission.terminal_at),
        "failure_reason": (
            None
            if submission.status
            in {
                AllocationSubmissionStatus.QUEUED,
                AllocationSubmissionStatus.RUNNING,
                AllocationSubmissionStatus.SUCCEEDED,
            }
            else (submission.error_code or None)
        ),
        "attempt_count": submission.attempt_count,
        "max_attempts": submission.max_attempts,
        "claimed_by": submission.claimed_by or None,
        "queued_at": utc_iso(submission.queued_at),
        "deadline_at": utc_iso(submission.deadline_at),
        "heartbeat_at": utc_iso(submission.heartbeat_at),
        "mode": submission.mode,
        "protocol_version": submission.protocol_version,
        "tickers": list(submission.tickers or []),
        "idempotency_key": submission.idempotency_key,
        "request_hash": submission.request_hash,
        "request": submission.request,
        "provider": submission.provider or None,
        "model": submission.model or None,
        "synthetic_response": submission.synthetic_response,
        "plan_id": str(plan.id) if plan is not None else None,
        "plan_status": plan.status if plan is not None else None,
        "plan_version": plan.plan_version if plan is not None else None,
        "model_metadata": submission.model_metadata,
        "data_quality": submission.data_quality,
        "error": (
            {"code": submission.error_code or None, "detail": submission.error_detail or None}
            if submission.error_code or submission.error_detail
            else None
        ),
        "created_at": utc_iso(submission.created_at),
        "started_at": utc_iso(submission.started_at),
        "completed_at": utc_iso(submission.completed_at),
    }


def submission_plan_or_none(submission: AllocationSubmission) -> PortfolioPlan | None:
    return submission.plan


def analysis_is_terminal(submission: AllocationSubmission) -> bool:
    return submission.is_terminal
