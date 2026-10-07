# 0001. Enforce risk limits in deterministic code, not a prompt

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** Project maintainer
- **Related:** [ADR-0002](0002-adversarial-three-agent-debate.md) · [ADR-0006](0006-fail-loudly-fail-open.md) · [ADR-0007](0007-provider-agnostic-llm-with-heuristic-fallback.md) · [ADR-0009](0009-per-portfolio-sweep-scoping.md)

## Context

An LLM decides how much of a portfolio to deploy. Position limits, available cash
and a daily stop-loss must be enforced against every proposal. The cheapest
implementation is to state those limits in the system prompt and trust the model
to respect them — and the model is simultaneously the component most exposed to
prompt injection, the component most likely to be swapped for a different vendor,
and the component worst at arithmetic against a running balance.

The check is also not a single comparison. It has to consider the portfolio as a
whole: the per-trade budget derived from cash, the live price, the existing
position, and the realised profit and loss for the current UTC day. That is
enough interacting state that "it looked right in the prompt" is not a defence.

Rejected alternatives, recorded in the wiki:

- *Limits in the prompt.* Unverifiable, and silently ignored when the model is
  confused. Untestable by construction.
- *A second LLM as a risk reviewer.* Moves the problem rather than solving it —
  the reviewer is as fallible as the decider, and now also unaccountable.
- *Database constraints only.* Correct as a last line of defence, but they express
  themselves as exceptions rather than reasoned rejections, and they cannot
  consider the portfolio as a whole.

*Date note: 2026-09-30 is the initial-release commit (`f6f3397`) that shipped this
design; the decision itself predates it and no earlier dated record exists.*

## Decision

We enforce every risk limit in `evaluate_proposal()` in `services/execution.py` —
a pure Python function with no I/O, no clock beyond what is passed in, and no
model. The AI returns a *proposal*; only the guard can turn it into a
`Transaction`, and `apply_trade()` is the only function that writes money.

The controls are:

- a per-trade allocation ceiling (`max_trade_allocation_pct` of cash), with
  oversized BUYs **clamped down** to the ceiling rather than rejected, and the
  adjustment written to `GuardDecision.notes`; rejected outright only when
  nothing fits;
- a SELL clamp to the held position;
- available-cash and open-position checks;
- a daily loss limit: once realised losses for the UTC day breach
  `daily_loss_limit_usd`, new risk (BUY) is frozen while de-risking (SELL)
  remains permitted;
- amounts quantized to eight decimals with `ROUND_DOWN` and notional to cents, so
  rounding can never push a position up past a limit.

## Consequences

### Positive

- Risk behaviour is exhaustively testable. The guard has no I/O, so every branch
  is a fast, deterministic unit test rather than an integration test with mocks.
- A prompt injection, a model upgrade or a provider outage cannot widen a limit.
- Refusals are typed and persisted, so the audit trail records *why* a trade was
  refused, not merely that it was.
- It is a single choke point: no code path reaches the ledger without passing it,
  and the same implementation serves the REST API, the Telegram bot and Celery.

### Negative / trade-offs

- Adding a limit means writing code, a migration and a test rather than editing
  prose. That friction is deliberate, but it does slow tuning.
- The guard is not model-aware and has no view of market context, so a limit that
  is wrong for the moment is enforced anyway.
- Clamping means the system can execute an order materially different in size from
  the one the model argued for. The adjustment is recorded, but the reasoning
  attached to the decision no longer exactly describes the executed trade.
- The ceiling is **per trade**, not cumulative. A deliberate sequence of
  individually-permitted trades is unbounded; the temporal control added after
  INC-001 mitigates overlapping runs but not repeated ones (see
  [ADR-0009](0009-per-portfolio-sweep-scoping.md)).

### Neutral

- Database `CHECK` constraints on the same limits remain, but as a last line of
  defence only — they cannot reason about the portfolio as a whole.
- Human approval of a recommendation is not a bypass: approving re-runs
  `evaluate_proposal()` against a freshly fetched price, so a stale approval can
  legitimately fail.
