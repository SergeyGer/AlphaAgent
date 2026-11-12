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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.allocation_models import (
    EXECUTION_MODE_LIVE,
    AllocationSubmission,
    AllocationSubmissionStatus,
    PortfolioPlan,
)
from core.decision_json import content_hash, json_safe, utc_iso
from core.models import Asset, Portfolio
from services.allocation_provider import allocation_budget, build_allocation_client
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

logger = logging.getLogger("alphaagent.allocation_submission")

__all__ = [
    "ANALYSIS_PROTOCOL_VERSION",
    "AllocationSubmissionConflict",
    "AllocationSubmissionError",
    "QuoteCollection",
    "SubmissionRequest",
    "collect_quotes",
    "process_submission",
    "submission_as_dict",
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


def submit_analysis(
    portfolio: Portfolio,
    *,
    data: Mapping[str, Any],
    user=None,
) -> tuple[AllocationSubmission, bool]:
    """Record one idempotent submission and execute it inline.

    The ``POST`` returns ``202`` with the submission id whether the analysis has
    completed or failed; the status URL is the authoritative view. Execution is
    inline because the project cannot assume a Celery worker is running for a
    single portfolio call, and a queued task with no worker would leave the
    status at ``accepted`` forever. The submission row is committed **before**
    the model call, so a crash mid-call is visible as ``running`` rather than
    silently re-billed on retry.

    Returns ``(submission, created)``.
    """
    request = SubmissionRequest.build(portfolio, data)
    created = False
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
                    "status": AllocationSubmissionStatus.ACCEPTED,
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

    if created:
        _ = user
        process_submission(submission)
        submission.refresh_from_db()
    return submission, created


def process_submission(submission: AllocationSubmission) -> AllocationSubmission:
    """Execute the analysis once, recording either a completed plan or a typed failure."""
    if submission.status not in {
        AllocationSubmissionStatus.ACCEPTED,
        AllocationSubmissionStatus.RUNNING,
    }:
        return submission
    submission.status = AllocationSubmissionStatus.RUNNING
    submission.started_at = submission.started_at or timezone.now()
    submission.save(update_fields=["status", "started_at", "updated_at"])

    portfolio = submission.portfolio
    request = dict(submission.request or {})
    tickers = [str(ticker).upper() for ticker in (submission.tickers or [])]
    horizon_days = int(request.get("horizon_days") or 0) or None
    stub_response = request.get("stub_response")
    quotes = QuoteCollection()
    classification_info = classification_provenance(portfolio, tickers)
    candidate_info: dict | None = None
    brief = None
    try:
        held = set(
            Asset.objects.filter(portfolio=portfolio, amount__gt=ZERO).values_list(
                "ticker", flat=True
            )
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
            # History is deliberately not fetched: a declared absence of
            # correlation is honest, an invented one is not.
            price_history=None,
            candidates=candidates,
            policy=policy,
            cost_model=cost_model,
            as_of=now,
            horizon_days=horizon_days,
            history_source="not_fetched",
            candidate_source="stored_decision_runs_plus_declared_absence",
        )
        if submission.mode == "stub" and stub_response is None:
            hold = build_hold_proposal(
                brief,
                "Explicit stub mode: deterministic no-change proposal; no model was called.",
            )
            client = StaticAllocationClient(hold.as_dict())
            max_repairs = 0
        else:
            if submission.mode == "stub" and isinstance(stub_response, Mapping):
                # A scripted stub cannot know the brief's opaque portfolio
                # version before the brief exists. The paper command does the
                # same; the response is still validated by the full target
                # contract and is labelled ``provider: stub``.
                stub_response = dict(stub_response)
                stub_response["portfolio_version"] = brief.portfolio_version
            client = build_allocation_client(
                mode=submission.mode,
                stub_response=stub_response,
                budget=allocation_budget(),
            )
            # The client does not choose how many paid repairs it gets: a live
            # request gets the protocol's single bounded repair, a stub none.
            max_repairs = 1 if submission.mode == "live" else 0
        proposal = PortfolioCIO(client=client, policy=policy, max_repairs=max_repairs).decide(brief)
        plan, created = create_plan(
            portfolio,
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
        metadata = dict(plan.model_metadata or {})
        metadata.update(
            {
                "submission_id": str(submission.id),
                "analysis_mode": submission.mode,
                "synthetic_response": bool(
                    metadata.get("synthetic_response") or submission.mode == "stub"
                ),
                "input_provenance": {
                    "prices": quotes.as_dict(),
                    "classifications": classification_info,
                    "candidate_source": "stored_decision_runs_plus_declared_absence",
                    "price_history": "not_fetched",
                },
            }
        )
        plan.model_metadata = json_safe(metadata)
        plan.save(update_fields=["model_metadata", "updated_at"])
        data_quality = _data_quality(
            quotes=quotes,
            classification_info=classification_info,
            candidate_info=candidate_info,
            brief=brief,
        )
        submission.plan = plan
        submission.provider = str(metadata.get("provider") or "")
        submission.model = str(metadata.get("model") or "")
        submission.synthetic_response = bool(metadata.get("synthetic_response"))
        submission.model_metadata = plan.model_metadata
        submission.data_quality = json_safe(data_quality)
        submission.status = AllocationSubmissionStatus.COMPLETED
        submission.completed_at = timezone.now()
        submission.error_code = ""
        submission.error_detail = ""
        submission.save()
        logger.info(
            "Allocation analysis %s completed: plan %s created=%s provider=%s model=%s",
            submission.id,
            plan.id,
            created,
            submission.provider,
            submission.model,
        )
        return submission
    except Exception as exc:
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        detail = str(getattr(exc, "detail", "") or exc)
        submission.status = AllocationSubmissionStatus.FAILED
        submission.completed_at = timezone.now()
        submission.error_code = code[:64]
        submission.error_detail = detail[:4000]
        try:
            submission.data_quality = json_safe(
                _data_quality(
                    quotes=quotes,
                    classification_info=classification_info,
                    candidate_info=candidate_info,
                    brief=brief,
                )
            )
        except Exception:  # pragma: no cover - failure recording must not mask the cause
            logger.exception("Could not record submission data quality for %s", submission.id)
        submission.save()
        logger.warning("Allocation analysis %s failed (%s): %s", submission.id, code, detail)
        return submission


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def submission_as_dict(submission: AllocationSubmission) -> dict:
    plan = submission.plan
    return {
        "run_id": str(submission.id),
        "portfolio_id": submission.portfolio_id,
        "status": submission.status,
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
    return submission.status in {
        AllocationSubmissionStatus.COMPLETED,
        AllocationSubmissionStatus.FAILED,
    }
