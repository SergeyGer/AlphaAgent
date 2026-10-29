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

```
+ 1d  evaluated=15  calls=15  hit_rate=26.7  avg_decision=-0.2666%  alpha=-0.2203
+ 7d  evaluated= 2  calls= 2  hit_rate= 0.0  avg_decision=-1.4768%  alpha=-1.2606
+30d  evaluated= 0  calls= 0  hit_rate=  n/a  avg_decision= 0.0000%  alpha=None
```

That is the real output as of writing, and it is unflattering. Fifteen scored
calls is far too few to conclude anything, and one-day returns are mostly noise —
but the number is shown as measured rather than withheld, because a system that
reports only its flattering statistics is not reporting.

## The ticker column

Attribution needs to know what each decision was about. The ticker used to be
reachable only through `transaction`, which meant a HOLD — or a HALT — had none at
all, and those are precisely the rows most worth reviewing.

`AgentDecisionLog.ticker` is now a real column, indexed, written by every path that
creates a row. A migration backfilled 62 rows from their transactions. The 1247
that remain blank are pre-migration HOLDs whose ticker was never stored anywhere;
inventing one would be worse than admitting the gap, so they are simply
unscorable.

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

## The published scorecard, as it stands

Every decision is scored against what the market did next, and the scorecard is shown
to any visitor of the dashboard. Across **57 scored decisions at a 1-day horizon**:

| | |
| --- | --- |
| **Hit rate** | **10.5%** — 6 of 57 directional calls |
| **Average decision return** | **−1.78%** |
| **Alpha versus SPY** | **−1.54%** |
| **BUY calls at 1 day** | **7.0%** hit rate, average forward return −2.08% |

Put plainly: **the system buys things that fall and sells things that rise.** SPY was
roughly flat over the same windows, so this is not the market going down.

That result is published rather than hidden because it is the point of the feature. A
measurement that can only produce good news is not a measurement. The flaw I found in
my own scoring method — one that made these numbers *worse* — is recorded below.

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

