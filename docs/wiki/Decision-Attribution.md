# Decision Attribution

The audit trail answers **why** every decision was made. This page describes the
other half: **whether any of them were right**.

Until this existed, a `SELL` that avoided a twenty-percent drawdown and a `SELL`
that missed a twenty-percent rally were identical in the database — both
well-reasoned decisions with cited evidence. The system could be described but not
evaluated, and the most natural question a reader asks had no answer anywhere in
the repository.

## What is recorded

`DecisionOutcome` stores one row per **decision × horizon**, for horizons of 1, 7
and 30 days:

| Column | Meaning |
| --- | --- |
| `forward_return_pct` | What the instrument did over the window |
| `decision_return_pct` | What that was worth **to the action taken** |
| `benchmark_return_pct` | SPY over the same window, or null |
| `directional_hit` | True / False, or **null** for a HOLD |

### Two framing choices

Both are deliberate, and both are easy to get wrong in a way that flatters or
punishes the system.

**A decision is scored by what the action was worth, not by what the price did.**
For a `BUY`, `decision_return_pct` is the forward return. For a `SELL` it is the
negation: a sell is right when the price falls afterwards. Scoring every decision
against "did the price rise" would mark every correct exit as a failure and
produce a hit rate that punishes the system for protecting capital.

**A HOLD is not scored as a directional call.** `directional_hit` is `NULL` for a
HOLD, not `False`. Holding makes no directional claim, and counting those rows as
misses would drag the measured hit rate toward zero in proportion to how cautious
the system was. HOLDs still carry a `decision_return_pct` of zero, so they are
visible in the average without distorting the hit rate.

A missing benchmark is `NULL`, never zero. An unknown comparison is not a loss,
and `avg_alpha_pct` is null rather than negative when nothing has been benchmarked.

## Horizons are separate rows

A call can look wrong at one day and right at thirty. That disagreement is the
interesting part, so it is preserved rather than collapsed into a single score.
The scorecard prints `evaluated`, `directional_calls`, `hit_rate_pct`,
`avg_decision_return_pct` and `avg_alpha_pct` per horizon.

Each horizon is now scheduled independently. `OutcomeEvaluation` is keyed by
decision, horizon and method version, so a day-one score no longer makes days
seven and thirty ineligible; an unpriceable decision gets a state and a reason
instead of silently blocking the batch. `manage.py backfill_outcome_records`
repairs older rows that were scored on the wrong basis, preserving the prior
revision rather than overwriting it.

This page does not reproduce a live scorecard dump. The dashboard computes it from
the database on every read, so the figures change as outcomes mature and as new
decisions are scored; they are not a committed artefact and cannot be checked from
this repository. The measurements that *are* committed are the synthetic Phase 3
report at
`docs/spec/fixtures/experiments/phase3-engineering.v1.report.json`, and it is
labelled engineering evidence only. For the method behind each number, see
[`MEASUREMENT.md`](https://github.com/SergeyGer/AlphaAgent/blob/main/docs/spec/MEASUREMENT.md) §1.5.

## The ticker column

Attribution needs to know what each decision was about. The ticker used to be
reachable only through `transaction`, which meant a HOLD — or a HALT — had none at
all, and those are precisely the rows most worth reviewing.

`AgentDecisionLog.ticker` is now a real column, indexed, written by every path that
creates a row. A migration backfilled 62 rows from their transactions. The 1247
that remain blank are pre-migration HOLDs whose ticker was never stored anywhere;
inventing one would be worse than admitting the gap, so they are simply
unscorable. Those counts are recorded in
[ADR-0014](https://github.com/SergeyGer/AlphaAgent/blob/main/docs/adr/0014-decision-outcome-attribution.md);
they come from the production database at migration time and cannot be reproduced
from this repository.

The logs endpoint now filters on the column too, with a fallback to the
transaction — filtering through `transaction__ticker` silently excluded every HOLD.

## Running it

```bash
# Score everything that has matured (idempotent)
python manage.py shell -c "from services.attribution import evaluate_pending; print(evaluate_pending())"

# Read the scorecard
curl -H "Authorization: Token $TOKEN" http://127.0.0.1:8080/api/portfolio/attribution/
```

Beat runs `tasks.evaluate_decision_outcomes_task` hourly. Hourly rather than on the
fifteen-minute sweep because an outcome only matures when a decision crosses a
day-scale boundary, and the task fetches price history per pending decision.

## What it does not measure

Worth stating plainly, because a scorecard invites more confidence than it earns:

- **HOLD decisions are invisible to the hit rate.** They are excluded by design,
  but a system that mostly holds shows a rate computed from a small fraction of
  its decisions. Read `evaluated` alongside `hit_rate_pct`, never the rate alone.
- **The measure is per-decision, not portfolio-level.** A sold position that would
  have risen 10% scores −10%, even if the proceeds went into something that rose
  20%.
- **The benchmark is naive.** SPY over the same calendar window takes no account of
  beta: a crypto position compared to an equity index is not like-for-like.
- **The sample is small and the horizons are short.** Nothing here is statistically
  meaningful yet.

Related pages: [[Execution-Guard]], [[Agent-Debate]], [[Data-Model]], [[Quality-and-Testing]].

## The live scorecard

Every decision that names an instrument is scored against what the market did
next, and the scorecard is shown to a signed-in visitor of the dashboard. The
numbers are read at request time from `DecisionOutcome` rows; this page does not
quote them, because a figure that changes between reads and cannot be reproduced
from the repository is not evidence a reviewer can check. Publishing the same
numbers as a generated, always-current file with its sample size is open as
[#34](https://github.com/SergeyGer/AlphaAgent/issues/34).

What can be checked here is the method: the action signing, the HOLD null, the
benchmark null, the start-price choice and the limitations below. The only
committed measurement report is the synthetic Phase 3 fixture report, and it says
`engineering_validation_only: true`, `no_profitability_claim: true` and
`strict_historical_claims: false`.

The flaw I found in my own scoring method is recorded in
`services/attribution.py` and `CHANGELOG.md`: forward returns used to start at
the day's close while trades executed at the live quote. An executed decision is
now measured from its linked fill's stored price and timestamp
(`measurement_basis: "execution_fill"`), so a recommendation approved at 130 is
measured from 130, not from the proposal price of 100. A HOLD, a rejection or an
executed decision with no linked fill uses the first close at or after the
proposal (`measurement_basis: "proposal_close"`) with `fill: null` and a stated
reason; an unknown basis is never presented as a fill. The row publishes both
`measurement_basis` and `reference_at`, and the scorecard says
`portfolio_performance: false` and `summable: false` beside the aggregate.

The direction of the historical bias — it flattered a BUY whenever the close sat
below the entry — is why it is kept documented rather than quietly corrected.
The correction is pinned by
`test_p1_attribution_remediation.py::test_the_unmocked_start_price_is_the_recorded_fill_not_the_proposal_close`;
the basis and independence of each horizon are pinned by
`test_p1_attribution_remediation.py::test_the_scorecard_publishes_that_this_is_a_signal_diagnostic`
and `test_a_day_one_score_does_not_prevent_days_seven_and_thirty`.

### Why this is a product feature, not a leaderboard

| Question a stakeholder asks | Where the answer lives |
| --- | --- |
| Did the model's calls work? | Forward return per decision at 1, 7 and 30 days |
| Did it beat the benchmark? | Alpha versus SPY over the same window |
| Was it right in direction? | `directional_hit`, which is `None` rather than `False` while unresolved |
| What did the run cost? | `api_cost_usd` and `tokens_used`, stored on the decision row |
| Why was the decision made? | `bull_case` and `bear_case`, stored verbatim with the decision |

The last row is the one that matters for audit: a decision row carries both sides of
the argument that produced it, so a number can always be traced back to a transcript.

