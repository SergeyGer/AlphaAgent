# Position Thesis

Why the system holds what it holds — and what would prove it wrong.

## The problem it solves

The crew runs with `memory=False` (see [ADR-0007](https://github.com/SergeyGer/AlphaAgent/blob/main/docs/adr/0007-provider-agnostic-llm-with-heuristic-fallback.md)),
deliberately: a conversational memory would grow without bound, make each run
depend on every previous one, and destroy reproducibility.

The consequence had not been examined. Every sweep built a fresh crew with no
knowledge of what it decided last time, so the system had no notion of *holding* a
position — only a series of independent opinions about an instrument it happened
to own. It could sell at 10:00 and buy back at 10:15 without noticing, not because
it changed its mind but because it had no record of having one.

It also left the bear agent with nothing specific to attack. "The bull case is
wrong" is not a criticism, and a short seller with no stated thesis to argue
against produces generic risk lists.

## The model

One live thesis per (portfolio, ticker):

| Field | Meaning |
| --- | --- |
| `thesis` | Why the position is worth holding, in the agent's words |
| `invalidation` | The observable condition that would make it wrong |
| `status` | `OPEN`, `CLOSED` or `INVALIDATED` |
| `opened_by` / `closed_by` | The decisions that opened and closed it |

A conditional unique constraint — `one_open_thesis_per_portfolio_ticker` — makes
"exactly one live thesis per instrument" a **schema invariant**, not a convention.

### Superseded, never overwritten

When a new thesis is recorded for an instrument that already has one, the old row
is **closed and kept**. The table reads as a history of what was believed and
when, which is what makes a reversal auditable as a change of mind. Editing in
place would silently rewrite the past — the one thing an audit trail must not do.

## Lifecycle

```
BUY executes  ──▶  thesis recorded, opened_by = the decision
                   (a blank thesis is not recorded)
BUY again     ──▶  previous thesis CLOSED, new one opened
SELL, partial ──▶  thesis stays OPEN
SELL, full    ──▶  thesis CLOSED, close_reason = "Position fully closed by a SELL."
```

Two rules worth stating:

- **Only after an executed trade.** An advisory recommendation may yet be
  rejected, and a thesis recorded for a trade that never happened would be fed
  back to the next run as prior belief.
- **A blank thesis is not recorded.** Writing a placeholder would put words in the
  agent's mouth and then present them to it later as its own considered view.

## What the agents see

The thesis is injected into the CIO's brief:

```
PRIOR THESIS (recorded when this position was opened)
Services margin expanding; upgrade cycle intact.
Stated invalidation condition: Gross margin below 40% for two quarters.
```

When there is none, the brief **says so**:

```
PRIOR THESIS
None on record - either this is a new position or it was opened before theses
were recorded. You are not contradicting an earlier view; there is none.
```

The negative case is stated rather than omitted on purpose. A blank section reads
as "no information", which a model is free to interpret as "no opinion was ever
formed" — and then to invent one.

The CIO is instructed that proposing a `SELL` against a recorded thesis requires
saying what stopped being true, and that a `BUY` must fill in both `thesis` and
`invalidation`. A thesis that cannot be wrong is not a thesis.

## Limitations

- **A stale thesis is worse than no thesis.** If a position is closed outside the
  normal path — an operator editing the database, a manual trade — the thesis
  stays open and is presented to the next run as current belief. Nothing detects
  that.
- **It is model-written and unverified.** A plausible-sounding but wrong reason
  for holding becomes a prior that later runs treat as established fact.
- **It anchors the agent.** The instruction to say what stopped being true
  mitigates the risk of stubbornly holding a bad position; it does not remove it.
- **`invalidation` is free text and nothing evaluates it.** It informs the agent;
  it does not automate an exit.
- An advisory trade writes no thesis, so a position opened by an operator-approved
  recommendation has no recorded reason until the next autonomous buy.

Related pages: [[Agent-Debate]], [[Execution-Guard]], [[Decision-Attribution]], [[Data-Model]].
