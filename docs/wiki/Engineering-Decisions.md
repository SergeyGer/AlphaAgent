# Engineering Decisions

Every significant choice in this system, recorded with the alternatives that
were rejected and the consequences accepted. The point of this page is not to
show that the decisions were obvious — most were not — but that they were made
deliberately and can be challenged.

Format: **Context → Decision → Consequences → Alternatives rejected.**

---

## ADR-001 — The risk guard is deterministic code, not a prompt

**Context.** An LLM decides how much of a portfolio to deploy. Position limits,
available cash and a daily stop-loss must be enforced. The cheapest
implementation is to state these limits in the system prompt and trust the model
to respect them.

**Decision.** Limits are enforced by a pure Python function that the model has no
influence over. The AI returns a *proposal*; only `evaluate_proposal()` can turn
it into a transaction.

**Consequences.**
- Risk behaviour is exhaustively testable — the guard has no I/O, no model, no
  clock dependency beyond an injected timestamp, so every branch is unit-tested.
- A prompt injection, a model upgrade or a provider outage cannot widen a limit.
- Adding a new limit means writing code and a test, not editing prose. This is
  deliberate friction.

**Alternatives rejected.**
- *Limits in the prompt.* Unverifiable, and silently ignored when the model is
  confused. Untestable by construction.
- *A second LLM as a risk reviewer.* Moves the problem rather than solving it —
  the reviewer is as fallible as the decided, and is now also unaccountable.
- *Database constraints only.* Correct as a last line of defence, but they
  express themselves as exceptions rather than reasoned rejections, and they
  cannot consider the portfolio as a whole.

See [[Execution-Guard]].

---

## ADR-002 — Three adversarial agents rather than one analyst and one decider

**Context.** The original design was sequential: a research analyst produces a
summary, a decision-maker consumes it. This is the standard CrewAI shape and is
cheap — one reasoning pass plus one decision pass.

**Decision.** Replace it with an adversarial debate: a bull analyst and a short
seller independently construct opposing cases, and a chief investment officer
adjudicates between them.

**Consequences.**
- The two cases are built from *different* evidence — the short seller is the
  only agent with access to the fundamentals tool. Disagreement is therefore
  real rather than performative.
- Cost roughly triples: three agents, ~100k tokens and about $0.10 per
  instrument per run.
- Both arguments can be persisted and shown to the user, so a verdict is
  traceable to the two cases that produced it.

**Alternatives rejected.**
- *Sequential analyst → decider.* A single summary feeding a single
  decision-maker is a hallucination amplifier: whatever the analyst asserts
  becomes the premise, and nothing argues the other side.
- *Two agents plus a majority vote.* Voting teaches nothing about *why* a case is
  weak, and produces no artefact a human reviewer can audit.
- *N agents with different personas.* More personas on the same evidence produce
  correlated errors, not independent scrutiny.

See [[Agent-Debate]].

---

## ADR-003 — The ledger is derived, not stored

**Context.** Positions, cost basis and realised profit could be kept as mutable
columns updated on each trade, or recomputed from the transaction history.

**Decision.** Recompute. `services/ledger.py` replays the transaction history
using weighted-average cost to derive positions, cost basis and realised P&L.

**Consequences.**
- State cannot drift from its history: there is exactly one source of truth.
- Any past portfolio state can be reconstructed, which is what makes the equity
  curve and the audit trail trustworthy.
- Correcting a bad transaction is a data operation on the history, not a
  reconciliation exercise.
- Cost is O(transactions) per read. Acceptable at this scale; the mitigation at
  larger scale is a materialised snapshot, not mutable current-state columns.

**Alternatives rejected.**
- *Mutable position rows.* Fast to read, but every write path must remember to
  update them, and any missed path produces a silent, permanent inconsistency.
- *Event sourcing with no current-state table.* Correct but premature here; the
  replay is fast enough that the extra machinery buys nothing.

See [[Data-Model]].

---

## ADR-004 — Fan-out subtasks rather than a sequential loop

**Context.** A monitoring sweep must run the agent for every instrument in every
autonomous portfolio.

**Decision.** Dispatch a Celery `group` of one independent subtask per
`(portfolio, ticker)` pair, rather than looping inside a single task.

**Consequences.**
- A slow LLM call for one instrument cannot delay the others.
- A failure is contained to one pair; the rest of the sweep completes.
- Retries are safe and per-item.
- Queue depth becomes a direct, observable measure of outstanding work.
- Requires per-item throttling to prevent overlapping sweeps compounding
  exposure — the failure that motivated [[Operations]]' cooldown design. See
  [[Incident-Log]] INC-001.

**Alternatives rejected.**
- *One task looping over tickers.* Simple, but head-of-line blocking means one
  slow provider response delays the entire sweep, and a single exception aborts
  the remaining instruments.
- *One task per portfolio.* Still couples instruments within a portfolio.

---

## ADR-005 — MCP as the tool boundary

**Context.** The market and news tools were Python objects passed directly into
the CrewAI agents. They were useful only inside this process.

**Decision.** Serve them over the Model Context Protocol, supporting both stdio
and streamable HTTP, and let the crew consume them over MCP when configured.

**Consequences.**
- Any MCP client — Claude Code, Cursor, a local Ollama agent — can use the same
  tools with no changes to this codebase.
- The tool server has no database access and cannot place a trade, so exposing it
  is not a privilege escalation.
- One more service to operate, and a network hop on the tool path when MCP mode
  is enabled. The in-process tools remain as the default fallback.

**Alternatives rejected.**
- *A REST wrapper.* Doesn't compose with AI tooling, which is the entire point.
- *Publish the tools as a library.* Requires consumers to run Python and import
  this codebase.

See [[MCP-Tool-Server]].

---

## ADR-006 — Throttling fails open

**Context.** Overlapping sweeps can compound exposure, so a Redis-backed cooldown
gates each `(portfolio, ticker)` run. Redis is also the Celery broker, so a Redis
outage is already a degraded state.

**Decision.** If the cooldown check itself fails, **allow the run** and log a
warning.

**Consequences.**
- Losing a cooldown costs money at the margin; refusing to run the risk engine
  costs safety. In an autonomous system, a silent trading halt is the worse
  failure — nobody notices until positions are unmanaged.
- The trade-off is only acceptable because the *authoritative* risk control (the
  execution guard) is unaffected by Redis. The cooldown is a cost optimisation,
  not a safety control. This distinction is the whole justification.

**Alternatives rejected.**
- *Fail closed.* Turns a cache outage into an unannounced trading halt.
- *No cooldown.* Rejected after INC-001.

See [[Operations]].

---

## ADR-007 — No credential fallbacks; fail loudly at startup

**Context.** Configuration could default to convenient development values so the
project "just runs".

**Decision.** `POSTGRES_PASSWORD` has no default and the application refuses to
start without it. `DJANGO_SECRET_KEY` is mandatory whenever `DJANGO_DEBUG=false`,
and is otherwise generated at random per process.

**Consequences.**
- A misconfigured deployment fails immediately and visibly, rather than running
  insecurely with a shipped default password.
- Slightly more setup friction — one mandatory edit to `.env.example`.
- A random per-process secret key in debug mode means sessions do not survive a
  restart locally. That is the intended signal that you are in development.

**Alternatives rejected.**
- *Shipped defaults like `changeme`.* These reach production. This is the single
  most common way a portfolio project leaks its database.
- *Warn but continue.* Warnings are ignored; a failed start is not.

---

## ADR-008 — Providers are abstracted; the pipeline is not provider-dependent

**Context.** The AI layer should work with DeepSeek, OpenAI-compatible gateways
and Anthropic, and should remain fully exercisable without any credential at all.

**Decision.** A single configuration block (`AI_CONFIG`) selects provider, model,
base URL and headers. A header-injection hook applies provider-specific
requirements to the real outbound HTTP request. When no credential is present,
`HeuristicDecisionEngine` produces the same output contract deterministically.

**Consequences.**
- The same debate code runs against three vendors; switching is configuration.
- The whole pipeline — guard, ledger, events, dashboard, audit trail — is
  testable offline with no network and no cost.
- Provider-specific quirks are contained in one hook rather than scattered
  through the agent code. This was validated the hard way; see INC-005.

**Alternatives rejected.**
- *A single hard-coded provider.* Locks the project to one vendor's pricing and
  availability.
- *Mocking the LLM in tests only.* The fallback engine is production code, so the
  offline path is exercised continuously rather than only under test.

---

## Related pages

- [[Architecture]] — how these decisions fit together
- [[Incident-Log]] — what went wrong, and which decisions it changed
- [[Execution-Guard]] · [[Agent-Debate]] · [[Data-Model]] · [[Operations]]
