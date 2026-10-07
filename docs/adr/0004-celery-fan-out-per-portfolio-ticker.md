# 0004. Fan out one Celery subtask per portfolio and ticker

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** Project maintainer
- **Related:** [ADR-0009](0009-per-portfolio-sweep-scoping.md) · [ADR-0010](0010-bounded-agent-runs.md)

## Context

A monitoring sweep must run the agent for every instrument in every autonomous
portfolio, on a 15-minute Celery Beat schedule. The obvious implementation is one
task that loops over the tickers and runs the agent inline. Each run includes
several network calls and an LLM debate, so the loop's runtime is dominated by
the slowest provider on the worst instrument.

Rejected alternatives, recorded in the wiki:

- *One task looping over tickers.* Simple, but head-of-line blocking means one
  slow provider response delays the entire sweep, and a single exception aborts
  the remaining instruments.
- *One task per portfolio.* Still couples the instruments within a portfolio.

*Date note: 2026-09-30 is the initial-release commit (`f6f3397`) that shipped this
design; the decision itself predates it and no earlier dated record exists.*

## Decision

We query the autonomous portfolios once and dispatch a Celery `group` containing
one independent `run_alpha_agent_task` per `(portfolio, ticker)` pair, rather than
looping inside a single task. Held tickers are dispatched before the watchlist so
the system can exit positions. Throttling is applied per item and per portfolio
([ADR-0009](0009-per-portfolio-sweep-scoping.md)).

## Consequences

### Positive

- A slow LLM call for one instrument cannot delay the others.
- A failure is contained to one pair; the rest of the sweep completes.
- Retries are safe and per-item.
- Queue depth becomes a direct, observable measure of outstanding work, and the
  sweep scales by adding workers rather than by making one task faster.

### Negative / trade-offs

- It requires per-item throttling. Without it, overlapping sweeps compound
  exposure — the failure that motivated the cooldown design and INC-001.
- Task redelivery is now load-bearing: `acks_late` plus reject-on-worker-lost
  means a lost worker's task is re-run, which is safe only because the cooldown
  prevents the retry from trading twice. Remove the cooldown and a `kill -9` can
  become a double trade.
- The fan-out multiplies LLM spend with watchlist size. Individual runs are
  bounded ([ADR-0010](0010-bounded-agent-runs.md)), but the number of runs is a
  function of how many instruments are watched.
- A Celery group gives no aggregate result or natural barrier, so "the sweep
  finished" is observable only through queue depth, logs and per-item outcomes.

### Neutral

- Cancelling a whole sweep is not supported; each subtask is independent by
  design, which is the same property that contains failures.
