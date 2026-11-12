"""Observation-time and freshness policy for execution marks.

A price without an observation time is not a market mark: it is a number that
happens to equal a price. The execution guard therefore treats an unknown
observation time as missing data wherever a current valuation is required. The
policy lives here so the plan path, the recommendation path and the shared
ledger path all apply exactly the same rule.

Configuration
-------------
``EXECUTION_MAX_QUOTE_AGE_SECONDS``
    The default ceiling. This is the setting the execution guard already used.
``EXECUTION_MAX_PRICE_DRIFT_PCT``
    The default tolerance between an approved reference price and the current
    mark. A plan may persist a tighter value; a recommendation snapshots the
    value in force when the human approves.
``EXECUTION_FRESHNESS_POLICY``
    Optional mapping::

        {
            "by_source": {"cache": 600, "yfinance": 900},
            "by_instrument": {"BTC-USD": 300},
            "allow_risk_reducing_exception": true,
        }

    The narrowest matching ceiling wins. A source or instrument override may
    tighten the default; it may not make an untimed or future-dated observation
    acceptable. The policy object records the ceiling it used so a rejection can
    name both the mark and the rule.

Fallback prices
---------------
An acquisition cost or a stored proposal price is a planning estimate. It is
allowed only when the caller declares a paper or estimation mode, and it is
recorded as such. A live execution that receives one is refused with
``fallback_quote_disallowed`` rather than quietly treating the estimate as a
market fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings

__all__ = [
    "DEFAULT_MAX_PRICE_DRIFT_PCT",
    "DEFAULT_MAX_QUOTE_AGE_SECONDS",
    "ESTIMATE_SOURCES",
    "ESTIMATION_MODES",
    "ExecutionFreshnessPolicy",
    "MarkValidation",
    "is_estimate_source",
    "parse_decimal",
    "session_close_utc",
    "validate_mark",
]

DEFAULT_MAX_QUOTE_AGE_SECONDS = 900
DEFAULT_MAX_PRICE_DRIFT_PCT = Decimal("2.0")

#: Sources that are a stored accounting figure or an explicit estimate, never a
#: current market observation.
ESTIMATE_SOURCES = frozenset(
    {
        "fallback",
        "fallback_price",
        "average_cost",
        "average_cost_fallback",
        "cost_basis",
        "operator_supplied",
        "caller_supplied_untimed",
        "paper_plan_mark",
        "planned_mark",
    }
)

#: Modes in which a planning estimate is an explicit, labelled choice.
ESTIMATION_MODES = frozenset({"paper", "estimate", "estimation", "simulated"})

#: A small allowance for the clock difference between the data source and this
#: process. It never makes a mark fresh; it only keeps a one-second skew from
#: reading as future-dated.
FUTURE_SKEW_SECONDS = 2


class _Missing:
    pass


def parse_decimal(value: Any) -> Decimal | None:
    """Parse a finite Decimal, or return None. Never guesses a price."""
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if parsed.is_nan() or parsed.is_infinite():
        return None
    return parsed


@dataclass(frozen=True)
class ExecutionFreshnessPolicy:
    """The declared freshness rule, with the values that produced it."""

    default_max_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS
    max_price_drift_pct: Decimal = DEFAULT_MAX_PRICE_DRIFT_PCT
    by_source: dict[str, int] = field(default_factory=dict)
    by_instrument: dict[str, int] = field(default_factory=dict)
    allow_risk_reducing_exception: bool = True
    source: str = "defaults"

    @classmethod
    def from_settings(cls) -> ExecutionFreshnessPolicy:
        try:
            default_age = max(
                int(
                    getattr(
                        settings,
                        "EXECUTION_MAX_QUOTE_AGE_SECONDS",
                        DEFAULT_MAX_QUOTE_AGE_SECONDS,
                    )
                ),
                0,
            )
        except (TypeError, ValueError):
            default_age = DEFAULT_MAX_QUOTE_AGE_SECONDS
        try:
            drift = Decimal(
                str(
                    getattr(
                        settings,
                        "EXECUTION_MAX_PRICE_DRIFT_PCT",
                        DEFAULT_MAX_PRICE_DRIFT_PCT,
                    )
                )
            )
        except (InvalidOperation, TypeError, ValueError):
            drift = DEFAULT_MAX_PRICE_DRIFT_PCT
        if drift < 0:
            drift = DEFAULT_MAX_PRICE_DRIFT_PCT

        raw = getattr(settings, "EXECUTION_FRESHNESS_POLICY", {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        by_source: dict[str, int] = {}
        for key, value in dict(raw.get("by_source") or {}).items():
            try:
                by_source[str(key).strip().lower()] = max(int(value), 0)
            except (TypeError, ValueError):
                continue
        by_instrument: dict[str, int] = {}
        for key, value in dict(raw.get("by_instrument") or {}).items():
            try:
                by_instrument[str(key).strip().upper()] = max(int(value), 0)
            except (TypeError, ValueError):
                continue
        return cls(
            default_max_age_seconds=default_age,
            max_price_drift_pct=drift,
            by_source=by_source,
            by_instrument=by_instrument,
            allow_risk_reducing_exception=bool(raw.get("allow_risk_reducing_exception", True)),
            source="settings.EXECUTION_FRESHNESS_POLICY",
        )

    def max_age_seconds(self, ticker: str, source: str) -> int:
        symbol = (ticker or "").strip().upper()
        label = (source or "").strip().lower()
        candidates = [self.default_max_age_seconds]
        if label in self.by_source:
            candidates.append(self.by_source[label])
        if symbol in self.by_instrument:
            candidates.append(self.by_instrument[symbol])
        return min(candidates)

    def drift_tolerance(self, requested: Decimal | None = None) -> Decimal:
        if requested is None:
            return self.max_price_drift_pct
        parsed = parse_decimal(requested)
        if parsed is None or parsed < 0:
            return self.max_price_drift_pct
        return parsed


@dataclass(frozen=True)
class MarkValidation:
    """The result of checking one mark against the freshness policy."""

    ok: bool
    ticker: str
    code: str = ""
    detail: str = ""
    age_seconds: int | None = None
    max_age_seconds: int | None = None
    source: str = ""
    observed_at: datetime | None = None
    retrieved_at: datetime | None = None
    rule: str = ""
    reason: str = ""
    estimated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ticker": self.ticker,
            "code": self.code or None,
            "detail": self.detail or None,
            "age_seconds": self.age_seconds,
            "max_age_seconds": self.max_age_seconds,
            "source": self.source or None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "rule": self.rule or None,
            "reason": self.reason or None,
            "estimated": self.estimated,
        }


def is_estimate_source(source: str) -> bool:
    return (source or "").strip().lower() in ESTIMATE_SOURCES


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def validate_mark(
    *,
    ticker: str,
    price: Decimal | None,
    as_of: datetime | None,
    retrieved_at: datetime | None,
    source: str,
    now: datetime,
    mode: str = "live",
    require_current: bool = True,
    risk_reducing: bool = False,
    policy: ExecutionFreshnessPolicy | None = None,
) -> MarkValidation:
    """Check one mark. ``require_current=False`` is the labelled paper path.

    A risk-reducing exception applies only to a mark used for valuation, never
    to the price of the instrument being traded. Callers pass
    ``risk_reducing=True`` for a material holding in a sale-only plan, and the
    returned rule/reason is persisted with the decision.
    """
    policy = policy or ExecutionFreshnessPolicy.from_settings()
    symbol = (ticker or "").strip().upper()
    label = (source or "unknown").strip()
    observed = _as_utc(as_of)
    retrieved = _as_utc(retrieved_at)
    mode_label = (mode or "live").strip().lower()
    estimated = is_estimate_source(label)
    max_age = policy.max_age_seconds(symbol, label)

    parsed = parse_decimal(price)
    if parsed is None or parsed <= 0:
        return MarkValidation(
            ok=False,
            ticker=symbol,
            code="invalid_quote",
            detail=f"Quote for {symbol} is not a finite, positive price: {price!r}.",
            source=label,
            observed_at=observed,
            retrieved_at=retrieved,
            max_age_seconds=max_age,
            estimated=estimated,
        )

    if estimated and mode_label not in ESTIMATION_MODES:
        return MarkValidation(
            ok=False,
            ticker=symbol,
            code="fallback_quote_disallowed",
            detail=(
                f"Quote for {symbol} comes from {label!r}, which is a planning "
                "estimate rather than a market observation. This is allowed only "
                "in an explicitly labelled paper or estimation mode."
            ),
            source=label,
            observed_at=observed,
            retrieved_at=retrieved,
            max_age_seconds=max_age,
            estimated=True,
        )

    if not require_current or mode_label in ESTIMATION_MODES:
        return MarkValidation(
            ok=True,
            ticker=symbol,
            source=label,
            observed_at=observed,
            retrieved_at=retrieved,
            rule="explicit_estimation_mode" if mode_label in ESTIMATION_MODES else "",
            reason=(
                "Accepted because the caller declared an estimation/paper mode; "
                "this is not a live market mark."
                if mode_label in ESTIMATION_MODES
                else ""
            ),
            estimated=estimated,
        )

    if observed is None:
        if risk_reducing and policy.allow_risk_reducing_exception:
            return MarkValidation(
                ok=True,
                ticker=symbol,
                source=label,
                observed_at=None,
                retrieved_at=retrieved,
                max_age_seconds=max_age,
                rule="risk_reducing_missing_observation_time",
                reason=(
                    f"{symbol} has no observation time, but this mark is used only to "
                    "value a risk-reducing sale, not to price new risk."
                ),
                estimated=estimated,
            )
        return MarkValidation(
            ok=False,
            ticker=symbol,
            code="untimed_quote",
            detail=(
                f"Quote for {symbol} has no observation time. An unknown observation "
                "time is missing data, not a current mark."
            ),
            source=label,
            retrieved_at=retrieved,
            max_age_seconds=max_age,
            estimated=estimated,
        )

    if observed > now:
        future_skew = (observed - now).total_seconds()
        if future_skew > FUTURE_SKEW_SECONDS:
            return MarkValidation(
                ok=False,
                ticker=symbol,
                code="future_quote",
                detail=(
                    f"Quote for {symbol} is timestamped in the future "
                    f"({observed.isoformat()}); it cannot be a current observation."
                ),
                source=label,
                observed_at=observed,
                retrieved_at=retrieved,
                max_age_seconds=max_age,
                estimated=estimated,
            )

    age = max(int((now - observed).total_seconds()), 0)
    if age > max_age:
        if risk_reducing and policy.allow_risk_reducing_exception:
            return MarkValidation(
                ok=True,
                ticker=symbol,
                source=label,
                observed_at=observed,
                retrieved_at=retrieved,
                age_seconds=age,
                max_age_seconds=max_age,
                rule="risk_reducing_stale_quote_exception",
                reason=(
                    f"{symbol} is {age}s old against a {max_age}s ceiling; the exception "
                    "applies because the trade reduces risk and this mark does not "
                    "increase it."
                ),
                estimated=estimated,
            )
        return MarkValidation(
            ok=False,
            ticker=symbol,
            code="stale_quote",
            detail=(
                f"Quote for {symbol} is {age}s old; the policy ceiling for "
                f"{label or 'this source'} is {max_age}s."
            ),
            age_seconds=age,
            max_age_seconds=max_age,
            source=label,
            observed_at=observed,
            retrieved_at=retrieved,
            estimated=estimated,
        )

    return MarkValidation(
        ok=True,
        ticker=symbol,
        source=label,
        observed_at=observed,
        retrieved_at=retrieved,
        age_seconds=age,
        max_age_seconds=max_age,
        rule="within_freshness_policy",
        estimated=estimated,
    )


def session_close_utc(day: Any) -> datetime | None:
    """The UTC instant of a US equity session close for a trading date.

    Daily bars identify a session, not an instant. This converts the session
    date to 16:00 America/New_York and lets ``zoneinfo`` apply the DST offset.
    It is used only when the source itself supplied no observation instant; the
    caller must still label the source. A non-session value returns ``None``
    rather than inventing a time.
    """
    from datetime import date, time
    from zoneinfo import ZoneInfo

    if isinstance(day, datetime):
        if day.tzinfo is not None and (day.hour, day.minute, day.second) != (0, 0, 0):
            return day.astimezone(UTC)
        # A daily bar's index is commonly midnight of the session date; the
        # observation is the session close, so use the date rather than midnight.
        day = day.date()
    if not isinstance(day, date):
        return None
    try:
        local = datetime.combine(day, time(16, 0), tzinfo=ZoneInfo("America/New_York"))
    except (ValueError, KeyError):
        return None
    return local.astimezone(UTC)
