# Methodology — what AlphaAgent measures, and what it does not

This is the document a reviewer reads before quoting a number from this project.
It defines the measurement families, states which engine produced a figure, and
lists the claims the current evidence cannot support. Every factual statement
below points at a file in this repository. Numbers are quoted only from a
committed fixture or report, and the source is named beside them.

Phase 5's gate is: **all public claims trace to reviewed evidence.** This
document is the method behind that gate; [`CLAIMS.md`](CLAIMS.md) is the index of
public claims and their sources.

## 0. Two measurement systems, which are not interchangeable

The repository contains two ways to score a decision, and a reader must not let
the guarantees of one leak into the other.

| | Dashboard attribution (older) | Measurement contract (Phase 3) |
| --- | --- | --- |
| Code | `services/attribution.py` | `services/signal_diagnostics.py`, `services/portfolio_accounting.py`, `services/experiments.py` |
| Produces | a live scorecard per horizon | a versioned experiment report |
| Read at | `GET /api/portfolio/attribution/` and the dashboard panel | `GET /api/experiments/<id>/report/` |
| Committed evidence | none — the scorecard is computed from the live database at read time | [`fixtures/experiments/phase3-engineering.v1.report.json`](fixtures/experiments/phase3-engineering.v1.report.json) |
| Point-in-time protection | The live engine records the measurement basis and reference instant, and the execution path refuses an untimed or stale mark. It does not accept an `as_of`, so it lacks the Phase 3 knowledge-time guarantee. | `PriceBook` separates validity from knowledge time; a mark before the decision is refused |
| Method version on each row | `attribution.v1` / `attribution.v2` on `DecisionOutcome`; `attribution.legacy.v1` when carried into the Phase 3 family | `signal_diagnostics.v1`, `portfolio_accounting.v1` |

The live scorecard is a product feature: it reads `DecisionOutcome` rows that
`services.attribution.evaluate_pending` writes at 1, 7 and 30 days. It is **not**
reproducible from the repository, because the decisions and prices behind it are
not committed. No number from that scorecard is quoted in this document. The
committed report is produced from the three synthetic Phase 1 fixtures and a
synthetic accounting ledger, and is labelled as engineering evidence.

The distinction matters for a specific reason: the dashboard scorecard measures an
executed decision from its linked fill's stored price and timestamp
(`measurement_basis: "execution_fill"`) and a HOLD or rejection from the first
close at or after the proposal (`measurement_basis: "proposal_close"`), via
`resolve_measurement_reference` in `services/attribution.py`. It stores
`measurement_basis`, `reference_at` and `price_observed_at_decision` on the row.
It still does not accept an `as_of` and does not reject a backfilled mark; the
Phase 3 engine refuses a mark whose knowledge time is after the evaluation instant
and refuses an entry mark before the decision. The live engine now has an honest
**basis**, not the newer leakage guarantee. Do not describe the dashboard scorecard
with the Phase 3 method versions.

## 1. Signal diagnostics

Signal diagnostics answer one question: **was the call directionally right, and
what did the instrument do next?** They do not answer the account's return.

### 1.1 The unit of observation

One row per decision × horizon. The horizons are calendar days `1, 7, 30`
(`DecisionOutcome.HORIZONS` in `core/models.py`; `horizons.1_7_30.v1` in
`services/signal_diagnostics.py`). For each row the engine records:

- `forward_return_pct` — the instrument's return from the decision mark to the
  first permitted mark at or after the horizon, computed as
  `((price_at_horizon - price_at_decision) / price_at_decision) * 100` in
  `evaluate_horizon` (`services/signal_diagnostics.py`);
- `action_score_pct` — the same return signed by the action: `BUY` keeps it,
  `SELL` negates it, `HOLD` is `0` by the declared convention
  `hold_zero_convention.v1`;
- `directional_hit` — `forward > 0` for a `BUY`, `forward < 0` for a `SELL`, and
  **null for a HOLD or any non-directional action** (`directional_hit` in
  `services/attribution.py`, reused by the diagnostics engine).

A HOLD's zero is a convention, not a profit-and-loss statement. It is included
in a mean action score only because the convention says so; it is excluded from
the directional mean and from the hit-rate denominator. A flat move is not a hit
for either direction.

### 1.2 What signal diagnostics are not

- **Not portfolio returns.** The unit is an instrument forward return, not a
  marked-to-market account. The two families are separate services, separate
  report blocks and separate method versions; the report's `metric_families`
  block says `summable: false` for diagnostics and gives the reason
  (`docs/spec/fixtures/experiments/phase3-engineering.v1.report.json`).
- **Not additive across overlapping windows.** A 30-day window contains the
  1-day and 7-day windows for the same decision, and two decisions on the same
  ticker overlap. Adding per-decision scores therefore double-counts time and
  capital. `PerDecisionScoreSeries.total()`, `.sum()`, `+` and `sum([series])`
  raise `MetricFamilyError` (`services/signal_diagnostics.py`); the tests are
  `OverlappingDecisionTests` in `core/tests/test_portfolio_accounting.py`.
- **Not a measure of position size.** A 1-share decision and a 1,000-share
  decision produce the same score. Capital weight belongs to portfolio
  performance, not here.
- **Not a directional hit for HOLDs.** Holding makes no directional claim. A
  system that mostly holds has a hit rate computed from the small fraction of
  decisions that did make a claim; the report publishes `evaluated`,
  `directional_calls` and `hold_rows` so the denominator is visible.

### 1.3 Benchmark and aggregation rules

Benchmark returns are matched pairwise: the same decision and the same horizon,
with both returns present. Relative returns use matched pairs only, and the
report publishes `unmatched_benchmark_rows_excluded` so the smaller denominator
is visible. A missing benchmark is null, never zero.

Rows are grouped into `heuristic`, `llm` and `synthetic` cohorts. A heuristic
fallback attempt is a separate producer and is not pooled with a debate run. The
method version is published per slice; a stored legacy row is tagged
`attribution.legacy.v1` rather than given a version invented for it. These rules
are implemented in `aggregate_observations` and `SignalDiagnosticsReport`
(`services/signal_diagnostics.py`) and documented in
[`MEASUREMENT.md`](MEASUREMENT.md) §1.

### 1.4 Proposal versus executed action

Each row carries `proposal_action`, `executed_action` and `execution_status`
(`services/signal_diagnostics.py`). A guard rejection is
`rejected_by_guard`; the row is a counterfactual, excluded from headline
aggregates and reported in the counterfactual block. A clamped or approved
proposal with no linked transaction is `execution_not_recorded`, not proof that
nothing executed. The distinction is tested by
`SignalDiagnosticReportTests.test_proposal_and_executed_action_are_distinct` in
`core/tests/test_experiments.py`.

## 2. Portfolio performance

Portfolio performance answers a different question: **what did the account
earn?** It is computed from cash, holdings, fills, fees, external flows and
marks, never from per-decision scores (`services/portfolio_accounting.py`).

### 2.1 Equity

```text
E(t) = cash(t) + sum(quantity(i,t) * price(i,t))
```

- Positions are replayed from every fill up to `t`. The engine does not need
  average cost for equity.
- Cash is anchored to the current portfolio balance and later effects are
  reversed:

  ```text
  cash(t) = balance_now - sum(cash_effect(event) for event.at > t)
  ```

  The basis is published as `reverse_ledger_from_current_balance`, or
  `declared` when an experiment declares an opening state. It is exact relative
  to the stored balance, not a bank statement; a mismatch appears in
  reconciliation rather than being smoothed away.
- A missing price makes `equity` **null**, never zero; `unpriced_tickers` names
  what is missing (`_value_positions` in `services/portfolio_accounting.py`).
- A `SELL` may only close or reduce a long. An oversell raises
  `NegativePositionError`; it is never silently interpreted as a short.

### 2.2 Marks, validity and knowledge time

`PriceMark` stores the instant a price describes (`at`) separately from the
instant the system learned it (`observed_at`), and `PriceBook` exposes two
deliberately separate lookups:

- `mark_at_or_before` — latest mark valid **and known** at or before the instant,
  used for valuation (`latest_known_mark_at_or_before_valuation.v1`);
- `mark_at_or_after` — first mark valid at or after the instant, used for a
  simulated fill (`first_executable_mark_at_or_after_decision.v1`), with an
  optional `as_of` that also excludes a mark learned after the cut-off.

A mark older than seven days is flagged stale. Fill prices are themselves marks
at the fill instant, so a position can be valued when it is opened without
inventing a separate close.

### 2.3 Fees

`Transaction.fee` is nullable by design:

| Value | Meaning |
| --- | --- |
| `NULL` | No fee was recorded for this fill. Existing rows were not backfilled. |
| `Decimal("0")` | A fee was recorded and it was zero. |

Equity uses recorded fees only. If any fill in the window is `NULL`, the report
sets `fees_complete: false`, counts `unknown_fee_fill_count`, marks the result
approximate and names the reason. It never presents "unrecorded" as "zero".
No current producer writes a fee: the execution guard has no broker-fee source,
so live fills remain `NULL` until an integration records the charge. See
[`MEASUREMENT.md`](MEASUREMENT.md) §2.3 and
`docs/spec/fixtures/experiments/phase3-engineering.v1.json` (one fill has
`"fee": null`).

### 2.4 External flows and time-weighted return

`CashFlow` is a separate additive table (`DEPOSIT` / `WITHDRAWAL`), not a
`Transaction`: `services/ledger.replay_positions` treats every non-BUY
transaction as a SELL, so a deposit row would corrupt positions, and `tx_type` is
`varchar(4)`.

Cash reconciliation publishes every movement:

```text
opening_cash + deposits - withdrawals - buy_notional + sell_notional - recorded_fees
= expected_closing_cash
```

TWR chains sub-period returns at external-flow boundaries:

```text
TWR = product(1 + r_i) - 1, where each sub-period ends at a flow boundary
```

The flow changes the next base but is not itself a return. The example report
demonstrates the distinction: the manifest fixture records a deposit of
`2000.00` into a `10000.00` opening account, and the report publishes
`cumulative_return_pct: "21.5125"` while the flow-chained
`time_weighted_return_pct` is `"1.3737"` — and that TWR is still marked
`approximate: true` for an unrecorded fee
(`docs/spec/fixtures/experiments/phase3-engineering.v1.report.json`).

**When TWR is flagged approximate.** The exact chain requires a mark valid at
each flow boundary (`flow_boundaries[].valuation_exact`). If a boundary can only
be valued from a stale mark, or if the opening or closing equity is unavailable,
the engine falls back to Modified Dietz (`modified_dietz.v1`) or returns null,
sets `approximate: true` and records a reason such as
`flow_boundary_valuation_not_exact`, `flow_boundary_valuation_unavailable`,
`fees_unrecorded_for_some_fills` or `marks_older_than_staleness_threshold`.
Approximate TWR is never labelled exact. The behaviours are pinned by
`TwrApproximationTests` in `core/tests/test_portfolio_accounting.py`.

### 2.5 Drawdown, exposure, turnover and contribution

| Metric | Definition | Source |
| --- | --- | --- |
| Max drawdown | Maximum decline from a prior equity peak over priced observations; peak, trough and `sample_limited` are published | `_max_drawdown`, `services/portfolio_accounting.py` |
| Gross exposure | `sum(quantity * mark_price) / equity`, average and end | `_exposure` |
| Turnover | `sum(abs(quantity * fill_price)) / ((opening_equity + closing_equity) / 2)`; fees are not in the notional | `_turnover` |
| Contribution | For each ticker: `end_value - start_value - buy_notional + sell_notional - recorded_fees`, plus quantities, entries/exits and capital weights | `_contributions` |

Contribution is additive within a position over the window, and the report says
whether it reconciles to the equity change excluding flows. It comes from the
ledger, never from per-decision scores. A per-decision return is not a
contribution to the account.

### 2.6 LLM costs

Provider and API spend is external money, not portfolio cash. It is reported in
`llm_costs` and deducted only in the explicit `net_equity_series` /
`equity_series_net_of_llm` blocks:

- `funding: "external_provider_account"`, `deducted_from_portfolio_cash: false`,
  `double_counted: false`;
- if any run in the experiment reported no cost, the net return is **null** with
  a reason; a missing cost is not a zero cost, so the gross return remains
  computable but the net return is not established;
- `DecisionRun.total_cost_usd` is the source; a failed run's reported cost stays
  in the total.

### 2.7 Reconciliation

`PortfolioSnapshot` stores aggregate cash, positions value and equity, but **no
per-ticker quantities or prices**, and snapshots are not guaranteed at flow
boundaries. It is therefore used only as a reconciliation anchor: computed
equity at the snapshot instant is compared with the stored total and reported as
`matched`, `mismatched` or `no_snapshot` with a cent tolerance.
`InputSnapshot.holdings` is compared with the ledger replay and reported as an
`input_snapshots` mismatch. A mismatch is an error signal, not something the
engine smooths away (`_reconciliation`, `services/portfolio_accounting.py`).

## 3. Why Sharpe is not computed

The report does not publish a Sharpe ratio. `risk_adjusted.sharpe` contains
`value: null`, `included: false`, `frequency: "event_driven"`, `observations: 2`,
`annualization: null`, `risk_free_rate: null`, a `sample_limits` string and this
reason: the valuation frequency is event-driven, not a fixed daily series; a
Sharpe ratio needs a declared frequency, annualisation and risk-free rate, and
the report does not fabricate them. The exact block is in
`docs/spec/fixtures/experiments/phase3-engineering.v1.report.json`.

**What computing it honestly would require**, at minimum:

1. A declared, regular observation frequency (for example, one valuation per
   trading day) and a rule for a missing observation — not an event-driven
   series with irregular gaps.
2. A stated annualisation convention and the number of periods per year, applied
   consistently.
3. A named risk-free instrument and the matching holding-period return, not a
   constant assumed to be zero.
4. A sample large enough for the statistic to mean anything. The example report
   has two sub-period returns; it says so rather than presenting a ratio whose
   denominator would be noise.
5. A decision on whether returns are gross or net of fees and LLM cost, since
   the two series differ.

Until all five exist, a Sharpe number would be arithmetic dressed as evidence.
The test that holds this line is
`BuyHoldSellSequenceTests.test_sharpe_is_explicitly_absent_with_its_disclosure`
in `core/tests/test_portfolio_accounting.py`, alongside the `_risk_adjusted_block`
function in `services/portfolio_accounting.py`.

## 4. Horizon and session policy

### 4.1 Phase 3 measurement contract

| Definition | Version | Where |
| --- | --- | --- |
| Horizons in calendar days: `(1, 7, 30)` | `horizons.1_7_30.v1` | `services/signal_diagnostics.py` |
| Timezone | UTC | `TIMEZONE_POLICY` |
| Session policy | first mark at or after the target session | `first_mark_at_or_after_target_session.v1` |
| Valuation mark | latest known mark at or before valuation | `latest_known_mark_at_or_before_valuation.v1` |
| Simulated fill | first executable mark at or after decision | `first_executable_mark_at_or_after_decision.v1` |
| Counterfactual exit | first known mark at or after horizon | `first_known_mark_at_or_after_horizon.v1` |

A Saturday horizon uses the first session at or after Saturday, normally Monday;
`session_shift_days` records the shift. If no later session exists, the price is
null and the maturity is `unavailable`. The prior close is **never**
substituted: a price before the target is not the target's price.

### 4.2 Why a price earlier than the decision cannot be used

A return is a statement about what an observer could have earned. An entry price
observed before the decision is a price the decision did not act on; using it
would credit the decision with a move it could not have captured. The engine
therefore:

- rejects an entry mark strictly before the decision
  (`assert_price_not_before`, `services/portfolio_accounting.py`);
- refuses a stored horizon price whose `price_observed_at` is before the
  decision, marking the row `unavailable` with `price_observed_before_decision`
  (`_observation_from_outcome`, `services/signal_diagnostics.py`);
- refuses an outcome evaluated after the report's `as_of`
  (`future_evaluation_excluded`) and a price observed after `as_of`
  (`future_price_excluded`);
- never falls back to the prior close for a missing session.

The tests are `PriceLeakageTests` and `WeekendMaturityTests` in
`core/tests/test_portfolio_accounting.py`, plus
`SignalDiagnosticReportTests.test_an_outcome_evaluated_after_as_of_is_excluded`
in `core/tests/test_experiments.py`.

### 4.3 The older dashboard engine is weaker

`services/attribution.py` measures an executed decision from the linked fill's
stored price and timestamp, falls back to the first close at or after the decision
date for a HOLD or rejection, and finds the horizon close with
`_close_on_or_after`. It records `measurement_basis` and `reference_at`, and the
scorecard publishes `portfolio_performance: false` and `summable: false`. It does
not accept an `as_of` and does not reject a backfilled mark. That is why this
document does not attribute the Phase 3 leakage guarantee to the dashboard
scorecard. The scores are still signed by the action and HOLDs still have a null
directional hit, but the point-in-time guarantee belongs to the Phase 3 engine.
The fill-based correction is pinned by
`test_p1_attribution_remediation.py::test_the_unmocked_start_price_is_the_recorded_fill_not_the_proposal_close`.

## 5. The counterfactual rule for a guard-rejected proposal

A rejected proposal has no fill and no portfolio return. It is **not** measured
by default. It is measured only when the experiment manifest declares a
counterfactual rule:

```json
{"holding_period_days": 7, "horizons_days": [1, 7, 30],
 "quantity_basis": "requested_quantity",
 "exit_rule": "first_known_mark_at_or_after_horizon.v1"}
```

`evaluate_counterfactual` (`services/portfolio_accounting.py`) then reports the
hypothetical entry and exit, P&L, return, the assumptions (fill rule, exit rule,
quantity basis, no slippage/market impact, no fees) and a **horizon
sensitivity** across `horizons_days`. `confidence_interval` is null with the
reason that one observed price path cannot support a distributional interval.
Without a declared rule the result is `established: false` with
`reason: "no_declared_holding_exit_rule"`.

**Why a rejection alone does not establish that the guard prevented a loss.** A
rejection records only that a proposal was not executed. Whether it would have
lost money depends on an entry price, a holding period and an exit price that the
rejection does not contain. Choosing them after seeing the outcome would be
hindsight. Without a pre-declared rule, "the guard prevented a loss" is an
assertion, not a measurement; even with one, the result is a single hypothetical
path, not evidence that the guard improves returns.

The example report publishes `counterfactuals.rows: []`,
`established_count: 0`, `not_established_count: 0` and
`rejected_proposal_count: 0`: the synthetic fixture contains no guard-rejected
proposal, so there is no counterfactual to inspect. The rule is exercised by
`CounterfactualTests` in `core/tests/test_portfolio_accounting.py` and
`SignalDiagnosticReportTests.test_a_rejected_proposal_is_a_counterfactual_not_executed_performance`
in `core/tests/test_experiments.py`.

A guard **clamp** is not a rejection: the allowed quantity is what may execute. The
difference between requested and allowed is worth reporting, but it is not a
counterfactual and it cannot establish loss prevention either. A clamped proposal
with no linked transaction is `execution_not_recorded`, not proof that the allowed
size traded.

## 6. What cannot be claimed from the current evidence

These are prohibited claims, not cautious suggestions. Each is enforced by a
field in the committed report or by the absence of a committed source.

1. **No profitability claim.** The report sets `no_profitability_claim: true`,
   `profitability_threshold: null` and `engineering_validation_only: true`. The
   synthetic run's positive accounting return is a fixture, not a result.
2. **No comparative protocol claim.** V4 is declared and unimplemented
   (`variants` in `docs/spec/fixtures/experiments/phase3-engineering.v1.json`;
   the report publishes it as `unimplemented` with a null result). V2 and V3
   share the Phase 2 producer, so a V2-versus-V3 difference is not a protocol
   difference. The report's `sample_integrity.minimum_usable_sample.met` is
   `false`: one usable observation against a pre-registered minimum of two. The
   report's `fairness.comparable` field is `true` — the freeze checks match — but
   `fairness.comparable_for_protocol_claims` is `false` because the content is
   synthetic. A matched freeze is not a licence to compare protocols.
3. **No market-evidence claim from synthetic content.** All three stored
   decision runs are `mode: "synthetic"`, `source: "fixture"`, and the accounting
   ledger is declared `manifest_fixture`. The report says
   `strict_historical_claims: false`, `synthetic_content` is a limitation, and
   its own `limitations` block says no comparative or profitability claim may be
   drawn from it.
4. **No live recorded decision case is committed.** The Phase 2 protocol can
   produce live runs, but no live run is stored in the repository. Every curated
   case in [`CASES.md`](CASES.md) is a synthetic fixture and is labelled as such.
   A claim about how the live protocol behaved in production is not supported by
   a committed artefact.
5. **No Sharpe ratio, annualised volatility or distributional interval.** The
   report publishes the absence and the reason; see §3 and §5.
6. **No verified benchmark interval.** Stored `OutcomeRecord` rows carry a
   benchmark return but no benchmark observation timestamps, so the report
   declares the same-decision/same-horizon rule and sets
   `benchmark_interval_verified: false`; it does not claim an interval match it
   cannot re-derive.
7. **No claim that the guard prevented a loss.** See §5. The committed report
   contains zero counterfactual rows.
8. **No expected-return or optimizer claim.** The Phase 4 contract has no
   expected-return field; `services/portfolio_targets` rejects an invented
   `expected_returns`, `volatility`, `correlation` or `covariance` key. The
   allocation layer is a deterministic projection, not a mean-variance
   optimizer (`docs/spec/ALLOCATION.md` §2 and §4).
9. **No realised-fee claim.** Every live fill's `Transaction.fee` is `NULL`
   because no broker fee source exists. The allocation's transaction costs are
   planning estimates under `linear_bps.v1`, not recorded charges.
10. **No claim that the demo account is a track record.** The demo's seeded
    portfolio and the paper allocation are demonstration data. The Phase 4
    fixture is synthetic; its covariance and correlation are computed from a
    supplied synthetic series, not from market history.

## 7. Known limitations

Listed without euphemism. Each is a property of the current repository, not a
future intention.

**Evidence and sample**

- The only committed decision runs are three hand-authored synthetic fixtures
  (`docs/spec/fixtures/decision-runs.v1.json`). No live protocol transcript is
  committed.
- The example experiment schedules four runs: three complete, one has no stored
  output, and only one observation is usable for the primary metric against a
  pre-registered minimum of two. The report says
  `engineering_validation_only: true`.
- The three instruments are large-cap US equities selected by hand. Delisted,
  acquired and failed companies are absent, so the sample is
  survivorship-biased (`honesty.survivorship_bias` in the manifest).
- Pretraining contamination is unknown and unmeasured: the stored runs called no
  model, and no model or pretraining window is recorded.
- The report's bootstrap interval is null because there are fewer than two
  observations; no precision is claimed.

**Measurement**

- The dashboard scorecard (`services/attribution.py`) is not reproducible from
  the repository: it reads live `DecisionOutcome` rows and price history. Its
  numbers can change between reads and are not a committed report.
- Legacy attribution records no `price_observed_at`; the Phase 3 report therefore
  marks those rows `price_timestamp_unverified`.
- The benchmark interval is declared, not re-derived; `benchmark_interval_verified`
  is false.
- The example report's TWR is approximate because one fill has an unrecorded
  fee. The report's `methodology.approximate` is `true` and
  `approximate_reasons` names `fees_unrecorded_for_some_fills`.
- A `PortfolioSnapshot` cannot value a historical position by itself: it stores
  aggregate cash and positions value, not per-ticker quantities or prices.
- Cash is reconstructed by reversing the ledger from the current balance; it is
  exact relative to the stored balance, not a bank statement.
- The exact TWR chain requires a mark valid at every flow boundary. When one is
  missing, the fallback is Modified Dietz and the figure is approximate.
- No fee is recorded by any live producer. The fee contract exists so a future
  integration is additive, not because fees are currently measured.
- Turnover excludes fees from the notional by definition; it is not a
  transaction-cost measure.

**Allocation (Phase 4)**

- Sector classification is an explicit `InstrumentClassification` lookup. A
  database with no rows reports sector exposure as **incomplete**, not 0%, and
  the default policy is `label_only` for unknown sectors and missing correlation
  history. No sector is inferred from a ticker.
- Correlation and covariance are descriptive inputs, not used to size positions
  directly. There is no optimizer and no expected-return objective.
- Transaction costs in a plan are estimates and are not debited from the ledger,
  so actual cash after application can exceed the plan's post-cost estimate.
- Plan application is atomic: a failed leg rolls the whole plan back. A reviewer
  expecting partial application will see `failed`, not `partially_applied`.
- No HTTP endpoint or frontend was added for the allocation plan; the stored plan
  JSON and the paper command's output are the review surface.
- V4 remains unimplemented in the experiment manifest.

**Operational**

- The Phase 3 accounting engine is exercised against a manifest-declared ledger
  and a stored-ledger path; it has no broker integration.
- Market data comes from yfinance, whose historical coverage is uneven. There is
  no backtest, and a backtest that silently skips missing data is deliberately
  not shipped (`docs/wiki/Roadmap.md`).
- The Sharpe ratio, annualised volatility and any distributional uncertainty
  interval are absent for the reasons in §3 and §5, not because they were
  forgotten.

## 8. Source index

Every file cited above, and what it establishes.

| Path | What it establishes |
| --- | --- |
| [`MEASUREMENT.md`](MEASUREMENT.md) | Definition-of-record for the two metric families, accounting, experiments and the report |
| [`ALLOCATION.md`](ALLOCATION.md) | Definition-of-record for the Phase 4 brief, target contract, projection and plan execution |
| [`FIXTURES.md`](FIXTURES.md) | Fixture schema, honesty rules, loader behaviour and the Phase 4 paper fixture |
| [`fixtures/decision-runs.v1.json`](fixtures/decision-runs.v1.json) | The three synthetic recorded decision cases, their claims, rebuttals, guard record and outcomes |
| [`fixtures/experiments/phase3-engineering.v1.json`](fixtures/experiments/phase3-engineering.v1.json) | The synthetic experiment manifest: variants, runs, accounting ledger, counterfactual rule and honesty declarations |
| [`fixtures/experiments/phase3-engineering.v1.report.json`](fixtures/experiments/phase3-engineering.v1.report.json) | The example report: separation, counts, TWR, fees, risk-adjusted disclosure, limitations and claim boundary |
| [`fixtures/phase4-allocation.v1.json`](fixtures/phase4-allocation.v1.json) | The synthetic Phase 4 brief, candidate cases, classifications and price series |
| [`fixtures/phase4-allocation.v1.output.json`](fixtures/phase4-allocation.v1.output.json) | The captured paper run: requested versus allowed weights, deviations and applied legs |
| `services/signal_diagnostics.py` | Horizon evaluation, action scores, directional hits, cohorts and non-summation |
| `services/portfolio_accounting.py` | Equity, marks, TWR, fees, flows, drawdown, turnover, contribution, reconciliation and counterfactuals |
| `services/experiments.py` | Manifest validation, variant publication, fairness, eligibility and report assembly |
| `services/attribution.py` | The older dashboard scorecard: action signing, HOLD null, start-price choice |
| `services/portfolio_allocation.py` | Phase 4 plan creation and atomic application |
| `services/portfolio_targets.py` | Target validation, deterministic projection and leg derivation |
| `core/models.py` | `DecisionOutcome.HORIZONS`, `Transaction.fee` nullability and the ledger models |
| `core/decision_models.py` | Decision-run, claim, rebuttal, guard and outcome records |
| `core/tests/test_portfolio_accounting.py` | Accounting, leakage, TWR, fee, flow, overlap, Sharpe-absence and counterfactual tests |
| `core/tests/test_experiments.py` | Report, variant, fairness, sample and counterfactual-report tests |
| `docs/adr/0014-decision-outcome-attribution.md` | The decision to score outcomes, and what the score does not prove |
| `docs/adr/0015-cumulative-exposure-and-concentration-limits.md` | Why cumulative limits exist and what they still do not bound |
| `docs/adr/0017-constrained-portfolio-projection.md` | Why allocation is a projection rather than an optimizer |
