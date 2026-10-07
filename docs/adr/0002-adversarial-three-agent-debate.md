# 0002. Run an adversarial three-agent debate instead of a sequential pipeline

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** Project maintainer
- **Related:** [ADR-0001](0001-deterministic-execution-guard.md) · [ADR-0005](0005-mcp-tool-boundary.md) · [ADR-0007](0007-provider-agnostic-llm-with-heuristic-fallback.md) · [ADR-0010](0010-bounded-agent-runs.md)

## Context

The original design was sequential: a research analyst produces a summary and a
decision-maker consumes it. That is the standard CrewAI shape and it is cheap —
one reasoning pass plus one decision pass.

It is also a hallucination amplifier. Whatever the analyst asserts becomes the
premise the decider reasons from, and nothing in the system ever argues the other
side, so a single confident error propagates unchallenged into a trade. The
project's evidence for this is not theoretical: the short seller in one live run
reported that it "cannot build a credible bear case for Apple at current levels",
and the final verdict cited that admission.

Rejected alternatives, recorded in the wiki:

- *Two agents plus a majority vote.* Voting teaches nothing about *why* a case is
  weak and produces no artefact a human reviewer can audit.
- *N agents with different personas.* More personas reasoning from the same
  evidence produce correlated errors, not independent scrutiny.

*Date note: 2026-09-30 is the initial-release commit (`f6f3397`) that shipped this
design; the decision itself predates it and no earlier dated record exists.*

## Decision

We run an adversarial debate between three agents per instrument:

- a **bullish research analyst** building the strongest honest bull case from news
  sentiment and price history;
- a **risk assessor (short seller)** building the bear case — and the *only* agent
  with access to the fundamentals tool, so the disagreement is built from
  different evidence rather than a different tone;
- a **chief investment officer** that adjudicates on evidence and returns a single
  `TradeProposal` (`action`, `amount`, `sentiment`, `reasoning`).

Both debating agents are explicitly instructed to concede when their case is weak
("fabricating a risk is as damaging as missing one"). Both cases are persisted on
`AgentDecisionLog` as `bull_case` and `bear_case` and streamed to the dashboard,
so a verdict is traceable to the two arguments that produced it. The output
contract is enforced twice — Pydantic via CrewAI's `output_pydantic`, and a task
guardrail that re-parses the raw text and forces a retry on malformed output —
because a model that wraps valid JSON in prose passes one check and fails the
other.

## Consequences

### Positive

- Disagreement is substantive rather than performative, because the two cases rest
  on different tool outputs.
- A verdict can be reviewed months later against the arguments that produced it;
  the audit trail records what the system was told, what it argued, and which
  argument won.
- A failure in one data source degrades only one side of the debate: a missing
  fundamentals feed makes the bear case visibly thinner rather than quietly
  weakening both cases.
- A structurally invalid response cannot become a proposal.

### Negative / trade-offs

- Cost roughly triples. The wiki records three agents, roughly 100k tokens and
  about $0.10 per instrument per run, and that is before the sweep runs across a
  whole watchlist on a timer.
- Latency roughly triples, which is why runs are bounded by iteration, retry and
  time caps ([ADR-0010](0010-bounded-agent-runs.md)).
- The debate produces a written case, not a calibrated probability. A confident,
  well-argued wrong case can still win adjudication.
- The asymmetry that makes the debate valuable is also a bias: if the fundamentals
  tool is down, the bear side is systematically weaker.
- More prompt surface to maintain, and provider-specific quirks in the crew have
  caused real incidents (INC-005).

### Neutral

- The crew still runs sequentially (bull → bear → CIO). "Adversarial" describes
  the evidence and the mandates, not concurrency.
- The debate is optional by design: with no LLM credential the pipeline falls back
  to the deterministic engine ([ADR-0007](0007-provider-agnostic-llm-with-heuristic-fallback.md)).
