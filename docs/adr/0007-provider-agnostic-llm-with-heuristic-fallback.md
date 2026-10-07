# 0007. Abstract the LLM provider and keep a deterministic heuristic fallback

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** Project maintainer
- **Related:** [ADR-0001](0001-deterministic-execution-guard.md) · [ADR-0002](0002-adversarial-three-agent-debate.md)

## Context

The AI layer has to work against DeepSeek, OpenAI-compatible gateways and
Anthropic, and it has to remain fully exercisable with no credential at all — in
CI, with no network and no spend. Vendors differ in request headers, model naming
and pricing, and a hard-coded provider would lock the project to one vendor's
pricing and availability.

The fallback question is separate and sharper: when the provider fails, or no key
is configured, should the pipeline stop or decide by other means? An autonomous
sweep that fails loudly leaves positions unmanaged until someone notices.

Rejected alternatives, recorded in the wiki:

- *A single hard-coded provider.* Locks the project to one vendor's pricing and
  availability.
- *Mocking the LLM in tests only.* The fallback engine is production code, so the
  offline path should be exercised continuously rather than only under test.

*Date note: 2026-09-30 is the initial-release commit (`f6f3397`) that shipped this
design; the decision itself predates it and no earlier dated record exists.*

## Decision

A single configuration block (`AI_CONFIG`) selects provider, model, base URL,
temperature, token ceiling and headers. Provider-specific requirements are applied
to the real outbound HTTP request through a header-injection hook rather than
being threaded through the agent code.

When no credential is configured, when the provider raises, or when the crew
returns an unparseable proposal, `HeuristicDecisionEngine` produces the same output
contract deterministically — rules over sentiment, price momentum and risk profile
— labelled `source="heuristic_fallback"` and recorded as a mechanical read rather
than a reasoned argument. `ALLOW_HEURISTIC_FALLBACK=false` turns the same
situations into failures instead.

## Consequences

### Positive

- The same debate code runs against three vendors; switching provider is
  configuration, not a code change.
- The whole pipeline — guard, ledger, events, dashboard, audit trail — is
  testable offline with no network and no cost, and the offline path is exercised
  continuously rather than only in tests.
- Provider quirks are contained in one hook. This was validated the hard way in
  INC-005, where a provider-specific requirement broke the agent path.
- The fallback still passes through the execution guard, so the flag changes *who
  decides*, never the limits.

### Negative / trade-offs

- Silent degradation is the default. With the flag on, a provider outage produces
  rule-based decisions that land in the same dashboard and audit trail as reasoned
  ones; they are labelled, but only a reader who checks the label will notice.
- The heuristic engine is a second decision engine to maintain, test and reason
  about, and it is not equivalent — it reads sentiment, momentum and risk with no
  argument and no bear case.
- The abstraction is leaky. Each vendor still needs quirks in the hook and a
  pricing entry, or cost accounting silently guesses.
- Behaviour differs across providers, so "the same debate" is not the same output
  for the same instrument.

### Neutral

- The fallback is a documented degraded mode with its own tests, not an error path.
