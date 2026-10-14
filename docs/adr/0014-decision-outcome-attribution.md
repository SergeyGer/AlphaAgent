# 0014. Score decisions against outcomes, not just record them

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Project maintainer
- **Related:** [ADR-0001](0001-deterministic-execution-guard.md), [ADR-0002](0002-adversarial-three-agent-debate.md), [ADR-0007](0007-provider-agnostic-llm-with-heuristic-fallback.md)

## Context

The project's claim is that it can prove, months later, exactly why it made every
decision it made. It could. It could not say whether any of them were any good.

`AgentDecisionLog` recorded the action, the sentiment, the reasoning, the bull
case, the bear case and the exact token cost of every run. Nothing recorded what
happened next. A `SELL` that avoided a twenty-percent drawdown and a `SELL` that
missed a twenty-percent rally were, in that table, identical: both well-reasoned
decisions with cited evidence.

That made three things impossible. The system could not be evaluated, only
described. A change to the prompts or the debate could not be shown to help or
hurt. And the most natural question a reader asks — "is it any good?" — had no
answer anywhere in the repository.

It also meant the deterministic fallback engine and the live LLM could not be
compared, even though the project carries both.

## Decision

**We score every decision against what the market did next, at fixed horizons,
and publish the aggregate.**

Specifically:

- `AgentDecisionLog` gains a real `ticker` column. The ticker was previously
  reachable only through `transaction`, so a HOLD — or a HALT — had none at all,
  and those are the rows most worth reviewing.
- `DecisionOutcome` stores one row per (decision, horizon) for horizons of 1, 7
  and 30 days: the instrument's forward return, what that return was worth *to
  the action taken*, the benchmark's return over the same window, and whether the
  call was directionally right.
- **A decision is scored by what the action was worth, not by what the price
  did.** For a BUY, `decision_return_pct` is the forward return; for a SELL it is
  the negation. Scoring every decision against "did the price rise" would mark
  every correct exit as a failure.
- **A HOLD is not scored as a directional call.** `directional_hit` is `NULL` for
  a HOLD rather than `False`. Holding makes no directional claim, and counting
  those rows as misses would drag the measured hit rate toward zero in proportion
  to how cautious the system was.
- The benchmark is SPY, and a missing benchmark is stored as `NULL`, never as
  zero. An unknown comparison is not a loss.
- Horizons are separate rows rather than one collapsed score, because a call can
  look wrong at one day and right at thirty, and that disagreement is the
  interesting part.
- Scoring runs hourly on Celery Beat, is idempotent, and is aggregated on read
  rather than materialised.

## Consequences

### Positive

- The system is now falsifiable. A prompt change can be shown to help or hurt
  rather than argued about.
- The first real measurement is unflattering, which is the point: a 26.7% one-day
  hit rate across fifteen scored calls, with an average alpha of −0.22% against
  SPY. That number was unavailable before, and its absence was mistaken for
  soundness.
- Every input already existed. `created_at` was indexed, the ticker was one join
  away, and `get_price_history` was already in the codebase.
- Attribution is derived from the audit trail rather than stored beside it, so
  the two cannot disagree.

### Negative / trade-offs

- **The sample is tiny and the horizons are short.** Fifteen scored calls over
  fourteen days says almost nothing statistically, and one-day returns are mostly
  noise. The scorecard shows what it has rather than what would be significant,
  which risks being read as more meaningful than it is.
- **HOLD decisions are invisible to the hit rate.** They are excluded by design,
  but a system that mostly holds shows a hit rate computed from a small fraction
  of its decisions. The `evaluated` count sits next to the rate for that reason,
  and reading the rate without it would mislead.
- **Scoring a SELL by the negation of the forward return ignores what the capital
  did next.** A sold position that would have risen 10% scores −10%, even if the
  proceeds were immediately deployed into something that rose 20%. The measure is
  per-decision and not portfolio-level.
- **Benchmark comparison is naive.** SPY over the same calendar window takes no
  account of beta: a high-volatility crypto position compared to an equity index
  is not a like-for-like comparison, and the alpha figure inherits that.
- One more scheduled task, and it calls yfinance once per pending decision per
  horizon. Hourly rather than on the fifteen-minute sweep to keep that load
  trivial.
- Rows written before the `ticker` column cannot be scored at all. The migration
  recovered 62 from their transactions; 1247 HOLDs and HALTs stay unscorable
  permanently, and no effort was made to guess them.

### Neutral

- `directional_hit` being three-valued (true / false / null) rather than boolean
  is deliberate and flows through the API and the UI. A nullable column is
  slightly more awkward everywhere, and it is the honest model.
- The scorecard is computed on read. The rows are small and indexed, and a
  materialised aggregate would be one more thing to keep in step.
