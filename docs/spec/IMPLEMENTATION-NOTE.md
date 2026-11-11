# Implementation note — Technical Specification v1.0

This records the Phase 0 baseline the specification requires, and what is being
built against it. The specification is a five-phase programme; this note covers
Phase 0 in full, Phase 1, and Phase 2 in implementation.

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
