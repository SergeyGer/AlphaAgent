"""Keeping a position's thesis current as the position changes.

The rule this module enforces is narrow on purpose: **an open position has exactly
one live thesis, written by the agent that opened it, and it is closed when the
position is.** Everything else here is bookkeeping around that.

Why it exists at all: the crew runs with ``memory=False`` (ADR-0007), so every
sweep started from nothing. The agent could sell at 10:00 and rebuy at 10:15
without noticing, not because it changed its mind but because it had no record of
having one. A single durable paragraph per position fixes that without giving the
crew a conversational memory that would grow unbounded and make decisions
irreproducible.

Superseded theses are kept, not overwritten. ``one_open_thesis_per_portfolio_ticker``
allows any number of closed rows, so the table reads as a history of what was
believed and when - which is more useful than a single mutable field, and is what
makes "the thesis changed" auditable.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import PositionThesis, ThesisStatus

logger = logging.getLogger("alphaagent.thesis")

__all__ = ["close_thesis", "current_thesis", "record_thesis"]

ZERO = Decimal("0")


def current_thesis(portfolio, ticker: str) -> PositionThesis | None:
    """The live thesis for an instrument, if there is one."""
    return PositionThesis.open_for(portfolio, ticker)


def record_thesis(
    *,
    portfolio,
    ticker: str,
    thesis: str,
    invalidation: str = "",
    decision=None,
) -> PositionThesis | None:
    """Open or refresh the thesis for a position.

    Called after a BUY is executed. If a live thesis exists it is **superseded**
    rather than edited: the old row is closed and a new one opened, so the history
    of what was believed survives. Editing in place would silently rewrite the
    past, which is the one thing an audit trail must not do.

    A blank thesis is not recorded. The agent is asked for one on every BUY, and
    an empty string means it did not provide one - writing a placeholder would
    put words in its mouth that would then be fed back to it as its own prior
    belief.
    """
    text = (thesis or "").strip()
    if not text:
        logger.info("No thesis supplied for %s; nothing recorded", ticker)
        return None

    symbol = (ticker or "").upper()
    if not symbol:
        return None

    with transaction.atomic():
        existing = PositionThesis.objects.select_for_update().filter(
            portfolio=portfolio, ticker=symbol, status=ThesisStatus.OPEN
        )
        for previous in existing:
            previous.status = ThesisStatus.CLOSED
            previous.closed_at = timezone.now()
            previous.close_reason = "Superseded by a new thesis on the same position."
            previous.closed_by = decision
            previous.save(update_fields=["status", "closed_at", "close_reason", "closed_by"])

        opened = PositionThesis.objects.create(
            portfolio=portfolio,
            ticker=symbol,
            thesis=text,
            invalidation=(invalidation or "").strip(),
            opened_by=decision,
            status=ThesisStatus.OPEN,
        )

    # Returned outside the transaction rather than from inside it. A `return`
    # nested in a `with` leaves the function ending on the `with` rather than on a
    # return, which reads to both a linter and a human as a path that can fall
    # through. CodeQL reports it as py/mixed-returns. Keeping the commit point and
    # the return separate removes the ambiguity and makes the transaction scope
    # explicit.
    return opened


def close_thesis(*, portfolio, ticker: str, reason: str = "", decision=None) -> int:
    """Close the live thesis for an instrument. Returns how many were closed.

    Called when a SELL empties the position: the reason for holding no longer
    applies to anything, and leaving it open would feed a stale belief into the
    next run as though it were current.
    """
    symbol = (ticker or "").upper()
    if not symbol:
        return 0

    closed = PositionThesis.objects.filter(
        portfolio=portfolio, ticker=symbol, status=ThesisStatus.OPEN
    ).update(
        status=ThesisStatus.CLOSED,
        closed_at=timezone.now(),
        close_reason=(reason or "Position closed.")[:2000],
        closed_by=decision,
    )
    if closed:
        logger.info("Closed %s thesis row(s) for %s", closed, symbol)
    return closed


def sync_after_trade(
    *, portfolio, ticker: str, position_amount, decision=None, thesis="", invalidation=""
):
    """Bring the thesis in line with the position after an executed trade.

    One entry point rather than two calls at the call site, so a future branch
    cannot close a position without closing its thesis - which is precisely the
    kind of omission that leaves a stale belief being fed to the crew.
    """
    if position_amount is not None and Decimal(str(position_amount)) <= ZERO:
        return close_thesis(
            portfolio=portfolio,
            ticker=ticker,
            reason="Position fully closed by a SELL.",
            decision=decision,
        )
    return record_thesis(
        portfolio=portfolio,
        ticker=ticker,
        thesis=thesis,
        invalidation=invalidation,
        decision=decision,
    )
