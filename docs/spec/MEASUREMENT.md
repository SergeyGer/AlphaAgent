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

Benchmark provenance is stored, not assumed. Each scored row carries the
benchmark prices, the session each was observed on, and the price source
(`benchmark_price_at_decision`, `benchmark_price_at_horizon`,
`benchmark_observed_at_decision`, `benchmark_price_observed_at`,
`benchmark_price_source`); the instrument's own observation dates are stored
beside them. The report then **verifies** the interval instead of only declaring
the rule:

* `benchmark_interval_verified` is true only when there is at least one matched
  pair and **every** matched pair has all four observations and the benchmark's
  decision and horizon observations fall on the same sessions as the
  instrument's, with the horizon observation not before the target session.
* precision is `session_date`: prices are daily closes, so the intraday
  observation instant is **not** recorded and is not claimed
  (`intraday_observation_instant_verified: false`).
* a row without provenance (all rows written before this change, and any row
  whose producer could not price the benchmark) keeps its benchmark return but
  is counted in `benchmark_interval.unverified_pairs` with the missing field
  named in `unverified_reasons`. It is never assumed matched.

The declared rule (`benchmark_interval_rule`) and the verification rule
(`benchmark_interval.verification_rule`) are published separately, so a reader
can tell what was promised from what was checked.

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

### 1.5 Live attribution basis (`attribution.v2`)

The dashboard scorecard is the same signal-diagnostics family as §1, with a
reference policy chosen for a live decision rather than a frozen experiment.
`resolve_measurement_reference` decides it, and every row publishes the choice:

| Case | Reference price and instant | `measurement_basis` |
| --- | --- | --- |
| Executed decision with a linked fill | The fill's stored `price` and `timestamp`; a recommendation approved at 130 is measured from 130, not from the proposal price of 100 | `execution_fill` |
| HOLD, rejection, or executed decision with no linked fill | The first close at or after the proposal, with `fill: null` and a stated reason | `proposal_close` |
| No price at or after the proposal | No score; `maturity: unavailable` with a reason | `unavailable` |

A fill for a different instrument is ignored with a warning and left for the
backfill report; it is never used to measure the wrong asset. The basis is
written to `DecisionOutcome.measurement_basis` and `reference_at`, and to the
detail projection's `signal_diagnostics.measurement_basis` together with
`portfolio_performance: false`.

Each horizon has its own `OutcomeEvaluation` row keyed by decision, horizon and
method version. The scheduler selects per horizon, so a day-one score does not
make days seven and thirty ineligible; the row carries `pending`, `matured`,
`unavailable` or `evaluated`, an attempt count, `next_attempt_at` and a reason.

The batch limit applies to **due pairs**, not to candidate decisions. The due
query filters the work ledger on `state` and `next_attempt_at` before applying
the limit, so an evaluated horizon, a terminal-unavailable horizon and a retry
still in backoff cannot consume a slot; an older decision with day 7 and day 30
due is selected even when newer decisions are already complete. A decision the
ledger has never seen is seeded oldest-first, and only a due pair is measured.
`build_scorecard` publishes the state counts and the basis counts beside the
aggregate. The aggregate explicitly says `portfolio_performance: false` and
`summable: false`: this is a per-decision signal diagnostic, not the account's
realised return.

`manage.py backfill_outcome_records` recovers older rows that were scored from
the proposal close or a missing horizon. It is a dry run by default, preserves
the older `attribution.v1` row and prior revisions, logs an unresolvable fill
rather than guessing, follows the same due-pair rules (terminal unavailable and
backoff deferred pairs are skipped), and bounds candidate work rather than
decision rows.

A `pending -> unavailable` transition writes the versioned
`outcome.recorded.v1` event and its outbox row in the same transaction as the
projection. The payload records `maturity: unavailable`, the reason and the
attempt count, with every value `null`: an unavailable horizon is a measured
absence, not a 0% return. The decision detail therefore shows the reason and a
completed run can receive the late update without reopening its status.

The tests that hold this section:

| Rule | Test |
| --- | --- |
| The fill, not the proposal close, is the start price | `test_p1_attribution_remediation.py::test_the_unmocked_start_price_is_the_recorded_fill_not_the_proposal_close` |
| A HOLD has a proposal basis and no fabricated fill | `test_a_hold_keeps_a_proposal_basis_and_has_no_fabricated_fill` |
| A day-one score does not block 7 or 30 | `test_a_day_one_score_does_not_prevent_days_seven_and_thirty` |
| Newer completed decisions do not starve an older due pair | `test_p1_step3_fair_scheduling.py::test_3a_the_older_decisions_due_horizons_are_scheduled` |
| Deferred, evaluated and unavailable pairs do not consume slots | `test_p1_step3_fair_scheduling.py::test_3b_deferred_evaluated_and_unavailable_pairs_do_not_consume_slots` |
| The scorecard publishes the signal-diagnostic basis | `test_the_scorecard_publishes_that_this_is_a_signal_diagnostic` |
| The backfill preserves provenance and is idempotent | `test_apply_recovers_horizons_preserves_provenance_and_is_idempotent` |

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
* `DecisionRun.total_cost_usd` is the **billed** charge and is nullable;
  `DecisionRun.estimated_cost_usd` is the price-table arithmetic and
  `cost_basis` names which is which. A run whose provider reported tokens but no
  dollars has `total_cost_usd = NULL` and a labelled estimate beside it; it is
  never written as a zero. The only measured zero is a run that made no model
  call at all (`cost_basis: "deterministic_no_model_call"`), where zero is
  exact by construction.
* A failed or timed-out call's cost stays in the record when the client reported
  usage for it (`failed_attempt_usage`, `timeout_reconciled`); a timed-out call
  that could not be reconciled is counted in `unreconciled_timeout_calls` and
  named as excluded, because the provider may still bill it. The monetary
  ceiling is therefore **not** a strict billed-dollar cap (`strict_billed_ceiling:
  false` on the usage block and on the run metadata).
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
| `V2` | Current pipeline (no explicit response round) | `debate` | `debate.no_rebuttal.v1` | yes |
| `V3` | Explicit rebuttal | `debate_with_rebuttal` | `debate.v1`, run must show a recorded rebuttal round | yes |
| `V4` | Portfolio allocation | `portfolio_allocation` | — | **no**; declared, not measured |

V3 is the Phase 2 rebuttal protocol (`debate.v1`) and is identified by the
recorded rebuttal round. V2 is the same producer with the rebuttal stage
**omitted**: it records `debate.no_rebuttal.v1`, so the two arms have genuinely
distinct protocol identities instead of being one run relabelled as two. A
manifest must declare all five variants; V4 cannot be marked implemented. The
Phase 3 report publishes V4 as `unimplemented` with a null result; the
comparison execution report publishes `status: "not_executed"` and
`implemented: false`. Either way the result is null, not a measured zero, and
V4 is outside the denominator.

### 3.1a Execution path (`services/experiment_execution.py`)

`build_experiment` measures; it never runs a model. The execution path is the
separate producer:

```bash
python manage.py run_experiment --path docs/spec/fixtures/experiments/alphaagent-arms.v1.json
python manage.py run_experiment --provider stub --output /tmp/comparison.json
```

It schedules V0-V3 against **one frozen opportunity set** and, per opportunity,
**one `InputSnapshot` shared by every arm** (the report compares the content
hashes across arms rather than asserting they match). Each arm records its own
protocol id, decisions, simulated fills and equity trajectory:

| Arm | Producer | Protocol id |
| --- | --- | --- |
| V0 | deterministic engine, no model call | `heuristic_fallback.v1` |
| V1 | one analyst, no cases, no rebuttal | `single_agent_baseline.v1` |
| V2 | Bull / Bear / CIO, no response round | `debate.no_rebuttal.v1` |
| V3 | Bull / Bear / CIO + one rebuttal round | `debate.v1` |

The per-arm trajectory is replayed from that arm's decisions through the same
`evaluate_proposal` guard, the same
`first_executable_mark_at_or_after_decision.v1` fill rule and the same
`portfolio_accounting.v1` builder, against the same declared risk rules and
marks. It is a **simulation**, labelled `synthetic: true` and
`provenance.simulated_arm`, not realised history.

When no provider is configured, V1-V3 stay `not_executed` with
`provider_not_configured` and remain in the denominator. The scripted
`--provider stub` client runs the real code through the real protocol but its
replies are authored: those runs are `mode="simulated"`, their cohort is
`simulated`, and the report says `engineering_validation_only` and
`comparative_protocol_claim: false`. A scripted case is never presented as a
completed model arm.

The opportunity set must declare a pre-registration (`registered_at` at or
before `declared_at` and before the first scheduled opportunity) containing the
sample plan, primary metric, cost ceiling and acceptable drawdown. The report
publishes the registration **and** whether it could be honoured.

The comparison report's execution path has its own archive format,
`experiment_comparison.v3` / `report_schema_version: "3.0"`. It is a different
document from the Phase 3 `build_experiment` report and is not read as one.
Version 3 records the resolved attempt identity, the benchmark observations and
the fill-time validation inputs a rebuild needs. A version-2 archive was
produced under the decision-time quantity semantics; the rebuild refuses it by
name rather than rebuilding it as if the same arithmetic produced it.

#### 3.1b Execution namespace and the registered metric

Each execution has a **deterministic namespace**, derived from the portfolio id,
the opportunity-set content hash, the provider mode and the injected client
identity (`execution_namespace(...)`, 16 hex characters). The namespace is the
first segment of every `DecisionRun.fixture_id` (followed by the arm, the
opportunity digest, the attempt identity hash and the attempt ordinal) and is
published as `execution.execution_id` and `execution.namespace`. A
`configuration_id` hash over the four implemented arms' resolved identities is
published beside it, so a reader can tell two executions that share a namespace
but differ in model or budget apart. Consequences:

* two portfolios, or two opportunity sets that share a natural key but differ
  in tickers, marks, starting state or provider mode, cannot read or overwrite
  each other's decisions, snapshots or spend;
* a rerun with the same inputs, provider mode and resolved configuration
  resolves to the same namespace and attempt identity and reuses its recorded
  runs, so it does not double-spend; a changed configuration has the same
  namespace but a different identity and attempt key;
* the cost ledger is scoped: `cost_breakdown.scope` names the execution and its
  run ids, and `verification.run_ids` lists them per arm;
* each execution's input snapshots are immutable `InputSnapshot` rows; the
  snapshot version includes the opportunity-set content hash, so a changed
  document gets a new snapshot rather than reusing the old one.

The namespace answers *which experiment is this?*. It is not the attempt
identity. Each `(arm, opportunity)` attempt carries a resolved identity
(`experiment_attempt.v1`) hashed from the provider and model the client actually
reports, the protocol version, the client parameters, the protocol budget the
protocol applies and the declared execution policy (fill rule, timing rule, fee,
slippage, trading day, cost basis and fee policy). `attempt_identities` is
published per arm and every run row carries `attempt` and `attempt_identity`.
The identity is resolved, not copied from the request: `--provider auto` with no
configured provider records `provider: "unknown"`, and a scripted client records
`provider: "stub"` even when the requested mode was `auto`.

The attempt ordinal is part of the run key, not of the identity hash:

* an exact repeat resolves to the same identity and reuses its latest attempt;
* a changed model, parameter or budget has a different identity and its own
  attempt; it cannot resolve to the other configuration's run;
* `--force` appends the next attempt after every attempt of that
  `(namespace, arm, opportunity)` key. It removes nothing: the earlier
  `DecisionRun`, its decision, guard evaluations and events remain resolvable,
  and a previously written archive keeps every run id it named.

The registered metric is read from the immutable pre-registration record - its
id, family, horizon and direction - not from a module constant. For the
`signal_diagnostics` family the calculator uses the decision's snapshot quote
(or the latest mark known at the decision) as the entry and
`first_mark_at_or_after_target_session.v1` for the exit, with the archive's
`as_of` cut-off. The result publishes:

| Field | Meaning |
| --- | --- |
| `status` | `computed` or `not_computable` |
| `value` | Mean action score (or the registered id's value), as a Decimal string |
| `denominator.scheduled` / `.usable` / `.excluded` | Decisions scheduled, decisions with a matured horizon price, and the exclusions by stable reason |
| `denominator.by_state` | `evaluated`, `pending`, `failed`, `budget_blocked`, `missing` and `not_recorded` counted separately; the six sum to `.scheduled` |
| `denominator.excluded_by_reason` | Stable reasons, including each benchmark exclusion when the registered id is relative |
| `benchmark_pairs` | The declared benchmark ticker and policy, and the matched, excluded and excluded-by-reason pair counts |
| `contributing_runs` / `contributing_run_ids` | How many recorded runs produced a usable observation, and which |
| `dispersion` | Sample standard deviation, min, max and range (null with `fewer_than_two_observations` when only one) |
| `interval` | Seeded time-block bootstrap interval; null with a reason when it cannot be formed |
| `by_arm` | The same fields per implemented arm; V4 is absent |
| `reason` / `missing_input` | Null only when the metric is computed; otherwise the stable reason and the input that was missing |

A `null` value therefore means "not measurable from this data", never "not
implemented": an unsupported registered id or family is refused when the
opportunity set is validated, and a horizon that no mark reaches reports
`no_session_at_or_after_target` with the missing mark named. A target session
after the archive's `as_of` is counted as `pending`, not as a missing mark; a
run that failed for a non-budget reason is `failed`; a run the protocol budget
stopped is `budget_blocked`; an observation without a usable input is `missing`.
V4 remains `not_executed`, `value: null`, `in_denominator: false`; it is not a
zero.

**Benchmark windows are matched, not assumed.** The declared benchmark may carry
its own `benchmark.marks`. Every scored row publishes the benchmark's entry and
horizon observations with their prices, observation timestamps, sessions and
price sources, and the declared policy
`benchmark_same_session_pair.v1` (the target is the first benchmark mark at or
after the instrument's own session, never the prior close). A pair is usable only
when both benchmark observations fall on the instrument's entry and horizon
sessions, and the horizon observation is not before the target session. A
relative metric (`mean_relative_forward_return_pct`) takes its value only from
matched pairs; an excluded pair is published with one of
`benchmark_entry_observation_missing`, `benchmark_entry_session_mismatch`,
`benchmark_horizon_observation_missing`,
`benchmark_horizon_session_mismatch`,
`benchmark_horizon_before_target_session` or `benchmark_marks_not_recorded`.
An excluded pair is never a zero, and the matched-pair count is the visible
denominator.

The report embeds the recorded inputs a rebuild needs: `frozen.opportunity_set`
(the full document), `frozen.portfolio`, `execution.snapshots_used` (the
snapshot payloads and hashes), each run's recorded decision, and
`frozen.methods`. Rebuilding therefore reads the archive, not the current
fixture file:

```bash
python manage.py rebuild_experiment_report --path /tmp/comparison.json
python manage.py rebuild_experiment_report --path /tmp/comparison.json --execute
```

The command is a dry run by default. It reports `reproduced` or `differs`, the
metric value and the archived/rebuilt content hashes, and every difference by
path. `--execute` writes the rebuilt document to `--output` (default
`<archive>.rebuilt.json`) and never overwrites the archive it is checking. An
unsupported `report_schema_version` or `archive_format` is refused by name; a
tampered opportunity set or snapshot is refused by hash. The rebuilt document
keeps `honesty.simulated_content: true` and `synthetic_label:
"synthetic_simulation"`.

#### 3.1c Simulated fills (`services/experiment_simulation.py`)

The simulator keeps a chronological event queue, but a decision is a **proposal**
and a fill is a separate event created when the simulated clock reaches the
executable mark. Immediate and deferred orders use one validation path at the
executable instant (`fill_time_revalidation.v1`):

* the same `evaluate_proposal` guard is re-evaluated against the arm's state at
  that instant, with the decision-time quantity as a ceiling rather than a fill
  authorisation;
* available cash is checked **including the declared fee** (`fee_bps`), and a
  buy is clipped to what cash + fee can actually pay for; a fill that cannot be
  paid for is rejected, never written with a negative cash balance;
* a sell is clipped to the eligible holding at that instant; proceeds are
  credited for the shares actually held, never for the decision-time quantity;
* the day's realised P&L is passed to both the decision-time guard and the
  fill-time guard. It accumulates from completed fills under
  `weighted_average_cost.v1`, with `fee_reduces_realised_pnl.v1`, and resets at
  the declared `utc_calendar_day.v1` boundary. The per-day and running totals are
  published in `trajectory.simulation.realised_pnl`; each order carries
  `realised_pnl_today_usd` and each fill step carries the delta and the running
  total;
* each order publishes a `fill_validation` block: policy, executable price,
  approval at the decision, cash and holdings before the fill, the fee basis, the
  guard result, whether it was clipped and why, the filled quantity, the
  realised P&L delta and cash after. The timeline method is
  `monotonic_fill_validation.v2`.

A fill can therefore be `filled`, `deferred`, `rejected`, `expired`,
`unpriced`, `hold_no_trade` or `not_executed`, and a successful fill can carry
`fill_validation.status: "clipped"` when the guard or the cash/fee recheck
reduced the quantity. The rejection reasons are stable
(`insufficient_cash_including_fees_at_fill`,
`clipped_to_available_cash_including_fees`, `no_eligible_holding_at_fill`,
`clipped_to_holding_at_fill`, `clipped_by_guard_at_fill`,
`fee_exceeds_sale_proceeds_at_fill`).

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
the report from stored rows and appends a new `report_version`; it does not
create a second experiment for the same `(portfolio, experiment_id)`.

Report versions are **immutable and addressable**. Each build appends an
`ExperimentReportVersion` row containing the published document, its SHA-256, a
dependency snapshot (the manifest and variants, portfolio limits, scheduled
runs, decision runs with their cost basis, decisions, guards, outcomes including
benchmark provenance, claims, rebuttals, ledger rows, cash flows and snapshots)
and that snapshot's digest. `verify_report_version(experiment, version)` rebuilds
the version from the current rows and compares it byte for byte, naming the
dependency groups that drifted. A drift never edits the published version: the
stored document remains the record of what was published, and the drift is the
news. A manifest checksum alone cannot do this, because it does not cover
later-maturing outcome rows or a changing ledger.

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
are integers. `dependency_digest` names the row snapshot the version was built
from; the published versions themselves are addressable through
`ExperimentReportVersion` (see §3.3), not through the latest-report endpoint.

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

The V0-V3 execution path has its own example output:
[`fixtures/experiments/alphaagent-arms.v1.comparison.json`](fixtures/experiments/alphaagent-arms.v1.comparison.json).
It is generated by `python manage.py run_experiment --provider stub` and, like
the Phase 3 report, is labelled engineering validation. Its arm trajectories are
simulations under authored marks; they are not the demo account's history.
The committed archive is `experiment_comparison.v3` (`report_schema_version`
`"3.0"`). It carries its own opportunity-set document, benchmark marks, snapshot
payloads, resolved attempt identities and decisions, so its metric, trajectories,
fill decisions and `contents_hash` can be recomputed without a database:

```bash
python manage.py rebuild_experiment_report \
  --path docs/spec/fixtures/experiments/alphaagent-arms.v1.comparison.json
```

The committed metric is `mean_action_score_pct` at the 7-day horizon. The AAPL
and MSFT horizons are reached by the 2026-02-11 closing marks; the TSLA target is
after the last mark, is counted in `denominator.excluded_by_reason` as
`no_session_at_or_after_target` and in `denominator.by_state.pending` (its target
session is after the archive's `as_of`), and is not scored as a zero. The
opportunity set also declares SPY benchmark marks, so each scored row publishes a
matched benchmark entry and horizon beside its instrument observation even though
the registered metric is not the relative one. V4 is `not_executed` and outside
the denominator.

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
| A changed model or budget is a distinct attempt identity | `test_p1_step7_identity_benchmarks.py::test_two_resolved_models_and_a_changed_budget_get_distinct_identities` |
| A forced rerun keeps the earlier run, decision, guards and events | `test_p1_step7_identity_benchmarks.py::test_forced_rerun_creates_a_new_attempt_and_keeps_the_old_one_resolvable` |
| Only matching benchmark windows enter a relative result | `test_p1_step7_identity_benchmarks.py::test_matching_mismatched_and_missing_benchmark_windows_are_separated` |
| A later price or fee cannot overdraw cash | `test_p1_step8_fill_validation.py::test_a_later_price_or_fee_that_exceeds_cash_clips_and_never_overdraws` |
| A second sell cannot credit unheld shares | `test_p1_step8_fill_validation.py::test_two_sells_beyond_the_holding_clip_the_second_and_never_overcredit` |
| The day's realised loss blocks same-day risk and resets next day | `test_p1_step8_fill_validation.py::test_the_loss_blocks_same_day_risk_allows_the_next_day_and_is_passed_to_the_decision` |
| The frozen archive reproduces the timeline and fill decisions | `test_p1_step8_fill_validation.py::test_rebuild_reproduces_the_timeline_fill_decisions_and_digest` |
