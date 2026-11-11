# Portfolio allocation contract — Phase 4

This is the definition-of-record for the portfolio CIO brief, the target-weight
contract, the deterministic projection, the plan and its execution semantics.
It is written so a reader can tell **what a number is**, **what produced it**,
and **what is null because the data does not exist**.

Phase 4's gate is: *concurrency/constraint tests and paper evaluation reviewed*.
The core rule is that the model proposes weights and the deterministic layer
decides what the portfolio can actually support. The model is never the
optimizer, and no expected return is requested, computed or implied.

## 1. Responsibility (PORT-01)

The CIO role is extended to the **portfolio decision boundary**. One call sees
all holdings, all candidates, the constraints, the theses and the data-quality
flags, and returns one coherent allocation. There is no per-ticker order
sequence and no separate "Portfolio Allocator" persona: adding a second persona
would have duplicated the CIO's judgement without changing the decision.

`services/portfolio_allocation.PortfolioCIO` is the model seam:

```python
client.complete(stage, prompt) -> str
```

Tests and the paper command substitute `StaticAllocationClient`; nothing in the
allocation path imports a provider SDK, so the module runs with no key and no
network.

## 2. The input brief (PORT-02)

`services/portfolio_brief.build_allocation_brief` emits
`portfolio_brief.v1`:

| Block | Contents | Source / honesty rule |
| --- | --- | --- |
| `portfolio_version` | Opaque state version (`pv-…`) | Cash, holdings and last ledger id; a price move does not invalidate a plan |
| `snapshot_id`, `captured_at`, `analysis_cutoff` | One `InputSnapshot` | Holdings, cash and quotes come from that single snapshot |
| `equity_usd`, `cash`, `weights` | `E = cash + Σ quantity × price` and weights (incl. `CASH`) | `equity_usd` is `null` when a holding cannot be priced at all; it is never 0 |
| `holdings[]` | quantity, average cost, price, value, weight, `sector`, `asset_class`, `classification_source` | Sector/class is only from `InstrumentClassification`; a missing row is `null` |
| `exposure` | sector values/weights, asset-class values/weights, unclassified value/weight, coverage | `coverage.status: incomplete` when any valued position is unclassified; unknown is not a 0% sector |
| `limits` | Per-name, sector, total exposure, turnover, cash buffer, per-trade fraction | From the `Portfolio` row; the policy version is recorded |
| `transaction_costs` | `linear_bps.v1`, bps, fixed, minimum, source | A declared assumption; not a broker fee |
| `theses[]` | Open `PositionThesis` rows | Labeled with status and opened-at |
| `candidate_cases[]` | Latest completed `DecisionRun` per ticker, or an explicit supplied case | `run_id`, `mode`, action, rationale, objection, claims; `evidence_sufficient` per case |
| `claim_catalog[]` | Claims the model is allowed to cite | Unknown claim ids are rejected by the target contract |
| `risk_statistics` | Deterministic covariance, correlation, variance | `covariance.sample_daily_returns.v1`, daily simple close-to-close, pairwise common observations; every value publishes its observation count |
| `data_quality` | Typed flags, snapshot age, stale flag, sector coverage, correlation/evidence flags, policy actions | Each flag is visible; the policy action is recorded |

`risk_statistics` is `null` with a reason where history is insufficient:

* no history supplied → `status: not_available`, no pairs, `no_history` flag;
* fewer than `COVARIANCE_MIN_OBSERVATIONS` (default 20) returns → the pair is in
  `missing_pairs` with `insufficient_history` and its observation count;
* fewer than two instruments → `insufficient_instruments` (a correlation needs
  a pair);
* zero variance → covariance is still reported, correlation is `null` with
  `zero_variance`;
* no annualisation: the observations are date-aligned rather than a guaranteed
  regular series, and annualising would invent a frequency.

**No expected returns.** The brief has no expected-return field. The prompt
forbids the model from inventing one, and `services.portfolio_targets` rejects a
response containing `expected_returns`, `expected_return_pct`, `correlations`,
`covariance`, `volatility` or similar keys with `invented_risk_statistics`.

**No sector inference.** `InstrumentClassification` is a per-portfolio explicit
lookup. Existing databases have no rows, so their brief says the sector exposure
is incomplete. A ticker string is never parsed or mapped to a sector: a confident
wrong answer is worse than an explicit absence.

## 3. Target-weight contract (PORT-03)

`services.portfolio_targets.validate_target_proposal` validates
`portfolio_targets.v1`:

```json
{
  "schema_version": "portfolio_targets.v1",
  "portfolio_version": "<must equal the brief>",
  "target_weights": {"AAPL": "0.20", "CASH": "0.35"},
  "actions": [{"ticker": "AAPL", "action": "increase",
               "rationale": "...", "claim_ids": ["..."]}],
  "funding_source": "cash | sales | cash_and_sales | none",
  "rationale": "...",
  "rejected_alternatives": [{"ticker": null, "reason": "..."}],
  "unresolved_uncertainties": ["..."],
  "data_sufficiency": "sufficient | insufficient"
}
```

* `CASH` is mandatory; weights are decimal strings, not floats, and every weight
  is `>= 0` (long-only, unlevered).
* The request must sum to 1 within **0.000001**. It is then **normalised** to
  exactly 1 at six decimals: risky weights round down and CASH takes the
  residual. The stored `target_weights` therefore sum exactly to `1.000000` and
  cannot drift on serialisation.
* Every instrument with a target weight or a current holding needs exactly one
  action. An increase must cite at least one claim id from the brief; an action
  that contradicts the weights (`increase` for a reduction, etc.) is rejected.
* `maintain` / `no_change` are valid actions, and a plan that holds the current
  weights and trades nothing is a valid plan (`PlanStatus.NO_CHANGE`). Cash is an
  allowed allocation, not a failure.
* A changed `portfolio_version` is `ProposalStaleError` and is **not** repairable
  by re-prompting: the state or the number would not change.

## 4. Deterministic projection and sizing (PORT-04)

The model's weights are a **request**, not the portfolio. The projection runs in
`services.portfolio_targets.constrain_targets` under
`deterministic_constrained_projection.v1`:

1. Targets for instruments with no price go to cash (`unpriceable_target`).
2. Evidence policy from the brief is applied: `block_new_risk` removes increases;
   `retain_cash` raises the cash floor to at least the current cash weight,
   removing increases first and scaling risky weights only if necessary.
3. Per-name ceiling: weight above `max_ticker_concentration_pct` is clamped, and
   the difference goes to cash.
4. Sector ceiling: a sector above `max_sector_concentration_pct` is scaled to the
   ceiling. If that sector was **already** over the ceiling before the plan, new
   positions in it are **declined first** (the acceptance scenario); existing
   names are then reduced. A sector brought over the ceiling only by this plan is
   scaled to the cap, not refused outright.
5. Total exposure and cash buffer: the enforceable risky maximum is
   `min(max_portfolio_exposure_pct, 100 - min_cash_buffer_pct)`. The excess goes
   to cash; it is never redistributed to another risky name.
6. Turnover: `Σ |target_weight − current_weight|` must stay within
   `max_turnover_pct`. The changes are scaled toward the current portfolio; no
   trade is scaled beyond its requested size.
7. The limits are scaled by a conservative **cost reserve** so the post-cost
   weights remain inside the policy limits. The reserve is the declared cost on
   the requested turnover (which is at least the executed turnover) plus fixed
   per-leg charges, capped at 5%. It is an estimate, not a fee.

Every clamp/scaling is recorded in `deviations` with the requested value, the
applied value and the binding limit. A deficit goes to **cash**, never to another
name. `constraints` records the method version, objective version, inputs,
solver identity, covariance method version, every check and any
`not_evaluated` sector (unclassified exposure).

`services.portfolio_targets.derive_legs` then converts the constrained weights
into orders:

* prices are quantised to cents; quantities floor to the lot size (default 1) and
  round **down**, never up;
* a SELL never exceeds the held quantity; a BUY includes the estimated cost and
  must fit both the cash-buffer floor and the live per-trade ceiling
  (`max_trade_allocation_pct` of available cash);
* sells are sequenced before buys; full exits are `funding`, other reductions are
  `risk_reducing`, buys are `new_risk` or `rebalance`;
* the cash available to buys includes **planned** net sale proceeds. It is not a
  claim that a sale has executed: apply revalidates and applies every selling leg
  before any buying leg, and halts on failure;
* a post-trade arithmetic check runs on the rounded legs. If rounding or costs
  would take a dimension over its limit, buys in that dimension are trimmed
  (recorded as `post_trade_trimmed`) until the plan is inside the limit. A
  pre-existing breach is reported as `existing_breach`, not hidden; a plan that
  would *increase* past a limit is refused rather than emitted.

Costs are `linear_bps.v1` planning estimates. The execution guard has no broker
fee source, so **`Transaction.fee` stays `NULL`**; the plan's
`estimated_cost_usd` is not debited from the ledger. `NULL` means no fee was
recorded and is not a zero fee (the Phase 3 contract).

## 5. Plan execution (PORT-05)

`PortfolioPlan` and `PortfolioPlanLeg` are the record. A plan references exactly
one `InputSnapshot`; its brief, requested weights, constrained weights,
deviations, constraints and model provenance are stored so it can be reviewed
after the portfolio has moved.

`services.portfolio_allocation.apply_plan`:

* locks the plan and the portfolio, and refuses if `revalidation_required` is set
  or the current `portfolio_state_version` differs from
  `evaluated_portfolio_version`;
* processes legs in sequence — sells first — and revalidates **each** leg through
  `services.execution.evaluate_proposal` against the current mark, position,
  realised P&L, cash and `Exposure`;
* requires the guard to approve exactly the planned quantity. A clamp or
  rejection halts the plan, because the remaining funding arithmetic would no
  longer be valid;
* writes every fill through `services.execution.apply_trade`, the only ledger
  mutating path, inside **one database transaction**. Any failure rolls the whole
  application back, marks the plan `failed`, marks the attempted leg `failed`,
  later legs `skipped`, and writes no ledger row. Proceeds from a failed sale are
  therefore never assumed;
* is idempotent: applying an applied plan returns the existing result, and the
  one-to-one `PortfolioPlanLeg.transaction` makes a duplicate fill structurally
  impossible.

A changed portfolio calls for `revalidate_plan`, which re-projects the stored
requested weights against the current holdings, cash and limits, rebuilds the
legs and moves the evaluated version. It records `revalidated` and increments
`revalidation_count`; the original snapshot remains the proposal's provenance.

Idempotency at submission is `submission_key`: a duplicate returns the first
plan and does not create a second plan or legs. The unique constraint on
`(portfolio, submission_key)` backstops a concurrent duplicate.

## 6. Null semantics

| Field | `null` means |
| --- | --- |
| `equity_usd` | A held position has no price and no cost basis; weights cannot be computed |
| `holdings[].sector` / `asset_class` | No explicit classification exists. Not a default bucket, and never inferred |
| `exposure.coverage.coverage_pct` | No valued positions, so coverage is not applicable |
| `risk_statistics.status` | `not_available` / `insufficient_history` / `insufficient_instruments` |
| `correlation[].value` | Pair below the observation minimum, or zero variance; `missing_pairs` says which |
| `annualisation` / `annualised_volatility` | Not computed; the frequency is date-aligned, not a guaranteed regular series |
| `expected_return*` | Not computed and not requested; the field does not exist in the contract |
| `Transaction.fee` | No fee was recorded. `0` would mean a measured zero |
| `PlanStatus.NO_CHANGE` | The valid decision to trade nothing |

## 7. The paper evaluation

`python manage.py run_allocation --reset --apply` runs the whole pipeline with
`StaticAllocationClient` against
[`fixtures/phase4-allocation.v1.json`](fixtures/phase4-allocation.v1.json). It
prints the brief, the requested and constrained weights, the plan with per-leg
quantities, and the application result. No key, no network, no fetching.

`--fail-leg TICKER` removes that ticker's current mark at apply time. The plan
halts, the transaction rolls back, the command prints the failure and the plan
state, and no ledger row is written.

The fixture is **synthetic**. Its risk statistics are computed from the supplied
synthetic series, but that is engineering/paper evidence, not realised
performance.

## 8. Tests that hold the gate

| Rule | Test |
| --- | --- |
| A favourable candidate is declined in an already over-weight sector | `SectorAcceptanceTests.test_a_favourable_candidate_is_declined_because_its_sector_is_already_over_the_limit` |
| Unknown sector is incomplete, never 0% | `SectorAcceptanceTests.test_sector_exposure_is_reported_as_incomplete_not_zero_when_unclassified`, `BriefTests.test_sector_exposure_is_null_and_incomplete_without_a_classification` |
| Two candidates share finite cash in one plan | `CashCompetitionAcceptanceTests.test_two_attractive_candidates_share_the_limited_cash_in_one_plan` |
| Stale snapshot / missing correlation / weak evidence are labelled and policy-driven | `DataQualityAcceptanceTests.*` |
| Costs and lot rounding leave cash and report deviations | `InfeasibleTargetAcceptanceTests.test_costs_and_rounding_make_a_target_infeasible_without_exceeding_limits` |
| Contract: sum, cash, long-only, claims, stale version, invented statistics | `TargetContractTests.*` |
| Two concurrent plans cannot spend the same cash | `ConcurrencyTests.test_two_concurrent_plans_cannot_both_spend_the_same_cash` |
| Cumulative limits are not bypassed | `ConcurrencyTests.test_cumulative_limits_cannot_be_bypassed_by_two_valid_proposals` |
| Duplicate submission/apply is idempotent | `PlanLifecycleTests.test_repeating_a_submission_key_returns_the_existing_plan`, `test_applying_a_plan_is_atomic_and_a_second_apply_is_idempotent`, `ConcurrencyTests.test_duplicate_concurrent_apply_adds_no_duplicate_ledger_row` |
| A changed version requires revalidation | `PlanLifecycleTests.test_a_changed_portfolio_version_requires_revalidation` |
| A failed leg halts and rolls back | `PlanLifecycleTests.test_a_failed_leg_halts_the_plan_and_writes_no_ledger_row`, `PaperCommandTests.test_the_command_shows_a_leg_failure_and_writes_no_ledger_row` |
