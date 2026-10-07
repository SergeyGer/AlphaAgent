# Roadmap

What is deliberately not built yet, and why. Several of these are ideas the
system's own incident history argues for — those are marked.

---

## Next

### Cumulative daily deployment ceiling
**Motivated by [INC-001](Incident-Log#inc-001--overlapping-sweeps-bought-the-same-stock-twice).**

The per-ticker cooldown bounds *overlapping* runs, but nothing bounds a
deliberate sequence of individually-permitted trades. Two sweeps three seconds
apart each respected the per-trade ceiling and together doubled the intended
exposure.

The remaining gap is a portfolio-level cap: total capital deployed per day,
enforced the same way the per-trade ceiling is — in the execution guard, as a
pure function, not in a prompt.

*Why not done:* it changes the guard's signature (it needs today's deployment
total as an input), and that is a change worth making deliberately rather than
alongside the throttling work.

### Backtesting against historical data
The agent layer produces a decision for a point in time. Replaying a historical
period would answer the question the project cannot currently answer: *would this
strategy have made money?*

*Why not done:* yfinance's historical coverage is uneven for the instruments in
use, and a backtest that silently skips missing data is worse than none. It needs
a data source with a stated completeness guarantee before the results mean
anything.

### Deterministic replay of a recorded decision
The audit trail stores both arguments, the verdict and the context. A replay mode
would re-run the guard against a recorded proposal and confirm it still produces
the same verdict — proving the risk behaviour is reproducible rather than merely
tested.

*Why not done:* needs a serialised form of the evaluation context. Straightforward,
but it is a testing facility rather than a feature, and it competes for priority
with the deployment ceiling above.

---

## Later

### Per-user strategy configuration
Risk profile, watchlist and limits are currently portfolio-level. A user model
would let several strategies run concurrently against separate portfolios.

*Why not done:* the multi-tenant boundaries exist (every query is ownership-scoped)
but there is no reason to rush into a user-facing configuration surface before
there is more than one user.

### Hosted demonstration instance
A read-only public deployment would let a reviewer explore the dashboard without
running Docker.

*Why not done:* this is a system that executes trades. A public instance needs
hard isolation — a sandboxed broker, no outbound LLM spend, seeded data — and
getting that wrong is worse than not having a demo. The recorded
[demo](https://github.com/SergeyGer/AlphaAgent#see-it-running) covers the same
ground safely.

### Additional asset classes and providers
The ledger already handles fractional quantities to eight decimal places for
crypto. Extending to other instruments is mostly a matter of additional market
data adapters behind the existing tool interface.

*Why not done:* no verified data source with the same reliability as the current
one.

---

## Explicitly not planned

**Leverage, margin or short selling.**
The entire risk model assumes losses are bounded by the cash balance. Supporting
leverage invalidates the guard's core invariants — particularly the daily-loss
asymmetry, where de-risking is always permitted because it cannot increase
exposure. That assumption does not survive margin.

**Replacing the deterministic guard with a learned model.**
This is the one decision that is not open for reconsideration. See
[[Engineering-Decisions|ADR-001]]. A model can be argued with, influenced, or
upgraded underneath you; a pure function cannot.

**Autonomous execution without an audit trail.**
Every run writes a decision log, including HOLDs and rejections. There is no
configuration flag to disable it, and there will not be one.

---

## How this list is ordered

By what the system's own evidence argues for, not by what is most interesting to
build:

1. **INC-001** is the only incident in the log where the *design* was incomplete
   rather than the implementation. A cumulative ceiling is the remaining half of
   that fix.
2. Replay and backtesting both increase confidence in behaviour that is currently
   only unit-tested.
3. Everything else is a feature, and features are cheaper to add than trust is to
   recover.

## Related pages

- [[Incident-Log]] — the evidence behind the ordering
- [[Engineering-Decisions]] — the decisions these items would revisit
- [[Execution-Guard]] — where a deployment ceiling would live
