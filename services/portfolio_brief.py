"""The portfolio input brief (PORT-02).

The CIO cannot compare candidates at a portfolio boundary without seeing the
portfolio. This module builds that view and - importantly - builds it from data
that exists, labelling every field that does not.

Four honesty rules:

1. **No invented statistics.** Prices come from the snapshot, classifications
   from the explicit lookup, and covariance/correlation only from supplied price
   history with a published observation window, frequency and observation count.
   Expected returns are not computed or supplied at all; the prompt forbids the
   model from inventing them.
2. **Unknown is not zero.** A position with no classification is reported as
   unclassified coverage, not as a 0% sector. A pair with too little history is
   ``null`` with a reason, not a correlation of zero.
3. **No sector inference.** A ticker string is never mapped to a sector. The
   classification lookup is the only source, and absence means unknown.
4. **The snapshot is one state.** Holdings, cash and marks come from a single
   ``InputSnapshot``. Live tables are consulted only for the qualitative inputs
   (theses, candidate cases, classifications) that are not part of a price
   snapshot, and every one of those is labelled with its own source.

Weight arithmetic is ``Decimal`` throughout. The brief is JSON-safe: decimals
are strings, timestamps are UTC ISO-8601 with ``Z``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from core.allocation_models import InstrumentClassification
from core.decision_json import content_hash, decimal_string, utc_iso
from core.decision_models import DecisionRun, DecisionStatus
from core.models import Portfolio, PositionThesis, ThesisStatus
from services.evidence import create_snapshot
from services.portfolio_policy import (
    ACTION_LABEL_ONLY,
    CENT,
    HUNDRED,
    ZERO,
    AllocationPolicy,
    CostModel,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.decision_models import InputSnapshot

logger = logging.getLogger("alphaagent.portfolio_brief")

__all__ = [
    "BRIEF_SCHEMA_VERSION",
    "CANDIDATE_CLAIM_LIMIT",
    "COVARIANCE_METHOD_VERSION",
    "CandidateCase",
    "CandidateClaim",
    "Holding",
    "PortfolioBrief",
    "PortfolioState",
    "build_allocation_brief",
    "build_portfolio_state",
    "build_risk_statistics",
    "capture_allocation_snapshot",
    "load_candidate_cases",
    "load_classifications",
    "portfolio_state_version",
]

BRIEF_SCHEMA_VERSION = "portfolio_brief.v1"
COVARIANCE_METHOD_VERSION = "covariance.sample_daily_returns.v1"
CANDIDATE_CLAIM_LIMIT = 10
PRICE_QUANTUM = CENT
VALUE_QUANTUM = CENT
PUBLISHED_WEIGHT_QUANTUM = Decimal("0.00000001")

OBJECTIVE = (
    "Allocate the portfolio's capital across the permitted instruments under the stated "
    "constraints. This is a constrained capital-allocation decision, not a forecast and "
    "not a guarantee of profitability."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _dec(value: Any, *, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return default
    if parsed.is_nan() or parsed.is_infinite():
        return default
    return parsed


def _quantize(value: Decimal, quantum: Decimal = CENT) -> Decimal:
    return value.quantize(quantum)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return utc_iso(value)
    return value.isoformat()


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return _aware(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(text)
            except ValueError:
                return None
    return None


def _price_from_quote(value: Any) -> Decimal | None:
    """Accept a Decimal, a numeric string, or a ``PriceQuote``-like object."""
    if value is None:
        return None
    if hasattr(value, "price"):
        return _dec(getattr(value, "price", None))
    return _dec(value)


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values))


def _sample_variance(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        return ZERO
    average = _mean(values)
    total = sum(((value - average) ** 2 for value in values), ZERO)
    return total / Decimal(len(values) - 1)


def _sample_covariance(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) < 2 or len(left) != len(right):
        return ZERO
    left_mean = _mean(left)
    right_mean = _mean(right)
    total = sum(
        ((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)), ZERO
    )
    return total / Decimal(len(left) - 1)


# ---------------------------------------------------------------------------
# Classification lookup
# ---------------------------------------------------------------------------
def load_classifications(
    portfolio: Portfolio, tickers: Iterable[str] | None = None
) -> dict[str, dict]:
    """Explicit sector/asset-class labels for this portfolio.

    A missing key means unknown. The function does not consult a ticker pattern,
    a symbol suffix or a bundled mapping: those would produce a confident wrong
    answer, which is worse than the honest absence.
    """
    queryset = InstrumentClassification.objects.filter(portfolio=portfolio)
    wanted = {str(t).strip().upper() for t in (tickers or []) if str(t).strip()}
    if wanted:
        queryset = queryset.filter(ticker__in=wanted)
    result: dict[str, dict] = {}
    for row in queryset:
        result[row.ticker.upper()] = {
            "sector": row.sector,
            "asset_class": row.asset_class,
            "source": row.source,
            "source_detail": row.source_detail,
        }
    return result


# ---------------------------------------------------------------------------
# Candidate cases
# ---------------------------------------------------------------------------
@dataclass
class CandidateClaim:
    claim_id: str
    agent_role: str
    stance: str
    claim_type: str
    statement: str
    evidence_keys: list[str] = field(default_factory=list)
    horizon: str = ""
    limitations: str = ""

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence_keys)

    def as_dict(self, *, ticker: str, run_id: str, action: str) -> dict:
        return {
            "claim_id": self.claim_id,
            "ticker": ticker,
            "run_id": run_id,
            "candidate_action": action,
            "agent_role": self.agent_role,
            "stance": self.stance,
            "claim_type": self.claim_type,
            "statement": self.statement,
            "evidence_keys": list(self.evidence_keys),
            "horizon": self.horizon,
            "limitations": self.limitations,
        }


@dataclass
class CandidateCase:
    ticker: str
    action: str
    rationale: str
    strongest_objection: str = ""
    thesis: str = ""
    invalidation_condition: str = ""
    run_id: str = ""
    mode: str = ""
    decided_at: str | None = None
    claims: list[CandidateClaim] = field(default_factory=list)

    @property
    def evidence_sufficient(self) -> bool:
        return any(claim.has_evidence for claim in self.claims)

    def as_dict(self, *, price: Decimal | None = None) -> dict:
        return {
            "ticker": self.ticker,
            "action": self.action,
            "run_id": self.run_id or None,
            "mode": self.mode or None,
            "decided_at": self.decided_at,
            "rationale": self.rationale,
            "strongest_objection": self.strongest_objection,
            "thesis": self.thesis,
            "invalidation_condition": self.invalidation_condition,
            "claims": [
                claim.as_dict(ticker=self.ticker, run_id=self.run_id, action=self.action)
                for claim in self.claims
            ],
            "claim_ids": [claim.claim_id for claim in self.claims],
            "evidence_sufficient": self.evidence_sufficient,
            "price": str(price) if price is not None else None,
        }


def load_candidate_cases(
    portfolio: Portfolio,
    *,
    tickers: Iterable[str] | None = None,
    as_of: datetime | None = None,
    limit: int = 20,
    max_claims: int = CANDIDATE_CLAIM_LIMIT,
) -> list[CandidateCase]:
    """Latest completed decision run per candidate ticker, with its claims.

    The case is the Phase 2 producer's output; this function does not invent one
    when a run is missing. A ticker with no run simply is not a candidate, and
    the brief says so.
    """
    queryset = (
        DecisionRun.objects.filter(portfolio=portfolio, status=DecisionStatus.COMPLETED)
        .select_related("decision")
        .prefetch_related("claims__evidence")
        .order_by("-started_at")
    )
    if as_of is not None:
        queryset = queryset.filter(started_at__lte=_aware(as_of))
    wanted = {str(t).strip().upper() for t in (tickers or []) if str(t).strip()}
    if wanted:
        queryset = queryset.filter(ticker__in=wanted)

    cases: dict[str, CandidateCase] = {}
    for run in queryset[: max(limit * 4, limit)]:
        if len(cases) >= limit and run.ticker.upper() not in cases:
            break
        ticker = (run.ticker or "").strip().upper()
        if not ticker or ticker in cases:
            continue
        try:
            decision = run.decision
        except ObjectDoesNotExist:
            continue
        claims = []
        for claim in list(run.claims.all())[:max_claims]:
            evidence_keys = [str(item.id) for item in claim.evidence.all()]
            claims.append(
                CandidateClaim(
                    claim_id=str(claim.claim_id),
                    agent_role=claim.agent_role,
                    stance=claim.stance,
                    claim_type=claim.claim_type,
                    statement=claim.statement,
                    evidence_keys=evidence_keys,
                    horizon=claim.horizon,
                    limitations=claim.limitations,
                )
            )
        cases[ticker] = CandidateCase(
            ticker=ticker,
            action=decision.action,
            rationale=decision.concise_rationale,
            strongest_objection=decision.strongest_objection,
            thesis=decision.thesis,
            invalidation_condition=decision.invalidation_condition,
            run_id=str(run.id),
            mode=run.mode,
            decided_at=utc_iso(run.started_at),
            claims=claims,
        )
        if len(cases) >= limit:
            break
    return [cases[ticker] for ticker in sorted(cases)]


# ---------------------------------------------------------------------------
# Portfolio state from one snapshot
# ---------------------------------------------------------------------------
@dataclass
class Holding:
    ticker: str
    quantity: Decimal
    avg_cost: Decimal
    price: Decimal | None
    price_source: str

    @property
    def market_value_usd(self) -> Decimal | None:
        if self.price is None:
            return None
        return _quantize(self.quantity * self.price, VALUE_QUANTUM)


@dataclass
class PortfolioState:
    """The arithmetic state a plan is sized against.

    ``weights`` are raw Decimals (not quantised) so the projection is not fed a
    rounded version of the current portfolio; the brief quantises only on output.
    """

    holdings: list[Holding]
    cash_usd: Decimal
    positions_value_usd: Decimal
    equity_usd: Decimal | None
    weights: dict[str, Decimal]
    prices: dict[str, Decimal]
    unpriced_tickers: list[str] = field(default_factory=list)
    prices_marked_at_cost: list[str] = field(default_factory=list)
    holding_prices: dict[str, Decimal] = field(default_factory=dict)

    def quantity(self, ticker: str) -> Decimal:
        wanted = (ticker or "").upper()
        for holding in self.holdings:
            if holding.ticker == wanted:
                return holding.quantity
        return ZERO

    def weight(self, ticker: str) -> Decimal:
        return self.weights.get((ticker or "").upper(), ZERO)


def _snapshot_rows(snapshot: InputSnapshot) -> tuple[list[dict], dict, dict]:
    holdings = snapshot.holdings if isinstance(snapshot.holdings, list) else []
    cash = snapshot.cash if isinstance(snapshot.cash, dict) else {}
    quotes = snapshot.quotes if isinstance(snapshot.quotes, dict) else {}
    return holdings, cash, quotes


def build_portfolio_state(
    portfolio: Portfolio,
    *,
    quotes: Mapping[str, Any] | None = None,
    snapshot: InputSnapshot | None = None,
) -> PortfolioState:
    """Build holdings, cash, marks and weights from one snapshot.

    Price precedence is ``quotes`` (caller-supplied current marks) -> snapshot
    quote -> the price stored on the snapshot holding -> average cost. The average
    cost fallback is labelled; it is a valuation crutch for a ceiling, not a
    market price, and the brief reports it as such.
    """
    provided: dict[str, Decimal] = {}
    for ticker, value in (quotes or {}).items():
        price = _price_from_quote(value)
        symbol = str(ticker).strip().upper()
        if symbol and price is not None and price > ZERO:
            provided[symbol] = price

    snapshot_quotes: dict[str, Decimal] = {}
    raw_holdings: list[dict] = []
    cash = portfolio.balance_usd
    if snapshot is not None:
        raw_holdings, cash_payload, quote_payload = _snapshot_rows(snapshot)
        parsed_cash = _dec(cash_payload.get("balance_usd"), default=cash)
        cash = parsed_cash if parsed_cash is not None else cash
        for ticker, value in quote_payload.items():
            price = _price_from_quote(value)
            symbol = str(ticker).strip().upper()
            if symbol and price is not None and price > ZERO:
                snapshot_quotes[symbol] = price
    else:
        for asset in portfolio.assets.all():
            raw_holdings.append(
                {
                    "ticker": asset.ticker,
                    "quantity": str(asset.amount),
                    "avg_cost": str(asset.avg_purchase_price),
                }
            )

    holdings: list[Holding] = []
    prices: dict[str, Decimal] = dict(provided)
    unpriced: list[str] = []
    at_cost: list[str] = []

    for row in raw_holdings:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        quantity = _dec(row.get("quantity", row.get("amount")), default=ZERO) or ZERO
        avg_cost = _dec(row.get("avg_cost", row.get("avg_purchase_price")), default=ZERO) or ZERO
        row_price = _dec(row.get("price"))
        row_source = str(row.get("price_source") or "")
        price: Decimal | None
        source: str
        if ticker in provided:
            price, source = provided[ticker], "provided_quote"
        elif ticker in snapshot_quotes:
            price, source = snapshot_quotes[ticker], "snapshot_quote"
        elif row_price is not None and row_price > ZERO:
            if row_source == "avg_cost_fallback":
                price, source = row_price, "avg_cost_fallback"
                at_cost.append(ticker)
            else:
                price, source = row_price, "snapshot_holding_price"
        elif avg_cost > ZERO:
            price, source = avg_cost, "avg_cost_fallback"
            at_cost.append(ticker)
        else:
            price, source = None, "missing"
            unpriced.append(ticker)

        if price is not None and price > ZERO:
            prices[ticker] = price
        holdings.append(
            Holding(
                ticker=ticker,
                quantity=quantity,
                avg_cost=avg_cost,
                price=price,
                price_source=source,
            )
        )

    positions_value = sum(
        (holding.market_value_usd for holding in holdings if holding.market_value_usd is not None),
        ZERO,
    )
    positions_value = _quantize(positions_value, VALUE_QUANTUM)
    equity: Decimal | None
    if unpriced:
        equity = None
    else:
        equity = _quantize(cash + positions_value, VALUE_QUANTUM)

    weights: dict[str, Decimal] = {}
    holding_prices: dict[str, Decimal] = {}
    if equity is not None and equity > ZERO:
        for holding in holdings:
            value = holding.market_value_usd or ZERO
            weights[holding.ticker] = value / equity
            if holding.price is not None:
                holding_prices[holding.ticker] = holding.price
        weights["CASH"] = cash / equity

    for symbol, price in provided.items():
        holding_prices.setdefault(symbol, price)

    return PortfolioState(
        holdings=holdings,
        cash_usd=_quantize(cash, VALUE_QUANTUM),
        positions_value_usd=positions_value,
        equity_usd=equity,
        weights=weights,
        prices=prices,
        unpriced_tickers=sorted(set(unpriced)),
        prices_marked_at_cost=sorted(set(at_cost)),
        holding_prices=holding_prices,
    )


def portfolio_state_version(portfolio: Portfolio) -> str:
    """A deterministic version of the portfolio's *state*, not its prices.

    Cash, quantities and the last ledger row are included. A price move does not
    invalidate a plan - its legs are re-marked at apply time anyway - while any
    fill or balance change does. The last transaction id makes a same-quantity
    round trip visible even if it happens to leave identical holdings.
    """
    assets = list(portfolio.assets.order_by("ticker").values_list("ticker", "amount"))
    last_transaction_id = (
        portfolio.transactions.order_by("-id").values_list("id", flat=True).first() or 0
    )
    payload = {
        "portfolio_id": portfolio.id,
        "cash_usd": str(portfolio.balance_usd),
        "holdings": [[ticker.upper(), str(amount)] for ticker, amount in assets],
        "last_transaction_id": last_transaction_id,
    }
    return f"pv-{content_hash(payload)[:24]}"


def capture_allocation_snapshot(
    portfolio: Portfolio,
    *,
    quotes: Mapping[str, Any],
    as_of: datetime | None = None,
    portfolio_version: str | None = None,
) -> InputSnapshot:
    """Capture one immutable snapshot for an allocation plan.

    ``quotes`` is required: an allocation sized from average cost is not a
    market allocation. The caller supplies every held name and every candidate it
    intends to size. A missing quote is preserved as missing, not filled in
    silently by this function.
    """
    moment = _aware(as_of or timezone.now())
    state = build_portfolio_state(portfolio, quotes=quotes)
    if state.equity_usd is None:
        raise ValueError(
            "Cannot capture an allocation snapshot: unpriced holdings "
            f"{', '.join(state.unpriced_tickers)} have no quote and no cost basis."
        )

    holdings_payload = []
    for holding in state.holdings:
        holdings_payload.append(
            {
                "ticker": holding.ticker,
                "quantity": str(holding.quantity),
                "avg_cost": str(holding.avg_cost),
                "price": str(holding.price) if holding.price is not None else None,
                "market_value_usd": (
                    str(holding.market_value_usd) if holding.market_value_usd is not None else None
                ),
                "price_source": holding.price_source,
            }
        )

    quotes_payload: dict[str, str] = {}
    for ticker, value in quotes.items():
        symbol = str(ticker).strip().upper()
        price = _price_from_quote(value)
        if symbol and price is not None and price > ZERO:
            quotes_payload[symbol] = str(price)

    return create_snapshot(
        portfolio=portfolio,
        portfolio_version=portfolio_version or portfolio_state_version(portfolio),
        analysis_cutoff=moment,
        holdings=holdings_payload,
        cash={"balance_usd": str(state.cash_usd), "currency": "USD"},
        quotes=quotes_payload,
        captured_at=moment,
    )


# ---------------------------------------------------------------------------
# Deterministic covariance / correlation
# ---------------------------------------------------------------------------
def _normalise_history(price_history: Mapping[str, Any] | None) -> dict[str, dict[date, Decimal]]:
    series: dict[str, dict[date, Decimal]] = {}
    for ticker, points in (price_history or {}).items():
        symbol = str(ticker).strip().upper()
        if not symbol:
            continue
        if hasattr(points, "points"):
            points = points.points  # a PriceHistory-like object
        observations: dict[date, Decimal] = {}
        for item in points or []:
            if isinstance(item, Mapping):
                stamp, price = item.get("at", item.get("date")), item.get("price")
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                stamp, price = item[0], item[1]
            else:
                continue
            day = _coerce_date(stamp)
            parsed = _dec(price)
            if day is None or parsed is None or parsed <= ZERO:
                continue
            observations[day] = parsed
        if observations:
            series[symbol] = observations
    return series


def _returns_between(prices: Mapping[date, Decimal], dates: Sequence[date]) -> list[Decimal]:
    returns: list[Decimal] = []
    for previous, current in pairwise(dates):
        start = prices[previous]
        if start <= ZERO:
            continue
        returns.append((prices[current] - start) / start)
    return returns


def build_risk_statistics(
    tickers: Iterable[str],
    price_history: Mapping[str, Any] | None,
    *,
    policy: AllocationPolicy,
    as_of: datetime | None = None,
    history_source: str = "provided",
) -> dict:
    """Deterministic covariance and correlation from supplied daily closes.

    The method is ``covariance.sample_daily_returns.v1``: simple close-to-close
    returns on pairwise common observation dates, sample covariance (n-1), and
    correlation = cov / (sd_x * sd_y). No annualisation is applied, because the
    observations are event/date-aligned rather than a guaranteed regular series
    and annualising would invent a frequency. Every value publishes its
    observation count; a pair below the minimum publishes ``null`` and a reason.
    """
    moment = _aware(as_of or timezone.now())
    symbols = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    series = _normalise_history(price_history)
    if not price_history:
        return {
            "method_version": COVARIANCE_METHOD_VERSION,
            "status": "not_available",
            "frequency": "daily",
            "return_definition": "simple_close_to_close",
            "alignment": "pairwise_common_observations",
            "history_source": "none",
            "tickers": symbols,
            "observation_window": None,
            "history_end_age_days": None,
            "annualisation": None,
            "annualisation_note": (
                "Not annualised: the observations are date-aligned, not a guaranteed "
                "regular series, and annualising would invent a frequency."
            ),
            "pairs": [],
            "covariance": [],
            "correlation": [],
            "variance": [],
            "missing_pairs": [
                {"ticker_a": a, "ticker_b": b, "observations": 0, "reason": "no_history"}
                for index, a in enumerate(symbols)
                for b in symbols[index + 1 :]
            ],
            "quality_flags": ["no_history"],
            "note": (
                "No price history was supplied. Correlation and covariance are null; "
                "zero was not assumed."
            ),
        }

    all_dates = sorted({day for observations in series.values() for day in observations})
    window = (
        {"start": all_dates[0].isoformat(), "end": all_dates[-1].isoformat()} if all_dates else None
    )
    history_end_age_days = (moment.date() - all_dates[-1]).days if all_dates else None
    quality_flags: list[str] = []
    if (
        history_end_age_days is not None
        and history_end_age_days > policy.covariance_max_history_age_days
    ):
        quality_flags.append("stale_history")

    pairs: list[dict] = []
    covariance_rows: list[dict] = []
    correlation_rows: list[dict] = []
    variance_rows: list[dict] = []
    missing_pairs: list[dict] = []

    with localcontext() as context:
        context.prec = 28

        for symbol in symbols:
            observations = series.get(symbol)
            if not observations:
                quality_flags.append(f"missing_history:{symbol}")
                continue
            dates = sorted(observations)
            returns = _returns_between(observations, dates)
            if len(returns) < policy.covariance_min_observations:
                quality_flags.append(f"insufficient_history:{symbol}")
                continue
            variance = _sample_variance(returns)
            variance_rows.append(
                {
                    "ticker": symbol,
                    "value": decimal_string(variance.quantize(Decimal("0.0000000001"))),
                    "observations": len(returns),
                    "quality": "ok" if variance > ZERO else "zero_variance",
                }
            )
            if variance <= ZERO:
                quality_flags.append(f"zero_variance:{symbol}")

        for index, left in enumerate(symbols):
            for right in symbols[index + 1 :]:
                left_series = series.get(left)
                right_series = series.get(right)
                if not left_series or not right_series:
                    missing_pairs.append(
                        {
                            "ticker_a": left,
                            "ticker_b": right,
                            "observations": 0,
                            "reason": "missing_history",
                        }
                    )
                    continue
                common = sorted(set(left_series) & set(right_series))
                left_returns = _returns_between(left_series, common)
                right_returns = _returns_between(right_series, common)
                observations_count = min(len(left_returns), len(right_returns))
                if observations_count < policy.covariance_min_observations:
                    missing_pairs.append(
                        {
                            "ticker_a": left,
                            "ticker_b": right,
                            "observations": observations_count,
                            "reason": "insufficient_history",
                        }
                    )
                    quality_flags.append(f"insufficient_history:{left}|{right}")
                    continue
                covariance = _sample_covariance(left_returns, right_returns)
                left_variance = _sample_variance(left_returns)
                right_variance = _sample_variance(right_returns)
                correlation: Decimal | None
                quality = "ok"
                if left_variance <= ZERO or right_variance <= ZERO:
                    correlation = None
                    quality = "zero_variance"
                    quality_flags.append(f"zero_variance:{left}|{right}")
                else:
                    correlation = covariance / (left_variance.sqrt() * right_variance.sqrt())
                pair_window = {
                    "start": common[0].isoformat(),
                    "end": common[-1].isoformat(),
                }
                pairs.append(
                    {
                        "ticker_a": left,
                        "ticker_b": right,
                        "observations": observations_count,
                        "window": pair_window,
                    }
                )
                covariance_rows.append(
                    {
                        "ticker_a": left,
                        "ticker_b": right,
                        "value": decimal_string(covariance.quantize(Decimal("0.0000000001"))),
                        "observations": observations_count,
                        "window": pair_window,
                    }
                )
                correlation_rows.append(
                    {
                        "ticker_a": left,
                        "ticker_b": right,
                        "value": (
                            decimal_string(correlation.quantize(Decimal("0.000001")))
                            if correlation is not None
                            else None
                        ),
                        "observations": observations_count,
                        "quality": quality,
                        "window": pair_window,
                    }
                )

    if len(symbols) < 2:
        status = "insufficient_instruments"
        quality_flags.append("insufficient_instruments")
    elif correlation_rows:
        status = "computed"
    else:
        status = "insufficient_history"

    if not pairs and status != "computed":
        quality_flags.append("no_correlation_pairs")
    quality_flags = list(dict.fromkeys(quality_flags))
    note = (
        "Deterministic sample estimates from supplied price history. Values not "
        "computed are null with a reason; no expected return is supplied or inferred."
    )
    return {
        "method_version": COVARIANCE_METHOD_VERSION,
        "status": status,
        "frequency": "daily",
        "return_definition": "simple_close_to_close",
        "alignment": "pairwise_common_observations",
        "history_source": history_source,
        "tickers": symbols,
        "observation_window": window,
        "history_end_age_days": history_end_age_days,
        "min_observations": policy.covariance_min_observations,
        "max_history_age_days": policy.covariance_max_history_age_days,
        "annualisation": None,
        "annualisation_note": (
            "Not annualised: the observations are date-aligned, not a guaranteed "
            "regular series, and annualising would invent a frequency."
        ),
        "pairs": pairs,
        "covariance": covariance_rows,
        "correlation": correlation_rows,
        "variance": variance_rows,
        "missing_pairs": missing_pairs,
        "quality_flags": quality_flags,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Exposure blocks
# ---------------------------------------------------------------------------
def _exposure_block(
    state: PortfolioState,
    classifications: Mapping[str, dict],
    *,
    equity: Decimal | None,
) -> dict:
    sector_values: dict[str, Decimal] = {}
    sector_tickers: dict[str, list[str]] = {}
    class_values: dict[str, Decimal] = {}
    class_tickers: dict[str, list[str]] = {}
    unclassified_value = ZERO
    unclassified_tickers: list[str] = []

    for holding in state.holdings:
        value = holding.market_value_usd
        if value is None:
            continue
        info = classifications.get(holding.ticker)
        if not info:
            unclassified_value += value
            unclassified_tickers.append(holding.ticker)
            continue
        sector = str(info.get("sector") or "").strip()
        asset_class = str(info.get("asset_class") or "").strip()
        if sector:
            sector_values[sector] = sector_values.get(sector, ZERO) + value
            sector_tickers.setdefault(sector, []).append(holding.ticker)
        else:
            unclassified_value += value
            unclassified_tickers.append(holding.ticker)
        if asset_class:
            class_values[asset_class] = class_values.get(asset_class, ZERO) + value
            class_tickers.setdefault(asset_class, []).append(holding.ticker)

    def _rows(values: dict[str, Decimal], tickers: dict[str, list[str]]) -> list[dict]:
        rows = []
        for label, value in values.items():
            row = {
                "value_usd": str(_quantize(value, VALUE_QUANTUM)),
                "tickers": sorted(tickers.get(label, [])),
            }
            if label:
                row_key = "sector" if values is sector_values else "asset_class"
                row[row_key] = label
            if equity is not None and equity > ZERO:
                row["weight"] = decimal_string((value / equity).quantize(PUBLISHED_WEIGHT_QUANTUM))
            else:
                row["weight"] = None
            rows.append((value, row))
        ordered = sorted(rows, key=lambda pair: (-pair[0], str(pair[1])))
        return [row for _value, row in ordered]

    sector_rows = _rows(sector_values, sector_tickers)
    class_rows = _rows(class_values, class_tickers)
    known_value = sum(sector_values.values(), ZERO)
    coverage_pct: str | None = None
    if state.positions_value_usd > ZERO:
        coverage_pct = str((known_value / state.positions_value_usd * HUNDRED).quantize(CENT))
    if not state.holdings and state.positions_value_usd <= ZERO:
        status = "no_positions"
        note = "No positions are held; there is no sector exposure to classify."
    elif unclassified_value > ZERO:
        status = "incomplete"
        note = (
            f"Sector exposure is incomplete: {_quantize(unclassified_value, VALUE_QUANTUM)} "
            f"USD across {len(unclassified_tickers)} position(s) has no classification. "
            "Unknown is not zero and no sector was inferred from a ticker."
        )
    else:
        status = "complete"
        note = "Every valued position has an explicit classification."

    unclassified_weight: str | None = None
    if equity is not None and equity > ZERO:
        unclassified_weight = decimal_string(
            (unclassified_value / equity).quantize(PUBLISHED_WEIGHT_QUANTUM)
        )

    return {
        "sector_exposure": sector_rows,
        "asset_class_exposure": class_rows,
        "unclassified": {
            "value_usd": str(_quantize(unclassified_value, VALUE_QUANTUM)),
            "weight": unclassified_weight,
            "tickers": sorted(unclassified_tickers),
        },
        "coverage": {
            "status": status,
            "known_value_usd": str(_quantize(known_value, VALUE_QUANTUM)),
            "unclassified_value_usd": str(_quantize(unclassified_value, VALUE_QUANTUM)),
            "coverage_pct": coverage_pct,
            "positions_value_usd": str(_quantize(state.positions_value_usd, VALUE_QUANTUM)),
        },
        "note": note,
    }


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------
@dataclass
class PortfolioBrief:
    portfolio_version: str
    snapshot_id: str
    captured_at: str | None
    analysis_cutoff: str | None
    objective: str
    investment_horizon_days: int
    state: PortfolioState
    holdings: list[dict]
    cash: dict
    weights: dict[str, Decimal]
    exposure: dict
    limits: dict
    transaction_costs: dict
    theses: list[dict]
    candidate_cases: list[dict]
    claim_catalog: list[dict]
    risk_statistics: dict
    data_quality: dict
    policy: dict
    permitted_tickers: list[str]
    classifications: dict[str, dict]

    @property
    def equity_usd(self) -> Decimal | None:
        return self.state.equity_usd

    @property
    def claim_ids(self) -> set[str]:
        return {str(claim["claim_id"]) for claim in self.claim_catalog}

    def as_dict(self) -> dict:
        return {
            "brief_schema_version": BRIEF_SCHEMA_VERSION,
            "decision_boundary": "portfolio",
            "responsibility": "chief_investment_officer",
            "objective": self.objective,
            "portfolio_version": self.portfolio_version,
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "analysis_cutoff": self.analysis_cutoff,
            "investment_horizon_days": self.investment_horizon_days,
            "equity_usd": str(self.equity_usd) if self.equity_usd is not None else None,
            "cash": self.cash,
            "weights": {
                ticker: decimal_string(weight.quantize(PUBLISHED_WEIGHT_QUANTUM))
                for ticker, weight in sorted(self.weights.items())
            },
            "holdings": self.holdings,
            "permitted_tickers": self.permitted_tickers,
            "exposure": self.exposure,
            "limits": self.limits,
            "transaction_costs": self.transaction_costs,
            "theses": self.theses,
            "candidate_cases": self.candidate_cases,
            "claim_catalog": self.claim_catalog,
            "classifications": self.classifications,
            "risk_statistics": self.risk_statistics,
            "data_quality": self.data_quality,
            "policy": self.policy,
            "statistics_note": (
                "Every risk statistic above is computed deterministically from the "
                "supplied inputs. Expected returns are not provided; a response that "
                "invents one is outside the contract."
            ),
        }


def _load_theses(portfolio: Portfolio) -> list[dict]:
    rows = PositionThesis.objects.filter(portfolio=portfolio, status=ThesisStatus.OPEN).order_by(
        "ticker"
    )
    return [
        {
            "ticker": row.ticker.upper(),
            "thesis": row.thesis,
            "invalidation": row.invalidation,
            "status": row.status,
            "opened_at": utc_iso(row.opened_at),
        }
        for row in rows
    ]


def build_allocation_brief(
    portfolio: Portfolio,
    snapshot: InputSnapshot,
    *,
    quotes: Mapping[str, Any] | None = None,
    price_history: Mapping[str, Any] | None = None,
    candidates: Sequence[CandidateCase] | None = None,
    classifications: Mapping[str, dict] | None = None,
    policy: AllocationPolicy | None = None,
    cost_model: CostModel | None = None,
    as_of: datetime | None = None,
    horizon_days: int | None = None,
    evidence_sufficient: bool | None = None,
    history_source: str = "provided",
    candidate_source: str = "stored_decision_runs",
) -> PortfolioBrief:
    """Assemble the complete PORT-02 input brief.

    ``candidates`` and ``classifications`` are injectable so a reviewer or test
    can supply an explicit case and an explicit label. When omitted, completed
    ``DecisionRun`` rows and the portfolio's ``InstrumentClassification`` rows are
    read. Nothing is inferred from a ticker.
    """
    moment = _aware(as_of or timezone.now())
    policy = policy or AllocationPolicy.from_portfolio(portfolio)
    cost_model = cost_model or CostModel.from_config()
    horizon = int(horizon_days if horizon_days is not None else policy.investment_horizon_days)

    state = build_portfolio_state(portfolio, quotes=quotes, snapshot=snapshot)
    if classifications is not None:
        classifications = {
            str(ticker).upper(): (
                {"sector": value, "asset_class": None, "source": None}
                if isinstance(value, str)
                else dict(value)
            )
            for ticker, value in classifications.items()
        }
    if candidates is None:
        candidates = load_candidate_cases(
            portfolio,
            as_of=moment,
            limit=policy.max_candidate_cases,
            max_claims=policy.max_claims_per_case,
        )
    candidate_tickers = sorted({case.ticker for case in candidates})
    permitted = sorted({holding.ticker for holding in state.holdings} | set(candidate_tickers))
    if classifications is None:
        classifications = load_classifications(portfolio, permitted)

    claim_catalog: list[dict] = []
    seen_claims: set[str] = set()
    for case in candidates:
        for claim in case.claims:
            if claim.claim_id in seen_claims:
                continue
            seen_claims.add(claim.claim_id)
            claim_catalog.append(
                claim.as_dict(ticker=case.ticker, run_id=case.run_id, action=case.action)
            )

    holdings_payload = []
    for holding in state.holdings:
        info = classifications.get(holding.ticker, {})
        market_value = holding.market_value_usd
        weight = None
        if state.equity_usd is not None and state.equity_usd > ZERO and market_value is not None:
            weight = decimal_string(
                (market_value / state.equity_usd).quantize(PUBLISHED_WEIGHT_QUANTUM)
            )
        holdings_payload.append(
            {
                "ticker": holding.ticker,
                "quantity": str(holding.quantity),
                "avg_cost": str(holding.avg_cost),
                "price": str(holding.price) if holding.price is not None else None,
                "price_source": holding.price_source,
                "market_value_usd": str(market_value) if market_value is not None else None,
                "weight": weight,
                "sector": info.get("sector"),
                "asset_class": info.get("asset_class"),
                "classification_source": info.get("source"),
            }
        )

    exposure = _exposure_block(state, classifications, equity=state.equity_usd)
    risk_statistics = build_risk_statistics(
        permitted,
        price_history,
        policy=policy,
        as_of=moment,
        history_source=history_source if price_history else "none",
    )

    captured_at = _aware(snapshot.captured_at)
    snapshot_age_seconds = max(int((moment - captured_at).total_seconds()), 0)
    snapshot_stale = snapshot_age_seconds > policy.max_snapshot_age_seconds
    missing_correlation = bool(risk_statistics["missing_pairs"]) or risk_statistics[
        "status"
    ] not in {"computed", "insufficient_instruments"}
    if risk_statistics["status"] == "insufficient_instruments":
        missing_correlation = True
    if evidence_sufficient is None:
        insufficient_evidence = not candidates or not any(
            case.evidence_sufficient for case in candidates
        )
    else:
        insufficient_evidence = not evidence_sufficient

    flags: list[dict] = []
    if snapshot_stale:
        flags.append(
            {
                "code": "snapshot_stale",
                "severity": "warning",
                "detail": (
                    f"Snapshot is {snapshot_age_seconds}s old; the policy ceiling is "
                    f"{policy.max_snapshot_age_seconds}s."
                ),
            }
        )
    if exposure["coverage"]["status"] == "incomplete":
        flags.append(
            {
                "code": "sector_exposure_incomplete",
                "severity": "warning",
                "detail": exposure["note"],
            }
        )
    if missing_correlation:
        flags.append(
            {
                "code": "missing_correlation_history",
                "severity": "warning",
                "detail": (
                    "Not every permitted pair has the minimum "
                    f"{policy.covariance_min_observations} daily observations. "
                    "Missing pairs are null with reasons in risk_statistics."
                ),
            }
        )
    if insufficient_evidence:
        flags.append(
            {
                "code": "insufficient_evidence",
                "severity": "warning",
                "detail": (
                    "No candidate case has a claim backed by recorded evidence."
                    if candidates
                    else "No completed candidate case was supplied."
                ),
            }
        )
    if state.unpriced_tickers:
        flags.append(
            {
                "code": "unpriced_positions",
                "severity": "error",
                "detail": (
                    "Positions without a price or a cost basis: "
                    + ", ".join(state.unpriced_tickers)
                ),
            }
        )
    if state.prices_marked_at_cost:
        flags.append(
            {
                "code": "prices_marked_at_cost",
                "severity": "warning",
                "detail": (
                    "No market quote for "
                    + ", ".join(state.prices_marked_at_cost)
                    + "; average cost was used. This is not a market price."
                ),
            }
        )
    if not candidates:
        flags.append(
            {
                "code": "no_candidate_cases",
                "severity": "info",
                "detail": "No completed decision run supplied a candidate case.",
            }
        )

    policy_actions = []
    if snapshot_stale and policy.stale_snapshot_action != ACTION_LABEL_ONLY:
        policy_actions.append({"flag": "snapshot_stale", "action": policy.stale_snapshot_action})
    if missing_correlation and policy.missing_correlation_action != ACTION_LABEL_ONLY:
        policy_actions.append(
            {"flag": "missing_correlation_history", "action": policy.missing_correlation_action}
        )
    if insufficient_evidence and policy.insufficient_evidence_action not in {
        ACTION_LABEL_ONLY,
        "allow",
    }:
        policy_actions.append(
            {
                "flag": "insufficient_evidence",
                "action": policy.insufficient_evidence_action,
            }
        )
    if exposure["coverage"]["status"] == "incomplete" and policy.unknown_sector_action not in {
        ACTION_LABEL_ONLY,
        "allow",
    }:
        policy_actions.append(
            {"flag": "sector_exposure_incomplete", "action": policy.unknown_sector_action}
        )

    classification_payload = {
        ticker: (
            {
                "sector": classifications.get(ticker, {}).get("sector"),
                "asset_class": classifications.get(ticker, {}).get("asset_class"),
                "source": classifications.get(ticker, {}).get("source"),
            }
        )
        for ticker in permitted
    }

    data_quality = {
        "flags": flags,
        "flag_codes": [flag["code"] for flag in flags],
        "snapshot_age_seconds": snapshot_age_seconds,
        "snapshot_stale": snapshot_stale,
        "sector_coverage": exposure["coverage"],
        "missing_correlation_history": missing_correlation,
        "insufficient_evidence": insufficient_evidence,
        "unpriced_tickers": state.unpriced_tickers,
        "prices_marked_at_cost": state.prices_marked_at_cost,
        "policy_actions": policy_actions,
        "source": {
            "holdings": "input_snapshot",
            "prices": "input_snapshot",
            "sector": "explicit_portfolio_classification_lookup",
            "candidate_cases": candidate_source if candidates else "none",
            "risk_statistics": history_source if price_history else "none",
            "theses": "open_position_thesis_rows",
        },
    }

    return PortfolioBrief(
        portfolio_version=snapshot.portfolio_version,
        snapshot_id=str(snapshot.id),
        captured_at=utc_iso(captured_at),
        analysis_cutoff=utc_iso(snapshot.analysis_cutoff),
        objective=OBJECTIVE,
        investment_horizon_days=horizon,
        state=state,
        holdings=holdings_payload,
        cash={
            "balance_usd": str(state.cash_usd),
            "weight": (
                decimal_string(
                    (state.cash_usd / state.equity_usd).quantize(PUBLISHED_WEIGHT_QUANTUM)
                )
                if state.equity_usd is not None and state.equity_usd > ZERO
                else None
            ),
            "currency": "USD",
            "buffer_required_usd": (
                str((state.equity_usd * policy.min_cash_weight).quantize(CENT))
                if state.equity_usd is not None
                else None
            ),
        },
        weights=dict(state.weights)
        | {ticker: ZERO for ticker in permitted if ticker not in state.weights}
        | {"CASH": state.weights.get("CASH", ZERO)},
        exposure=exposure,
        limits=policy.as_dict(),
        transaction_costs=cost_model.as_dict(),
        theses=_load_theses(portfolio),
        candidate_cases=[case.as_dict(price=state.prices.get(case.ticker)) for case in candidates],
        claim_catalog=claim_catalog,
        risk_statistics=risk_statistics,
        data_quality=data_quality,
        policy=policy.as_dict(),
        permitted_tickers=permitted,
        classifications=classification_payload,
    )
