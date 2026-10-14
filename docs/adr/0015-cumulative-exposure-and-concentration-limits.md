# 0015. Cap cumulative exposure and single-instrument concentration

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Project maintainer
- **Related:** [ADR-0001](0001-deterministic-execution-guard.md) (which listed this as an open gap), [ADR-0009](0009-per-portfolio-sweep-scoping.md), [ADR-0012](0012-daily-ai-spend-ceiling.md)

## Context

ADR-0001 recorded this gap in its own consequences, in these words:

> The ceiling is **per trade**, not cumulative. A deliberate sequence of
> individually-permitted trades is unbounded.

Nothing about that was theoretical. Celery Beat sweeps every autonomous portfolio
every fifteen minutes. Each sweep may buy up to `max_trade_allocation_pct` of
cash — 5% by default. Twenty such buys reach fully invested, and **no single
decision looks wrong**: each one passes the guard legitimately, each is logged
with sound reasoning, and the portfolio ends up with no cash and no
diversification.

INC-001 had already shown the overlapping version of this failure, where two
concurrent sweeps each traded inside their own limit and compounded the real
exposure. The per-portfolio throttle fixed the concurrency; it does nothing about
a sequence.

A second risk was not covered even in principle. Every limit in the guard was
expressed per trade or per day. None of them constrained *concentration*: a
portfolio could hold 90% of its equity in one instrument and pass every check in
the system, because none of them asked how much sat in a single name.

## Decision

**We add two portfolio-level ceilings, evaluated alongside the per-trade limit.**

- `max_portfolio_exposure_pct` (default 90) caps total invested capital as a
  percentage of equity.
- `max_ticker_concentration_pct` (default 30) caps one instrument as a percentage
  of equity.
- Both are percentages of **equity** (cash plus positions at market), not of cash.
  A percentage of cash would let a portfolio with a large position and little
  cash look empty.
- The headroom is **folded into the existing clamp** rather than added as a
  separate rejection. A buy that does not fit the portfolio is sized down to what
  fits, exactly as one that does not fit the per-trade budget already was.
  Rejecting outright would refuse trades the portfolio can comfortably afford,
  just not at the size proposed.
- When a portfolio-level limit is what bound the trade, the guard's note names
  it. "Clamped to respect the ceiling" without saying which ceiling sends the
  reader to the wrong one.
- Both defaults are non-zero and both are enforced at the database level too, as
  range constraints, following the pattern of the existing limits.
- The limits need market values, which the guard cannot fetch without breaking
  its purity. They arrive as an `Exposure` value object; omitting it skips the
  checks.

## Consequences

### Positive

- The sequence attack on the per-trade limit is closed. A sweep that runs
  twenty times cannot invest past 90%.
- Concentration is now bounded, which no previous limit did at all.
- The guard remains a pure function. `evaluate_proposal` still performs no I/O,
  so the safety-critical logic stays testable without a database or a market.
- Clamping rather than rejecting means the system still trades at the size the
  portfolio can support, which is what an investor would expect.
- The binding constraint is named in the guard's note, which lands in the audit
  trail next to the decision.

### Negative / trade-offs

- **Omitting `exposure` silently skips both limits.** The parameter cannot be
  required without breaking the guard's callers and the deployed demo, so a new
  caller that forgets would lose the cumulative limits while every per-trade check
  still passed — the exact failure shape this change exists to remove.
  `ExposureWiringTests` asserts the production callers supply it, which is a test
  standing in for a type.
- **The limits are static.** There is no volatility scaling, no correlation
  adjustment, and no sector limit. Two 25% positions in highly correlated names
  pass both ceilings and are not diversified in any meaningful sense.
- **Equity is computed at market, and unpriced holdings are marked at cost.** For
  a ceiling that is the conservative direction, but it means the ratio moves when
  a price feed fails rather than when the portfolio changes.
- The default of 90% is a judgement, not a derived number. It leaves room to
  trade while preventing full investment, and it could reasonably be 80 or 95.
- Two more settings on every portfolio, and two more constraints in the schema.
- The `min()` across three ceilings makes the clamp note harder to predict: which
  limit bound a trade depends on live values, so a reader comparing two decisions
  may see different explanations for the same proposal size.

### Neutral

- The demo's Streamlit page calls the guard without an `Exposure`, so it exercises
  the per-trade limits only. It says so on the page rather than implying the full
  guard is being demonstrated.
- Concentration defaulting below exposure (30 vs 90) means the concentration limit
  usually binds first for a single name, which is the intended ordering.
