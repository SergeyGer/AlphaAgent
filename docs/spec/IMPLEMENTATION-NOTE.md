# Implementation note — Technical Specification v1.0

This records the Phase 0 baseline the specification requires, and what is being
built against it. The specification is a five-phase programme; this note covers
Phase 0 in full, and Phase 1, Phase 2 and Phase 3 in implementation.

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
8. **Benchmark interval matching is declared, not re-derived.** Stored
   `OutcomeRecord` rows carry a benchmark return but no benchmark observation
   timestamps, so the report can declare the same-decision/same-horizon rule and
   display the method version, but it sets `benchmark_interval_verified: false`.
   Recomputing the benchmark would require a benchmark price history that the
   stored rows do not contain.
9. **`build_experiment` rebuilds rather than archives every report version.** The
   latest report is stored on the experiment with an incremented `report_version`;
   earlier versions are not retained as separate rows. Replay is deterministic
   from the stored rows and the manifest checksum, so the previous report can be
   regenerated, but it cannot be fetched by version number. Adding a report-history
   table was not necessary for the Phase 3 gate and would have been a second source
   of drift.
