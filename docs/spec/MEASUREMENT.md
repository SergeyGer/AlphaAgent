# Measurement contract — Phase 3

This is the definition-of-record for the two metric families, the portfolio
accounting engine, the experiment manifest and the report. It is written so a
reader can tell **what a number is**, **what produced it**, and **what is null
because the data does not exist** without reading the implementation.

Phase 3's gate is: *accounting fixtures and leakage tests pass*. The core rule
is that signal diagnostics and portfolio performance are separate metric
families. They are never added, averaged or substituted for one another in a
report.

| | Signal diagnostics | Portfolio performance |
| --- | --- | --- |
| Question | Was the call directionally right? | What did the account earn? |
| Inputs | Stored decision outcomes, prices | Cash, holdings, fills, fees, external flows, marks |
| Unit | Instrument forward return / hit rate | Marked-to-market equity and its return |
| Additive? | **No** — overlapping windows, unequal size, HOLDs commit no capital | Within a window, yes; contributions reconcile to equity change |
| Method version | `signal_diagnostics.v1` (+ each row's outcome method) | `portfolio_accounting.v1` |
| A zero means | A HOLD convention, or a measured zero return | A measured zero, never a missing value |

## 1. Signal diagnostics (`services/signal_diagnostics.py`)

### 1.1 Action score and directional hit

| Proposal action | Action score | Directional hit |
| --- | --- | --- |
| `BUY` | instrument forward return | `forward > 0` |
| `SELL` | `-` instrument forward return | `forward < 0` |
| `HOLD` | `0` by convention (`hold_zero_convention.v1`) | **null** — no directional call |

A HOLD's zero is not a P&L statement. It is not counted as a hit or a miss, and
it is included in `mean_action_score_pct` only because the convention says so;
`mean_directional_action_score_pct` excludes it. A flat move is not a hit for
either direction.

### 1.2 Proposal action vs executed action

Every row carries both:

* `proposal_action` — what the CIO proposed (`DecisionRecord.action`, or the
  normalised legacy `action_taken` when no decision record exists).
* `executed_action` — what reached the ledger, resolved through the guard and
  the linked transaction.
* `execution_status` — `executed`, `hold_no_trade`, `rejected_by_guard`,
  `execution_not_recorded`, or `not_executed`.

A guard **rejected** proposal is a counterfactual: its row has
`measurement: "counterfactual"`, is excluded from the headline aggregates, and
appears in the report's `counterfactuals` block. It has no transaction and never
enters portfolio equity. A guard **clamped** or approved proposal with no linked
transaction is `execution_not_recorded`, not proof that nothing executed.

### 1.3 Horizons, sessions and benchmarks

| Definition | Version |
| --- | --- |
| Horizons in calendar days: `(1, 7, 30)` | `horizons.1_7_30.v1` |
| Timezone: UTC | — |
| Session policy: first mark at or after the target session | `first_mark_at_or_after_target_session.v1` |
| Benchmark match: pairwise, same decision and horizon, both returns present | `pairwise_same_horizon_forward_returns.v1` |

A Saturday horizon uses Monday's first session; `session_shift_days` records the
shift and `price_at_horizon` is null if no later session exists. The prior close
is **never** substituted. `evaluate_horizon` also refuses a mark whose knowledge
time is after the requested `as_of`, and rejects entry marks strictly before the
decision.

Benchmark rules are declared in the manifest:

* equity portfolios use `SPY` (a declared equity benchmark, not an index assumed
  to be the same thing);
* mixed portfolios must declare a `selection_rule` for an asset-appropriate
  benchmark.

Stored `OutcomeRecord` rows carry a benchmark return but **not benchmark
observation timestamps**. The report therefore declares the same-decision,
same-horizon rule, sets `benchmark_interval_verified: false`, and displays the
outcome's `method_version`. It does not claim an interval match it cannot
re-derive.

### 1.4 Aggregation rules

* `evaluated` counts rows whose maturity is `evaluated` and whose score exists.
* `pending` / `matured`, `unavailable` and `not_recorded` are reported by count;
  they are never given a zero score.
* `hit_rate_pct` = hits / directional calls. It is `null` when there are no
  directional calls; 0% would read as total failure.
* Relative returns use **matched pairs only**. `unmatched_benchmark_rows_excluded`
  publishes how many rows were left out, so the smaller denominator is visible.
* Rows are grouped by cohorts: `heuristic`, `llm`, `synthetic`. Heuristic
  fallbacks are not pooled with LLM runs.
* `method_versions` is published per slice; a legacy `DecisionOutcome` row is
  tagged `attribution.legacy.v1` rather than given a version invented for it.
* There is **no total return** in this family. `PerDecisionScoreSeries.total()`,
  `.sum()`, `+` and `sum([series])` raise `MetricFamilyError`; the report's
  family block says `summable: false` with the reason.

A stored horizon price observed before its decision is rejected as leakage
(`price_before_decision_rejected`). An outcome evaluated after the report's
`as_of` is shown as unavailable (`future_evaluation_excluded`), not scored.

## 2. Portfolio accounting (`services/portfolio_accounting.py`)

### 2.1 Equity

```text
E(t) = cash(t) + Σ quantity(i,t) × price(i,t)
```

* **Positions** are replayed from every fill up to `t` (weighted quantities; the
  engine does not need the average cost for equity).
* **Cash** is anchored to the current portfolio balance and later fills/flows are
  reversed:

  ```text
  cash(t) = balance_now - Σ cash_effect(event) for event.at > t
  ```

  The basis is published as `reverse_ledger_from_current_balance`, or `declared`
  when the experiment declares an opening state. It is exact relative to the
  stored balance, not a bank statement; a mismatch is visible in reconciliation.
* A SELL may only close or reduce a long. An oversell raises
  `NegativePositionError`; it is never silently interpreted as a short.
* A missing price makes `equity` **null**, never zero. `unpriced_tickers` names
  what is missing.

### 2.2 Prices and marks

`PriceMark` separates **validity** (`at`) from **knowledge** (`observed_at`).
`mark_at_or_before(t)` returns only marks valid and known at `t`; `at_or_after`
returns only marks valid at or after a timestamp and (when `as_of` is supplied)
known by the cut-off. The declared policies are:

| Policy | Version |
| --- | --- |
| Valuation mark | `latest_known_mark_at_or_before_valuation.v1` |
| Simulated fill | `first_executable_mark_at_or_after_decision.v1` |
| Exit in a counterfactual | `first_known_mark_at_or_after_horizon.v1` |

Marks older than seven days are flagged stale. Fill prices are themselves marks
at the fill instant, so a position can be valued when it is opened without
inventing a separate close.

### 2.3 Fees

`Transaction.fee` is nullable by design:

| Value | Meaning |
| --- | --- |
| `NULL` | **No fee was recorded for this fill.** Existing rows were not backfilled. |
| `Decimal("0")` | A fee was recorded and it was zero. |

Equity uses recorded fees only. If any fill in the window is `NULL`, the report
sets `fees_complete: false`, counts `unknown_fee_fill_count`, marks the result
approximate, and says why. It never presents "unrecorded" as "zero". No current
producer writes a fee — the execution guard has no broker-fee source — so live
fills are `NULL` until an integration records the charge. The column and the
contract exist so that change is additive.

### 2.4 External flows

`CashFlow` is a separate additive table (`DEPOSIT` / `WITHDRAWAL`). It is not a
`Transaction`: `services.ledger` treats every non-BUY transaction as a SELL, and
`tx_type` is `varchar(4)`. Keeping every transaction a trade preserves the
ledger's meaning.

Cash reconciliation publishes every movement explicitly:

```text
opening_cash + deposits - withdrawals - buy_notional + sell_notional - recorded_fees
= expected_closing_cash
```

A `matched_with_unrecorded_fees` status means only that the known movements
reconcile; it does not turn an unrecorded fee into zero.

Time-weighted return chains sub-period returns at flow boundaries:

```text
TWR = Π (1 + r_i) - 1, where r_i ends at an external-flow boundary
```

The flow changes the next base but is not itself a return. A deposit of 5000
into a 10000 account therefore gives `cumulative_return_pct = 50` and
`time_weighted_return_pct = 0`.

An exact boundary valuation requires a mark valid **at** the flow timestamp
(`flow_boundaries[].valuation_exact`). If the boundary can only be valued from a
stale mark, the engine falls back to Modified Dietz, sets
`method: "modified_dietz.v1"`, `approximate: true`, and records
`flow_boundary_valuation_not_exact`. If no valuation is possible at all, the
return is null with a reason. Approximate TWR is never labelled exact.

### 2.5 Drawdown, exposure, turnover and contributions

| Metric | Definition |
| --- | --- |
| Max drawdown | Maximum decline from a prior equity peak over priced observations; `peak_at`, `trough_at`, `sample_limited` published |
| Gross exposure | `Σ quantity × mark_price / equity`, average and end |
| Turnover | `Σ abs(quantity × fill_price) / ((opening_equity + closing_equity) / 2)` — fees are not in the notional |
| Contribution | For each ticker: `end_value - start_value - buy_notional + sell_notional - recorded_fees`; plus quantities, entries/exits, capital weight at each fill and average capital weight |

Contribution is additive within a position over the window, and the report says
whether the total reconciles to equity change excluding flows. It comes from the
ledger, never from per-decision scores. A per-decision return is not a
contribution to the account.

### 2.6 LLM costs

Provider/API spend is **external money**, not portfolio cash. It is reported in
`llm_costs` and deducted only in the explicit
`net_equity_series` / `equity_series_net_of_llm`:

* `funding: "external_provider_account"`, `deducted_from_portfolio_cash: false`,
  `double_counted: false`.
* If any run in the experiment reported no cost, the net return is **null** with
  `net_return_reason`. A missing cost is not a zero cost, so the gross return
  remains computable but the net return is not established.
* `DecisionRun.total_cost_usd` is the source; a failed run's reported cost stays
  in the total.
* If costs are incomplete, `net_return_pct` is null and
  `known_cost_only_return_pct` is the arithmetic from the costs that were
  reported, labelled as such. The net equity series remains visible so the
  deduction can be audited without pretending it is complete.

### 2.7 Reconciliation

`PortfolioSnapshot` stores aggregate cash, positions value and equity — **no
per-ticker quantities or prices**. It cannot value a historical position on its
own. The report therefore uses it only as an anchor: the computed equity at the
snapshot instant is compared with the stored total and reported as `matched`,
`mismatched` or `no_snapshot` with a cent tolerance. `InputSnapshot.holdings` is
the only stored per-instrument quantity; the report compares it with the ledger
replay at the snapshot instant and reports `input_snapshots` mismatches. A
mismatch is an error signal, not something the engine smooths away.

### 2.8 Counterfactuals

A rejected proposal has no fill and no portfolio return. It is measured only if
the manifest declares a counterfactual rule:

```json
{"holding_period_days": 7, "horizons_days": [1, 7, 30],
 "quantity_basis": "requested_quantity",
 "exit_rule": "first_known_mark_at_or_after_horizon.v1"}
```

The result reports the hypothetical entry/exit, P&L, assumptions, and a
**horizon sensitivity range**. `confidence_interval` is `null` with the reason
that one observed price path cannot support a distributional interval. Without a
declared rule, `established: false` and `reason:
"no_declared_holding_exit_rule"`. Loss prevention is never established by the
rejection alone.

## 3. Experiments (`services/experiments.py`)

### 3.1 Variants (EXP-01)

| Key | Label | Engine | Protocol | Implemented |
| --- | --- | --- | --- | --- |
| `V0` | Deterministic baseline | deterministic | `heuristic_fallback.v1` producer, no LLM | yes |
| `V1` | One analyst | `single_agent` | `single_agent_baseline.v1` | yes |
| `V2` | Current pipeline | `debate` | `debate.v1` | yes |
| `V3` | Explicit rebuttal | `debate_with_rebuttal` | `debate.v1`, run must show a recorded rebuttal round | yes |
| `V4` | Portfolio allocation | `portfolio_allocation` | — | **no**; declared, not measured |

Phase 2 delivered rebuttals inside `debate.v1`, so V3 is identified by the
recorded rebuttal round rather than a second producer. A manifest must declare
all five variants; V4 cannot be marked implemented. A report publishes V4 as
`unimplemented` with a null result — not a measured zero.

### 3.2 Manifest (version 1, schema 1.0)

Top level:

| Field | Required | Meaning |
| --- | --- | --- |
| `manifest_version` | yes | `1` |
| `schema_version` | yes | `"1.0"` |
| `experiment_id` | yes | Stable natural key; replay updates this experiment |
| `label`, `description`, `generated_for` | yes / no / no | Human metadata |
| `declared_at` | yes | Pre-registration timestamp (UTC) |
| `observation_window` | yes | `{start, end}` UTC |
| `evaluation_mode` | no | `historical` (default) or `forward` |
| `as_of` | no | Historical cut-off for outcomes; defaults to report build time |
| `primary_metric` | yes | `{id, family, horizon_days, direction, description}` |
| `sample_requirements` | no | `minimum_scheduled_runs`, `minimum_completed_runs`, `minimum_observations` |
| `cost_ceiling_usd`, `acceptable_drawdown_pct` | no | Pre-registered gates |
| `benchmark` | no | `{ticker, asset_class, selection_rule}`; equity defaults to SPY |
| `uncertainty` | no | Bootstrap method, block size, resamples, confidence, seed |
| `frozen` | no | Instruments, dates, starting state, risk rules, execution assumptions, snapshot hashes |
| `variants` | yes | Exactly V0–V4, with optional model configuration and budget |
| `runs` | yes | Every scheduled observation, including ones with no stored output |
| `accounting` | no | `stored_ledger` (default) or `manifest_fixture` |
| `honesty` | no | Synthetic declarations, survivorship and pretraining disclosures |
| `checksum` | no | `{algorithm, scope, canonicalisation, value}`; validated if present |

Each `runs[]` entry has `run_key`, `variant`, `scheduled_at`, and either
`decision_run_id` or `fixture_id`. A reference that does not resolve is stored as
`not_executed`; it remains in the denominator. A stored run that failed is
`failed` and remains in both completion and cost denominators.

`accounting.source: "manifest_fixture"` declares an explicit synthetic ledger in
the manifest (`opening_state`, `fills`, `flows`, `marks`, `llm_costs`). It is
labelled synthetic in the report and cannot be presented as realised history.
`stored_ledger` reads `Transaction`, `CashFlow`, `PortfolioSnapshot`,
`InputSnapshot`, `OutcomeRecord` and `DecisionRun` rows.

The checksum is SHA-256 over the whole manifest with the `checksum` member
removed, canonicalised as sorted-key compact JSON in UTF-8. A drifted manifest is
refused by the command unless `--force` is passed:

```bash
python - <<'PY'
import json
from pathlib import Path
from core.decision_json import content_hash

path = Path("docs/spec/fixtures/experiments/phase3-engineering.v1.json")
manifest = json.loads(path.read_text(encoding="utf-8"))
payload = {k: v for k, v in manifest.items() if k != "checksum"}
manifest.setdefault("checksum", {})
manifest["checksum"]["value"] = content_hash(payload)
path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
```

### 3.3 Build and replay

```bash
python manage.py build_experiment
python manage.py build_experiment --path docs/spec/fixtures/experiments/phase3-engineering.v1.json
python manage.py build_experiment --username demo --force
```

The command reads stored rows and the manifest. It imports no agent or protocol
module, makes no model call and fetches no market data. Re-running it rebuilds
the report from stored rows and increments `report_version`; it does not create
a second experiment for the same `(portfolio, experiment_id)`.

### 3.4 Fair comparison (EXP-02)

`fairness` publishes a check per frozen dimension:

| Check | What it compares | Status values |
| --- | --- | --- |
| `instruments` | Declared universe vs observed runs/fills | match / mismatch / not_declared |
| `dates` | Runs inside the declared window | match / mismatch / not_verifiable |
| `portfolio_starting_state` | Declared cash/positions vs the accounting opening state | match / mismatch / not_verifiable |
| `risk_rules` | Declared limits vs the portfolio's current configuration | match / mismatch / not_declared |
| `execution_assumptions` | Declared guard policy version vs recorded guards | match / mismatch / not_verifiable |
| `benchmark` | Declared benchmark vs observed outcome benchmarks | match / mismatch / not_verifiable / declared_not_verifiable |
| `snapshots` | Declared snapshot hashes vs stored content hashes | match / mismatch / not_declared |

It also reports `equal_opportunity` (scheduled runs per implemented variant),
`equal_budget` (declared caps and observed costs), per-variant model
configuration checks, and `comparable_for_protocol_claims`. Synthetic content
sets that last flag to false even if the freeze checks pass.

Because historical risk configuration is not versioned, `risk_rules` compares
against the current portfolio and says so. A match is necessary, not sufficient.

### 3.5 Point-in-time eligibility (EXP-03)

A run is eligible for strict historical claims only when:

* it has an `InputSnapshot`;
* the snapshot's `analysis_cutoff` is at or before the decision it informed;
* no evidence item was available after that cutoff;
* the content is not synthetic and not a heuristic fallback;
* the experiment is `historical`, not `forward`.

The report also discloses `survivorship_bias` and `pretraining_contamination`
from the manifest; absent disclosures are reported as unknown. Synthetic runs,
missing snapshots and late evidence make `strict_historical_claims: false` with
each reason listed. That is a statement about eligibility, not a silent caveat.

### 3.6 Uncertainty and claims (EXP-05)

The primary metric is pre-registered in the manifest. Its estimate is
accompanied by a seeded **time-block bootstrap**:

* observations are ordered by decision time and resampled in contiguous blocks,
  because adjacent decisions share market conditions and may overlap;
* `block_size`, `resamples`, `confidence` and `seed` are published;
* the interval is a percentile interval and is `null` with a reason when there
  are fewer than two observations or when an approximate TWR cannot be
  decomposed into resamplable sub-periods;
* every variant's primary metric and interval are published, including the null
  ones.

The report publishes gates against the pre-registered sample, cost ceiling and
acceptable drawdown. It asserts **no profitability threshold**
(`profitability_threshold: null`, `no_profitability_claim: true`). A sample below
the declared minimum sets `engineering_validation_only: true`.

Sharpe is not computed. `risk_adjusted.sharpe` says so explicitly and discloses
the frequency (`event_driven`), annualisation (`null`), risk-free assumption
(`null`) and sample limits, rather than presenting a ratio whose annualisation
would be invented.

## 4. Report shape

`GET /api/experiments/<id>/report/` returns the stored report, authenticated and
scoped to the caller's portfolios. An unbuilt experiment returns `409`; another
user's experiment is a `404`. Reading it never calls a model or rebuilds
accounting.

| Block | Contents |
| --- | --- |
| `report_schema_version`, `generated_at`, `report_version` | Document identity |
| `experiment` | id, label, window, evaluation mode |
| `manifest` | versions, checksum, source path, honesty, benchmark, accounting source |
| `metric_families` | The two families, method versions and the non-summation rule |
| `signal_diagnostics` | rows, summaries by horizon, cohorts, proposal-vs-executed, unresolved, counterfactual rows |
| `counterfactuals` | Rejected proposals measured under a declared rule, with uncertainty |
| `portfolio_performance` | equity series, net-of-LLM series, TWR, drawdown, exposure, turnover, fees, contributions, reconciliation, methodology, risk-adjusted disclosure |
| `variants` | Every variant, counts/failure rate, operational metrics, diagnostics summary, evidence, action changes, primary metric, and why variant portfolio performance is null |
| `sample_integrity` | counts, denominator, date range, asset universe, source mode, cohorts, unresolved observations, sample gate, eligibility |
| `fairness` | freeze checks, equal opportunity/budget, model configurations |
| `uncertainty` | pre-registration, method, primary metric interval, gates |
| `limitations` | Typed list of what is missing or approximate |
| `no_profitability_claim`, `profitability_threshold`, `engineering_validation_only` | The claim boundary |

Decimals are JSON strings (DATA-02). Timestamps are UTC ISO-8601 with `Z`. Counts
are integers.

### Null semantics

| Field | `null` means |
| --- | --- |
| `price_at_decision`, `price_at_horizon`, `forward_return_pct` | No permitted price exists |
| `benchmark_return_pct`, relative returns | No matched benchmark pair |
| `directional_hit` | No directional call (HOLD) |
| `equity_usd`, `time_weighted_return_pct` | A held position could not be priced, or an endpoint is unavailable |
| `net_of_llm_return_pct` | At least one run's LLM cost was not reported |
| `primary_metric.value` | No usable observations, or the variant is unimplemented |
| `portfolio_performance` per variant | Variants did not run in isolated accounts; decision scores cannot be summed into account return |
| `cost_per_scheduled_run` / cost gate | Cost was not reported for every scheduled run |

## 5. The example fixture

[`fixtures/experiments/phase3-engineering.v1.json`](fixtures/experiments/phase3-engineering.v1.json)
is a synthetic **measurement harness** fixture. It links the three Phase 1
decision fixtures to V1/V2/V3, schedules a V0 run with no stored output, defines
V4 as unimplemented, and declares a synthetic accounting ledger with a recorded
fee, an unknown fee, an external deposit, a drawdown and an explicit LLM cost.
It is not a protocol comparison and makes no profitability claim.

After loading the Phase 1 fixtures:

```bash
python manage.py load_decision_fixtures
python manage.py build_experiment
python manage.py shell -c "from core.measurement_models import Experiment; \
  print(Experiment.objects.get(experiment_key='phase3-engineering.v1').report['sample_integrity']['counts'])"
```

Expected counts are `scheduled: 4, completed: 3, failed: 0, not_executed: 1`.
The report must be read as engineering evidence only: `engineering_validation_only`
is true, `strict_historical_claims` is false, and every variant result says what
it could and could not measure. A generated snapshot of the report is kept at
[`fixtures/experiments/phase3-engineering.v1.report.json`](fixtures/experiments/phase3-engineering.v1.report.json);
it is an example output, is not loaded by any command, and can be regenerated.

## 6. Tests that hold the gate

| Fixture / leakage rule | Test |
| --- | --- |
| BUY → HOLD → SELL | `BuyHoldSellSequenceTests.test_equity_follows_cash_plus_marked_positions`, `test_hold_preserves_exposure_without_a_trade`, `test_sell_closes_exposure_and_does_not_become_a_short` |
| Fees: recorded vs zero vs unknown | `FeeAccountingTests.*` |
| TWR approximation is responsive | `TwrApproximationTests.test_a_fresh_boundary_valuation_is_exact`, `test_a_stale_boundary_valuation_falls_back_and_flags_approximate` |
| Deposits / withdrawals | `ExternalFlowTests.*` |
| Missing prices / benchmarks | `MissingPriceTests`, `MissingBenchmarkTests`, `SignalDiagnosticReportTests.test_relative_returns_use_only_matched_pairs` |
| Weekend maturities | `WeekendMaturityTests.*` |
| Overlapping decisions are not summable | `OverlappingDecisionTests.*` |
| Price before the decision is rejected | `PriceLeakageTests.test_a_price_before_the_decision_is_rejected`, `test_the_guard_itself_raises_for_a_backdated_fill` |
| Future data cannot enter a historical evaluation | `PriceLeakageTests.test_future_knowledge_cannot_value_an_earlier_instant`, `SignalDiagnosticReportTests.test_an_outcome_evaluated_after_as_of_is_excluded` |
| Failed run stays in the denominator | `ExperimentBuildTests.test_a_failed_run_stays_in_the_completion_and_cost_denominator` |
| Per-decision scores cannot become portfolio return | `OverlappingDecisionTests.test_a_per_decision_series_refuses_every_summing_form`, `ExperimentBuildTests.test_the_two_metric_families_are_separate_and_the_diagnostic_series_refuses_a_total` |
| Snapshot cross-check | `SignalDiagnosticReportTests.test_portfolio_snapshot_mismatch_is_a_reconciliation_error`, `test_input_snapshot_holdings_mismatch_is_reported` |
| Cash / fee / flow reconciliation | `BuyHoldSellSequenceTests.test_return_and_drawdown_are_computed_from_accounting`, `ExternalFlowTests.test_a_deposit_is_not_a_return`, `FeeAccountingTests.test_an_unknown_fee_is_flagged_and_visibly_different` |
| LLM cost completeness | `LlmCostTests.*` |
| Report API ownership and build state | `ReportApiTests.*` |
| Replay without an LLM | `ExperimentBuildTests.test_building_a_report_never_calls_an_llm`, `BuildExperimentCommandTests.*` |
