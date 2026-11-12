# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **FIX-08 [P1] — the portfolio advisory product, with one real recorded model run.**
  The allocation service existed but nothing could submit a plan, review it, approve
  it or execute it, and the documented command used a stub client.

  - **`POST /api/decision-runs/`** — authenticated submission returning 202 with a run
    id and status URL. An idempotency key plus a canonical request hash means a repeat
    returns the same run and a changed body under the same key is a 409. A submission
    never writes a ledger row.
  - **A provider-backed CIO adapter** (`BudgetedAllocationClient`) over the real
    protocol seam, with the budget enforcement from FIX-07: deadline, token caps,
    retries and a monetary ceiling, and the five cost quantities kept apart. Stub mode
    is selectable only by name; live mode requires a configured provider.
  - **Review and approval** — `GET /api/portfolio-plans/` and `/<id>/` return holdings
    and weights, requested versus constrained allocation, cash and sector effects,
    funding, costs, rejected alternatives and data-quality limits. Approval binds the
    exact plan version, permission hash and expiry; a material change bumps the
    version and requires a new approval; rejection and cancellation write audit rows.
  - **A review screen** at `#/plans`, using the existing panels and palette. A stub
    plan is labelled "Stub — no model call", and apply is a separate confirmed act.

  **One real model call was made and recorded** as
  `docs/spec/fixtures/fix08-live-allocation.v1.json`: `mode: "live"`,
  `source: "anthropic"`, `model: "claude-haiku-5-5"`,
  `run_kind: "real_recorded_model_run"`, `synthetic_response: false`. One call, no
  retries, five thousand and five input tokens against three thousand and twenty
  output, a price-table estimate of `$0.002010`, and a billed cost of `null` because
  the provider reports tokens rather than dollars. The plan came back proposed with
  two sell legs, approval pending and **zero ledger rows** — the submission never
  trades.

  The artifact also records what it is not: `input_data_is_synthetic: true`, because
  the brief is operator-supplied demonstration data rather than realised history. A
  real model answering a synthetic brief is labelled as exactly that.

  **36 new backend tests and 11 frontend tests**, covering the 202 and status URL, key
  reuse, the 409, declared-not-dropped tickers, missing classification and price
  policies, approval binding and expiry, apply without approval refused, apply through
  the shared execution service, and rejection and cancellation audit rows.

### Fixed

- **Remediation steps 1–10 [P0/P1] — ten remediation steps from the acceptance
  review, and the mechanism that verifies the repair.** Each finding behind them was
  reproduced against the tree before anything changed. The suite at `7969cd7` is **955 backend tests and 276
  frontend tests**; ruff, the format check and the migration check are clean.

  **Step 1 — quote provenance and approval binding (`602a899`).** Observation times
  now travel from provider to cache to quote collection to proposal to approval to
  application. A cache hit keeps the cached UTC instant; a daily bar maps to the real
  16:00 New York close. An unknown observation time stays unknown and is treated as
  missing data, which is what stops an untimed mark authorising a fill. The shared
  check in `services/quote_policy.py` validates the traded instrument and every
  material holding used for exposure, with per-source and per-instrument ceilings.
  Approval persists the side, quantity, reference price, observation time, tolerance,
  expiry and policy versions the operator actually saw, so the drift check no longer
  compares the current price with itself. Proved by
  `test_p0_execution_remediation.py::test_untimed_quote_is_missing_data_not_a_current_mark`,
  `test_stale_traded_quote_is_refused_with_a_stable_reason` and
  `test_price_drift_returns_revised_terms_and_writes_no_fill`.

  **Step 2 — execution and audit commit together (`602a899`).** The fill, final guard
  evaluation, decision-run link and state, run event and durable outbox row now commit
  inside the ledger transaction; model calls and notifications stay outside it. A
  repeated execution key returns the original transaction and run id, and
  `manage.py repair_execution_audit` is dry-run by default. Plan failure and staleness
  updates are conditional on status, version and attempt, so a delayed failure can no
  longer overwrite a successful apply. Proved by
  `test_injected_audit_failure_rolls_back_then_retry_writes_one_fill`,
  `test_one_connection_fails_while_another_applies_and_delayed_failure_is_ignored` and
  `test_a_suppressed_duplicate_run_is_terminal_and_links_the_existing_recommendation`.

  **Step 3 — attribution across every horizon (`76c3017`).** The starting price was
  read from a `Transaction.created_at` field that does not exist, and the scheduler
  excluded any decision that already had an outcome, so a day-one score made days
  seven and thirty permanently ineligible. Executed decisions now measure from the
  linked fill's stored price and timestamp; HOLD and rejection keep a proposal-close
  basis with no fabricated fill. `OutcomeEvaluation` is keyed by decision, horizon and
  method version, so each horizon is selected independently. The scorecard and each
  detail outcome publish `portfolio_performance: false` and `summable: false`, and
  `manage.py backfill_outcome_records` corrects older revisions from the fill while
  preserving the v1 row. Proved by
  `test_p1_attribution_remediation.py::test_the_unmocked_start_price_is_the_recorded_fill_not_the_proposal_close`,
  `test_a_day_one_score_does_not_prevent_days_seven_and_thirty` and
  `test_apply_recovers_horizons_preserves_provenance_and_is_idempotent`.

  **Step 4 — replay order and late outcomes (`76c3017`).** The shared replay reducer
  painted the final guard record from the first guard event, so a cursor before
  adjudication showed the eventual rejection instead of the approved quantity true at
  that instant. Detail and export now publish every guard evaluation with a stable id,
  and the reducer resolves the latest one visible at the cursor. Scoring a completed
  run now writes a versioned `outcome.recorded.v1` event and outbox row in the same
  transaction as the result and projection, so an open view learns that a later
  horizon matured; a retry does not append a second event. Proved by
  `test_p1_replay_remediation.py::test_detail_publishes_every_evaluation_with_its_identifier`
  and `test_a_completed_run_keeps_its_execution_state_and_records_the_late_outcome`.

  **Step 5 — portable exports and cross-language canonical JSON (`f54b2bf`).** The
  browser canonical serialiser hashed `90` where Python hashed `90.0`, so an untouched
  export failed its own digest. Both runtimes now share one declared canonical form -
  sorted keys, compact UTF-8, ECMAScript number formatting and normalised fixed-point
  decimals - pinned by `docs/spec/fixtures/canonical-json.v1.json`, which a Python test
  and a Vitest test both read. The importer preserves the full run envelope, the whole
  `guard_evaluations` collection, superseded outcomes and the outcome schedule.
  Verification hashes the stored payload, names an unsupported version by field and
  value, and fails on a semantic edit anywhere. Proved by
  `test_p1_export_portability.py::test_every_shared_vector_matches_python` and
  `test_a_semantic_edit_anywhere_fails_verification`.

  **Step 6 — analysis is a recoverable job (`f54b2bf`).** `POST /api/decision-runs/`
  wrote a durable `AllocationSubmission` and returned before the provider was called.
  The worker owns an attempt count, an idempotency key and a visible state - queued,
  running, succeeded, failed, cancelled, timed out - and a retry cannot duplicate the
  plan or spend a second provider call for the same submission. A cancelled or timed
  out submission cannot later apply a trade. `manage.py repair_allocation_submissions`
  reports a stuck worker without changing it, and `--execute` times it out, fails an
  unreconciled provider call, or re-queues a worker that died before the paid call.
  Proved by
  `test_p1_allocation_jobs.py::test_6a_the_response_returns_before_the_model_is_called`,
  `test_6b_a_crash_after_the_provider_result_resumes_without_a_second_spend` and
  `test_6c_a_cancelled_running_submission_cannot_attach_its_persisted_result`.

  **Step 7 — isolated experiments and the registered metric (`4e6c71f`).** The
  comparison report gave the pre-registered seven-day metric a null value on every
  run because it was described in the pre-registration and never computed. It now
  reads the definition from the immutable pre-registration and computes it, publishing
  `mean_action_score_pct`, the scheduled/usable/excluded denominator with a reason per
  exclusion, sample deviation, min, max and a seeded time-block bootstrap interval. A
  null now means "not measurable from this data" with the missing input named.
  Experiment executions have their own id, runs, immutable input snapshots and scoped
  cost ledger, so two experiments cannot see or overwrite each other's decisions. Proved
  by
  `test_p1_experiment_isolation_metric.py::test_two_portfolios_do_not_share_decisions_snapshots_or_spend`
  and `test_a_supported_horizon_carries_a_value_denominator_and_dispersion`.

  **Step 8 — monotonic simulated time and report rebuild (`4e6c71f`).**
  `services/experiment_simulation.py` enforces a monotonic simulated clock: a fill may
  not be priced from an observation after the current instant, and the refusal carries
  a stable reason. Each step records the clock, the data available at that instant, the
  decision and the fill, so replaying the same fixtures twice produces byte-identical
  records. `manage.py rebuild_experiment_report` rebuilds an archived report from its
  recorded inputs, names the difference when an input does not reproduce, reports an
  unsupported format version by name, and keeps synthetic content labelled synthetic.
  It is a dry run by default. Proved by
  `test_p1_simulation_rebuild.py::test_a_fill_priced_after_the_clock_is_deferred_and_invisible_to_the_next_decision`,
  `test_replaying_twice_produces_byte_identical_decision_records` and
  `test_rebuild_reproduces_the_numbers_and_hash`.

  **Step 9 — the spend limiter counts unknown cost (`7969cd7`).** A day of real token
  spend looked free: the cents quantiser rounded a sub-cent estimate away, and the main
  adapter reports tokens with `cost_usd: None`, which the aggregate read as nothing to
  add. A new `AIUsageLedger` keyed by a unique call key reserves the declared upper
  bound before the provider call, refuses with `ai_spend_reserve_exceeded` or
  `ai_spend_ceiling` on the conservative figure before the adapter is touched, and
  reconciles afterwards. Provider dollars settle as billed; tokens with a price-table
  figure settle as estimated with billed null; no usable report stays unknown with the
  reservation held. The state publishes spent, estimated, reserved, committed,
  remaining, unknown-cost runs and `basis: lower_bound`, so an operator sees a floor
  rather than a fact. Proved by
  `test_p1_cost_limiter.py::test_tokens_without_dollars_reconcile_to_an_estimate_not_zero`,
  `test_a_provider_that_reports_nothing_stays_unknown_not_zero` and
  `test_the_provider_is_not_called_when_the_reserved_bound_breaches`.

  **Step 10 — a recorded artefact tied to a verified application (`7969cd7`).**
  `scripts/verify_application.py` runs backend tests, frontend tests, typecheck, build,
  lint, format, migrations and an HTTP smoke test, prints `VERIFY_STAGE` lines and one
  `VERIFY_SUMMARY` JSON, and exits non-zero on failure or a required skip. `--record`
  writes `docs/spec/acceptance/<commit>.json` with the commit, tree state, verdict,
  per-stage results and a deterministic digest. Recording refuses a dirty tree and a
  missing or failing record with stable codes, before provider configuration is read,
  so a refusal cannot spend. Committed recordings carry a `recording` block naming
  their commit and digest, and `manage.py check_recordings` fails on a mismatch. Proved
  by
  `test_p1_acceptance_verification.py::test_the_record_contains_the_commit_stages_counts_and_digest`,
  `test_a_clean_tree_without_an_acceptance_record_is_refused` and
  `test_every_committed_recording_names_a_passing_record`.

## [1.2.0] — 2026-11-12

### Fixed

- **FIX-07 [P1] — cost and deadline semantics, and the experiment execution path.**
  Four defects in cost accounting and budgeting, plus the missing arm executor.

  **An estimate was being reported as a measurement.** `ai_agent.py` priced tokens
  with a price-table estimate; that is now carried as `estimated_cost_usd` with a
  `cost_basis` label, separate from the nullable `api_cost_usd` that means a billed
  charge.

  **Unknown usage became a measured zero.** `_read_totals` returned `(0, 0)` when
  the usage summary was unavailable, so a provider that failed to report looked
  like a run that spent nothing. It now returns `None`, and the run records `null`
  with a reason rather than `0`.

  **A retry could breach the monetary ceiling**, because the budget was checked once
  before the retry loop. It is now checked before every attempt, and a call's timeout
  and backoff are bounded by the remaining run deadline rather than the stage
  timeout — a run could previously overrun its own deadline by a full stage.

  **The budget contract is stated honestly.** The run metadata carries
  `strict_billed_ceiling: false`; the code no longer promises a hard dollar ceiling
  while late provider charges are unknown, and unreconciled timed-out calls are
  labelled.

  **V0–V3 now execute.** `services/experiment_execution.py` and
  `manage.py run_experiment` schedule all four arms against one frozen opportunity
  set with a shared `InputSnapshot` per opportunity, and four genuinely distinct
  protocol ids — `heuristic_fallback.v1`, `single_agent_baseline.v1`,
  `debate.no_rebuttal.v1` and `debate.v1`. Each arm carries its own decisions, fills
  and trajectory through the same guard, fill rule and accounting. V4 stays declared
  and unimplemented, excluded from the equal-opportunity denominator, and failed or
  not-executed arms stay in the denominator.

  **Reports are immutable by version.** `ExperimentReportVersion` stores the report
  JSON with a hash and its dependency rows, including a digest of itself, so replay
  appends rather than overwrites. `verify_report_version` rebuilds from live rows and
  names the drift groups when they no longer match.

  **Benchmark provenance is recorded** — observation dates and sources on
  `DecisionOutcome` and `OutcomeRecord`, with `benchmark_interval_verified` computed
  per matched pair at session-date precision and the reasons named when it cannot be.

  The committed V0–V3 comparison is explicitly **simulated engineering validation**:
  authored prices, a scripted model client, measured tokens zero, estimated tokens
  21,394 in / 2,937 out, price-table cost $0.018190, billed cost `null` for nine runs.
  It states `engineering_validation_only: true` and
  `comparative_protocol_claim: false`.

## [1.1.9] — 2026-11-11

### Fixed

- **FIX-02 [P0] — every trade is revalidated under lock.** `apply_trade` locked the
  portfolio and asset rows, then checked cash and quantity, but never reran
  cumulative exposure, concentration, daily-loss or budget rules under that lock.
  Two independently approved additions to a 20% position could therefore pass a 30%
  cap and reach 40%. There is now one authoritative `execute_trade` that locks,
  rebuilds cash/holdings/marks/exposure, reruns the current policy, and mutates the
  ledger in the same transaction. `evaluate_proposal` is explicitly read-only
  simulation, and a hand-built `GuardDecision` cannot authorise anything.
  A stable `execution_key` with a uniqueness constraint makes Celery redelivery and
  repeated approval return the existing row.

- **FIX-03 [P0] — portfolio policy is enforced at application time.** `apply_plan`
  rechecked legs through the legacy guard, which has no sector, turnover or
  cash-buffer rule, and the plan's state version ignored prices and policy limits.
  A tightened sector ceiling or a doubled sector price could therefore be applied
  against a plan approved under different constraints. Plans now bind limits,
  classifications, cost model and allocation into a hash; `apply_plan` requires
  timestamped finite marks for every material holding, recomputes the plan under
  lock, checks the projected final state against every limit, and refuses materially
  changed plans with `PlanReapprovalRequired`. Planned prices are accepted only in
  explicit `paper` mode, which is persisted and labelled.

- **FIX-01 [P1] — the decision lifecycle is one run, end to end.** The protocol
  marked a run `completed` before the guard had run, so a real run could appear
  finished while approval or execution was outstanding, and the detail screen lacked
  the guard and outcome a synthetic fixture showed. Analysis now ends at `validating`;
  a typed `GuardRecord` is persisted from the exact under-lock evaluation; approval,
  rejection, expiry, execution and failure all update the same run; HOLD records an
  explicit no-trade completion rather than a fabricated fill.

- **FIX-06 [P1] — event delivery is durable.** Publishing used `on_commit` plus a
  best-effort send, so a crash after commit or a Redis outage lost the notification.
  An `EventOutbox` now persists the notification in the same transaction as the
  event, a flush task retries with backoff, and `GET /api/events/catch-up/` lets a
  client recover the ordered timeline after a reconnect.

- **FIX-04 [P1] — live refresh and replay now agree.** Detail did not refresh on
  events, so an open run stayed stale, and replay filtered the timeline while the
  final verdict stayed visible. Detail now refreshes on persisted stage, claim,
  rebuttal, decision, guard and outcome events; one as-of presentation state drives
  claims, rebuttals, verdict, guard and timeline together, so before adjudication
  there is no verdict in the scene. Compressed and recorded-time playback are
  separately labelled, and the cursor and timing mode survive browser navigation.

- **FIX-05 [P1] — a standalone offline viewer.** REP-02 asks for replay with no
  database, and the previous path needed PostgreSQL and an authenticated API. A
  static viewer now builds to `docs/viewer/`, validated by schema and SHA-256 before
  import, labelling each run **Real recorded / Synthetic / Heuristic / Legacy**.
  It has no execution, approval or live-analysis capability, and a fetch guard plus
  CSP `default-src 'none'` keep it offline; verified under `unshare -n`.

- **FIX-09 [P2] — CI publishes the coverage badge without touching `main`.** The
  badge step pushed directly to `main` and failed every run with GH013, turning a
  green test suite into a red workflow. It now pushes to an unprotected `badges`
  branch and the README points at that raw URL; branch protection is unchanged.

### Changed

- **The walkthrough is rebuilt around one decision.** Narration starts on the first
  frame over the social preview, names the models actually used (Claude Haiku 5.5
  via CrewAI), opens on the login screen as the earlier demo did, crops into the
  reasoning so the arguments are legible, removes the second title card, and drops
  the music level further.

## [1.1.8] — 2026-11-11

### Added

- **Phase 4 of the technical specification: portfolio-aware capital allocation.**
  The CIO now compares candidates at a portfolio decision boundary and returns
  target weights, which a deterministic service converts into sized, constrained
  orders.

  - **Input brief (PORT-02).** Holdings, cash, equity, weights, sector and
    asset-class exposure, limits, cost estimates, theses, candidate cases, claims,
    data-quality flags, horizon, and **deterministic** covariance/correlation
    estimates with their observation window, frequency and coverage. The prompt
    forbids inventing expected returns, correlations or volatility, and the
    validator rejects those keys outright.
  - **Target contract (PORT-03).** Weights sum to 1 within **0.000001** after
    Decimal validation, long-only, unlevered, non-negative, with cash included.
    Maintaining cash and proposing no change are both valid outcomes.
  - **Deterministic sizing (PORT-04).** `deterministic_constrained_projection.v1`
    enforces per-name, sector, total-exposure, turnover and cash-buffer limits,
    with lot rounding and cost reserves. There is deliberately **no optimizer**:
    the model proposes, deterministic code projects. The method, objective version
    and covariance version are all recorded.
  - **Execution (PORT-05).** A plan is built from one snapshot, sells are processed
    and revalidated first, and application is **atomic** — a failed leg rolls the
    whole plan back rather than leaving partial state. Proceeds from a failed sale
    are never treated as available cash.

  **Sector data did not exist** — `grep sector core/models.py` returned nothing. An
  explicit `InstrumentClassification` lookup was added rather than a nullable column,
  so an unclassified name is unambiguously *unknown* and sector exposure reports as
  incomplete instead of 0%. No sector is ever inferred from a ticker string.

  **53 new tests**, including all four acceptance scenarios (a favourable candidate
  declined by sector, two candidates sharing limited cash, stale/missing data labelled
  with policy action, infeasible targets reporting deviations) and the concurrency
  gate: two plans cannot spend the same cash, cumulative limits cannot be bypassed,
  a changed portfolio version requires revalidation, and a duplicate apply adds no
  ledger row.

  The paper evaluation runs offline against a stub: equity 20000, requested MSFT
  0.25 declined because technology was already at 50% against a 40% ceiling,
  constrained weights AAPL 0.20 / JPM 0.15 / XYZ 0.05 / CASH 0.60 summing to
  1.000000, three legs applied, cash after 12130. Forcing a leg to fail halts the
  plan with `price_unavailable` and writes **zero** ledger rows.

## [1.1.7] — 2026-11-11

### Added

- **Phase 3 of the technical specification: measurement, kept honest by
  construction.** The specification's gate is that accounting fixtures and leakage
  tests pass, and the central requirement is that signal diagnostics and portfolio
  performance can never be mistaken for one another.

  - **Two metric families, provably separate.** `services/signal_diagnostics.py`
    produces forward returns, signed action scores and directional hit rates, and its
    `PerDecisionScoreSeries` **raises `MetricFamilyError`** on `total()`, `sum()`,
    `+` and `sum([series])`. Per-decision scores cover overlapping windows with
    unequal position sizes; their sum is not a portfolio return, so no API can
    produce one. `services/portfolio_accounting.py` computes the other family from
    cash, holdings, fills, fees and flows.
  - **Accounting (MET-02).** `E(t) = cash + Σ quantity × price`, flow-boundary time-
    weighted return with a Modified Dietz fallback, maximum drawdown, turnover with
    its formula, contribution analysis that reconciles to the equity change, and a
    separate net-of-LLM equity series with no double counting.
  - **No lookahead (MET-03).** A `PriceBook` separates *validity* from *knowledge
    time*; a price earlier than the decision is rejected, and weekend targets roll to
    the next session or return `unavailable` rather than zero.
  - **Experiments (EXP-01..05).** A versioned manifest with a checksum, variants
    V0–V4 (V4 declared and unimplemented, excluded from the equal-opportunity
    denominator rather than counted as zero), seven freeze checks, seeded time-block
    bootstrap, and a report that includes failed runs in the denominator.

  **Two additions to existing tables, both driven by gaps found while implementing:**
  `Transaction.fee` is **nullable** — `null` means *no fee was recorded*, `0` means
  *recorded as zero* — with no backfill, and external flows live in a separate
  `CashFlow` table because `services/ledger.py` treats every non-BUY row as a SELL
  and `tx_type` is `varchar(4)`.

  **73 new tests**, covering BUY→HOLD→SELL, fees recorded/zero/unknown, deposits that
  must not read as returns, missing benchmarks, weekend maturities, overlapping
  decisions, price leakage, future data, and failed runs staying in the denominator.

  The example report is built from a synthetic manifest fixture and says so: 
  `engineering_validation_only: true`, `no_profitability_claim: true`,
  `profitability_threshold: null`, Sharpe **not** computed, and
  `strict_historical_claims: false` because the content is synthetic.

## [1.1.6] — 2026-11-11

### Added

- **Phase 2 of the technical specification: a real, bounded debate protocol.** The
  pipeline was a single sequential crew that produced free-text cases; it now runs an
  explicit protocol that records what actually happened.

  - **Independent first round (DEB-01)** — Bull and Bear produce structured cases
    without seeing each other. Independence is a context property, not a timing
    assumption: both are persisted before any rebuttal begins.
  - **One explicit rebuttal round (DEB-02)** — each agent answers the opponent's
    validated claims using the `Rebuttal` contract and declares whether its position
    changed. A concession is a valid output; an agent is never made to manufacture an
    objection to fill a quota. Bounded at 3 claims per agent and 2 responses per agent,
    with the limits used recorded on the run.
  - **Adjudication (DEB-03)** — the CIO sees both cases *and* the rebuttals, must name
    the strongest remaining objection, and may return HOLD. It is not called consensus
    when the analysts still disagree.
  - **Budgets and typed failures (DEB-04)** — per-stage timeout, run deadline, token
    caps, one schema repair per stage, two provider retries with backoff, and a
    monetary ceiling. A failed stage stops the protocol; a heuristic fallback becomes a
    **separate run** that cannot inherit debate events it never had.
  - **Reproducibility (DEB-05)** — provider, model, parameters, prompt template
    version, tool versions, snapshot hash and raw validated outputs are recorded. The
    code says plainly that stored events replay deterministically and a regenerated
    model output does not.

  `services/debate_protocol.py` does not import `ai_agent`, so the whole protocol is
  testable with an injected model client and no key or network. `ai_agent.py` keeps
  the legacy sequential crew behind `AI_DEBATE_PROTOCOL=legacy_sequential`.

  **32 new tests**, including all four acceptance scenarios from the specification:
  a dispute the CIO links to its decisive claim, a genuine concession, a provider
  timeout that produces no fictional rebuttal, and a one-agent baseline on the same
  snapshot and proposal contract.

  What this does *not* do: no live provider call was made. The protocol, persistence,
  event log, API and fallback separation are real; the model in tests and the demo is
  a deterministic stub, recorded as such in `run.protocol`.

## [1.1.5] — 2026-11-10

### Added

- **Phase 1 of the technical specification: explain one decision.** A reviewer can
  now open a decision and read the decisive evidence, the strongest objection, the
  reason for the verdict, the guard intervention and the measured outcome without
  reading a raw log — offline, with no keys.

  Backend: nine additive tables (`DecisionRun`, `RunEvent`, `InputSnapshot`,
  `DecisionRecord`, `GuardRecord`, `EvidenceItem`, `Claim`, `Rebuttal`,
  `OutcomeRecord`), the `services/decision_runs.py` and `services/evidence.py`
  domain layer, and five read endpoints under `/api/decision-runs/` — list,
  metadata, detail, paginated events with `after_seq` recovery, and a redacted
  export.

  Frontend: `DecisionDetail`, `EvidenceDrawer`, `DebateTimeline`,
  `GuardComparison`, `ReplayControls` and a `#/runs` route.

  Three fixtures load through `manage.py load_decision_fixtures`: a guard clamp, a
  disagreement with rebuttals, and an uncertainty case that reports `unavailable`
  rather than inventing a price.

  **What is deliberately absent.** The current pipeline has no rebuttal stage, so a
  run that did not produce rebuttals renders no rebuttal section at all, and all
  three fixtures are labelled `mode="synthetic"` with `source="fixture"` and exposed
  as such through the API. `portfolio_impact` is `null`, not zero, because portfolio
  accounting is not reconciled yet.

  Baseline and scope decisions are recorded in `docs/spec/IMPLEMENTATION-NOTE.md`;
  the five-phase programme is in the Roadmap wiki page.

## [1.1.4] — 2026-10-28

### Changed

- **The walkthrough follows the five-beat scenario, and invents nothing.** An
  81-second video: a four-second title plate while the voice states the problem,
  then the application for the body, then a closing call to action.

  | Beat | At | On screen |
  | --- | --- | --- |
  | Hook | 0:00 | title plate for 4s, then the dashboard and the ticker being chosen |
  | Core value | 0:13 | the debate cropped in close, so the arguments read as text |
  | Trust | 0:37 | the ceiling meters, a real guard run, the scorecard |
  | Flex | 0:59 | a real MCP handshake and tool discovery |
  | Call to action | 1:11 | closing plate with the repository |

  The voice is `en-US-AndrewNeural`, chosen by measurement rather than by ear:
  the reference video's narration sits at **F0 110 Hz**, and of six candidate voices
  Andrew measured 110 Hz against Christopher's 108 and Guy's 147.

  **Two beats were placeholders and are now real output.** The guard panel had shown
  a docstring, and the MCP panel had shown a tool list written by hand. Both now print
  what the running system returns:

  ```
  BUY   3 NVDA @ $100   ALLOWED   Approved BUY of 3.00000000 NVDA.
  BUY  45 NVDA @ $100   CLAMPED   45 -> 10.49 units, the 5.00% per-trade ceiling
  BUY   2 NVDA @ $100   REJECTED  daily loss: -$600.00 breaches -$500.00

  serverInfo  alphaagent-market-data  1.30.0
  tools  6 registered
  401 Unauthorized without x-alphaagent-mcp-key
  ```

  Every figure and every reason string in those frames is printed by
  `services/execution.py` or by the MCP server. Nothing is typed by hand.

  Earlier figures in the README (90, then 77, then 47 seconds) were each wrong for the
  file they described. It is 81 seconds now, stated once, and there is one video.

- **The Streamlit demo now looks like the product.** It was a documentation page —
  an emoji title, numbered headings, paragraphs, and one form at the end. It is now
  a dashboard-shaped terminal: the dark palette, seven metric cards in the same
  order as the real dashboard, the two ceiling meters with their real captions, and
  four tabs, with the guard playground inside the first one.

  The guard is still the real `evaluate_proposal` — all nine presets, the clamp and
  refuse verdicts, and the mirror SELL are its own return values, not restatements.
  The offline property is unchanged and re-verified independently: the page serves
  200 with health `ok` inside a network namespace where `api.anthropic.com` does not
  resolve.

## [1.1.3] — 2026-10-28

### Fixed

- **Twenty-four agent runs failed because the response budget was too small.** The
  worker log showed the cause on the line before each failure:

  ```
  Anthropic API usage: {'input_tokens': 7586, 'output_tokens': 2048, ...}
  ERROR crewai: Invalid response from LLM call - None or empty.
  ```

  `2048` was `max_tokens`, exactly. The 5.x Anthropic models think adaptively by
  default, and that thinking is billed from the same allowance as the answer — so a
  run could reason until the budget ran out and return no text at all. CrewAI
  surfaced that as a `ValueError` which reached the task and was logged as
  `ERROR - agent pipeline failed` rather than as a decision.

  Across the log history: **26 runs hit the cap and 24 of them failed.** The cap now
  defaults to **8192**, and a response that consumes it is logged as a truncation
  rather than passing as a short answer.

  Cost rises about 1.4x ($0.0145 → $0.019–0.025 per run) because the model is no
  longer being cut off mid-thought. That is the intended trade.

- **Disabling thinking was tried first and is not possible through CrewAI.** It
  serialises the parameter as `{"type": "disabled", "budget_tokens": None}`, and
  Anthropic rejects that with `thinking.disabled.budget_tokens: Extra inputs are
  not permitted` — the two fields are mutually exclusive. Recorded in the code so
  the next person does not repeat the attempt.

### Added

- **A truncation guard.** A run whose output consumes the entire budget now logs an
  explicit error naming the cap. Before this the failure was invisible: it either
  crashed inside CrewAI or produced a proposal from a partial reply.

### Added

- **A narrated 77-second walkthrough** (`docs/demo-walkthrough.mp4`), rewritten for a
  finance audience. Script by Claude Opus 5.5, spoken, and built on a real recorded
  session: the ticker being switched, a magnified crop of the bull/bear reasoning,
  then the guard schema. A quiet ambient bed sits under the narration.
- **Role-based onboarding** in the README: four collapsible tracks — managing money,
  building with LLMs, running infrastructure, reviewing the work — each an ordered
  list with per-step reading times. Collapsed by default, so the four role names and
  their durations are visible without scrolling and the detail is one click away.

### Removed

- **The "Skills demonstrated" section.** Its content is better made by the work it
  describes; a section that praises itself costs a reader's attention and buys
  nothing.

### Changed

- The equity curve in the demo shows 30 days rather than the default 24 hours, where
  this portfolio has no data and the chart renders an empty state.

## [1.1.2] — 2026-10-28

A security release, and the last open advisory closed.

### Security

- **`mcp` 1.28.1 → 1.30.0** — unbounded session accumulation in the streamable-HTTP
  transport. This advisory had been left open through two releases because the fix
  was genuinely unreachable: `crewai` declared `mcp~=1.28.1`, which caps the
  resolver below 1.29. **`crewai` 1.15.32 now declares `mcp<2,>=1.30.0`**, verified
  from the sdist metadata of the release rather than assumed, so the pin no longer
  binds. Upgraded `crewai` 1.15.28 → 1.15.32 as the enabling change.

### Fixed

- **`pip-compile` silently reproduces existing pins.** Regenerating to
  `requirements.txt` failed with `ResolutionImpossible` while regenerating the same
  inputs to a fresh path succeeded and resolved `mcp` 1.30.0. The existing output
  file is re-read as a set of constraints, so `--upgrade-package` cannot lift a pin
  the old lockfile still asserts. Generating to a new path and moving it into place
  is the reliable form.

### Corrected from the previous entry

The 1.1.2 draft claimed `requirements-dev.txt` was at fault for a `tomli` /
`tomli-w` conflict. **That diagnosis was wrong.** Neither package is pinned there;
both arrive transitively through `crewai`, which requires `tomli~=2.0.2` and
`tomli-w~=1.1.0`. The conflict only appeared because a `pip-audit` left over in the
development virtualenv requires `tomli>=2.2.1`. Removing it makes
`pip install -r requirements-dev.txt` report nothing.

The lockfile was correct throughout.

### Added

- **`make audit`** — scans the pinned dependencies for known vulnerabilities from
  an isolated environment. `pip-audit` cannot live in this project's virtualenv:
  `tomli~=2.0.2` (crewai) and `tomli>=2.2.1` (pip-audit) do not intersect, so no
  resolver can satisfy both. A throwaway environment sidesteps that entirely and
  still audits the lockfile that actually ships.

  Its first run found **five advisories in `chromadb` 1.5.9 that GitHub's
  Dependabot does not report** — the two use different advisory databases. None has
  a published fix and 1.5.9 is the newest release. `chromadb` arrives through
  `crewai` and is imported at load time, but this project sets `memory=False`, so
  CrewAI memory — the only consumer here — never runs. The four IDs are ignored
  **individually** rather than excluded wholesale, so a new advisory still fails
  the target.

### Verified

528 backend tests, ruff clean, all six containers healthy on the new versions, the
dashboard returns 200, and the MCP tool server answers `401` without its shared
secret - the authentication guard still working on `mcp` 1.30.0.

## [1.1.1] — 2026-10-25

A security release. No new features and no behaviour changes: the execution guard,
the attribution ledger and the API are untouched.

### Security

- **`twisted` 26.4.0 → 26.10.0rc1** — HTTP request smuggling in `twisted.web` via a
  bare CR or LF in a header value. This is the one advisory against `twisted` that
  this project actually reaches: `daphne` imports `twisted.web.http` and
  `twisted.web.http_headers`, which is the HTTP parsing path the server runs on.
  The other four (IMAP4, IRC, conch Telnet, `twisted.words`) cover subsystems that
  are never loaded.
  Pinned to a **release candidate** deliberately, because `26.10.0rc1` is the only
  published release containing the fix and no stable `26.10.0` exists yet. The pin
  is explicit in `requirements.in` with the reason and a note to replace it, rather
  than reached implicitly through `daphne`'s `twisted[tls]>=22.4`.
- **`filelock` 4.0.12 → 4.2.4** — SQLite reopen of the lock database by name.
- **`virtualenv` 21.14.9 → 21.14.13** — unescaped `--prompt` value in the Bash
  activator.
- **`setuptools` 84.1.0 → 84.2.1** — Windows path escape via `namespace_packages.txt`.
- **`undici` 8.12.x → 8.12.3** (frontend) — denial of service via an unhandled
  error on socket close.

### Known remaining

- **`mcp` 1.28.1** — an unbounded session accumulation in the streamable-HTTP
  transport, patched in 1.30.0. Still blocked: `crewai` declares `mcp~=1.28.1`
  (`>=1.28.1,<1.29.0`), and no released `crewai` widens that range. The vulnerable
  code is in this project's execute path, so it is left visible rather than
  dismissed. Bounded by loopback binding and a required shared secret.

### Verified

528 backend tests and 115 frontend tests pass, and `daphne` serves on the release
candidate: `Listening on TCP address 0.0.0.0:8000`, dashboard returns 200.

## [1.1.0] — 2026-10-23

Three features, a security pass, and the infrastructure that makes the project
reviewable. Roughly 60 commits.

### Added

- **Decision outcome attribution** — every decision is scored against what the
  market did next, at 1, 7 and 30-day horizons. `DecisionOutcome` records forward
  return, the return *the action* implies, and SPY's return over the same window,
  so the scorecard reports **alpha** rather than a bare hit rate. A SELL is scored
  as correct when the price falls; a HOLD is `NULL` rather than `False`, because it
  makes no directional claim. Exposed as `GET /api/portfolio/attribution/` and a
  dashboard panel.
- **Cumulative exposure and concentration ceilings** — `max_portfolio_exposure_pct`
  and `max_ticker_concentration_pct` bound the whole portfolio, not one trade, so
  a sequence of individually-legal buys can no longer reach full investment. Both
  fold into the existing clamp, and every note names the ceiling that bound.
- **Persistent position thesis** — each open position carries a written thesis and
  an invalidation condition, written by the agent that opened it and fed back on
  every later run, so an exit has to argue with the reason for entry. Superseded
  rather than edited, so the history reads as a sequence of beliefs. Exposed as
  `GET /api/portfolio/thesis/`.
- **AI spend ceiling** (`AI_DAILY_SPEND_LIMIT_USD`) — the agent halts when the
  day's recorded cost exceeds the limit, instead of a runaway loop billing silently.
- **MCP authentication** — the tool server requires a shared secret compared with
  `hmac.compare_digest`; it will not construct with an empty one.
- **One-command start/stop** — `make start` / `stop` / `down` / `status`, mirrored
  as VS Code tasks so the workflow is identical on Windows and Linux, plus a
  generated Russian-language operator guide.
- **Dev Containers support** — `.devcontainer/` drives the project's own compose
  file rather than a parallel environment, with `shutdownAction: stopCompose` so
  closing the window releases the ports.
- **Multi-stack isolation** — `COMPOSE_PROJECT_NAME`, `ALPHA_CONTAINER_PREFIX` and
  per-service port variables, so two developers can run separate stacks on one
  host. Every value defaults to the single-developer case.
- **`LLM-Quality-and-Guardrails`** — a wiki page documenting the seven layers that
  contain unreliable model output, and the outcome ledger that measures it.
- **Documentation link tests** — every source link is checked offline against the
  checkout: the file must exist and the line anchor must land inside it.

### Changed

- **Model: Claude Haiku 5.5.** Roughly 90% cheaper per token and materially
  stronger. It rejects `temperature` with a 400, so sampling parameters are now
  omitted for models that do not accept them — a model swap that would otherwise
  have failed every run. Measured on a real run: **$0.161 → $0.0145**.
- **Redis requires authentication** (`--requirepass`), including in the demo file.
- **The data services bind loopback** (`127.0.0.1`) rather than every interface.
- **Tailwind 4 and Vite 8** on the frontend, Vite's Rolldown build replacing
  esbuild.
- **Hash-pinned Python lockfiles** (`requirements.txt`, `requirements-dev.txt`) so
  the dependency graph is visible to Dependabot.
- **The README leads with the result** — six badges instead of seventeen, a
  collapsible table of contents, and the measured hit rate stated as text rather
  than only inside a screenshot.

### Fixed

- **Decision scoring measured from the wrong price.** Forward returns started at
  the day's close while trades executed at the live quote — $166 apart on a real
  BTC decision, biased in the direction that flattered BUYs. Correcting it made the
  published numbers *worse*, which is why it is recorded rather than quietly
  applied.
- **`DecisionOutcome` could not be attributed for HOLD and HALT rows** —
  `AgentDecisionLog` had no ticker of its own, so every non-trading decision had
  none. Now a real indexed column, backfilled.
- **Scheduled snapshots never emitted** even though the task captured them.
- **The guard's refusal named the wrong limit** — every refusal said "per-trade
  ceiling", including refusals caused by the concentration ceiling.
- **Twenty-two CodeQL alerts**, including log injection in two sinks, a missing
  explicit return in the MCP guard, and implicit string concatenation in a list.
- **The paginated thesis envelope** was read as a bare array, so the panel rendered
  nothing at all — no error, no warning, just an absent section.

### Security

- **PostgreSQL and Redis were reachable from the public internet.** Both were
  published on `0.0.0.0` with no host firewall; PostgreSQL completed a handshake
  from outside and had been reachable that way for two weeks. Now bound to
  loopback, and enforced by a test that resolves the Compose configuration.
- **Redis ran unauthenticated** on a published port.
- **Dependency advisories** — Pillow, certifi and Vite patched; the remaining
  `mcp`, `twisted` and `uv` findings are documented with their reachability
  assessed rather than silently dismissed.
- **`main` is protected by a repository ruleset** with five required status
  checks, a pull-request rule, and no force-push.

## [1.0.0] — 2026-09-30

Initial release: a four-tier autonomous investment platform.

### Added

- **Domain model** (`core/models.py`) — `Portfolio`, `Asset`, `Transaction` and
  `AgentDecisionLog`, with database-level `CHECK` constraints, a
  `UNIQUE(portfolio, ticker)` constraint and hot-path indexes.
- **REST API** — token-authenticated endpoints for the portfolio snapshot with
  live metrics, the safety-gated autonomy toggle, the AI decision audit trail,
  the trade ledger and on-demand agent sweeps. Query counts are bounded and
  asserted in tests.
- **AI layer** (`ai_agent.py`) — a sequential three-agent CrewAI crew running
  an adversarial debate (bullish analyst → risk assessor / short seller → CIO
  adjudicator) whose tools are strictly read-only. The output contract is
  enforced twice: a Pydantic `TradeProposal` model and a CrewAI task guardrail
  that forces a retry on malformed JSON. Both arguments are persisted on the
  decision log (`bull_case`, `bear_case`) so every verdict is auditable.
- **Deterministic fallback engine** — when no LLM credential is configured, or
  the provider fails, decisions come from an auditable rule engine instead of
  crashing the pipeline.
- **Async engine** (`tasks.py`) — Celery Beat fans out one independent subtask
  per `(portfolio, ticker)` pair via a single `group` publish, with a per-trade
  execution guard, row-locked execution, a daily stop-loss sweep and
  housekeeping.
- **Ledger accounting** (`services/ledger.py`) — exact weighted-average-cost
  replay deriving positions and realised P&L without a snapshot table.
- **Market and news services** — cached `yfinance` quotes with graceful
  degradation to stored cost basis, and RSS scraping hardened with `defusedxml`.
- **Explainability** — every agent run, including HOLDs, blocked trades and
  crashes, writes an `AgentDecisionLog` row with the full chain of thought,
  token usage and cost.
- **Container stack** — `Dockerfile` plus `web`, `worker` and `beat` Compose
  services with healthchecks and restart policies.
- **Operations** — `seed_demo` to provision a demo account, and `dry_run_agent`
  to exercise the live LLM without writing to the database.
- **Security** — no credential fallbacks in settings, mandatory environment
  secrets, and a CI secret scan over every publishable file.
- **CI** — Ruff lint/format, the full test suite against PostgreSQL 16 and
  Redis 7, migration-drift detection and a Docker build smoke test.

[Unreleased]: https://github.com/SergeyGer/AlphaAgent/compare/v1.1.4...HEAD
[1.1.4]: https://github.com/SergeyGer/AlphaAgent/compare/v1.1.3...v1.1.4
[1.1.3]: https://github.com/SergeyGer/AlphaAgent/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/SergeyGer/AlphaAgent/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/SergeyGer/AlphaAgent/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/SergeyGer/AlphaAgent/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/SergeyGer/AlphaAgent/releases/tag/v1.0.0
