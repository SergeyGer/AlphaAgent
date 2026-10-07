# 0009. Scope sweeps and throttling per portfolio

- **Status:** Accepted
- **Date:** 2026-10-05
- **Deciders:** Project maintainer
- **Related:** [ADR-0001](0001-deterministic-execution-guard.md) · [ADR-0004](0004-celery-fan-out-per-portfolio-ticker.md) · [ADR-0006](0006-fail-loudly-fail-open.md)

## Context

The throttle and the on-demand sweep were originally global. One debounce key
covered every fan-out, and `POST /api/portfolio/run-agent/` dispatched the global
Beat sweep. With a single demo portfolio that looked harmless; with more than one
it is wrong in two ways:

- one user's click could spend every other autonomous user's LLM budget, because
  the endpoint dispatched a sweep across all portfolios;
- a global Beat sweep could silently debounce an unrelated on-demand sweep for one
  portfolio — and the endpoint still answered `202` as though it had dispatched.

Both were found during the documentation review recorded in the incident log's
"eight further defects", and are now pinned by regression tests. The date above is
the fix commit (`02febfa`).

## Decision

Scope the work and the throttle keys by portfolio:

- the fan-out debounce key is `alpha:sweep:debounce:{portfolio_id}` for scoped
  callers, while the scheduled Beat path keeps the unscoped key;
- the ticker cooldown is namespaced per `(portfolio, ticker)` —
  `alpha:cooldown:{portfolio_id}:{TICKER}` — so two users holding the same
  instrument never block each other;
- `POST /api/portfolio/run-agent/` calls `dispatch_market_sweep(portfolio_id)` for
  the caller's portfolio only, inline rather than via `.delay()`, and reports what
  actually happened: `202` with the real dispatch count, `429` + `Retry-After: 60`
  when debounced, `409` when the portfolio is not autonomous.

## Consequences

### Positive

- A sweep for one portfolio can no longer spend another portfolio's LLM budget, so
  cost is attributable to whoever requested it.
- A global scheduled sweep and an on-demand sweep for one portfolio no longer
  debounce each other; two portfolios never debounce each other.
- The API response is truthful. `202` means work was dispatched, `429` means it was
  not — previously the endpoint reported success for a discarded request.
- The fix is covered by regression tests, so the behaviour is stated rather than
  assumed.

### Negative / trade-offs

- Two key spaces (scoped and unscoped) now exist, and which one applies depends on
  the caller. A future un-scoped global path would reintroduce the original defect.
- Because portfolios no longer share a debounce, two portfolios watching the same
  ticker both pay for an analysis of that ticker. Suppressing that duplication was
  never a goal, but the global key did it incidentally.
- The throttle still bounds only *overlapping* runs. A deliberate sequence of
  non-overlapping trades remains unbounded; a cumulative daily deployment ceiling
  is still on the roadmap.
- Each portfolio adds its own cooldown keys to the shared Redis cache, which is
  also the market-quote cache; flushing that database clears both.

### Neutral

- The scheduled Beat sweep intentionally keeps the unscoped key: it is one sweep
  for all portfolios, and it should not be blocked by one portfolio's manual
  trigger.
