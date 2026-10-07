# 0003. Derive the ledger by replaying transactions instead of storing positions

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** Project maintainer
- **Related:** [ADR-0001](0001-deterministic-execution-guard.md)

## Context

Positions, cost basis and realised profit could be kept as mutable columns updated
on every trade, or recomputed from the transaction history. The system already
depends on the history being complete for the audit trail, so storing derived
state as well would mean two records of the same fact that can disagree.

Rejected alternatives, recorded in the wiki:

- *Mutable position rows.* Fast to read, but every write path must remember to
  update them, and any missed path produces a silent, permanent inconsistency.
- *Event sourcing with no current-state table.* Correct but premature here; the
  replay is fast enough that the extra machinery buys nothing.

*Date note: 2026-09-30 is the initial-release commit (`f6f3397`) that shipped this
design; the decision itself predates it and no earlier dated record exists.*

## Decision

`Transaction` is append-only, and `services/ledger.py` replays that history using
weighted-average cost to derive positions, cost basis and realised P&L for the
current UTC day. No production code updates or deletes a `Transaction`; the only
writes are `Transaction.objects.create()` inside `services/execution.apply_trade()`
and the demo seeder.

The same replay serves Celery's execution path and the REST API, so both read the
same derived state. Historical *value* is the one thing replay cannot reconstruct,
and that is the sole job of the `PortfolioSnapshot` time series.

## Consequences

### Positive

- State cannot drift from its history: there is exactly one source of truth.
- Any past portfolio state can be reconstructed, which is what makes the equity
  curve and the audit trail trustworthy.
- Correcting a bad transaction is a data operation on the history, not a
  reconciliation exercise.
- The guard and the dashboard cannot disagree about a position, because both call
  the same replay.

### Negative / trade-offs

- Cost is O(transactions) per read on hot paths — every guard evaluation replays
  the portfolio's history. Acceptable at this scale; the mitigation at larger
  scale is a materialised snapshot, not mutable current-state columns.
- The append-only rule is a convention enforced by having a single writer and no
  update paths. The database does not forbid an `UPDATE`; correctness depends on
  that discipline holding.
- The history is both the source of truth and an operational burden: it grows
  without bound and needs pruning commands, and `purge_decision_logs` refuses to
  delete rows linked to an executed trade.

### Neutral

- Weighted-average cost is a choice embedded in the replay, not in the schema.
  Switching to FIFO would be a change to `services/ledger.py`, and the stored
  history would not need to change.
