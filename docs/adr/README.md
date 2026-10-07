# Architecture Decision Records

An Architecture Decision Record (ADR) captures **one** significant technical
decision: the forces that made it a real choice rather than an obvious one, what
was decided, and the consequences that were accepted. A decision recorded this way
can be reviewed and challenged later; an undocumented one has to be reverse
engineered from the code.

These records were converted from the project's prose decision log so that each
decision has one addressable file and a stable number. The source material lives
in the wiki — chiefly `Engineering-Decisions`, supported by `Architecture`,
`Execution-Guard`, `Agent-Debate`, `Data-Model`, `Real-Time-Layer`, `Operations`,
`Quality-and-Testing`, `Roadmap` and `Incident-Log` — and in the README.

## Format

Every ADR uses this section order:

```markdown
# NNNN. Short imperative title

- **Status:** Accepted
- **Date:** YYYY-MM-DD
- **Deciders:** Project maintainer
- **Related:** links to related ADRs, or omit

## Context
What forces are at play — the problem, the constraints, and the alternatives that
were rejected, so the decision does not look obvious in hindsight.

## Decision
What was decided, in the active voice: "We use X."

## Consequences
### Positive
### Negative / trade-offs
### Neutral
```

Consequences are meant to be honest. An ADR that lists only benefits is useless
for a future review, so every record here states what the decision costs.

**Statuses.** `Accepted` — in force. `Superseded by ADR-NNNN` — replaced; the
record is kept for history. `Deprecated` — no longer relevant, but not replaced.

**Dates.** The date is the day the decision was recorded where that is known. This
repository's history begins with a single initial-release commit, so several
decisions share the date `2026-09-30`; where that date is an inference rather than
a record, the ADR says so in its Context section.

**Deciders.** The project is solo-maintained, so every ADR names "Project
maintainer".

## Index

| # | Decision | Status | Summary |
| --- | --- | --- | --- |
| [0001](0001-deterministic-execution-guard.md) | Enforce risk limits in deterministic code, not a prompt | Accepted | Position, cash and stop-loss limits live in a pure function the model cannot influence; the AI proposes, the code decides. |
| [0002](0002-adversarial-three-agent-debate.md) | Run an adversarial three-agent debate instead of a sequential pipeline | Accepted | Bull and short seller argue from different evidence and a CIO adjudicates, because a single analyst feeding a decider is a hallucination amplifier. |
| [0003](0003-derived-ledger.md) | Derive the ledger by replaying transactions instead of storing positions | Accepted | The `Transaction` history is the only source of truth for positions, cost basis and realised P&L, so stored state cannot drift. |
| [0004](0004-celery-fan-out-per-portfolio-ticker.md) | Fan out one Celery subtask per portfolio and ticker | Accepted | One independent subtask per `(portfolio, ticker)` pair instead of a loop, so no slow call blocks the sweep and failures stay contained. |
| [0005](0005-mcp-tool-boundary.md) | Expose market and news tools as an MCP server | Accepted | The read-only data tools become a protocol-level capability any MCP client can use, with execution left behind the guard. |
| [0006](0006-fail-loudly-fail-open.md) | Fail loudly on missing configuration, fail open on optional infrastructure | Accepted | Missing credentials refuse to start; a failed cooldown check allows the run, because a silent trading halt is the worse failure. |
| [0007](0007-provider-agnostic-llm-with-heuristic-fallback.md) | Abstract the LLM provider and keep a deterministic heuristic fallback | Accepted | Provider selection is configuration, and a deterministic engine keeps the whole pipeline working offline and through provider outages. |
| [0008](0008-daphne-asgi-single-port.md) | Serve HTTP and WebSockets from one Daphne/ASGI process | Accepted | WSGI cannot hold a WebSocket open, and an earlier gunicorn deployment silently had no live feed, so Daphne serves both protocols on one port. |
| [0009](0009-per-portfolio-sweep-scoping.md) | Scope sweeps and throttling per portfolio | Accepted | Debounce and cooldown keys are namespaced per portfolio, and the on-demand endpoint reports whether it actually dispatched. |
| [0010](0010-bounded-agent-runs.md) | Bound each agent run with iteration, retry and time caps | Accepted | Explicit `max_iter`, retry, request and Celery time limits keep cost and worker time bounded across a whole watchlist. |
| [0011](0011-pip-tools-locked-dependencies.md) | Lock Python dependencies with pip-tools so the full tree is visible | Accepted | Committed hash-pinned lockfiles make all 180 Python packages visible to Dependabot instead of the 34 that direct pins exposed. |
| [0012](0012-daily-ai-spend-ceiling.md) | Cap daily AI spend, separately from the trading limits | Accepted | A hard daily ceiling, defaulting to a non-zero $5.00, checked before dispatch and shown live on the dashboard. The trading guard caps what the system may trade; this caps what it may cost. |
| [0013](0013-mcp-shared-secret-and-network-boundary.md) | Authenticate the MCP server and keep it off the host network | Accepted | The tool server was reachable on host port 8100 with no authentication. Now unpublished, plus a shared-secret header required on every request. |

## Adding a new ADR

1. Take the next free number. Numbers are never reused, even if a record is
   superseded or deprecated.
2. Create `NNNN-short-kebab-case-title.md` using the format above.
3. Add a row to the index table in this file.
4. **Never edit an accepted ADR.** If a decision changes, write a new ADR that
   supersedes it, state the new decision and the reasoning, and update the old
   record's Status to `Superseded by ADR-NNNN`. Editing history in place destroys
   the only thing the format provides — a reviewable trail of what was decided and
   why.
5. A decision that is merely refined (for example a threshold changed) gets a new
   ADR too, unless the refinement contradicts nothing and the original record
   already states it.
