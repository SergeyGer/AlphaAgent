# 0017. Allocate capital with a constrained projection, not an optimizer

- **Status:** Accepted
- **Date:** 2026-11-11
- **Deciders:** Project maintainer
- **Related:** [ADR-0001](0001-deterministic-execution-guard.md) (the guard this layer feeds), [ADR-0015](0015-cumulative-exposure-and-concentration-limits.md) (which recorded the missing sector limit), [ADR-0016](0016-persistent-position-thesis.md) (theses carried into the brief)

## Context

Phase 4 asks the CIO to compare all candidates at a portfolio boundary and
produce one capital allocation. Two designs were possible:

1. **A numerical optimizer.** Give it expected returns, a covariance matrix and a
   risk-aversion parameter, solve for mean-variance weights, and let the model
   explain the result.
2. **A constrained projection.** Let the model propose weights, then have
   deterministic code project that request into the set the portfolio can support.

The first is superficially attractive and cannot be done honestly with this
system's data. There is no expected-return model; the data model had no sector,
no asset class and no covariance history before Phase 4. A mean-variance
optimizer fed invented expected returns produces precise-looking weights whose
inputs nobody can defend, and the model cannot be blamed for them because the
optimizer, not the model, chose the weights. The specification also says not to
ask the model to invent expected returns, correlations or risk statistics.

A second design question was whether to add a separate "Portfolio Allocator"
persona. The CIO already adjudicates candidates; a second persona would duplicate
that judgement and make it unclear which role is accountable for the allocation.
PORT-01 asks for extended responsibility, not more personas.

## Decision

**We extend the CIO to the portfolio boundary and treat its output as a
request that a deterministic projection constrains.**

- `PortfolioCIO` makes one model call with every holding, candidate, limit,
  thesis and data-quality flag in the brief. The prompt forbids inventing
  expected returns, correlations and risk statistics; the target contract rejects
  responses containing them.
- `deterministic_constrained_projection.v1` applies evidence policy, per-name,
  sector, total-exposure, turnover and cash-buffer limits, in that order, and
  sends every deficit to cash. It never redistributes a deficit to another risky
  name.
- `derive_legs` converts the constrained weights into lot-rounded, cost-aware
  orders, with sells sequenced before buys.
- The plan records the method version, objective version, inputs, solver identity,
  covariance method version and every deviation. The model is the explainer of
  candidate selection; the projection is the allocator of last resort.
- Sector is an explicit `InstrumentClassification` lookup, not an inferred field.
  Missing classification is reported as incomplete coverage, never 0%.

## Consequences

### Positive

- Every weight can be traced to a request, a constraint and a recorded deviation.
  A reviewer can see that a candidate was declined and which limit declined it.
- The system never depends on an expected-return number it does not have.
- The execution guard remains the only route to the ledger. The allocation layer
  adds constraints; it does not bypass them.
- A model failure or a malformed response cannot produce an unchecked trade: the
  target contract and the deterministic projection run before any leg exists.
- A plan is applied atomically. Sells execute and revalidate before buys, and a
  failed sale cannot fund a purchase.

### Negative / trade-offs

- **The allocation is not optimal in any mathematical sense.** It is feasible and
  explainable, not mean-variance efficient. There is no objective function to
  optimise, and no claim of one.
- **Sector enforcement depends on classification coverage.** A portfolio with no
  classifications has an incomplete sector view and the sector ceiling is
  reported as not evaluable for those names. Importing classifications is an
  operational prerequisite, not something the code guesses.
- **The projection is a sequence of caps, not a joint solution.** A turnover
  limit can force changes toward the current portfolio and then re-trigger a cap;
  the implementation iterates to convergence and falls back to the current
  weights if it cannot. That fallback is safe but can propose no change when a
  more clever solution might exist.
- **Costs are estimates.** `Transaction.fee` stays `NULL` because there is no
  broker fee source. The plan reserves a cost, but the ledger does not debit it;
  actual cash can therefore be slightly higher than the post-cost estimate.
- **A changed portfolio version refuses the apply.** The caller must invoke
  `revalidate_plan`. Automatic revalidation would silently change the decision
  the model made.
- **There is no HTTP endpoint or frontend in this phase.** The output contract is
  the stored plan JSON and the paper command. A dashboard consumer still has to be
  built.

### Neutral

- Three additive `Portfolio` limit fields (`max_sector_concentration_pct`,
  `max_turnover_pct`, `min_cash_buffer_pct`) are exposed by the existing portfolio
  serializer. Existing field names and values are unchanged.
- V4 in the experiment manifest remains declared and unimplemented: Phase 4
  delivers the service, not a measured experiment arm.
