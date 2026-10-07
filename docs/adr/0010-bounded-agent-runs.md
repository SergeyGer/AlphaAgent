# 0010. Bound each agent run with iteration, retry and time caps

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** Project maintainer
- **Related:** [ADR-0002](0002-adversarial-three-agent-debate.md) · [ADR-0004](0004-celery-fan-out-per-portfolio-ticker.md) · [ADR-0007](0007-provider-agnostic-llm-with-heuristic-fallback.md) · [ADR-0009](0009-per-portfolio-sweep-scoping.md)

## Context

A tool-calling agent has no natural stopping point. Each of the three debating
agents can call its tools repeatedly, and every additional pass costs tokens and
holds a Celery worker. The deployment makes that a real constraint rather than a
theoretical one:

- the sweep runs on a 15-minute Beat schedule across every autonomous portfolio
  and every watchlist ticker, one subtask per pair
  ([ADR-0004](0004-celery-fan-out-per-portfolio-ticker.md));
- a single run is a three-agent debate — the wiki records roughly 100k tokens and
  about $0.10 per instrument ([ADR-0002](0002-adversarial-three-agent-debate.md));
- a hung provider request would otherwise occupy a worker slot indefinitely,
  delaying every other instrument in the fan-out.

The shipped configuration therefore sets explicit caps per agent, and they are not
uniform: the risk assessor has three tools and gets more iterations than the two
agents with fewer.

*Date note: 2026-09-30 is the initial-release commit (`f6f3397`) that shipped
these caps; the decision itself predates it and no earlier dated record exists.*

## Decision

We bound every agent run at four levels, all configurable:

- **Agent iterations.** `max_iter=6` for the bull analyst, `max_iter=8` for the
  risk assessor (which holds three tools), `max_iter=6` for the CIO, set in
  `ai_agent.py`.
- **Retries.** `max_retry_limit=AI_MAX_RETRIES` (default `2`) on every agent, which
  also caps the task guardrail's retry loop when the model emits malformed output
  ([ADR-0002](0002-adversarial-three-agent-debate.md)); the same setting drives
  market-data attempts.
- **Per-request limits.** `AI_REQUEST_TIMEOUT` (default 90 s) and
  `AI_LLM_MAX_TOKENS` (default 2048, a response ceiling).
- **Task time limits.** `CELERY_TASK_TIME_LIMIT=600` with a soft limit of 540 s,
  which bound one agent run end to end and are env-overridable.

A run that exhausts its caps is not retried forever: it either produces a result
from what it gathered or falls back to the deterministic engine
([ADR-0007](0007-provider-agnostic-llm-with-heuristic-fallback.md)).

## Consequences

### Positive

- Cost and latency per instrument are bounded, which is what makes a 15-minute
  fan-out over a whole watchlist predictable.
- A hung or looping agent cannot hold a worker slot indefinitely and delay the
  rest of the sweep.
- The caps differ per role, so the agent with the most tools — and therefore the
  most to check — gets the most room.
- The hard time limit gives Celery a defined point at which to reclaim the task.

### Negative / trade-offs

- A run that hits `max_iter` returns what it has. A genuinely deep investigation
  can be truncated mid-argument, and nothing in the recorded decision says the cap
  was reached, so a truncated case looks like a weak case.
- The values are hand-tuned constants, not derived from measurement. There is no
  telemetry in this repository that would show whether 6/8/6 is the right shape.
- The hard time limit kills the task and, with `acks_late` plus
  reject-on-worker-lost, redelivery re-runs it. That is safe only because the
  per-`(portfolio, ticker)` cooldown
  ([ADR-0009](0009-per-portfolio-sweep-scoping.md)) prevents a second trade — the
  cap's safety depends on a different subsystem.
- The token ceiling limits a single response, not the total spend across
  iterations; the effective per-run cost is the product of iterations, retries and
  the response ceiling.

### Neutral

- All four limits are configuration, so the bounds can be changed without touching
  the debate's structure — at the cost of changing the cost profile of every sweep.
