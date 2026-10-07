# 0012. Cap daily AI spend, separately from the trading limits

- **Status:** Accepted
- **Date:** 2026-10-07
- **Deciders:** Project maintainer
- **Related:** [ADR-0001](0001-deterministic-execution-guard.md) (the trading guard this complements), [ADR-0006](0006-fail-loudly-fail-open.md) (the configuration principle applied here), [ADR-0010](0010-bounded-agent-runs.md) (per-run bounds)

## Context

The execution guard limits what the system may **trade**: a per-trade allocation
ceiling, a daily loss limit, a position-size clamp. Nothing limited what the
system may **cost**.

That gap mattered because of how the system is scheduled. Celery Beat sweeps
every portfolio and every ticker on an interval. Each sweep calls a language model
— three agents, several tool round-trips, thousands of tokens per pair. An
unattended deployment therefore had no upper bound on its API bill. The failure
mode was not dramatic: the guard would keep doing its job, refusing trades on risk
grounds, while the meter kept running. Nothing in the system would stop, and
nothing in the dashboard would mention money being spent.

Two things made this urgent rather than theoretical. The first was measuring real
spend: a single debate run cost $0.15, and the default sweep runs across multiple
tickers every fifteen minutes. The second was a defect fixed earlier in the
project's life, where one portfolio's "Run agent" button dispatched the *global*
sweep and spent every other portfolio's model budget. That fix removed the
immediate bug but left the underlying exposure — one actor can trigger spend that
belongs to everyone.

The system already had the data to do something about it. Costs are recorded per
decision, computed from the real per-bucket token split, so today's spend is a
single aggregate over rows that already exist.

## Decision

**We enforce a hard daily ceiling on language-model spend, defaulting to a
non-zero value, checked before any run is dispatched, and displayed live on the
dashboard.**

Specifically:

- The ceiling is `AI_DAILY_SPEND_LIMIT_USD`, defaulting to **$5.00**.
- The default is deliberately non-zero. A ceiling of `0` is indistinguishable
  from "disabled", and a spending control that silently defaults to off is worse
  than none because it implies a protection that is not there.
- Spend is derived by aggregating `AgentDecisionLog.api_cost_usd` since midnight
  UTC. There is no separate counter, so the figure cannot drift from the audit
  trail.
- The check runs *before* a run is dispatched and *before* the ticker throttle is
  claimed, so a halted run does not consume a cooldown slot it will not use. It
  is never checked mid-run: a run that starts finishes and writes its audit row.
- Reaching the ceiling writes an `AgentDecisionLog` row and emits a
  `budget.exhausted` event, so the halt is auditable in the same place as every
  other decision rather than only in the worker log.
- The dashboard shows remaining budget, spent-of-limit, and a progress bar, and
  updates over the existing WebSocket on every run.
- `AI_SPEND_LIMIT_ENFORCED=false` tracks and displays spend without halting, for
  demonstrations.

## Consequences

### Positive

- An unattended deployment can no longer produce an unbounded bill. The worst
  case is bounded by the ceiling times the number of days it runs.
- Spend is visible while it happens, next to the trading limits, rather than
  arriving as an invoice.
- Because the figure is recomputed from the audit trail rather than incremented
  in memory, it is correct across multiple workers and survives a restart.
- The halt is auditable. "Why did the agent stop?" has a row in the same table as
  every trade decision, with the exact figures.
- No new state to keep consistent: the ceiling is a setting and a query.

### Negative / trade-offs

- **The ceiling is global, not per-portfolio.** On a multi-tenant deployment one
  portfolio's heavy use can exhaust the budget for all of them. This is the same
  class of issue as the dispatch bug fixed earlier, narrowed rather than solved.
  Per-portfolio ceilings would address it and are not implemented.
- **The reset is midnight UTC**, not a rolling window. Spend at 23:50 and again
  at 00:10 falls either side of the boundary, so two consecutive days can each
  approach the ceiling back to back.
- **The check is coarse.** It runs once per (portfolio, ticker) pair, so a run
  that starts just under the ceiling can overshoot by the cost of that run
  (measured: about $0.15). The ceiling bounds the overshoot, it does not prevent
  it.
- The dashboard's figure is only as fresh as the last event or REST resync. It is
  not a live meter to the cent.
- The payload gained one database query — a `SUM` over an indexed `created_at`
  returning a single row. The API test that bounds query counts was raised from 6
  to 7 to record this deliberately rather than let it drift.

### Neutral

- `AI_SPEND_LIMIT_ENFORCED=false` exists mainly so the demo can run without
  hitting the ceiling. It is a real footgun if left off in production, which is
  why the default is on and the dashboard distinguishes "enforced" from "not
  enforced" in the metric's own subtitle.
- The setting is read once at process start, so changing it requires a restart of
  the worker. Making it hot-reloadable was not worth the complexity for a value
  expected to change rarely.
