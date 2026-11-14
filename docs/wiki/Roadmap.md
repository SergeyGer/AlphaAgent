# Roadmap

What has shipped, what is deliberately not built yet, and why. The open items are
tracked as GitHub Issues; this page keeps the reasoning and the boundary between
shipped and planned. When the two disagree, the issue and the code at HEAD are
newer than this page.

> **Open work** — see the [issue list](https://github.com/SergeyGer/AlphaAgent/issues).
> The issue body names the current state at HEAD and what remains. This page does
> not claim an issue is done because part of it shipped.

---

## Delivered since FIX-07

The acceptance review after FIX-07 produced ten remediation steps, a second
review re-tested every repair against the defect it claimed to close, and step 11
recorded the result. Each
finding behind them was reproduced before it was fixed. The commits and the
proving tests are recorded in
[`CHANGELOG.md`](https://github.com/SergeyGer/AlphaAgent/blob/main/CHANGELOG.md)
under Unreleased, and the implementation detail is in
[`IMPLEMENTATION-NOTE.md`](https://github.com/SergeyGer/AlphaAgent/blob/main/docs/spec/IMPLEMENTATION-NOTE.md).

| Delivered | Commit / PR | What it established |
| --- | --- | --- |
| FIX-08 portfolio advisory product, one real recorded provider run | `4078a94` / #54 | Submission, review, approval and apply; one recorded real model call |
| Steps 1–2, first pass: quote provenance, approval binding, execution/audit atomicity | `602a899` / #60 | An untimed or stale mark cannot authorise a fill; a fill and its audit commit together |
| Steps 1–2, second pass: the approval binds the displayed terms, a redelivery returns the original fill | `153e238` / #69 | A stale screen or foreign identity is refused without a fill; the Celery task id is the execution identity |
| Steps 3–4, first pass: attribution across horizons, replay order, late outcomes | `76c3017` / #61 | Each horizon is independent and measured from the fill; the guard visible at the cursor is the one shown |
| Steps 3–4, second pass: fair scheduling, event-revision replay, unavailable outcomes | `84a8060` / #70 | The work limit selects due work, not candidate decisions; a partial page hides the future; an exhausted horizon is a durable event |
| Steps 5–6, first pass: portable exports, recoverable analysis jobs | `f54b2bf` / #62 | One canonical JSON in Python and the browser; analysis is a pollable worker job with a repair path |
| Steps 5–6, second pass: explicit run origin, recovery without an operator | `8ff7194` / #71 | An authored export cannot claim a provider run; a broker failure recovers itself and a resume keeps the input snapshot |
| Steps 7–8, first pass: isolated experiments, registered metric, simulated time, report rebuild | `4e6c71f` / #63 | The pre-registered metric is computed; simulated time is monotonic; an archive rebuilds from its recorded inputs |
| Steps 7–8, second pass: attempt identity, matched benchmarks, fill-time validation | `e93d7bd` / #72 | A changed model or budget is a new attempt; a fill is revalidated at its price and time; the day's realised P&L reaches the next decision |
| Steps 9–10, first pass: conservative spend limiter, verified acceptance | `7969cd7` / #64 | Unknown cost is reserved, not counted as zero; a recording names a passing commit |
| Steps 9–10, second pass: per-attempt spend identity, release gate | `bc030e4` / #75 | Each paid attempt reserves by its own identity; an incomplete stage set can never be usable for recording |
| Step 11: the walkthrough recut around the interface and republished on YouTube | `1bb1ee2`, `0051067` / #78, #79 | One narrated recording, its timecode-to-evidence table, and a published copy at <https://youtu.be/JuLyy8xqsWQ> |
| FIX-10: the walkthrough rebuilt around one decision | `3d229cd` / #59 | The earlier cut of the same single recording, with a poster |

The suites at HEAD `0051067` contain 1029 backend tests and 289 frontend tests.
`scripts/verify_application.py` also runs lint, format, migration check,
typecheck, build and an HTTP smoke test. The committed full eight-stage release
record is `docs/spec/acceptance/fe326ef3e593391ae525718e0a8f331720127e41.json`,
generated at `fe326ef`, committed in the PR head `877a6c4` and merged as
`bc030e4`: 1029 backend / 289 frontend, `tree_clean: true`,
`release_acceptance: true`. A commit cannot contain
the record that names its own hash, so the documented order is commit, verify,
then commit the record; #65 is closed on that record. The verification command
and the record contract are in
[`ACCEPTANCE.md`](https://github.com/SergeyGer/AlphaAgent/blob/main/docs/spec/ACCEPTANCE.md).

One operational gap from the same programme remains open:
[#66](https://github.com/SergeyGer/AlphaAgent/issues/66) (the V4 allocation arm,
declared in every manifest and never executed).

### What the programme did not complete

Four proposals from comparing against
[TradingAgents](https://github.com/TauricResearch/TradingAgents) gained a real
capability without meeting their full acceptance criteria. They remain open:

- **#29 deterministic replay** — the offline viewer replays stored payloads
  deterministically, the export round-trips every guard evaluation, and the
  second pass made replay resolve each outcome to its event revision and mark an
  exhausted horizon unavailable (`core/tests/test_p1_step4_replay_and_unavailable.py`).
  No command re-runs the guard against a recorded proposal and sweeps every
  stored decision.
- **#33 point-in-time contract** — execution marks now carry and validate an
  observation time (`services/quote_policy.py`), the approval binds the displayed
  observation provenance, and experiment evaluation refuses future knowledge
  (`core/tests/test_p0_terms_binding_and_redelivery.py`,
  `test_p1_step8_fill_validation.py`). The agent tools still fetch "now"; they do
  not accept an `as_of` bound, and no test iterates the tool registry looking for
  leakage.
- **#34 live scorecard** — the scorecard is computed at read time, the due
  horizons are scheduled oldest-first, and the experiment report computes its
  pre-registered metric. No generated public scorecard file, scheduled
  regeneration or README badge exists.
- **#35 verifiable audit bundle** — the export endpoint emits a checksummed
  bundle verified in Python and in the browser, now with an explicit `run.origin`
  claim, tamper and redaction tests. The proposed CLI and the offline re-run of
  the guard verdict are not built.

---

## Next

### Cumulative daily deployment ceiling
**Tracked as [#27](https://github.com/SergeyGer/AlphaAgent/issues/27).**
**Motivated by [INC-001](Incident-Log#inc-001--overlapping-sweeps-bought-the-same-stock-twice).**

The per-ticker cooldown bounds *overlapping* runs, but nothing bounds a
deliberate sequence of individually-permitted trades. Two sweeps three seconds
apart each respected the per-trade ceiling and together doubled the intended
exposure.

Phase 4 added useful ceilings — `max_portfolio_exposure_pct`,
`max_ticker_concentration_pct`, `max_sector_concentration_pct`,
`max_turnover_pct` and `min_cash_buffer_pct` — and none of them closes this gap.
The sector and exposure checks bound the *state after one trade*; turnover bounds
*one plan*, not a day's sequence of plans. The missing control is a portfolio-level
cap on total capital deployed per day, enforced in `evaluate_proposal` as a pure
function, with today's deployment total as an input.

*Why not done:* it changes the guard's signature, and that is a change worth
making deliberately rather than alongside the throttling work.

### Backtesting against historical data
**Tracked as [#28](https://github.com/SergeyGer/AlphaAgent/issues/28).**
The agent layer produces a decision for a point in time. Replaying a historical
period would answer the question the project cannot currently answer: *would this
strategy have made money?*

Even after the remediation, the answer is blocked by the missing half of #33: a
backtest must be able to ask every tool "what was true on 2026-06-01", and the
tools still fetch the current quote and the current news feed. The experiment
simulator (`services/experiment_simulation.py`) is not a backtest: it replays an
authored opportunity set with a monotonic clock, not a historical market.

*Why not done:* yfinance's historical coverage is uneven for the instruments in
use, and a backtest that silently skips missing data is worse than none. It needs
a data source with a stated completeness guarantee before the results mean
anything.

### Deterministic replay of a recorded decision
**Tracked as [#29](https://github.com/SergeyGer/AlphaAgent/issues/29).**
The audit trail stores both arguments, the verdict and the context. A replay mode
would re-run the guard against a recorded proposal and confirm it still produces
the same verdict — proving the risk behaviour is reproducible rather than merely
tested.

The shipped part is a replay of *stored payloads*: `docs/viewer/index.html` and
the authenticated detail screen resolve the guard evaluation visible at a cursor,
and the export preserves every evaluation. They do not re-evaluate the guard; the
viewer says so in its own limitations. The remaining work is a management command
that replays one decision, a sweep over every stored decision, and a failing exit
code on divergence.

---

## Later

### Per-user strategy configuration
**Tracked as [#30](https://github.com/SergeyGer/AlphaAgent/issues/30).**
`Portfolio.user` is a `OneToOneField`, so one user owns one portfolio. Risk
profile and limits are already per portfolio, and `is_autonomous` is too. The
watchlist (`ALPHA_WATCHLIST`) and the AI spend ceiling
(`AI_DAILY_SPEND_LIMIT_USD`) are deployment-global. `AIUsageLedger` now carries
an explicit `scope`/`scope_key`, so a reservation can name a portfolio, but the
configured ceiling is still one deployment-wide value.

*Why not done:* the multi-tenant boundaries exist (every query is ownership-scoped)
but there is no reason to rush into a user-facing configuration surface before
there is more than one user.

### Hosted demonstration instance
**Tracked as [#31](https://github.com/SergeyGer/AlphaAgent/issues/31).**
A read-only public deployment would let a reviewer explore the dashboard without
running Docker. The current public surfaces are the Streamlit page, which runs the
real guard on unsaved objects and nothing else, and the offline viewer, which is a
static no-database replay with no execution or approval path.

*Why not done:* this is a system that executes trades. A public instance needs
hard isolation — a sandboxed broker, no outbound LLM spend, seeded data — and
getting that wrong is worse than not having a demo.

### Additional asset classes and providers
**Tracked as [#32](https://github.com/SergeyGer/AlphaAgent/issues/32).**
The ledger already handles fractional quantities to eight decimal places for
crypto. Extending to other instruments is mostly a matter of additional market
data adapters behind the existing tool interface.

*Why not done:* every market-data path still goes through yfinance
(`services/market_data.py`, `services/fundamentals.py`), and there is no verified
second source with the same graceful-degradation behaviour.

### Point-in-time data contract
**Tracked as [#33](https://github.com/SergeyGer/AlphaAgent/issues/33).**
Execution marks now carry observation times and pass the freshness policy; the
live path treats an untimed mark as missing data. The tool contract still has no
`as_of` parameter, so a historical question cannot be asked safely. See the
delivered/remaining note above.

### Live performance scorecard
**Tracked as [#34](https://github.com/SergeyGer/AlphaAgent/issues/34).**
The dashboard scorecard is computed at read time for 1/7/30 days, with the basis
and sample size beside the rate. It is not published as a generated file, and no
badge reflects it. See the delivered/remaining note above.

### Verifiable audit bundle
**Tracked as [#35](https://github.com/SergeyGer/AlphaAgent/issues/35).**
`GET /api/decision-runs/<id>/export/` emits a redacted, checksummed bundle; the
offline viewer verifies it in the browser with the same canonical JSON as Python.
The proposed CLI and guard recomputation are the missing part. See the
delivered/remaining note above.

---

## Explicitly not planned

**Leverage, margin or short selling.**
The entire risk model assumes losses are bounded by the cash balance. Supporting
leverage invalidates the guard's core invariants — particularly the daily-loss
asymmetry, where de-risking is always permitted because it cannot increase
exposure. That assumption does not survive margin.

**Replacing the deterministic guard with a learned model.**
This is the one decision that is not open for reconsideration. See
[[Engineering-Decisions|ADR-001]]. A model can be argued with, influenced, or
upgraded underneath you; a pure function cannot.

**Autonomous execution without an audit trail.**
Every run writes a decision log, including HOLDs and rejections. There is no
configuration flag to disable it, and there will not be one.

---

## How this list is ordered

By what the system's own evidence argues for, not by what is most interesting to
build:

1. **INC-001** is the only incident in the log where the *design* was incomplete
   rather than the implementation. A cumulative daily ceiling is the remaining
   half of that fix, and the Phase 4 ceilings do not replace it.
2. Replay and backtesting both increase confidence in behaviour that is currently
   only unit-tested. Replay has a shipped foundation; backtesting is still blocked
   on the tool-level `as_of` bound.
3. Everything else is a feature, and features are cheaper to add than trust is to
   recover.

## Related pages

- [[Incident-Log]] — the evidence behind the ordering
- [[Engineering-Decisions]] — the decisions these items would revisit
- [[Execution-Guard]] — where a deployment ceiling would live

## Implementation programme (Technical Specification v1.0)

The specification at `docs/spec/IMPLEMENTATION-NOTE.md` sets out a five-phase
programme. It is a programme, not a single change: each phase has an exit gate, and
later phases depend on earlier ones. Recorded here so the boundary between shipped
and planned stays explicit.

| Phase | Deliverable | State |
| --- | --- | --- |
| **0 / Baseline** | Pin the commit, inventory models, endpoints and events, confirm fallback and transaction behaviour, record divergences | **Done** — baseline `1527cdc`, divergences recorded |
| **1 / Explain one decision** | Decision detail, guard comparison, legacy labels, recorded fixture playback, accurate README and video edits | **Done** — v1.1.5; see `docs/spec/FIXTURES.md` |
| **2 / Real rebuttals** | Evidence and claim contracts populated by a live protocol, independent first round, one bounded response round, stage events, repair budgets | **Done** — v1.1.6; `services/debate_protocol.py` |
| **3 / Measure value** | Versioned signal diagnostics, ledger-based portfolio performance, experiment manifests, V0–V3 reports | **Done** — v1.1.7; `docs/spec/MEASUREMENT.md` |
| **4 / Allocate capital** | Complete portfolio brief, target weights, deterministic plan sizing, advisory workflow | **Done** — v1.1.8; `docs/spec/ALLOCATION.md` |
| **5 / Publish evidence** | Curated recorded cases, revised demonstration, methodology report | **Done** — `0724f11`; `docs/spec/METHODOLOGY.md`, `CASES.md`, `DEMO.md`, `CLAIMS.md` |
| **P0/P1 remediation** | Quote provenance, execution/audit atomicity, attribution horizons, replay order, portable exports, recoverable jobs, experiment isolation and metric, simulated time, spend limiter, verified acceptance, the walkthrough | **Done** — steps 1–11 across two passes in #60–#64, #69–#72, #75 and #78–#79; `docs/spec/IMPLEMENTATION-NOTE.md` and `ACCEPTANCE.md` |

### Why the phases cannot be collapsed

- **Phase 2 needs real evidence.** Phase 1 establishes the claim, rebuttal and
  evidence contracts and serves them; a rebuttal section only appears when a run
  actually produced rebuttals. Fixtures are labelled `synthetic` so a reviewer can
  tell the difference. Letting the UI imply a debate that never happened would
  breach the specification's REL-02 invariants.
- **Phase 3 needs a frozen metric definition.** Signal diagnostics and portfolio
  performance must be defined before any comparative claim. Adding portfolio
  accounting after publishing a comparison would mean re-labelling numbers that
  people already quoted.
- **Phase 4 needs Phase 3.** An allocator without reconciled portfolio accounting and
  execution safeguards is the exact configuration REL-02 calls release-blocking.

### What Phase 1 deliberately does not do

The specification's acceptance for Phase 1 is that a reviewer can identify the
decisive evidence, the strongest objection, the reason for the verdict, the guard
intervention and the measured outcome **without reading a raw log, offline, with no
keys**. It does not require new agents, and this implementation adds none.
