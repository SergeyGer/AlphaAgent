"""Daily AI spend accounting and the ceiling that halts dispatching.

The execution guard answers "may this trade happen?". This answers a different
question the guard deliberately does not: "may we afford to think about it?".

Nothing capped language-model spend before this module. Celery Beat sweeps every
portfolio and ticker on a schedule, so an unattended deployment could run up an
API bill indefinitely - the guard would keep refusing trades on risk grounds while
the meter kept running. The trading limits and the spend limit are separate
controls because they protect different things: one protects the portfolio, the
other protects the operator.

Four quantities are kept apart
------------------------------
``billed``
    Dollars the provider reported. A measurement.
``estimated``
    Price-table arithmetic over provider-reported (or estimated) tokens. The
    provider did not report dollars; this is not a bill.
``reserved``
    An upper bound held **before** the provider is called. A request whose cost
    cannot be known in advance is never charged against zero.
``unknown``
    No usable cost was reported. The row stays ``unknown`` with a typed reason
    and its reservation stays held; the figure is never written as a measured
    zero.

The ceiling is enforced on ``spent + reserved + estimated`` - the conservative
committed figure, not billed dollars alone. A request that would cross the
ceiling on its reserved upper bound is refused before the provider call.

Durability and idempotency
--------------------------
Every logical call is one :class:`core.models.AIUsageLedger` row keyed by a
stable ``call_key``. The row is written in the same transaction as the ceiling
check; a PostgreSQL transaction advisory lock serialises concurrent reservations
for the same UTC day. A restart reads the rows back. Replaying a key returns the
existing row and cannot double-count it.

What this still cannot see
--------------------------
A provider charge reported after the run ends, or for a call this process never
saw, is outside the ledger. The payload therefore says ``basis: "lower_bound"``
and publishes ``unknown_cost_runs`` beside the numbers. It is not a statement
that the provider account stopped at the ceiling.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import connection, transaction

from services.cost_accounting import UNKNOWN_PROVIDER_COST

__all__ = [
    "MICRO_CENT",
    "BudgetState",
    "SpendDecision",
    "budget_state",
    "check_and_reserve",
    "cost_state",
    "logical_call_key",
    "mark_unknown",
    "reconcile_decision_run",
    "reconcile_report",
    "reserve_bound_for_run",
    "settle_call",
    "spend_limit",
    "spend_today",
    "utc_day",
]

ZERO = Decimal("0.00")
#: Six decimal places, not cents. A sub-cent price-table estimate (a small
#: Haiku call is worth fractions of a cent) must not round to a measured zero.
MICRO_CENT = Decimal("0.000001")

#: Stable refusal reason. It is a code, not prose, so callers and dashboards can
#: branch on it without parsing a message.
REASON_RESERVE_EXCEEDED = "ai_spend_reserve_exceeded"
REASON_CEILING_ALREADY_REACHED = "ai_spend_ceiling"


def _money(value) -> Decimal:
    """Coerce to a non-negative Decimal at six decimal places."""
    if value is None:
        return ZERO
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Not a decimal amount: {value!r}") from exc
    if amount < 0:
        raise ValueError("A spend amount must not be negative.")
    return amount.quantize(MICRO_CENT)


def utc_day(moment: datetime | None = None) -> date:
    """The UTC date a reservation belongs to."""
    from django.utils import timezone

    moment = moment or timezone.now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def logical_call_key(prefix: str, identity: str | int | None) -> str:
    """Stable key for one logical model call.

    The prefix names the producer (``decision``, ``allocation``); the identity is
    the durable idempotency token (a Celery task id, a submission key, a run id).
    When no durable identity exists a content-free fallback is derived from the
    prefix so a caller cannot accidentally share another call's ledger row; the
    caller is responsible for supplying an identity when replay matters.
    """
    prefix = str(prefix or "call").strip() or "call"
    token = str(identity).strip() if identity not in (None, "") else ""
    if not token:
        token = f"anonymous-{hashlib.sha256(prefix.encode('utf-8')).hexdigest()[:16]}"
    return f"{prefix}:{token}"[:255]


def spend_limit() -> Decimal:
    """The configured daily ceiling, in USD.

    Always non-zero. ``settings.AI_DAILY_SPEND_LIMIT_USD`` defaults to 5.00, and
    a value that fails to parse falls back to that default rather than to zero -
    a ceiling of zero would be indistinguishable from "disabled", which is the
    state this module exists to prevent.
    """
    limit = getattr(settings, "AI_DAILY_SPEND_LIMIT_USD", Decimal("5.00"))
    try:
        value = Decimal(str(limit))
    except (InvalidOperation, TypeError, ValueError):
        value = Decimal("5.00")
    return max(value, ZERO).quantize(MICRO_CENT)


def reserve_bound_for_run(kind: str = "decision") -> Decimal:
    """The declared per-run upper bound the limiter reserves.

    ``settings.AI_RUN_COST_UPPER_BOUND_USD`` is the explicit declaration. When it
    is absent or unusable the protocol's own monetary ceiling
    (``DEBATE_CONFIG['MONETARY_BUDGET_USD']``) is the next-best declared bound:
    the run cannot spend more than it because the protocol stops there. A zero
    or negative fallback is never used - the fallback chain ends at 1.00.

    ``kind="allocation"`` uses the allocation submission ceiling, because that
    path has its own declared budget. A caller that needs a different bound
    should pass it to :func:`check_and_reserve` directly.
    """
    if kind == "allocation":
        config = getattr(settings, "ALLOCATION_CONFIG", {}) or {}
        raw = config.get("SUBMISSION_MONETARY_BUDGET_USD")
    else:
        raw = getattr(settings, "AI_RUN_COST_UPPER_BOUND_USD", None)
        if raw in (None, ""):
            config = getattr(settings, "DEBATE_CONFIG", {}) or {}
            raw = config.get("MONETARY_BUDGET_USD")
    try:
        bound = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        bound = Decimal("0")
    if bound <= ZERO:
        bound = Decimal("1.00")
    return bound.quantize(MICRO_CENT)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _legacy_filter(start):
    from core.models import AgentDecisionLog

    # Rows booked under the ledger are authoritative there. Adding their billed
    # column again would double-count the same logical call.
    return AgentDecisionLog.objects.filter(created_at__gte=start, cost_ledger_key="")


def _ledger_filter(start):
    from core.models import AIUsageLedger

    return AIUsageLedger.objects.filter(created_at__gte=start)


def _aggregate(start) -> dict:
    """Aggregate billed, reserved, estimated and unknown for one UTC window."""
    from django.db.models import Count, Q, Sum

    ledger = _ledger_filter(start)
    ledger_totals = ledger.aggregate(
        billed=Sum("billed_usd", default=ZERO),
        estimated=Sum("estimated_usd", default=ZERO, filter=Q(state="estimated")),
        reserved=Sum("reserved_usd", default=ZERO),
        unknown_reserved=Sum("reserved_usd", default=ZERO, filter=Q(state="unknown")),
        unknown=Count("id", filter=Q(state="unknown")),
        reserved_rows=Count("id", filter=Q(state="reserved")),
        total=Count("id"),
    )
    legacy = _legacy_filter(start)
    legacy_totals = legacy.aggregate(
        billed=Sum("api_cost_usd", default=ZERO),
        estimated=Sum("estimated_cost_usd", default=ZERO, filter=Q(api_cost_usd__isnull=True)),
        unknown=Count("id", filter=Q(api_cost_usd__isnull=True)),
        total=Count("id"),
    )

    billed = _money(ledger_totals["billed"]) + _money(legacy_totals["billed"])
    estimated = _money(ledger_totals["estimated"]) + _money(legacy_totals["estimated"])
    reserved = _money(ledger_totals["reserved"])
    unknown_reserved = _money(ledger_totals["unknown_reserved"])
    unknown = int(ledger_totals["unknown"] or 0) + int(legacy_totals["unknown"] or 0)
    committed = (billed + estimated + reserved).quantize(MICRO_CENT)
    return {
        "billed": billed,
        "estimated": estimated,
        "reserved": reserved,
        "unknown_reserved": unknown_reserved,
        "unknown": unknown,
        "reserved_rows": int(ledger_totals["reserved_rows"] or 0),
        "ledger_unknown": int(ledger_totals["unknown"] or 0),
        "ledger_rows": int(ledger_totals["total"] or 0),
        "legacy_rows": int(legacy_totals["total"] or 0),
        "committed": committed,
        # Named so a reader does not have to infer that a null amount is
        # different from a measured zero.
        "unknown_amount_usd": None,
    }


def spend_today(*, since=None) -> Decimal:
    """Total **billed** AI spend booked since midnight UTC.

    A NULL ``api_cost_usd`` row contributes nothing to this figure; it is not a
    zero and is reported by :func:`cost_state` as unknown. Use
    :func:`budget_state` to see the conservative committed figure.
    """
    from services.ledger import utc_day_start

    return Decimal(cost_state(since=since or utc_day_start())["billed_spend_usd"])


def cost_state(*, since=None) -> dict:
    """Known billed, reserved, estimated and unknown cost for the window.

    The numbers are deliberately separate. ``estimated_spend_usd`` is a
    price-table figure, not a bill; ``reserved_usd`` is an upper bound held for
    calls whose charge is not settled; ``unknown_cost_runs`` counts rows whose
    charge remains unknown and therefore cannot be added to any dollar total.
    """
    from services.ledger import utc_day_start

    start = since or utc_day_start()
    totals = _aggregate(start)
    return {
        "billed_spend_usd": str(totals["billed"]),
        "estimated_spend_usd": str(totals["estimated"]),
        "reserved_spend_usd": str(totals["reserved"]),
        "unknown_reserved_usd": str(totals["unknown_reserved"]),
        "unknown_amount_usd": None,
        "committed_spend_usd": str(totals["committed"]),
        "unknown_cost_runs": totals["unknown"],
        "ledger_unknown_runs": totals["ledger_unknown"],
        "unreserved_unknown_runs": max(0, totals["unknown"] - totals["ledger_unknown"]),
        "reserved_cost_runs": totals["reserved_rows"],
        "runs_booked": totals["ledger_rows"] + totals["legacy_rows"],
        "basis": "lower_bound",
        "lower_bound": True,
        "note": (
            "spent_usd is billed only. reserved_usd is held against unresolved calls "
            "(including unknown-cost calls, whose reservation is kept). The sum "
            "spent+estimated+reserved is the conservative committed figure. "
            "unknown_amount_usd is null because the missing charge cannot be "
            "quantified; unknown_cost_runs counts it."
        ),
    }


# ---------------------------------------------------------------------------
# Reservation and reconciliation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SpendDecision:
    """The answer to "may this logical call be dispatched?".

    Both the allow and refuse shapes carry the same numbers, so a caller can log
    why a request was refused without re-querying (and racing) the ledger.
    """

    allowed: bool
    call_key: str
    reason: str = ""
    upper_bound_usd: Decimal = ZERO
    spent_usd: Decimal = ZERO
    reserved_usd: Decimal = ZERO
    estimated_spend_usd: Decimal = ZERO
    committed_usd: Decimal = ZERO
    limit_usd: Decimal = ZERO
    remaining_usd: Decimal = ZERO
    unknown_reserved_usd: Decimal = ZERO
    unknown_cost_runs: int = 0
    replay: bool = False

    def as_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "allowed": self.allowed,
            "reason": self.reason or None,
            "call_key": self.call_key,
            "upper_bound_usd": str(self.upper_bound_usd),
            "spent_usd": str(self.spent_usd),
            "reserved_usd": str(self.reserved_usd),
            "estimated_spend_usd": str(self.estimated_spend_usd),
            "committed_usd": str(self.committed_usd),
            "limit_usd": str(self.limit_usd),
            "remaining_usd": str(self.remaining_usd),
            "unknown_reserved_usd": str(self.unknown_reserved_usd),
            "unknown_cost_runs": self.unknown_cost_runs,
            "replay": self.replay,
        }


def _lock_day(day: date) -> None:
    """Serialise reservations for one UTC day (PostgreSQL only).

    Without this two workers could both read the same committed total and both
    decide there is room. ``pg_advisory_xact_lock`` is released at commit. On
    other databases (the test suite can use SQLite) the row locks below are the
    available protection; the production database is PostgreSQL.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [int(day.strftime("%Y%m%d"))])


def _decision(
    *,
    allowed: bool,
    call_key: str,
    reason: str,
    upper_bound: Decimal,
    totals: dict,
    replay: bool = False,
) -> SpendDecision:
    limit = spend_limit()
    committed = totals["committed"]
    return SpendDecision(
        allowed=allowed,
        call_key=call_key,
        reason=reason,
        upper_bound_usd=upper_bound,
        spent_usd=totals["billed"],
        reserved_usd=totals["reserved"],
        estimated_spend_usd=totals["estimated"],
        committed_usd=committed,
        limit_usd=limit,
        remaining_usd=max(limit - committed, ZERO).quantize(MICRO_CENT),
        unknown_reserved_usd=totals["unknown_reserved"],
        unknown_cost_runs=totals["unknown"],
        replay=replay,
    )


def check_and_reserve(
    *,
    call_key: str,
    upper_bound_usd,
    provider: str = "",
    model: str = "",
    stage: str = "",
    metadata: dict | None = None,
    now: datetime | None = None,
) -> SpendDecision:
    """Reserve ``upper_bound_usd`` for one logical call, or refuse it.

    The check and the reservation are one transaction. The request is refused
    when the **committed** total would exceed the ceiling on the reserved upper
    bound - before a provider is called, so a refused request cannot spend.

    Replaying a ``call_key`` is idempotent: the existing row is returned, the
    same bound is not added a second time, and a replay is allowed even if the
    day has since crossed the ceiling. That is the retry path, not a new spend.
    """
    from core.models import AIUsageLedger

    key = str(call_key or "").strip()
    if not key:
        raise ValueError(
            "call_key must not be empty: a stable key is what makes the ledger idempotent."
        )
    if len(key) > 255:
        raise ValueError("call_key must be at most 255 characters.")
    upper_bound = _money(upper_bound_usd)
    moment = now
    day = utc_day(moment)
    enforced = bool(getattr(settings, "AI_SPEND_LIMIT_ENFORCED", True))

    with transaction.atomic():
        _lock_day(day)
        existing = AIUsageLedger.objects.select_for_update().filter(call_key=key).first()
        if existing is not None:
            totals = _aggregate_for_day(day)
            return _decision(
                allowed=True,
                call_key=key,
                reason="",
                upper_bound=existing.reserved_upper_bound_usd,
                totals=totals,
                replay=True,
            )

        totals = _aggregate_for_day(day)
        projected = (totals["committed"] + upper_bound).quantize(MICRO_CENT)
        limit = spend_limit()
        if enforced and projected > limit:
            reason = (
                REASON_CEILING_ALREADY_REACHED
                if totals["committed"] >= limit
                else REASON_RESERVE_EXCEEDED
            )
            return _decision(
                allowed=False,
                call_key=key,
                reason=reason,
                upper_bound=upper_bound,
                totals=totals,
            )

        _row, created = AIUsageLedger.objects.get_or_create(
            call_key=key,
            defaults={
                "day": day,
                "provider": (provider or "")[:64],
                "model": (model or "")[:128],
                "stage": (stage or "")[:64],
                "state": "reserved",
                "reserved_upper_bound_usd": upper_bound,
                "reserved_usd": upper_bound,
                "cost_basis": "reserved_upper_bound",
                "metadata": dict(metadata or {}),
            },
        )
        totals = _aggregate_for_day(day)
        return _decision(
            allowed=True,
            call_key=key,
            reason="",
            upper_bound=upper_bound,
            totals=totals,
            replay=not created,
        )


def _aggregate_for_day(day: date) -> dict:
    """Aggregate the ledger rows belonging to one UTC day.

    The ledger is scoped by its own ``day`` column, not by wall-clock
    ``created_at``: a call authorised at 23:59 and settled after midnight stays
    on the day it was authorised against. Legacy audit rows have no ledger key,
    so they are counted for the same calendar day with an upper bound.
    """
    from datetime import timedelta

    from django.db.models import Count, Q, Sum

    from core.models import AgentDecisionLog, AIUsageLedger

    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    scoped = AIUsageLedger.objects.filter(day=day).aggregate(
        billed=Sum("billed_usd", default=ZERO),
        estimated=Sum("estimated_usd", default=ZERO, filter=Q(state="estimated")),
        reserved=Sum("reserved_usd", default=ZERO),
        unknown_reserved=Sum("reserved_usd", default=ZERO, filter=Q(state="unknown")),
        unknown=Count("id", filter=Q(state="unknown")),
        reserved_rows=Count("id", filter=Q(state="reserved")),
        total=Count("id"),
    )
    legacy = AgentDecisionLog.objects.filter(
        created_at__gte=start, created_at__lt=end, cost_ledger_key=""
    ).aggregate(
        billed=Sum("api_cost_usd", default=ZERO),
        estimated=Sum("estimated_cost_usd", default=ZERO, filter=Q(api_cost_usd__isnull=True)),
        unknown=Count("id", filter=Q(api_cost_usd__isnull=True)),
        total=Count("id"),
    )
    billed = _money(scoped["billed"]) + _money(legacy["billed"])
    estimated = _money(scoped["estimated"]) + _money(legacy["estimated"])
    reserved = _money(scoped["reserved"])
    return {
        "billed": billed,
        "estimated": estimated,
        "reserved": reserved,
        "unknown_reserved": _money(scoped["unknown_reserved"]),
        "unknown": int(scoped["unknown"] or 0) + int(legacy["unknown"] or 0),
        "reserved_rows": int(scoped["reserved_rows"] or 0),
        "ledger_unknown": int(scoped["unknown"] or 0),
        "ledger_rows": int(scoped["total"] or 0),
        "legacy_rows": int(legacy["total"] or 0),
        "committed": (billed + estimated + reserved).quantize(MICRO_CENT),
        "unknown_amount_usd": None,
    }


def settle_call(
    *,
    call_key: str,
    billed_usd=None,
    estimated_usd=None,
    unknown_reason: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    tokens_measured: bool = False,
    metadata: dict | None = None,
    cost_basis: str = "",
) -> object:
    """Reconcile one reservation to billed, estimated or unknown.

    A terminal row is returned unchanged: a redelivered result must not move a
    billed charge into an estimate, nor re-open an unknown charge. ``billed_usd``
    is only ever a provider-reported figure; a price-table number belongs in
    ``estimated_usd``.
    """
    from django.utils import timezone

    from core.models import AIUsageLedger

    key = str(call_key or "").strip()
    if not key:
        raise ValueError("call_key must not be empty.")
    if billed_usd is not None:
        billed = _money(billed_usd)
        estimated = None if estimated_usd is None else _money(estimated_usd)
        state = "billed"
        basis = cost_basis or "billed_provider_reported"
        reason = ""
    elif estimated_usd is not None:
        billed = None
        estimated = _money(estimated_usd)
        state = "estimated"
        basis = cost_basis or "price_table_estimate"
        reason = ""
    else:
        billed = None
        estimated = None
        state = "unknown"
        basis = cost_basis or "unknown"
        reason = (unknown_reason or UNKNOWN_PROVIDER_COST)[:64]

    with transaction.atomic():
        row = AIUsageLedger.objects.select_for_update().filter(call_key=key).first()
        if row is None:
            raise ValueError(f"No reservation exists for call_key {key!r}.")
        if row.state != "reserved":
            return row
        row.state = state
        row.billed_usd = billed
        row.estimated_usd = estimated
        row.unknown_reason = reason
        row.cost_basis = basis
        row.input_tokens = max(0, int(input_tokens or 0))
        row.output_tokens = max(0, int(output_tokens or 0))
        row.tokens_measured = bool(tokens_measured)
        row.settled_at = timezone.now()
        if state == "unknown":
            # The charge is unknown, so the conservative bound stays held. It is
            # still an upper bound, not a measurement of what was charged.
            row.reserved_usd = row.reserved_upper_bound_usd
        else:
            row.reserved_usd = ZERO
        if metadata:
            row.metadata = {**(row.metadata or {}), **metadata}
        row.save(
            update_fields=[
                "state",
                "billed_usd",
                "estimated_usd",
                "unknown_reason",
                "cost_basis",
                "reserved_usd",
                "input_tokens",
                "output_tokens",
                "tokens_measured",
                "metadata",
                "settled_at",
                "updated_at",
            ]
        )
    return row


def mark_unknown(
    *,
    call_key: str,
    reason: str,
    metadata: dict | None = None,
) -> object:
    """Leave a reservation unresolved with a typed reason, keeping its reserve."""
    return settle_call(
        call_key=call_key,
        estimated_usd=None,
        billed_usd=None,
        unknown_reason=reason,
        metadata=metadata,
        cost_basis="unknown",
    )


def reconcile_report(call_key: str, report: dict | None) -> object:
    """Reconcile a reservation from an allocation adapter's ``report()``.

    The adapter's usage block keeps billed and price-table figures separate. The
    billed field is used only when ``billed_cost_complete`` says every call
    reported one; otherwise the price-table figure is booked as an estimate, and
    a report with neither leaves the row unknown with its reserve held.
    """
    usage = dict((report or {}).get("usage") or {})
    billed = usage.get("billed_cost_usd")
    estimated = usage.get("price_table_cost_usd")
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    metadata = {
        "adapter": (report or {}).get("adapter"),
        "cost_basis_from_report": usage.get("monetary_ceiling_basis"),
        "billed_cost_unknown_reason": usage.get("billed_cost_unknown_reason"),
        "calls": int(usage.get("calls") or 0),
    }
    if billed is not None:
        return settle_call(
            call_key=call_key,
            billed_usd=billed,
            estimated_usd=estimated,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens_measured=True,
            metadata=metadata,
            cost_basis="billed_provider_reported",
        )
    if estimated is not None:
        return settle_call(
            call_key=call_key,
            estimated_usd=estimated,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens_measured=True,
            metadata={**metadata, "billed_cost_reported": False},
            cost_basis="price_table_estimate",
        )
    return settle_call(
        call_key=call_key,
        unknown_reason=usage.get("billed_cost_unknown_reason")
        or "allocation_provider_reported_no_cost",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tokens_measured=False,
        metadata={**metadata, "billed_cost_reported": False},
        cost_basis="unknown",
    )


def reconcile_decision_run(call_key: str, run_result) -> object:
    """Reconcile a completed agent run from its :class:`AgentRunResult`.

    The main provider adapter reports tokens but never dollars. That is not a
    zero: when the price-table figure is available it is booked as an
    **estimate** and the billed field stays ``None``. When neither exists the
    row is ``unknown`` with a reason and the reservation stays held.
    """
    billed = getattr(run_result, "api_cost_usd", None)
    estimated = getattr(run_result, "estimated_cost_usd", None)
    input_tokens = int(getattr(run_result, "input_tokens", 0) or 0)
    output_tokens = int(getattr(run_result, "output_tokens", 0) or 0)
    has_usage = bool(
        input_tokens or output_tokens or int(getattr(run_result, "tokens_used", 0) or 0)
    )
    metadata = {
        "source": getattr(run_result, "source", "") or None,
        "cost_basis_from_run": getattr(run_result, "cost_basis", "") or None,
        "tokens_used": int(getattr(run_result, "tokens_used", 0) or 0),
    }
    if billed is not None:
        return settle_call(
            call_key=call_key,
            billed_usd=billed,
            estimated_usd=estimated,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens_measured=bool(getattr(run_result, "tokens_measured", False)),
            metadata=metadata,
            cost_basis="billed_provider_reported",
        )
    if estimated is not None and has_usage:
        return settle_call(
            call_key=call_key,
            estimated_usd=estimated,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens_measured=True,
            metadata={
                **metadata,
                "billed_cost_reported": False,
                "estimate_source": "price_table",
            },
            cost_basis="price_table_estimate",
        )
    reason = getattr(run_result, "cost_unknown_reason", "") or UNKNOWN_PROVIDER_COST
    return settle_call(
        call_key=call_key,
        unknown_reason=reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tokens_measured=bool(getattr(run_result, "tokens_measured", False)),
        metadata={**metadata, "billed_cost_reported": False},
        cost_basis="unknown",
    )


# ---------------------------------------------------------------------------
# Display state
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BudgetState:
    """Today's spend against the ceiling, as shown on the dashboard."""

    spent_usd: Decimal = ZERO
    limit_usd: Decimal = ZERO
    enforced: bool = True
    #: Runs booked today whose provider reported no charge. They are *not* zeros
    #: in ``spent_usd`` and they are why the ceiling is not a strict billed cap.
    unknown_cost_runs: int = 0
    estimated_spend_usd: Decimal = ZERO
    #: Upper bounds currently held for calls that have not settled (and for
    #: calls that settled unknown, whose bound is deliberately kept).
    reserved_usd: Decimal = ZERO
    #: The part of ``reserved_usd`` that belongs to unknown-cost calls.
    unknown_reserved_usd: Decimal = ZERO
    #: Conservative spend = billed + estimated + reserved.
    committed_usd: Decimal = ZERO
    #: Unknown-cost runs whose legacy audit row carries no ledger reservation.
    #: They are counted, not given an invented dollar figure.
    unreserved_unknown_runs: int = 0
    basis: str = "lower_bound"

    @property
    def total_committed_usd(self) -> Decimal:
        """The conservative figure, derived when a caller did not supply it.

        ``BudgetState(spent_usd=10, limit_usd=5)`` is constructed directly by
        callers and older tests; using the default zero committed value there
        would make it look as though nothing had been spent.
        """
        computed = (self.spent_usd + self.estimated_spend_usd + self.reserved_usd).quantize(
            MICRO_CENT
        )
        if self.committed_usd:
            return max(self.committed_usd, computed)
        return computed

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.limit_usd - self.total_committed_usd, ZERO).quantize(MICRO_CENT)

    @property
    def exhausted(self) -> bool:
        """True when new runs must not be dispatched.

        Only meaningful while enforcement is on: with enforcement disabled the
        figure is still tracked and displayed, it simply does not stop anything.
        """
        return self.enforced and self.total_committed_usd >= self.limit_usd

    @property
    def used_pct(self) -> float:
        """Percent of the ceiling consumed, clamped to 0-100 for display."""
        if self.limit_usd <= ZERO:
            return 100.0
        return float(
            min(self.total_committed_usd / self.limit_usd * 100, Decimal("100")).quantize(
                Decimal("0.1")
            )
        )

    def as_dict(self) -> dict[str, str | float | bool | int | None]:
        return {
            # Kept for existing consumers: billed spend only.
            "spent_usd": str(self.spent_usd),
            "limit_usd": str(self.limit_usd),
            "remaining_usd": str(self.remaining_usd),
            "used_pct": self.used_pct,
            "enforced": self.enforced,
            "exhausted": self.exhausted,
            "unknown_cost_runs": self.unknown_cost_runs,
            "estimated_spend_usd": str(self.estimated_spend_usd),
            "reserved_usd": str(self.reserved_usd),
            "unknown_reserved_usd": str(self.unknown_reserved_usd),
            "committed_usd": str(self.total_committed_usd),
            # Null is the only honest value for the unknown dollars: they were
            # never reported. The count and the held reserve are the visible
            # evidence that the total is a lower bound.
            "unknown_amount_usd": None,
            "unreserved_unknown_runs": self.unreserved_unknown_runs,
            "basis": "lower_bound",
            "lower_bound": True,
            "note": (
                "spent_usd is billed only; reserved_usd and estimated_spend_usd are "
                "held/price-table figures. committed_usd = spent + estimated + "
                "reserved is the conservative figure the ceiling is enforced on. "
                "unknown_amount_usd is null because an unreported charge cannot be "
                "quantified."
            ),
        }


def budget_state(*, since=None) -> BudgetState:
    """Current spend, reserved bound, ceiling and enforcement flag in one call."""
    costs = cost_state(since=since)
    billed = Decimal(costs["billed_spend_usd"])
    estimated = Decimal(costs["estimated_spend_usd"])
    reserved = Decimal(costs["reserved_spend_usd"])
    unknown = int(costs["unknown_cost_runs"])
    ledger_unknown = int(costs["ledger_unknown_runs"])
    return BudgetState(
        spent_usd=billed,
        limit_usd=spend_limit(),
        enforced=bool(getattr(settings, "AI_SPEND_LIMIT_ENFORCED", True)),
        unknown_cost_runs=unknown,
        estimated_spend_usd=estimated,
        reserved_usd=reserved,
        unknown_reserved_usd=Decimal(costs["unknown_reserved_usd"]),
        committed_usd=Decimal(costs["committed_spend_usd"]),
        unreserved_unknown_runs=max(0, unknown - ledger_unknown),
    )
