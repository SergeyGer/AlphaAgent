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


## 9. Authenticated submission and advisory review (FIX-08)

The acceptance review found that the documented command still used the stub and
a synthetic paper account, and that no authenticated submission, plan review,
approval or expiry existed. This section is the definition-of-record for that
workflow.

### 9.1 Submission

`POST /api/decision-runs/` is authenticated and scoped to the caller's
portfolio. Body:

```json
{
  "portfolio_id": 7,
  "tickers": ["AAPL", "MSFT"],
  "mode": "live",
  "protocol_version": "portfolio_allocation.v1",
  "idempotency_key": "review-2026-11-12-1"
}
```

* `mode` is `live` (provider adapter) or `stub` (explicit; tests/demos). A live
  request may not carry `stub_response`. Stub mode may carry a scripted
  `stub_response`; without one it submits a deterministic hold request, both
  labelled stub.
* `protocol_version` must equal `portfolio_allocation.v1`.
* The row is written **before** the model call. `request_hash` covers the
  canonical request body, so the same key and body returns the first submission
  even if the model would answer differently, and the same key with a different
  body is **409**.
* Response: **202** with `run_id`, `status_url`, `status_path`, `plan_id`,
  `plan_url`, `idempotent_replay`, and the current submission status. The status
  endpoint is `GET /api/decision-runs/<run_id>/status/`.
* Submission is executed inline in the request because the project cannot
  assume a Celery worker for one portfolio call; a queued task with no worker
  would leave the status at `accepted` forever. The row is committed first, so a
  crash mid-call is visible as `running`.
* Submission creates a `proposed` plan and **no ledger row**. A failed analysis
  is stored as `failed` with a typed error and replayed on retry of the same key;
  it is never retried automatically and never replaced by a stub.

Why the record is an `AllocationSubmission`, not a `DecisionRun`: the existing
`DecisionRun` is per instrument (one ticker, one debate, one guard record). A
portfolio allocation is one decision over the whole portfolio and one plan.
Recording it as N decision runs would duplicate the portfolio call or invent a
ticker. The route is the one the review proposed; the status URL points at the
submission record.

### 9.2 Explicit data setup

Prices come from `services.market_data`; classifications come only from
`InstrumentClassification`. The submission gathers and records provenance:

* a missing held price may fall back to the recorded average cost, labelled
  `average_cost_fallback` and reported as `prices_marked_at_cost`;
* a missing candidate price stays missing; the projection sends any target in
  that name to cash with an `unpriceable_target` deviation;
* missing classifications are reported as `incomplete` coverage and are never
  inferred from a ticker;
* price history is **not fetched** by the endpoint, so `risk_statistics` is
  `not_available`, with the declared `missing_correlation_history` policy. If a
  caller needs correlations, it supplies history to the brief builder
  explicitly.

The review payload carries these as `data_quality.limitations`, not as prose
buried in a log. The live recording command reads the same explicit inputs from
[`fixtures/fix08-live-input.v1.json`](fixtures/fix08-live-input.v1.json) and
labels them `operator_supplied_demonstration`.

### 9.3 Review and approval endpoints

| Route | Purpose |
| --- | --- |
| `GET /api/portfolio-plans/` | Caller's plans, newest first, with approval state |
| `GET /api/portfolio-plans/<id>/` | Full review payload |
| `POST /api/portfolio-plans/<id>/approve/` | Bind an exact version + expiry |
| `POST /api/portfolio-plans/<id>/reject/` | Reject with a required reason |
| `POST /api/portfolio-plans/<id>/cancel/` | Withdraw with an audit row |
| `POST /api/portfolio-plans/<id>/revalidate/` | Re-derive after a material change |
| `POST /api/portfolio-plans/<id>/apply/` | Explicit execution through the guard |

The review payload contains the required review surface: current holdings and
weights, **requested versus constrained** target weights, cash effect, sector
effect including the unclassified part, funding path, estimated costs (with the
NULL-fee disclosure), rejected alternatives, unresolved uncertainties,
deterministic deviations, data-quality limitations and declared policies, the
model provenance and whether the response was a real provider decision or a
stub, the exact plan version and permission hash, the current approval with its
expiry, the audit history, and the execution state with transaction ids.

### 9.4 Approval binding and expiry

`approve_plan` requires `plan_version` and an expiry (`expires_at` or
`expires_in_seconds`, at least 60 seconds and at most
`ALLOCATION_CONFIG["MAX_APPROVAL_TTL_SECONDS"]`, default 7 days). It stores two
records: mutable current-approval fields on the plan (fast checks) and an
immutable `PlanApproval` row (audit). Approval writes no trade.

Approval is bound to:

* `plan_version` — bumped by `revalidate_plan`;
* `approval_hash` — the hash of limits, classifications, cost assumptions and
  the requested allocation;
* `evaluated_portfolio_version` — approval is refused if the portfolio moved
  after evaluation;
* `expires_at` — checked again inside `apply_plan` under the plan lock.

`revalidate_plan` increments `plan_version`, clears the current approval and
records an `approval_invalidated` deviation. The old `PlanApproval` row remains
readable; it simply no longer matches, so a materially changed plan cannot
execute under the old approval. An approval is also refused if the plan is
flagged for revalidation or the portfolio version changed.

Rejection and cancellation create both a `PlanApproval` audit row and a durable
event (`portfolio.plan.rejected` / `portfolio.plan.cancelled`). A repeated
cancellation is idempotent and creates no second audit row.

### 9.5 Execution

`POST /api/portfolio-plans/<id>/apply/` is the only endpoint that can write a
ledger row. It requires a current, unexpired approval for the exact version,
passes an `ApprovalReference` into `apply_plan`, and lets the shared
`services.execution` guard revalidate and write every leg atomically. `live`
mode supplies current quotes; `paper` mode uses the plan's own marks and says so.

### 9.6 The recorded live example

[`fixtures/fix08-live-allocation.v1.json`](fixtures/fix08-live-allocation.v1.json)
is a **real recorded provider run**: one Anthropic call through the budgeted
adapter, `mode: "live"`, `source: "anthropic"`, model `claude-haiku-5-5`, the
exact prompt (and its hash), the validated response, the plan review, token
counts and cost provenance. It is explicitly **not synthetic**:
`synthetic_response: false`, `run_kind: real_recorded_model_run`. The input
brief is the one labelled exception: the holdings/prices/classifications are
operator-supplied demonstration data, and the artifact says so
(`input_data_is_synthetic: true`). The model output is a real sample; it would
not be regenerated by asking again.

The provider reported 5005 input and 3020 output tokens. The billed cost is
`null` because the adapter receives tokens, not dollars; the price-table estimate
is `$0.002010`. The call failed no validation, wrote no ledger row, and left the
plan `proposed` and unapproved.

### 9.7 FIX-08 tests that hold the gate

| Rule | Test |
| --- | --- |
| Authenticated; another user's portfolio is a 404 | `SubmissionApiTests.test_submission_requires_authentication`, `test_another_users_portfolio_is_a_404_not_a_403` |
| 202 + run/status URL, no trade | `test_submission_returns_202_a_run_id_and_a_status_url_without_a_trade` |
| Same key + body replays; same key + different body is 409 | `test_repeating_the_same_key_and_body_returns_the_same_run_without_a_second_plan`, `test_reusing_a_key_with_a_different_payload_is_a_409` |
| Typed payload validation | `test_payload_validation_is_typed` |
| Stub is explicit and labelled | `test_explicit_stub_mode_without_a_scripted_response_is_a_deterministic_hold` |
| Failure is recorded and replayed, not retried or stubbed | `test_a_failed_analysis_is_recorded_and_replayed_not_retried` |
| Review surface completeness | `ReviewAndApprovalTests.test_review_shows_holdings_weights_requested_vs_constrained_cash_sector_costs` |
| Approval version/hash/expiry | `test_approval_binds_the_exact_version_hash_and_expiry`, `test_approval_demands_an_expiry_and_a_version` |
| No approval, no execution | `test_apply_without_approval_is_refused_and_writes_no_ledger_row` |
| Approved plan executes through the shared service | `test_approved_plan_applies_through_the_shared_execution_service` |
| Expired approval cannot execute | `test_an_expired_approval_cannot_execute` |
| Material change bumps version and requires new approval | `test_a_material_change_bumps_the_version_and_requires_new_approval` |
| Rejection/cancellation audit rows and events | `test_rejection_and_cancellation_write_audit_rows_and_events` |
| Provider adapter budgets and usage | `ProviderAdapterTests.*`, `LiveSubmissionIntegrationTests.*` |
| Recorded artifact labels and numbers | `RecordedLiveArtifactTests.*` |
| The UI never presents a stub as a real decision | `frontend/src/components/PortfolioPlansPage.test.tsx` |
