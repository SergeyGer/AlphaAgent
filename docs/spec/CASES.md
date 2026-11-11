# Curated recorded cases

This is a small set of cases a reviewer can inspect end to end. Each case names
what it demonstrates, the run or fixture that carries it, the exact evidence it
points at, and why it was selected.

Phase 5's gate is that **all public claims trace to reviewed evidence**. A case is
therefore not a highlight reel. The selection rule was fixed before inspecting the
results:

1. Represent every scenario the specification names — disagreement, risk
   intervention, uncertainty and outcome.
2. Prefer the run that carries the most explicit evidence for the scenario, not
   the run with the best result.
3. If the corpus contains only unfavourable or unresolved results, say so and use
   them. Do not substitute a synthetic favourable example.
4. Label every synthetic case as synthetic in the case itself, not only in a
   header.

## The corpus, and its central limitation

There are three committed decision runs, in
[`fixtures/decision-runs.v1.json`](fixtures/decision-runs.v1.json). All three are
`mode: "synthetic"`, `source: "fixture"`, `protocol_version: "synthetic.v1"`.
The manifest's `honesty.all_fixtures_synthetic` is `true` and its `why` field
states that they were authored before a producer existed.

**No live protocol transcript is committed.** Phase 2 can produce a live run
(`mode="live"`, `source="crewai"`), but no such run is stored in this repository.
A reviewer should treat every case below as a specification of the evidence
contract, not as a record of what the production model did. The repository has no
market evidence for a decision; it has fixtures that exercise the consumer and
the accounting contract.

The Phase 4 paper allocation is a separate synthetic fixture,
[`fixtures/phase4-allocation.v1.json`](fixtures/phase4-allocation.v1.json), and is
labelled as such in its own `synthetic` and `synthetic_note` fields. It is not a
decision run and no decision-run loader reads it.

## Case 1 — Disagreement: two rebuttals, then an explicit HOLD

**Scenario:** disagreement with an explicit adjudication.

**Run:** `disagreement-tsla-2026-02-04` in
[`fixtures/decision-runs.v1.json`](fixtures/decision-runs.v1.json).

**What it demonstrates.** The two initial cases are built from opposing evidence,
each is challenged once, the challenge changes a stated position, and the CIO
resolves the disagreement as a HOLD with named decisive claims and a named
strongest objection. A split debate is not reported as agreement.

**Exact evidence.**

| Evidence | Value | Fixture location |
| --- | --- | --- |
| Bull claim, storage offset | "Energy storage growth of 52% is large enough to offset the vehicle margin decline." | `timeline[]`, `kind: "claim"`, `key: "bull-storage"` |
| Bull claim, product cycle | "A 2026 product cycle can restore volume growth without further price cuts." | `key: "bull-product-cycle"` |
| Bear claim, deliveries | "Deliveries fell 6% year over year for a second consecutive quarter, so the core business is shrinking." | `key: "bear-deliveries"` |
| Bear claim, multiple | "The storage segment is too small to support a 58x forward multiple." | `key: "bear-multiple"` |
| Bear rebuttal | `bear-disputes-storage`, response type `dispute`, target `bull-storage`; position change: "Remains bearish; conviction increased." | `timeline[]`, `kind: "rebuttal"` |
| Bull rebuttal | `bull-qualifies-multiple`, response type `qualify`, target `bear-multiple`; position change: "Trims conviction from high to moderate." | same |
| Verdict | `HOLD`, `requested_quantity: null` | `timeline[]`, `kind: "decision"` |
| Rationale | "Each case rests on a verifiable fact and the rebuttals narrow the disagreement without resolving it. … Holding preserves the option to act once Q1 deliveries resolve it." | `decision.concise_rationale` |
| Decisive claims | `["bull-storage", "bear-deliveries"]` | `decision.decisive_claim_keys` |
| Strongest objection | "The storage segment is too small to support a 58x forward multiple." | `decision.strongest_objection` |
| Outcome | 7-day maturity `pending`; no score | `timeline[]`, `kind: "outcome"` |

The API exposes the same values through `GET /api/decision-runs/<id>/detail/`:
`has_rebuttals: true`, two entries in `rebuttals`, `verdict.action: "HOLD"`, and
an empty `guard` (a HOLD has no guard record). The three-stage shape —
`analysing → rebutting → adjudicating` — is visible in `stage_states`, with
`rebutting` marked `complete` rather than `not_run`.

**Why it was selected.** It is the only committed run with a recorded rebuttal
round, so it is the only case that can test the disagreement contract at all. It
was not selected because the debate produced a trade: it produced none. The
unresolved outcome is left unresolved rather than filled in.

**Synthetic label.** `mode: "synthetic"`, `source: "fixture"`; the manifest's
`honesty.rebuttal_note` says the rebuttals "exist to exercise the EVID-03
consumer contract for Phase 2; [they are] not a recording of a real debate."
This case is **not** evidence that the production debate behaves this way.

## Case 2 — Risk intervention: the guard clamps 40 shares to 2

**Scenario:** risk intervention by the execution guard, with requested versus
allowed and the binding rule.

**Run:** `risk-intervention-aapl-2026-02-03` in
[`fixtures/decision-runs.v1.json`](fixtures/decision-runs.v1.json).

**What it demonstrates.** A model proposes a trade far above the per-trade
budget. The deterministic guard clamps it, records the difference, and names the
limit that bound. The decision still executes at the allowed size; this is not a
rejection.

**Exact evidence.**

| Evidence | Value | Location |
| --- | --- | --- |
| Proposal | `BUY`, requested `40.00000000` AAPL at `225.00` | `decision` step |
| Requested notional | `9000.00` | `guard.numerical_checks.requested_notional_usd` |
| Cash | `10000.00` | `guard.numerical_checks.cash_balance_usd` |
| Per-trade limit | `5.00`% of cash | `guard.numerical_checks.max_trade_allocation_pct` |
| Budget | `500.00` | `guard.numerical_checks.max_trade_budget_usd` |
| Allowed | `450.00` notional, `2.00000000` shares | `guard.numerical_checks.allowed_notional_usd`, `guard.allowed_quantity` |
| Guard status | `clamped` | `guard.status` |
| Binding rule | `max_trade_allocation_pct` | `guard.binding_rule` |
| Reason codes | `requested_notional_exceeds_trade_budget`, `quantity_clamped_to_budget` | `guard.reason_codes` |
| Check result | `max_trade_allocation_pct`: `fail`, threshold `500.00`, actual `9000.00`, unit `USD` | `guard.checks[]` |
| Concentration check | `max_ticker_concentration_pct`: `pass`, threshold `30.00`, actual `0.00` | `guard.checks[]` |
| Daily-loss check | `not_evaluated`; no realised P&L is supplied in this fixture | `guard.checks[]` |
| Outcome | 7-day `evaluated`, forward return `-2.0000`, decision return `-2.0000`, benchmark `SPY` `-0.4000` | `timeline[]`, `kind: "outcome"` |

The report's signal row for the same run publishes `directional_hit: false` and
`relative_forward_return_pct: "-1.6000"` in
[`fixtures/experiments/phase3-engineering.v1.report.json`](fixtures/experiments/phase3-engineering.v1.report.json).

**Why it was selected.** It is the only committed guard record that carries a
clamp, and it carries the requested quantity, the allowed quantity, the binding
rule and the numerical checks — the four things the scenario asks a reviewer to
see. It is also unfavourable: the instrument then fell 2.00% over the seven-day
horizon while the benchmark fell 0.40%. The fixture records no transaction, so
that is the decision's measured outcome, not the return of a held 2-share
position — the report row says `execution_status: "execution_not_recorded"`. The
case does not show the guard "saving" the portfolio. The 38 shares that were
never bought have no recorded outcome, and **the guard preventing a loss is not
established** — a clamp is not a counterfactual, and no counterfactual rule was
declared for it.

**Synthetic label.** `mode: "synthetic"`, `source: "fixture"`, guard policy
`guard.v1`. The portfolio values and the quote are authored fixture data.

## Case 3 — Uncertainty: an undated source, a degraded stage, an unavailable outcome

**Scenario:** uncertainty, missing data and a failed stage showing honest
limitations.

**Run:** `uncertainty-msft-2026-02-05` in
[`fixtures/decision-runs.v1.json`](fixtures/decision-runs.v1.json).

**What it demonstrates.** The run does not pretend that missing data is a
measured zero. A source with no publication date is recorded as unknown; a stage
fails and is marked degraded; the CIO holds because the evidence does not support
a direction; the outcome is `unavailable` with a reason rather than an invented
price.

**Exact evidence.**

| Evidence | Value | Location |
| --- | --- | --- |
| Undated evidence | `msft-revenue`, publisher "Synthetic fundamentals archive", `published_at: null` | `timeline[]`, `kind: "evidence"` |
| Evidence limitation | "The publication date of this source is unknown; the archive is undated, so the information set available at the cutoff cannot be proven." | `claim` `bull-revenue`, `limitations` |
| Failed stage | `stage.failed`, stage `analysing` | `timeline[]`, `kind: "event"` |
| Stage state | `analysing: degraded`, limitation: "Analyst estimate feed unavailable; two of three expected evidence sources are missing. The run continues on partial data and records the gap." | API `run.stage_states` (loader-created from the timeline) |
| Verdict | `HOLD`, `requested_quantity: null` | `decision` step |
| Rationale | "Guidance is deferred and one source has no publication date. … inventing a probability from missing inputs would be worse than holding." | `decision.concise_rationale` |
| Strongest objection | "Revenue and cloud growth are both real and may not require guidance to keep compounding." | `decision.strongest_objection` |
| Outcome | 1-day maturity `unavailable`; reason: "No closing price was recorded for the decision date in the synthetic snapshot. Scoring it would require inventing one, so the outcome is recorded as unavailable rather than zero." | `timeline[]`, `kind: "outcome"` |

The run's overall status is `completed` while one stage is `degraded`. That is
deliberate: a degraded run is not a failed run, and the distinction is carried in
`stage_states`, not hidden in prose.

**Why it was selected.** It is the only committed run with a `stage.failed`
event, an undated evidence item and an `unavailable` outcome. It demonstrates
the three honesty rules that are easiest to violate: unknown publication date
stays unknown, a degraded stage stays visible, and a missing price becomes null
rather than zero. It contains no favourable result to cherry-pick.

**Synthetic label.** `mode: "synthetic"`, `source: "fixture"`. The missing price
is authored, not an operational incident.

## Case 4 — Outcome: the one matured result, and it is a loss

**Scenario:** a matured result linked to its decision.

**Run:** `risk-intervention-aapl-2026-02-03` (the same run as Case 2; the
outcome lens is separated because the linkage is the point).

**What it demonstrates.** A decision can be followed from proposal to guard to a
matured `OutcomeRecord` with a forward return, an action return and a benchmark
return, and the record says whether the direction was right.

**Exact evidence.**

| Evidence | Value | Location |
| --- | --- | --- |
| Decision | `BUY` 40 requested, `decision_at` `2026-02-03T14:31:05Z` | `decision` step |
| Guard | clamped to `2.00000000`; policy `guard.v1` | `guard` step |
| Outcome maturity | `evaluated`, horizon `7` days | `outcome` step |
| Entry mark | `225.00` recorded at the decision | `price_at_decision` |
| Exit mark | `220.50`, observed `2026-02-10T21:00:00Z` | `price_at_horizon`, `price_observed_at` |
| Forward return | `-2.0000`% | `forward_return_pct` |
| Decision return | `-2.0000`% (BUY: the forward return is the action return) | `decision_return_pct` |
| Benchmark | `SPY`, `-0.4000`% | `benchmark_return_pct` |
| Directional hit | `false` | Report row in [`fixtures/experiments/phase3-engineering.v1.report.json`](fixtures/experiments/phase3-engineering.v1.report.json) |
| Relative return | `-1.6000`% against SPY | Report row, `relative_forward_return_pct` |
| Method version | `attribution.synthetic.v1` | `outcome.method_version` |
| Synthetic flags | `signal_diagnostics.synthetic: true`, note: "Illustrative fixture values, not market data. Not usable for performance claims." | `outcome.signal_diagnostics` |

The 1-day outcome for the same run is `pending` and has no score. The 30-day
outcome is absent from the fixture. Nothing is inferred for either.

**Why it was selected.** It is the only evaluated decision outcome in the
committed corpus, so selecting it is not a choice between a winner and a loser —
there is no winner. It is a loss, and it underperformed the benchmark. It is
included specifically to show that the outcome path is not a success story with
a fixture attached.

**Synthetic label.** The outcome is synthetic and its `method_version` says so.
It is **not** market evidence, and it must not be quoted as the system's
performance. Its purpose is to show that a matured outcome is stored with an
entry price, an exit price, a benchmark and a directional result. The fixture
records no transaction, so this is a decision outcome, not a realised position
return.

## Case 5 — Risk intervention at the portfolio boundary (Phase 4)

**Scenario:** a deterministic limit declines a requested target, with requested
versus allowed and the binding rule.

**Run:** `run_allocation --reset --apply` against
[`fixtures/phase4-allocation.v1.json`](fixtures/phase4-allocation.v1.json). The
captured output is
[`fixtures/phase4-allocation.v1.output.json`](fixtures/phase4-allocation.v1.output.json).

**What it demonstrates.** The Phase 4 allocation layer is a projection, not an
optimizer. The stubbed CIO requests 25% MSFT, 15% JPM, 5% XYZ, 20% AAPL and 35%
cash. The deterministic layer declines MSFT because technology is already at the
40% sector ceiling; JPM and XYZ are bought after lot rounding; AAPL is sold down;
the target cash weight rises to `0.600000`.

**Exact evidence.**

| Evidence | Value | Location |
| --- | --- | --- |
| Starting state | cash `10000.00`, AAPL `100` shares at `100.00`; equity `20000.00` | `brief.equity_usd`, `brief.holdings` in the output |
| Requested weights | AAPL `0.200000`, MSFT `0.250000`, JPM `0.150000`, XYZ `0.050000`, CASH `0.350000` | `targets.requested_target_weights` |
| Constrained weights | AAPL `0.200000`, MSFT `0` (declined), JPM `0.150000`, XYZ `0.050000`, CASH `0.600000`, sum `1.000000` | `targets.target_weights`, `target_weights_sum` |
| Binding rule | `max_sector_weight_new_risk`; limit `0.3997000` after the cost reserve; technology already at `0.50000000` | `targets.deviations[]` |
| Rationale for the decline | "MSFT would open a new position in sector 'technology', which is already over the 0.40 ceiling … New risk in that sector was declined; the weight went to cash." | `targets.deviations[0].detail` |
| Rounding deviations | JPM `3000.00` requested, `2880.00` bought; XYZ `1000.00` requested, `990.00` bought; both recorded as `lot_rounding` | `targets.deviations[]` |
| Applied legs | SELL AAPL `60` at `100.00`; BUY JPM `18` at `160.00`; BUY XYZ `11` at `90.00`; all `applied` | `plan.legs[]`, `application.ledger_rows_for_portfolio: 3` |
| Cash after | `12130.00` | `application.portfolio_cash_after` |
| Provenance | provider `stub`, model `stub-model`, prompt template `portfolio_targets.v1`, `cost_reported: false` | `plan.model_metadata` |

**Why it was selected.** It is the only committed allocation plan, and it shows
the exact behaviour the Phase 4 gate describes: a favourable candidate is
declined because a sector is already over its limit, the difference goes to cash
rather than another name, and every deviation records the requested value, the
applied value and the binding limit. It is not an execution-guard rejection —
that distinction is stated explicitly so the two layers are not conflated.

**Synthetic label.** The fixture's `synthetic` field is `true`; its
`synthetic_note` states that prices, classifications and candidate cases are
authored, and that the risk statistics are computed from a synthetic series. It
is engineering/paper evidence, not realised performance.

## Selection gaps, stated rather than filled

- **No guard rejection with a recorded reason is committed.** The guard record in
  the corpus is a clamp. The Phase 3 report's `counterfactuals` block has
  `rejected_proposal_count: 0`, `rows: []`, `established_count: 0`. A reviewer
  who needs a rejection case must run one (the guard tests cover rejection
  paths), but no curated rejection fixture exists here.
- **No live run is committed.** All decision cases are synthetic. The repository
  can produce a live run but does not preserve one.
- **No profitable matured decision outcome is committed.** In the decision-run
  corpus the only evaluated outcome is a loss; the other two horizons are pending
  or unavailable. (The Phase 3 report does show a positive synthetic accounting
  return, but that is a ledger fixture, not a decision outcome.) This is a
  property of the corpus, not a selection filter.
- **No counterfactual result is established.** The rule exists and is tested, but
  the example report has no row to show. "The guard prevented a loss" is
  therefore unsupported in this repository.
- **No protocol comparison.** V4 is unimplemented, the sample is below the
  pre-registered minimum, and the content is synthetic. No case is offered as
  evidence that one variant is better than another.

## Reproducing the cases

The commands and what each produces are documented in [`DEMO.md`](DEMO.md).
In short:

```bash
python manage.py load_decision_fixtures
python manage.py run_allocation --reset --apply
python manage.py build_experiment
```

The decision detail is read from the API or from the stored records; the exact
shell command is in [`DEMO.md`](DEMO.md#2-decision-detail-replay). No command in
this section needs an API key or a network connection.

## Evidence index

| Case | Fixture or report | Row / step |
| --- | --- | --- |
| Disagreement | [`fixtures/decision-runs.v1.json`](fixtures/decision-runs.v1.json) | `fixture_id: disagreement-tsla-2026-02-04` |
| Risk intervention (guard) | [`fixtures/decision-runs.v1.json`](fixtures/decision-runs.v1.json) | `fixture_id: risk-intervention-aapl-2026-02-03` |
| Uncertainty | [`fixtures/decision-runs.v1.json`](fixtures/decision-runs.v1.json) | `fixture_id: uncertainty-msft-2026-02-05` |
| Outcome | [`fixtures/experiments/phase3-engineering.v1.report.json`](fixtures/experiments/phase3-engineering.v1.report.json) | `signal_diagnostics.rows[]`, `stored_run_key: risk-intervention-aapl-2026-02-03` |
| Portfolio-boundary intervention | [`fixtures/phase4-allocation.v1.output.json`](fixtures/phase4-allocation.v1.output.json) | `targets.deviations`, `plan.legs`, `application` |
