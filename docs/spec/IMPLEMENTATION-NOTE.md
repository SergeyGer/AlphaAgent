# Implementation note — Technical Specification v1.0

This records the Phase 0 baseline the specification requires, and what is being
built against it. The specification is a five-phase programme; this note covers
Phase 0 in full and Phases 1–5 in implementation. Phases 1–4 are merged; Phase 5
is the publication gate.

## BASE-01 — Baseline

| | |
| --- | --- |
| **Commit** | `1527cdc549a7bc43717fdd5ccf892e7fec15e1f9` |
| **Subject** | `chore(deps): streamlit 1.66.1, and the theme contract re-verified against it (#42)` |
| **Date** | 2026-11-10 14:09:40 +0300 |
| **Branch** | `main` |
| **Tags** | v1.0.0, v1.1.0, v1.1.1, v1.1.2, v1.1.3, v1.1.4 |
| **Repository instructions** | `CONTRIBUTING.md` — branch protection on `main`, five required status checks |

### Inventory at baseline

**Models** (`core/models.py`, 16): `RiskProfile`, `TxType`, `ExecutedBy`,
`MarketSentiment`, `Portfolio`, `Asset`, `Transaction`, `AgentDecisionLog`,
`ThesisStatus`, `PositionThesis`, `DecisionOutcome`, `PortfolioSnapshot`,
`RecommendationStatus`, `DecidedVia`, `TradeRecommendation`, `TelegramLink`.

**Services** (`services/`): `attribution`, `budget`, `events`, `execution`,
`fundamentals`, `ledger`, `llm_hooks`, `market_data`, `news`,
`portfolio_metrics`, `recommendations`, `sentiment`, `snapshots`, `thesis`,
`throttle`, `tickers`.

**API** (`core/views.py`, 669 lines): DRF `APIView` and `generics.ListAPIView`
under `/api/`. Portfolio detail, autonomy toggle, decision log list, transactions,
run-agent, snapshots, recommendations, recommendation decisions.

**Frontend** (`frontend/src/components/`): 22 components including
`Dashboard`, `ThoughtsFeed`, `DecisionQualityPanel`, `PortfolioLimits`,
`PositionTheses`, `MetricsGrid`, `TradesTable`.

### Confirmed behaviours the specification asks about

| Claim in the specification | Confirmed |
| --- | --- |
| `ai_agent.py` runs Bull → Bear → CIO sequentially, reports extracted after `crew.kickoff()` | Yes |
| `DecisionContext` carries one ticker, not a portfolio allocation brief | Yes |
| `ThoughtsFeed.tsx` renders expandable Bull / Bear / CIO sections | Yes |
| `tasks.py` emits pipeline status events around the run | Yes |
| `services/execution.py` applies deterministic constraints | Yes |
| Attribution evaluates 1/7/30-day outcomes; action score is instrument return for BUY, inverse for SELL, zero for HOLD | Yes — `DecisionOutcome.HORIZONS == (1, 7, 30)`, `BENCHMARK_TICKER == SPY` |
| The Streamlit demo is a risk-guard demonstration, not the live application | Yes |

## Divergences from the specification

Recorded as required, not silently absorbed:

1. **`DecisionContext` has no portfolio brief.** The specification's Phase 4
   portfolio CIO requires holdings, weights, sector exposure and covariance
   estimates. Phase 1 does not add them; it presents what the current pipeline
   actually produces. The detail view therefore shows a single-instrument
   decision with portfolio consequences, never a fabricated allocation rationale.
2. **The current pipeline has no rebuttal stage.** Phase 1 must not invent one.
   A run whose protocol is `legacy_sequential` renders a `Legacy report` badge and
   no rebuttal section at all. Phase 2 adds the real protocol.
3. **`AgentDecisionLog` is the existing consumer contract.** New records link to
   it rather than replacing it, per DATA-04.

## Scope of this change

**Phase 1 — Explain one decision.** A reviewer can identify decisive evidence,
the strongest objection, the reason for the CIO verdict, the guard intervention
and the measured outcome without reading a raw log, offline, with no keys.

Deliberately *not* in this change, and why:

| Phase | Why not now |
| --- | --- |
| 2 — real rebuttals | Needs the evidence/claim contracts to be real data, not fixtures. Phase 1 establishes the contracts; Phase 2 populates them from a live protocol. |
| 3 — measure value | Requires portfolio accounting to reconcile first. The specification itself says metric definitions must be frozen before comparative claims. |
| 4 — allocate capital | Depends on Phase 3 accounting and execution safeguards. Shipping an allocator without them would violate REL-02. |

The detail view is built so that Phase 2 changes the *producer*, not the
*consumer*: the API already serves claims, rebuttals and evidence; Phase 1
populates them from recorded runs where they exist and omits them honestly where
they do not.

## Phase 2 — real rebuttals (DEB-01 … DEB-05)

The producer now lives in `services/debate_protocol.py`. `ai_agent.run_alpha_agent`
remains the public entry point; the pre-Phase-2 sequential crew is still
available behind `AI_DEBATE_PROTOCOL=legacy_sequential`.

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| DEB-01 independent first round | Two prompts are built from the same brief and evidence catalog; neither contains the other side's output. Both cases are persisted before `status.changed -> rebutting`. | `IndependentFirstRoundTests` |
| DEB-02 one rebuttal round | One round, `agree` / `dispute` / `qualify` / `insufficient_evidence`, a required `position_change`, at most 3 claims and 2 responses per agent. Extras are excluded **and recorded**; a missing/invalid target gets one repair then the stage fails. A concession is a valid output. | `RebuttalRoundTests` |
| DEB-03 adjudication | The CIO sees both cases and the rebuttals, must name `strongest_objection` and `unresolved_uncertainty`, and HOLD is recorded as a decision. `analyst_consensus` is derived from the rebuttal types, so a split debate cannot be reported as agreement. | `AdjudicationTests` |
| DEB-04 budgets and failures | Per-stage timeout, run deadline, input/output token caps, one schema repair, two transient retries with backoff, and a monetary ceiling, all configurable under `settings.DEBATE_CONFIG`. A failure is typed, stops the protocol, and marks the stage failed. | `FailurePathTests` |
| DEB-05 reproducibility | `DecisionRun.protocol_metadata` records provider, model, parameters, prompt-template version, tool versions, snapshot hash, limits, usage and the raw validated stage outputs, together with a note that events replay deterministically but regenerating model output does not. | `ReproducibilityTests` |

A run produced by the protocol is `mode="live"` and `source="crewai"`. A
heuristic fallback is a **separate** run (`mode="heuristic"`,
`source="heuristic_fallback"`) with no claims and no rebuttals; the failed run it
replaces carries a `limitation.recorded` event and a `protocol.fallback` block
naming it, so the two attempts are never conflated.

### Phase 2 divergences, recorded rather than absorbed

1. **A stage timeout stops waiting; it does not cancel the call.** Python cannot
   kill the worker thread, so the provider may still finish and bill for the
   call. The protocol fails the stage typed and does not consume the reply.
2. **The monetary ceiling is enforced on reported cost only.** When the client
   reports no cost, the usage block records `cost_reported: false` and no
   monetary conclusion is drawn; a guessed cost would be indistinguishable from
   a measured one.
3. **Token counts are provider-reported when available, otherwise estimated**
   (four characters per token) and labelled `token_accounting` accordingly.
   They are not a substitute for billed usage.
4. **`legacy_sequential` records no `DecisionRun`.** Its two "cases" are free
   text sliced out of the crew output; presenting them as validated claims would
   be the fabrication Phase 1 refused. The mode is kept for compatibility only.
5. **Extra rebuttal rounds are not configurable.** `REBUTTAL_ROUNDS` is a
   constant; the specification says a second round must wait for an experiment
   that justifies its cost, so there is no flag that silently enables one.
6. **The baseline is selectable, not the default.** `AI_DEBATE_PROTOCOL=single_agent.v1`
   runs one analyst with the same snapshot and the same validated proposal
   contract, recording no claims and no rebuttals. The default remains
   `debate.v1`.
7. **The UI still labels Phase 1 fixtures `synthetic`.** Those fixtures were
   authored before a producer existed and were not regenerated by this change;
   new runs are live. Nothing in `frontend/` was touched.


## Phase 3 — measure value (MET-01 … MET-05, EXP-01 … EXP-05)

The measurement contract lives in [`MEASUREMENT.md`](MEASUREMENT.md). Phase 3 adds
`services/portfolio_accounting.py`, `services/signal_diagnostics.py`,
`services/experiments.py`, the additive `CashFlow` / `Experiment` /
`ExperimentVariant` / `ExperimentRun` tables, `Transaction.fee`, the
`build_experiment` command and `GET /api/experiments/<id>/report/`.

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| MET-01 separate metric families | Signal diagnostics and portfolio performance are separate services and report blocks. `PerDecisionScoreSeries` has no working total; the report has no summed decision field. Proposal and executed actions are separate, and a HOLD has no directional hit. | `OverlappingDecisionTests`, `SignalDiagnosticReportTests.test_a_hold_has_no_directional_hit_and_its_zero_is_a_convention`, `test_proposal_and_executed_action_are_distinct` |
| MET-02 accounting | `E(t) = cash + Σ quantity × price`; ledger replay; recorded fees deduct cash; external flows are flow boundaries; TWR falls back to Modified Dietz and flags approximate; drawdown, turnover and exposure are formula-bearing; LLM costs are separate and deducted only in the net series. | `BuyHoldSellSequenceTests`, `FeeAccountingTests`, `ExternalFlowTests`, `TwrApproximationTests` |
| MET-03 horizons and benchmark rules | `signal_diagnostics.v1` versions the 1/7/30 horizons, UTC and the first-session-at-or-after-target policy. Simulated fills use the first mark at or after the decision. The manifest declares the equity or mixed benchmark rule. Stored benchmark timestamps are disclosed as unverified rather than assumed. | `WeekendMaturityTests`, `PriceLeakageTests`, `SignalDiagnosticReportTests.test_method_version_horizon_policy_and_benchmark_rule_are_displayed` |
| MET-04 missing data and sample integrity | Missing prices and benchmarks are null; HOLD zeros are conventions; relative returns use matched pairs only; every report publishes counts, date range, asset universe, source mode and unresolved observations; heuristic, LLM and synthetic cohorts are separate. | `MissingPriceTests`, `MissingBenchmarkTests`, `test_relative_returns_use_only_matched_pairs`, `test_pending_outcomes_are_unresolved_not_scored_as_zero` |
| MET-05 attribution limits | Per-decision scores cannot be summed; contribution analysis records quantities, entries/exits, capital weights and reconciles to equity change; guard rejection is a counterfactual only under a declared rule, with a horizon sensitivity and no invented confidence interval. | `OverlappingDecisionTests`, `CounterfactualTests`, `test_contribution_analysis_records_quantities_entries_exits_and_weights`, `test_a_rejected_proposal_is_a_counterfactual_not_executed_performance` |
| EXP-01 variants | V0–V4 are normalised from the manifest; V4 cannot be marked implemented; every variant is published, with nulls where no run exists. | `ManifestValidationTests`, `test_all_variants_are_published_and_v4_is_null_not_zero` |
| EXP-02 fair comparison | The manifest freezes instruments, dates, starting state, risk rules, execution assumptions, snapshots and benchmark. The report publishes freeze checks, equal opportunity, equal budget and per-variant model configuration. | `test_fairness_publishes_equal_opportunity_budget_and_freeze_checks` |
| EXP-03 historical/forward | Strict historical claims require a snapshot, a cutoff at or before the decision, no late evidence, non-synthetic content and a historical mode. Survivorship and pretraining-contamination risks are disclosed. | `test_missing_snapshot_makes_strict_historical_claims_ineligible` |
| EXP-04 reports | Cumulative, TWR and net-of-LLM returns; drawdown; formula-bearing turnover; trade count; exposure; failure rate; token/API cost; latency; action changes; evidence coverage; unsupported-claim flags. Sharpe is explicitly not included, with its frequency/annualisation/risk-free/sample limits disclosed. | `test_primary_metric_publishes_a_time_block_interval`, `test_action_changes_after_rebuttal_are_reported_with_their_basis`, `test_evidence_coverage_and_unsupported_claim_flags_are_reported`, `test_sharpe_is_explicitly_absent_with_its_disclosure` |
| EXP-05 uncertainty | Pre-registered primary metric, window, minimum sample, cost ceiling and acceptable drawdown; seeded time-block bootstrap; all variant intervals published; no profitability threshold. | `test_primary_metric_publishes_a_time_block_interval`, `test_a_small_sample_is_engineering_only_and_asserts_no_threshold` |

### Phase 3 divergences, recorded rather than absorbed

1. **`Transaction.fee` is nullable, not defaulted to zero.** Existing rows are
   `NULL` and were not backfilled. `NULL` means *no fee was recorded*, while
   `Decimal("0")` means *a zero fee was recorded*. The consequence is deliberate:
   a window containing an unrecorded fee produces `fees_complete: false`,
   `unknown_fee_fill_count > 0` and `approximate: true`. Its equity and TWR are
   therefore visibly approximate, not silently optimistic. A reader who needs an
   exact net figure must supply the missing fees; the report will not invent them.
   No current producer writes a fee: the execution guard has no broker-fee source,
   so live fills remain `NULL` and a live report will say fees are incomplete until
   an integration records the charge. The column and the reporting contract exist
   now so that adding the source is an additive change rather than a reinterpretation
   of a zero.
2. **External flows are a separate `CashFlow` table, not new `TxType` values.**
   `services/ledger.replay_positions` treats every non-BUY transaction as a SELL,
   so a `DEPOSIT` row in `Transaction` would corrupt positions and realised P&L;
   `tx_type` is also `varchar(4)` and cannot hold `DEPOSIT`. Keeping every
   `Transaction` a trade preserves the Phase 0 ledger contract. The consequence is
   that a cash deposit is visible to accounting through `CashFlow` and to no
   existing trade endpoint; a consumer that only reads `Transaction` will not see
   it.
3. **`PortfolioSnapshot` cannot supply point-in-time marks.** It stores aggregate
   cash, positions value and equity but **no per-ticker quantities or prices**, and
   snapshots are not guaranteed at flow boundaries. Portfolio accounting therefore
   reconstructs positions from the fill ledger and cash by reversing later ledger
   effects from the current balance; snapshots are used only as reconciliation
   anchors. The consequence a performance reader must know: **TWR is exact only
   when a mark was valid and known at the flow boundary**; otherwise the report
   falls back to Modified Dietz and labels every figure that depends on that
   boundary as approximate. If the opening or closing equity cannot be priced, the
   TWR is `null` rather than an estimate. A snapshot mismatch is reported as
   `reconciliation.status: "mismatched"` and is not smoothed away; the underlying
   cash basis or snapshot is wrong until someone resolves it.
4. **V4 is declared and unimplemented.** The manifest must define it and must not
   mark it implemented. The report publishes a null V4 result with the reason, not
   a zero, and excludes it from the equal-opportunity count rather than treating
   an unimplemented arm as a zero-opportunity arm.
5. **V2 and V3 share the Phase 2 producer.** Phase 2 delivered rebuttals inside
   `debate.v1`; V3 is selected by the presence of a recorded rebuttal round, not by
   a second protocol. A V3 manifest run that records no rebuttal keeps its
   measurement status but carries an `expectation_violations` metadata entry, so
   the mismatch is visible in the run row rather than silently relabelled.
6. **The example experiment report uses a synthetic accounting fixture.** The
   three stored decision runs are the Phase 1 synthetic fixtures, and the ledger in
   `phase3-engineering.v1.json` is declared synthetic in the manifest. The report
   says `synthetic_content`, `strict_historical_claims: false` and
   `engineering_validation_only: true`. Its portfolio performance is **not** the
   demo account's realised history and must not be read as such; it exists to make
   the accounting family's numbers (fees, a deposit, a drawdown, net-of-LLM)
   reproducible from the repository without a model call.
7. **Sharpe is not computed.** The valuation frequency is event-driven rather than
   a fixed daily series, so a Sharpe ratio would require inventing a frequency and
   annualisation. `risk_adjusted.sharpe` publishes `included: false`, the frequency,
   the missing annualisation and risk-free assumptions, and the sample limit
   instead of the ratio.
8. **Benchmark interval matching is now recorded and checked, at session-date
   precision.** `DecisionOutcome` and `OutcomeRecord` carry the benchmark's
   decision and horizon prices, their observation dates and their source (and the
   instrument's own observation dates beside them). The report verifies that the
   benchmark's observations fall on the same sessions as the instrument's and
   sets `benchmark_interval_verified` only when every matched pair passes. Rows
   written before this change have no provenance: they keep their benchmark
   return, are counted as unverified with the missing field named, and are never
   assumed matched. What remains unverifiable is the intraday observation instant
   (prices are daily closes) and the exchange calendar itself; the report publishes
   `precision: "session_date"` and `intraday_observation_instant_verified: false`
   rather than claiming more.
9. **Report versions are now immutable.** Earlier builds were overwritten on the
   experiment row: replay was deterministic, but a version could not be fetched
   and later-maturing outcomes could change what a published number "was". Each
   replay now appends an `ExperimentReportVersion` row holding the published
   document, its hash, and a digest of every row the report read. An older
   version reads back byte-identically, and `verify_report_version` rebuilds it
   from current rows and names the dependency groups that drifted. The drift is
   reported; the published version is not edited. What is still not frozen is the
   model: a rebuild proves the *document* is reproducible from stored data, not
   that regenerating model output would repeat the decisions.

### FIX-07 — acceptance-review remediation

The review found four cost/deadline defects, an experiment path that could
measure but not execute, and report versions that were overwritten. This change
closes them; it does not revise the Phase 1-5 claims above except where marked.

**A — cost and deadline semantics.** Five quantities are now recorded and never
interchanged: measured tokens, estimated tokens, price-table cost, billed cost
and unknown. `ai_agent` no longer writes a price-table number into a billed-cost
field; `CrewAIStageClient` reports tokens but `cost_usd: None` with a reason (a
usage-summary failure is *unknown usage*, not `(0, 0)`); `ProtocolUsage` publishes
the split and refuses to present a partial price-table sum as a total; the
monetary ceiling is checked before **every** attempt, including every retry; each
call's timeout is `min(stage timeout, remaining run deadline)` and a backoff that
cannot fit the deadline is a typed `DeadlineExceeded`; an abandoned call is
reconciled from the client's counters where possible and otherwise counted as
`unreconciled_timeout_calls`. The budget docstring and the run metadata say
plainly that the ceiling is **not** a strict billed-dollar cap
(`strict_billed_ceiling: false`), because late provider charges and unreconciled
calls are not visible to the check. `AgentDecisionLog.api_cost_usd` is nullable
and `budget_state()` publishes `unknown_cost_runs` beside the billed total: a
provider that reports tokens but no dollars now shows spend `0` with unknown
runs, not a measured zero.

Divergences recorded:

1. A provider charge reported after the run ends is still not in the record.
   The run says so; it cannot be fixed from inside the run.
2. `estimate_cost` keeps its default-price fallback for the legacy path and for
   callers that explicitly want an estimate. The protocol path uses
   `price_table_cost`, which returns `None` for an unpriced model instead of
   substituting a default. The warning is logged under
   `alphaagent.cost_accounting`, not `alphaagent.ai`.
3. A failed call's usage is counted only when the client published it. A provider
   that bills a failed call silently is unreconciled by construction.

**B — experiment execution path.** `services/experiment_execution.py` and
`python manage.py run_experiment` schedule V0-V3 against one frozen opportunity
set and one shared `InputSnapshot` per opportunity. V2 now records
`debate.no_rebuttal.v1` (the Bull/Bear/CIO configuration with the rebuttal stage
omitted) and V3 records `debate.v1`; previously both were `debate.v1`, which made
"distinct arms" a relabelling. Each arm gets its own decisions, simulated fills
and equity trajectory through the same guard, fill rule and accounting method.
V4 remains declared and unimplemented (FIX-07 predates FIX-08; FIX-08 now
supplies the allocation provider and approval path, but no V4 arm has been run)
and is excluded from the equal-opportunity denominator.

Divergences recorded:

1. **The example execution is simulated.** The committed opportunity set's
   prices, quotes and evidence are authored, and the `--provider stub` client's
   replies are deterministic functions of the prompt. The runs are
   `mode="simulated"`, cohort `simulated`, and the report says
   `engineering_validation_only: true`, `comparative_protocol_claim: false`. The
   code path, guard, snapshots, hashes and accounting are real; the model output
   is not. With `--provider auto` and no configured provider, V1-V3 remain
   `not_executed` with the reason and stay in the denominator.
2. **Per-arm portfolio performance is a simulation, not the account's history.**
   It replays each arm's decisions against the frozen marks. It is not the demo
   account's realised trajectory and cannot be read as one.
3. **The pre-registered primary metric is computed from the frozen marks when
   the registered horizon is reached.** FIX-07 left it `null`/
   `no_matured_outcomes` because no `OutcomeRecord` rows existed; steps 7-8
   replace that placeholder with the metric defined in the pre-registration,
   measured from each decision's snapshot quote and the first mark at or after
   the registered horizon. A horizon no mark reaches is still `null`, with the
   excluded observations and the missing input named.
4. **The economic decision instant is the scheduled opportunity, not the
   wall-clock replay time.** Each run records both; pricing uses the scheduled
   instant, and the run metadata says so.

**C — provenance and report immutability.** Benchmark and instrument
observation dates and price sources are persisted; interval verification is
computed per row and summarised, with `session_date` precision stated. Report
versions are archived immutably with a dependency digest and a byte-identical
rebuild check. Pre-registration is required in the opportunity set and its
timing is validated.

## Phase 4 — allocate capital (PORT-01 … PORT-05)

The allocation contract lives in [`ALLOCATION.md`](ALLOCATION.md). Phase 4 adds
`services/portfolio_brief.py`, `services/portfolio_policy.py`,
`services/portfolio_targets.py`, `services/portfolio_allocation.py`, the
additive `InstrumentClassification` / `PortfolioPlan` / `PortfolioPlanLeg`
tables, three additive `Portfolio` limit columns, and the `run_allocation` paper
command.

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| PORT-01 responsibility, not personas | `PortfolioCIO` makes one call at the portfolio boundary with every candidate, holding and constraint in the brief. No second allocator persona was added. | `StubModelEndToEndTests.test_the_cio_calls_a_stub_once_and_records_the_provenance`, `test_run_allocation_creates_a_plan_and_no_ledger_row_without_apply` |
| PORT-02 required brief | `build_allocation_brief` includes holdings, cash, equity, weights, sector/asset-class exposure, limits, transaction-cost assumptions, theses, candidate cases, data-quality flags, horizon and deterministic covariance/correlation with observation window, frequency and quality flags. Expected returns do not exist in the contract. | `BriefTests.test_the_brief_contains_every_required_input`, `test_covariance_and_correlation_are_computed_when_history_exists`, `test_missing_history_produces_null_correlation_with_a_reason`, `test_insufficient_history_is_null_not_zero` |
| PORT-03 output contract | `validate_target_proposal` requires cash, non-negative decimal weights summing to 1 within `0.000001` (then normalises to exactly 1), actions with rationales and claim links, `funding_source`, `rejected_alternatives`, `unresolved_uncertainties` and the brief's `portfolio_version`. Maintaining cash / no change is valid. | `TargetContractTests.*`, `PlanLifecycleTests.test_a_no_change_plan_is_a_valid_outcome_with_no_ledger_row` |
| PORT-04 deterministic sizing | `constrain_targets` enforces per-name, sector, total-exposure, turnover and cash-buffer limits and sends every deficit to cash. `derive_legs` applies price/lot rounding, cost-aware funding and the live per-trade ceiling; post-trade trimming keeps the rounded plan inside the limits. The model is an explainer of candidate selection, not an optimizer. | `SectorAcceptanceTests.test_a_favourable_candidate_is_declined_because_its_sector_is_already_over_the_limit`, `CashCompetitionAcceptanceTests.test_two_attractive_candidates_share_the_limited_cash_in_one_plan`, `InfeasibleTargetAcceptanceTests.test_costs_and_rounding_make_a_target_infeasible_without_exceeding_limits` |
| PORT-05 plan semantics | A plan references one `InputSnapshot`. `apply_plan` locks plan and portfolio, refuses a changed version, processes sells before buys, revalidates each leg through `evaluate_proposal`, writes `apply_trade` in one transaction, and rolls back and marks the plan failed on any failure. `revalidate_plan` re-derives legs. | `PlanLifecycleTests.test_applying_a_plan_is_atomic_and_a_second_apply_is_idempotent`, `test_a_changed_portfolio_version_requires_revalidation`, `test_a_failed_leg_halts_the_plan_and_writes_no_ledger_row`, `ConcurrencyTests.*` |

### Phase 4 divergences, recorded rather than absorbed

1. **Sector is an explicit lookup, not `Asset.sector`.** The task allowed a
   nullable `Asset.sector` "or an equivalent lookup". `InstrumentClassification`
   was chosen because it also classifies candidate names that are not held yet,
   and because a missing row is unambiguously unknown. Sector is never inferred
   from a ticker. Consequence: existing databases show sector exposure as
   **incomplete**, not 0%, until an operator imports classifications. The paper
   fixture supplies fixture classifications and labels them as such.

2. **There is no optimizer, and therefore no optimizer objective to fit.** The
   model proposes weights; `deterministic_constrained_projection.v1` projects
   them into the feasible set and records every difference. The plan records the
   method version, objective version, inputs, solver identity and covariance
   method version. A mean-variance or expected-return objective was deliberately
   not introduced: it would require expected returns the data does not contain.

3. **Correlation estimates are informative, and their absence is a policy
   input.** They are not used to size positions. When history is missing, the
   pair is `null` with a reason; the policy can label it, retain cash or block new
   risk. The default is `label_only`; the paper command and tests exercise the
   stricter choices.

4. **Transaction costs are planning estimates, not recorded fees.**
   `Transaction.fee` remains `NULL` because no broker fee source exists. The plan
   reserves and reports `estimated_cost_usd`, but applying the plan does not debit
   it from the ledger. That is conservative for the cash buffer and preserves the
   Phase 3 NULL-versus-zero contract. A consequence is that the ledger's actual
   cash after application can be slightly higher than the plan's post-cost
   estimate.

5. **Application is atomic rather than partially applied.** The specification
   allows atomic "where feasible", or leg-by-leg with funding legs first. This
   implementation chooses one database transaction for the whole plan, with
   selling legs processed and revalidated first. Any failure rolls the
   transaction back and marks the plan failed; no earlier fill survives and no
   failed sale's proceeds are used. A reviewer who expected a partially applied
   plan will see `failed`, not `partially_applied`.

6. **Revalidation is explicit.** A changed `portfolio_version` is a refusal, not
   an automatic retry: an automatic re-derivation could silently change the
   decision the model made. `revalidate_plan` must be called, and it re-projects
   the stored requested weights against current holdings, cash and limits.

7. **No HTTP endpoint or frontend was added in Phase 4.** The output contract
   was the stored `PortfolioPlan` JSON (`plan_as_dict`) and the paper command's
   JSON. That kept the change out of `frontend/` and out of the Phase 1/2/3
   response shapes. The only Phase 4 API change was additive:
   `PortfolioSerializer` published the three new limit fields, because an
   existing test requires every limit on the model to be visible in the
   portfolio payload. **FIX-08 below supersedes this for the advisory plan
   workflow**; the Phase 4 statement describes the Phase 4 change only.

8. **V4 in the experiment manifest remains unimplemented.** Phase 4 delivers the
   allocation service, but no V4 experiment arm was scheduled or measured.
   `MEASUREMENT.md`'s V4 declaration is unchanged; marking it implemented without
   a run would be exactly the kind of unmeasured claim Phase 3 refused.

9. **The paper evaluation is synthetic.** `phase4-allocation.v1.json` is an
   authored fixture. Its covariance/correlation numbers are computed from the
   supplied synthetic series, but the series, prices, classifications and
   candidate cases are not realised history. The command labels the provider
   `stub` and the source `fixture`.

### FIX-08 — portfolio CIO adapter, submission and plan approval

The acceptance review found that the documented allocation command still used
the stub and a synthetic paper account, that `POST /api/decision-runs/` did not
exist, and that the UI exposed decision runs but no portfolio-plan review or
approval workflow. FIX-08 adds an authenticated idempotent submission, a
provider-backed adapter under the protocol's budgets, advisory plan review and
approval endpoints, a review screen, and one real recorded provider run.

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| Authenticated, idempotent submission | `services.allocation_submission`, `POST /api/decision-runs/`; request-hash conflict is 409, replay returns the first submission; 202 with `run_id` + status URL | `SubmissionApiTests.*` |
| Provider-backed CIO adapter | `services.allocation_provider.BudgetedAllocationClient` wraps the `StageClient` seam with `ProtocolBudget`; live and stub are explicit, and the stub can never be selected implicitly | `ProviderAdapterTests.*`, `LiveSubmissionIntegrationTests.test_a_live_submission_flows_through_the_provider_adapter_and_records_usage` |
| Advisory review surface | `services.portfolio_review.build_plan_review`, `/api/portfolio-plans/` list/detail; frontend `#/plans` | `ReviewAndApprovalTests.test_review_shows_holdings_weights_requested_vs_constrained_cash_sector_costs`, `PortfolioPlansPage.test.tsx` |
| Approval bound to an immutable version and expiry | `services.portfolio_approval`: approval names `plan_version` + `approval_hash` + `expires_at`; revalidation bumps the version and clears the approval; `apply_plan` rechecks under the lock | `test_approval_binds_the_exact_version_hash_and_expiry`, `test_a_material_change_bumps_the_version_and_requires_new_approval`, `test_an_expired_approval_cannot_execute` |
| Rejection/cancellation audit | `PlanApproval` rows plus `portfolio.plan.rejected` / `.cancelled` durable events | `test_rejection_and_cancellation_write_audit_rows_and_events` |
| No trade on submission or approval | Submission and approve paths never call `execute_trade`; only `POST /api/portfolio-plans/<id>/apply/` does, and it requires a current approval | `test_submission_returns_202_a_run_id_and_a_status_url_without_a_trade`, `test_apply_without_approval_is_refused_and_writes_no_ledger_row` |
| Declared data policies | Missing prices/classifications/history produce typed flags, `data_quality.limitations` and policy actions; no sector or correlation is inferred | `ReviewAndApprovalTests.*`, `SubmissionServiceUnitTests.test_collect_quotes_uses_cost_fallback_only_when_allowed_and_labels_it`, recording artifact `data_quality` |
| Recorded live example | `docs/spec/fixtures/fix08-live-allocation.v1.json`: `mode: live`, `source: anthropic`, model `claude-haiku-5-5`, 5005/3020 tokens, estimated `$0.002010`, billed `null`, plan `proposed`, `traded: false` | `RecordedLiveArtifactTests.*` |

Divergences recorded rather than absorbed:

1. **The submission record is `AllocationSubmission`, not a `DecisionRun`.** The
   existing `DecisionRun` is per instrument. A portfolio allocation is one
   decision over the whole portfolio and one plan; N decision runs would either
   duplicate the portfolio call or invent a ticker. `POST /api/decision-runs/`
   is the route the review proposed, and its status URL points at the
   submission.
2. **Submission executes inline.** A single portfolio call cannot rely on a
   Celery worker being present, and a queued task with no worker would leave the
   run `accepted` forever. The response is still 202 with the status URL; the
   submission row is committed before the model call so a crash is visible as
   `running`. This is not an asynchronous job, and the status URL is the
   authority.
3. **The live recording is one real model call with no retry and no repair.**
   `max_repairs=0` and `provider_retries=0` mean a malformed answer is recorded
   as a failure rather than re-prompted. That keeps spend bounded and avoids
   presenting a repaired second sample as the first. The artifact's input data
   is operator-supplied demonstration data, labelled `operator_supplied` and
   `input_data_is_synthetic: true`; the model response is real
   (`synthetic_response: false`, `run_kind: real_recorded_model_run`).
4. **Billed cost remains unknown in the recording.** The adapter's provider seam
   reports tokens, not a provider-billed dollar amount. The artifact records
   `billed_cost_usd: null` with the reason and the price-table estimate
   separately, following FIX-07A. It does not present the estimate as a bill.
5. **The submission endpoint does not fetch price history.** It declares
   `risk_statistics: not_available` and the `label_only` policy rather than
   inventing a correlation. A caller that needs correlations must supply history
   to the brief builder explicitly (the Phase 4 service still supports it).
6. **Approval expiry is capped at 7 days by default.** The approval is a bounded
   permission over a plan that the portfolio can outgrow. It is not renewable by
   a retry: an expired approval must be re-approved for the current version.
7. **The apply endpoint is an addition beyond the review's list.** The review
   asked for list/view/approve/reject; without an explicit apply, approval would
   have no route to the shared execution service that the acceptance criterion
   requires. Applying is a separate, confirmed act and remains the only route to
   `execute_trade`.
8. **V4 remains unimplemented as an experiment arm.** FIX-08 supplies the
   allocation provider and approval workflow, but no V4 arm has been scheduled
   or measured. The committed experiment manifest/report still records the older
   reason that its dependency is not done; that reason is now stale as a
   description of the code and remains accurate only as "V4 has not been run".
   Marking it implemented without a run would repeat the unmeasured claim Phase
   3 refused.
9. **The UI is a sibling hash route, not a dashboard restructure.** `#/plans`
   uses the existing panels, badges and dark palette. It presents a stub as
   "Stub — no model call"; it never lets a synthetic response read as a live
   model decision.

## Phase 5 — publish evidence

The final phase is documentation, not a new subsystem. Its gate is that **every
public claim traces to reviewed evidence**, and it adds four artefacts:

| Deliverable | File | What it establishes |
| --- | --- | --- |
| Methodology report | [`METHODOLOGY.md`](METHODOLOGY.md) | What signal diagnostics, portfolio performance, Sharpe, sessions and counterfactuals do and do not measure |
| Curated cases | [`CASES.md`](CASES.md) | Disagreement, risk intervention, uncertainty and outcome cases, with the exact fixture evidence and the reason each was selected |
| Revised demonstration | [`DEMO.md`](DEMO.md) | Exact offline commands for the Streamlit guard demo, the decision-detail replay, the paper allocation and the report build, with an accurate mode label for each |
| Claims index | [`CLAIMS.md`](CLAIMS.md) | Every public claim in `README.md` and `docs/wiki/`, its source, every correction made in this phase, and every claim that could not be traced |

The contributor map in [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) records
where each phase lives, how to run each test layer, the additive-migration rule
and the fixture-loading commands.

Phase 5 changed no application code. It corrected public claims that the code did
not support, including the description of the Streamlit demo, the ADR and incident
counts, the outdated security advisory, the stale test counts in the wiki and the
live scorecard figures that no committed file contained. Those corrections are
listed in [`CLAIMS.md`](CLAIMS.md) §3 and §4.

The committed evidence is still what the earlier phases produced: the three
synthetic decision fixtures, the synthetic Phase 3 experiment report and the
synthetic Phase 4 paper allocation. No live run was added, and no number in the
new documents is quoted from a live database.

## P1 remediation — export portability and recoverable analysis jobs (steps 5 and 6)

The acceptance review made two P1 findings: a detail export from a real run
could not be imported by the built viewer (the browser canonical serialiser
hashed `90` where Python hashed `90.0`, so every payload containing a JSON float
failed its checksum), and `POST /api/decision-runs/` called the provider before
returning `202`, so a slow model blocked the request and a process death left no
recovery path. Both are fixed here. The FIX-08 statement that submission executes
inline is superseded by §6 below.

### 5 — exported runs are portable

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| The browser importer supports every field the authenticated endpoint emits | `frontend/viewer/bundle.ts` starts from the producer's objects and only normalises the fields the replay joins on; the full run envelope, cost/failure/protocol/stage-state blocks, `guard_evaluations`, `superseded_outcomes`, `outcome_schedule`, `snapshot`, `verdict`, `limitations` and per-event extras are preserved. The verified document is kept as `raw_export`. | `exportParity.test.ts` (`imports the committed sample unchanged`, `preserves every guard evaluation…`), `ViewerExportRoundTrip.test.tsx` |
| One canonical serialisation in both runtimes | `core.decision_json.canonical_export_json` and `frontend/viewer/bundle.ts:canonicalJson`: keys sorted by UTF-16 code unit, compact UTF-8, ECMAScript number formatting (so `90.0` is `90`), `Decimal` as normalised fixed-point text, JSON.stringify-style string escaping. | `docs/spec/fixtures/canonical-json.v1.json` read by `test_p1_export_portability.py` **and** `exportParity.test.ts` |
| The digest covers the stored payload, not a re-serialised copy; edits fail and versions are named | `build_export_bundle` normalises to the served primitives (`export_json_safe`) before hashing and returns that exact document; `verify_export_bundle` recomputes from the parsed document; `importDecisionExport` verifies the parsed file. Unsupported versions raise/report the field name and value. | `ExportSampleVerificationTests`, `ExportEndpointShapeTests`, export tests in `exportParity.test.ts` |
| Guards, superseded outcomes and the synthetic fixtures survive | The exporter already published the full collections; the importer now keeps them instead of projecting them away. `docs/spec/fixtures/decision-run-export.v1.json` carries two guard checks and a superseded outcome; the three committed synthetic fixtures still import under 1.0. | `test_the_sample_preserves_the_two_guard_case_and_superseded_outcome`, `ViewerRunView — guard replay`, `committed synthetic fixtures…` |
| The endpoint, not only the fixture loader, is tested | The sample is captured from `GET /api/decision-runs/<id>/export/`; `scripts/build_export_sample.py` regenerates it; the Python suite compares a fresh endpoint export's recursive shape with the committed sample and the Vitest suite imports those bytes. | `ExportEndpointShapeTests.test_the_endpoint_shape_matches_the_committed_sample` |

A second real-run export defect was reproduced while building the sample:
`outcome_payload` passed a stored ISO `reference_at` string to `utc_iso`, which
assumes a datetime, so an outcome written with a string measurement basis made
the export endpoint return HTTP 500. It now passes an already-serialised string
through unchanged.

Divergences recorded rather than absorbed:

1. **A capture without `schema_version` is still accepted.** Captures produced
   before this change have only `bundle_version`; when `schema_version` is
   absent the viewer treats it as the bundle version. A capture *with* the field
   is checked by name. This keeps old files importable without weakening the
   version refusal for new ones.
2. **Decimal strings are opaque to the digest.** A `Decimal` is converted to the
   declared fixed-point form before it is served, but a value that is already a
   string remains exactly that string. Normalising arbitrary strings would make
   a text edit that changed a digit disappear from the checksum.
3. **JSON numbers are normalised, not tagged.** `90.0` and `90` are the same
   JSON number and hash the same; a change to `91` does not. The alternative -
   a tagged decimal encoding - would change the API's field types.

### 6 — portfolio analysis is a recoverable job

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| `202` after only the durable record | `submit_analysis` writes `AllocationSubmission` with `queued_at`, `deadline_at`, `max_attempts` and returns; `DecisionRunListView.post` dispatches `tasks.run_allocation_submission_task` after the row is committed. No provider call is in the request. | `test_6a_the_response_returns_before_the_model_is_called` |
| Explicit ownership, attempts, durable idempotency key, visible state | `claim_submission` uses a row lock, lease, owner and incrementing `attempt_count`; `idempotency_key` + `request_hash` remain the unique durable key; `submission_as_dict` publishes status, owner, attempts, deadline and terminal time. | `test_6a…`, `test_6b_a_crash_after_the_provider_result_resumes_without_a_second_spend` |
| Atomic, monotonic transitions; result before side effects | Provider result and model metadata are persisted at `_checkpoint_provider_result` before `create_plan`; plan and `succeeded` are committed in one locked transaction that re-checks terminality, owner, cancellation and deadline. A terminal row is never reopened. | `test_6b…`, `test_6c_a_cancelled_running_submission_cannot_attach_its_persisted_result`, `test_6b_a_timed_out_submission_creates_no_plan_and_applies_no_trade` |
| Six distinct states and a pollable status | `AllocationSubmissionStatus`: `queued`, `running`, `succeeded`, `failed`, `cancelled`, `timed_out`. `GET …/status/` publishes `terminal`, `terminal_at`, `failure_reason`, `attempt_count`, `claimed_by`, `deadline_at`, plan link and `Retry-After` while open. | status endpoint tests in `test_p1_allocation_jobs.py` |
| Cancellation cannot later trade | `POST …/cancel/`; the final locked step refuses a cancelled row, so no plan exists for `apply`. Cancelling a terminal row is 409. | `test_6c_a_cancelled_queued_submission_never_runs`, `test_6c_a_cancelled_running_submission_cannot_attach_its_persisted_result` |
| Contract preserved | 202 still returns `run_id` and status URL; key reuse with a different body is still 409; submission still creates no ledger row. | `test_submission_returns_202…`, `test_reusing_a_key_with_a_different_payload_is_a_409` |
| Repair path, dry run by default | `python manage.py repair_allocation_submissions`; `--execute` applies. It times out a past-deadline row, fails an unreconciled provider call, or re-queues a worker that died before the paid call. JSON on stdout; summary on stderr. | `test_6d_the_repair_command_reports_a_stuck_worker_without_changing_it`, `test_6d_a_lost_worker_before_the_paid_call_is_requeued`, `test_6d_an_unreconciled_provider_call_is_failed_not_retried` |

Divergences recorded rather than absorbed:

1. **A crash *during* the provider call is unreconciled.** The worker persists
   `provider_call_started_at` before the call and the validated result after it.
   If the process dies in between, the repair path marks the submission `failed`
   with `provider_call_unreconciled` instead of calling the provider again: the
   first call may already have been billed, and a silent second spend would be
   worse than a typed failure. The 6B retry case therefore resumes from a
   persisted provider result; the in-flight-call case is the documented failure.
2. **A resumed attempt rebuilds the brief from current state.** The persisted
   provider result is reused, but quotes, classifications and limits are read
   again when the plan is created. The plan records the proposal's
   `portfolio_version` and sets `revalidation_required` when the freshly
   evaluated version differs, so a changed portfolio cannot be silently applied
   under the old proposal; an operator must revalidate and approve.
3. **The endpoint now needs a worker.** A queued submission with no worker stays
   `queued` with a deadline and a `Retry-After` status; it is not executed
   inline as it was in FIX-08. That is the point of the change, but it is a
   deployment requirement, and the repair command exists for the case where the
   worker or broker is absent.
4. **No second scheduler was introduced.** The worker is the existing Celery
   application (`tasks.py`), using the existing broker and worker configuration.
   The repair command is a management command, not a beat schedule: a repair
   that runs automatically could re-queue a row an operator is investigating.
5. **Attempts are bounded and not auto-retried.** A broker redelivery does not
   increment the attempt count unless it can claim a lease; `max_attempts`
   (default 3) bounds repair re-queues. Exhaustion is `failed` with
   `attempts_exhausted`.

## P1 remediation — experiment isolation and simulated time (steps 7 and 8)

The acceptance review made two P1 findings: the comparison report always
returned the registered seven-day metric as `null`, and experiment executions
shared decision state with each other and with live runs; the simulator applied a
later-priced fill immediately after the decision, and an archived report could
not be rebuilt. Both are fixed here, without a model call.

### 7 — isolated executions and the registered metric

Reproduced before the repair:

* `build_comparison_report` wrote `primary_metric_result.value = None` and
  `reason = "no_matured_outcomes"` unconditionally. The committed opportunity
  set has 2026-02-11 closing marks, which reach the AAPL and MSFT 7-day
  horizons, so the data supported a value; the report still returned `null`.
* two executions for two portfolios shared all 12 `DecisionRun` rows. The second
  portfolio owned zero runs and its report quoted the first portfolio's decision
  ids, snapshots and spend.
* two opportunity sets with the same `opportunity_set_id` and different tickers
  reused the first set's `fixture_id`s, because the id did not include the
  content hash.
* V4's null was a hard-coded placeholder beside an already-excluding
  denominator.

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| Execution namespace scoped to portfolio, content and provider | `execution_namespace`; namespace suffixes `fixture_id`; snapshot version includes the opportunity-set content hash; `cost_breakdown.scope` | `test_two_portfolios_do_not_share_decisions_snapshots_or_spend`, `test_two_universes_with_the_same_key_do_not_reuse_each_other_s_decisions` |
| Metric definition read from the registration | `validate_opportunity_set` requires a supported id/family and a positive horizon; `compute_registered_metric` reads `pre_registration.primary_metric` | `test_the_registered_definition_controls_the_id_and_horizon`, `test_an_unsupported_registration_is_refused_instead_of_returning_a_null` |
| Value, denominator, dispersion and contributing runs | `primary_metric_result.status/value/denominator/dispersion/interval/contributing_runs/by_arm` | `test_a_supported_horizon_carries_a_value_denominator_and_dispersion` |
| Null means a named missing input | `no_usable_observations` + `missing_input` + `excluded_by_reason` | `test_a_run_without_the_registered_horizon_marks_is_null_with_a_specific_reason` |
| V4 visibly unimplemented and outside the denominator | arm `implemented: false`, `status: not_executed`, `in_denominator: false`; absent from `denominators.implemented_arms` and `by_arm` | `test_a_supported_horizon_carries_a_value_denominator_and_dispersion` |
| Committed fixtures recompute value and hash | `experiment_comparison.v2` embeds the opportunity set, snapshots, decisions and methods; `rebuild_experiment_report` reproduces the archive | `test_the_committed_archive_reproduces_its_value_and_hash` |

### 8 — monotonic simulated time and archived rebuilds

Reproduced before the repair: with AAPL's decision at 14:30 and its first
executable mark at 14:35, the pre-fix simulator applied the fill immediately.
A second decision at 14:31 already saw cash `9749.44` and an AAPL position; it
should have seen the opening `10000.00` and no position. The trajectory carried
no timeline, and there was no rebuild command.

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| Monotonic clock; later fill deferred, not applied | `experiment_simulation.simulate_arm_timeline`: pending fills are applied only when the clock reaches their mark; stable reason `fill_priced_after_simulated_clock` | `test_a_fill_priced_after_the_clock_is_deferred_and_invisible_to_the_next_decision`, `test_the_same_sequence_with_a_price_at_the_decision_fills_immediately` |
| A decision may not see its own future | opportunity-set validation rejects an `analysis_cutoff` or quote after the scheduled instant; the simulator rejects it with `decision_input_after_simulated_clock` | `test_a_decision_snapshot_from_its_own_future_is_rejected_with_a_stable_reason` |
| Every step is auditable | `trajectory.simulation.steps` carries clock, known marks/quote, decision, fill, cash/positions after; `steps_hash`; `monotonic` | same tests |
| Replay is deterministic | no wall-clock in steps; the recorded runs and decisions are the only inputs | `test_replaying_twice_produces_byte_identical_decision_records` |
| Archived report rebuilt from recorded inputs | `services/experiment_rebuild.py`; `python manage.py rebuild_experiment_report`; dry run by default; differences named by path; unsupported version refused by name | `test_rebuild_reproduces_the_numbers_and_hash`, `test_an_altered_input_names_the_difference`, `test_an_unsupported_format_version_is_reported_by_name`, `test_the_rebuild_command_is_dry_run_by_default_and_execute_writes` |
| Synthetic labelling survives | `honesty.simulated_content`, `synthetic_label: synthetic_simulation`, `real_market_outcome: false`; per-arm trajectories keep `provenance.synthetic` | `test_rebuilt_synthetic_content_stays_labelled_everywhere` |

Divergences recorded rather than absorbed:

1. **The headline signal metric pools the usable observations of the
   implemented arms.** The per-arm values are published in `by_arm` and are the
   comparison; the pooled value is the registered statistic over the experiment's
   observations, with V4 excluded. A reader must not read the pooled number as a
   protocol ranking.
2. **A deferred fill is applied at its mark time, not rejected.** The fill rule
   is `first_executable_mark_at_or_after_decision.v1`; rejecting every fill whose
   mark is later would make a daily-close simulation impossible. The deferral is
   visible in `orders[].fill_rule_status` and the timeline, and a fill whose mark
   is after the window is `expired`, not silently dropped.
3. **A rebuild proves the report, not the model.** It replays the recorded
   decisions and marks; regenerating the same model output is outside its scope.
   A method-version mismatch is named as a difference rather than hidden behind
   today's constants.
4. **LLM cost timing uses the decision's recorded time**, clamped to the window
   end, so the original build and the rebuild attribute external spend at the
   same instant. This is spend timing, not a portfolio flow.
5. **The archive is larger.** Embedding the opportunity set, full snapshot
   payloads and decisions roughly doubles the comparison JSON. That is what
   makes a database-free rebuild possible; the alternative was a rebuild that
   read today's fixture file.
6. **V4's reason no longer names FIX-08 as an unfinished dependency.** FIX-08
   supplies the allocation path, but no V4 arm has ever been scheduled or
   measured, so the report says exactly that and keeps V4 outside the
   denominator.

## P1 remediation — provider cost accounting and verified acceptance (steps 9 and 10)

The acceptance review made two P1 findings: the daily limiter counted known
billed charges only while the main provider adapter reports no dollars at all,
and the recorded demo had no acceptance record tied to the commit that produced
it. Both are fixed without changing the provider adapter's honest behaviour - it
still reports tokens and no bill - by making the limiter reserve before the call
and reconcile afterwards, and by making recording refuse a state the verifier
did not accept.

### 9 — the daily limiter is a conservative, durable reservation ledger

Reproduced before the repair: one run recording 500 input / 300 output tokens
and `estimated_cost_usd = 0.00150` with `api_cost_usd = NULL` produced
`spent_usd = 0.00`, `estimated_spend_usd = 0.00` (the cents quantiser rounded
the estimate away), and `exhausted = False` against a `$0.001` ceiling. A second
call would have been dispatched with the day already over the limit.

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| Reserve an unknown cost before the call | `services.budget.check_and_reserve`; called by `tasks.run_alpha_agent_task` and the live allocation path. The bound is `AI_RUN_COST_UPPER_BOUND_USD`, falling back to the protocol's declared monetary ceiling; a zero bound is replaced, never used. | `test_the_provider_is_not_called_when_the_reserved_bound_breaches`, `test_the_reserve_is_visible_while_the_call_is_open` |
| Reconcile tokens-without-dollars to a labelled estimate | `services.budget.reconcile_decision_run` / `reconcile_report`; `settle_call` stores billed, estimated and unknown as distinct states. A billed figure is never synthesised from the price table. | `test_tokens_without_dollars_reconcile_to_an_estimate_not_zero` |
| A provider that reports nothing stays unknown | `settle_call` keeps `state="unknown"`, a typed `unknown_reason`, `billed_usd=None`, `estimated_usd=None` and the reservation held. | `test_a_provider_that_reports_nothing_stays_unknown_not_zero` |
| Refuse before the provider call | The task checks the reservation before it builds context, reads quotes or calls `run_alpha_agent`; the allocation worker does the same before `provider_call_started_at`. Stable reasons: `ai_spend_reserve_exceeded` when the bound crosses the ceiling, `ai_spend_ceiling` when the day was already at it. | `test_the_provider_is_not_called_when_the_reserved_bound_breaches` |
| Durable and restart-safe | `core.models.AIUsageLedger` has a unique `call_key`, a UTC `day` and state columns; `check_and_reserve` serialises same-day reservations with `pg_advisory_xact_lock` and returns the existing row on replay. | `test_a_reservation_is_durable_across_a_connection_restart`, `test_replaying_a_settled_key_changes_nothing` |
| Expose spent, reserved, remaining and unknown | `BudgetState` / `budget_state().as_dict()` publishes `spent_usd`, `estimated_spend_usd`, `reserved_usd`, `unknown_reserved_usd`, `committed_usd`, `remaining_usd`, `unknown_cost_runs`, `unknown_amount_usd: null` and `basis: "lower_bound"`. The portfolio payload already exposes it as `ai_budget`. | `test_the_state_distinguishes_every_basis_and_reconciles` |

Divergences recorded rather than absorbed:

1. **The reservation is an upper bound, not a forecast.** A run that ultimately
   costs a fraction of a cent still holds the declared bound until it settles.
   That can refuse a call that would in fact have fitted, which is the intended
   direction of the error for a spend ceiling.
2. **An unknown charge keeps its reservation.** The operator cannot prove the
   call was free, so the day's committed total stays conservative and
   `unknown_amount_usd` is null. The state says the total is a lower bound; it
   does not invent a number for the missing charge.
3. **Legacy `AgentDecisionLog` rows are counted but not retro-reserved.** Rows
   written before the ledger have no reservation to hold. They contribute their
   billed or estimated figures and are counted in `unknown_cost_runs`; they are
   never given an invented reserve after the fact.
4. **Only real (live) allocation calls are reserved.** A stub run makes no
   provider call and has no cost to reserve; its plan already records the stub.
5. **The provider adapter is unchanged.** `CrewAIStageClient` still returns
   `cost_usd=None` with a reason; the fix is in what the limiter does with that
   silence, not in pretending the adapter received a bill.
6. **The ledger is not a provider invoice.** A charge reported after the run
   ends, or for a call this process never made, is outside it. `basis:
   "lower_bound"` says so on every payload.

### 10 — acceptance is verified before an artefact is recorded

Reproduced before the repair: there was no verification command and no
acceptance record; `record_live_allocation` would record from any working tree;
nothing checked that a committed recording named a passing commit. The
committed export and FIX-08 recording therefore carried no commit provenance at
all.

| Requirement | Where it is enforced | Evidence |
| --- | --- | --- |
| One command runs every check and exits non-zero on failure | `scripts/verify_application.py`; stages run sequentially: backend tests, frontend tests, type check, build, lint, format, migration check and an HTTP smoke test. `VERIFY_SUMMARY` is one machine-readable JSON line. | `test_a_passing_stage_reports_its_exit_and_count`, `test_a_failing_stage_exits_non_zero_with_the_stage_named`, `test_an_injected_failure_makes_the_verdict_non_zero` |
| Acceptance file tied to the commit | `--record` writes `docs/spec/acceptance/<commit>.json` with commit, subject, tree state, verdict, per-stage results, parsed test counts, UTC timestamp and `output_digest`. Durations, paths and timestamps are excluded from the digest, so the same commit reproduces the verdict and digest. | `test_the_record_contains_the_commit_stages_counts_and_digest`, `test_re_running_the_same_commit_reproduces_the_verdict_and_digest` |
| Refuse a dirty tree | `services.recording_provenance.require_clean_for_recording` reads real `git status` and refuses with stable codes; `record_live_allocation` calls it **before** configuring the provider, so a refusal spends nothing. The exactly expected untracked acceptance file for HEAD is ignored so the documented verify-then-record order is possible; every other change is refused. | `test_an_uncommitted_change_is_refused_before_recording`, `test_a_clean_tree_with_a_passing_record_is_allowed`, `test_a_clean_tree_without_an_acceptance_record_is_refused` |
| Every committed recording names commit + digest, and the pairing is checked | The Step 5 export builder and `record_live_allocation` emit a stable `recording` block; `python manage.py check_recordings` loads the published record for the named commit and fails on absent/incomplete provenance, a missing record, a digest mismatch or a non-passing verdict. | `test_a_matching_artefact_passes`, `test_a_different_commit_fails`, `test_a_different_digest_fails`, `test_a_missing_provenance_block_fails`, `test_every_committed_recording_names_a_passing_record` |
| Exact commands are documented | [`ACCEPTANCE.md`](ACCEPTANCE.md) is the definition-of-record; [`DEMO.md`](DEMO.md) §0 quotes the command and the recording order. | documentation-link tests |

Divergences recorded rather than absorbed:

1. **A commit cannot contain the acceptance record that names its own hash.**
   The documented order is commit, verify `--record`, record the artefact, then
   commit the artefact and record together. The guard ignores exactly the
   untracked `docs/spec/acceptance/<HEAD>.json` file; every other change remains
   a refusal. After the record is committed, HEAD has moved on, so a *new*
   recording correctly needs a new verification of the new commit.
2. **The record proves the checks, not the model.** It says the backend,
   frontend, build, lint, migration and smoke checks passed on that commit. It
   does not prove a provider response is authentic or that a recorded run is
   profitable; those remain the artefact's own labels and the review's job.
3. **`--allow-dirty` exists for developers.** It writes a record marked
   `tree_clean: false`, `usable_for_recording: false`; the recording guard
   refuses such a record. The dirty record cannot overwrite a clean one unless
   the caller points it at the published directory deliberately.
4. **The smoke stage needs a running stack.** It fails, rather than skips, when
   the URL is unreachable, so a CI job that forgot to start the application
   cannot produce a passing verdict. `--skip smoke` is explicit and, without
   `--allow-skipped`, is also a non-zero verdict.
5. **Existing committed recordings had provenance attached when the mechanism
   was introduced.** The FIX-08 artefact predates it; its block says
   `provenance_kind: "attached_at_remediation"` and names the commit whose clean
   acceptance record was used. It does not claim the recorder emitted the block.
   New recordings write it at creation time.
