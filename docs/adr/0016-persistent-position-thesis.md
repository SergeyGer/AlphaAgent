# 0016. Give each open position a persistent thesis

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Project maintainer
- **Related:** [ADR-0002](0002-adversarial-three-agent-debate.md), [ADR-0007](0007-provider-agnostic-llm-with-heuristic-fallback.md), [ADR-0014](0014-decision-outcome-attribution.md)

## Context

The crew runs with `memory=False`, and ADR-0007 gives the reason: a conversational
memory would grow without bound, make each run depend on every previous one, and
destroy reproducibility — the same inputs would no longer produce the same
decision.

The consequence had not been examined. Every sweep built a fresh crew with no
knowledge of what it had decided before, so the system had no notion of *holding*
a position. It had a series of independent opinions, each formed from scratch,
about an instrument it happened to own.

In practice that means the agent can sell a position at 10:00 and buy it back at
10:15 without noticing. Not because it changed its mind — it had no record of
having one. Its `reasoning` explained the decision it was making, not the position
it was managing.

Two things made this worse than it sounds. The bear agent had nothing specific to
attack: "the bull case is wrong" is not a criticism, and a short seller with no
stated thesis to argue against produces generic risk lists. And ADR-0014's
attribution could score a decision but had no way to distinguish a considered exit
from an accidental one.

## Decision

**We record one durable thesis per open position, written by the agent that opened
it, and carry it into every subsequent run as context.**

Specifically:

- `PositionThesis` holds `thesis` (why we hold it) and `invalidation` (the
  observable condition that would prove it wrong) per (portfolio, ticker).
- A unique constraint — `one_open_thesis_per_portfolio_ticker`, conditional on
  status `OPEN` — makes "one live thesis per instrument" a schema invariant rather
  than a convention.
- A superseded thesis is **closed and kept, never edited**. The table reads as a
  history of what was believed and when.
- It is written only after an **executed** trade. An advisory recommendation may
  yet be rejected, and a thesis recorded for a trade that never happened would be
  fed back as prior belief.
- A SELL that empties the position closes the thesis. A thesis left open against a
  position that no longer exists is worse than none, because it is presented to
  the next run as current.
- The crew receives it as a `PRIOR THESIS` block in the brief, with the **absence**
  stated explicitly — "None on record" — rather than omitted. A blank section
  reads as "no information", which invites a model to invent a prior it was never
  given.
- The CIO is instructed that proposing a SELL against a recorded thesis requires
  saying what stopped being true, and that a BUY must supply both a thesis and an
  invalidation. A thesis that cannot be wrong is not a thesis.

## Consequences

### Positive

- The system now behaves like something managing a position rather than forming
  independent opinions about an instrument it happens to own.
- The bear agent has a specific claim to attack, which should sharpen the debate
  in a way that is visible in the output.
- Exits become deliberate. An agent proposing to exit must engage with the reason
  it entered, in writing, in the audit trail.
- The full history of what was believed about an instrument is preserved, so a
  reversal is auditable as a change of mind rather than looking like noise.
- It is the narrow version of memory: one paragraph per open position, bounded,
  and reproducible in a way a conversation log is not.

### Negative / trade-offs

- **A stale thesis is worse than no thesis.** If the position is closed outside
  the normal path — an operator editing the database, a manual trade — the thesis
  stays open and is fed to the next run as current belief. Nothing detects that.
- **The thesis is model-written and unverified.** An agent that writes a
  plausible-sounding but wrong reason for holding creates a prior that later runs
  treat as established fact. The invalidation condition is the only defence, and
  the field is optional because a model that cannot name one must be allowed to
  say so.
- **It anchors the agent.** A recorded thesis may make the system slower to
  reverse a genuinely bad position: the prior is presented as its own considered
  view, and models weight their stated priors. The instruction to say what
  stopped being true mitigates this; it does not eliminate it.
- Prompt length grows by a paragraph per run, and the thesis is duplicated into
  every sweep for that instrument, so cost rises slightly with position count.
- The `invalidation` field is free text and nothing evaluates it. It informs the
  agent; it does not automate an exit.
- An advisory trade writes no thesis, so an operator approving a recommendation
  later produces a position with no recorded reason until the next autonomous buy.

### Neutral

- The default `memory=False` is unchanged. This is not conversational memory and
  does not make runs depend on each other: the thesis is an input, reconstructed
  from the database, and a run with no thesis behaves exactly as before.
- `sync_after_trade` is a single entry point rather than two calls at the call
  site, so a future branch cannot close a position without closing its thesis.
