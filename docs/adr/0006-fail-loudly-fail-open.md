# 0006. Fail loudly on missing configuration, fail open on optional infrastructure

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** Project maintainer
- **Related:** [ADR-0001](0001-deterministic-execution-guard.md) · [ADR-0004](0004-celery-fan-out-per-portfolio-ticker.md) · [ADR-0009](0009-per-portfolio-sweep-scoping.md)

## Context

The system has two opposite failure questions, and answering both with the same
instinct is what makes this a decision rather than a default.

*Missing configuration.* Configuration could default to convenient development
values so the project "just runs". But shipped defaults like `changeme` reach
production, and that is one of the most common ways a portfolio project leaks its
database. Warnings are not a control: they are ignored.

*Missing infrastructure.* A Redis-backed cooldown gates each `(portfolio, ticker)`
run to stop overlapping sweeps compounding exposure. Redis is also the Celery
broker, so a Redis outage is already a degraded state — and the cooldown check
itself goes through the same cache. If the check cannot be performed, the run must
either be allowed or refused.

Rejected alternatives, recorded in the wiki:

- *Shipped credential defaults.* They reach production.
- *Warn but continue on missing credentials.* Warnings are ignored; a failed start
  is not.
- *Fail closed on the cooldown.* Turns a cache outage into an unannounced trading
  halt.
- *No cooldown.* Rejected after INC-001, where two sweeps three seconds apart each
  bought AAPL inside the per-trade limit and doubled the intended exposure.

*Date note: 2026-09-30 is the initial-release commit (`f6f3397`) that shipped this
design; the decision itself predates it and no earlier dated record exists.*

## Decision

Safety-critical configuration has no fallback and no default.
`POSTGRES_PASSWORD` is required at import time, and `DJANGO_SECRET_KEY` is
mandatory whenever `DJANGO_DEBUG=false`; otherwise the process refuses to start.
In debug mode the secret key is generated at random per process.

Optional infrastructure fails open. If the Redis cooldown or sweep-debounce check
raises, the run is **allowed** and a warning is logged.

## Consequences

### Positive

- A misconfigured deployment fails immediately and visibly, rather than running
  insecurely with a shipped default password.
- A silent trading halt is avoided. In an autonomous system nobody notices a halt
  until positions are unmanaged, which is the worse failure.
- The asymmetry is only acceptable because the *authoritative* risk control — the
  execution guard ([ADR-0001](0001-deterministic-execution-guard.md)) — does not
  depend on Redis. The cooldown is a cost optimisation, not a safety control, and
  that distinction is the whole justification.
- Both behaviours are tested, including fail-open when the cache is unavailable.

### Negative / trade-offs

- More setup friction: one mandatory edit to `.env.example` before anything runs.
- A random per-process secret in debug mode means local sessions do not survive a
  restart. That is the intended signal that you are in development, but it is
  inconvenient.
- During a Redis outage, duplicate sweeps are silently re-enabled and spend LLM
  budget. Fail-open converts an availability problem into a cost problem, by
  design.
- Anyone who mistakes the throttle for a safety control will over-trust it; the
  system's actual safety property is the guard, and the docs have to keep saying so.

### Neutral

- Fail-open is logged as a warning, not alerted on. Detecting it means watching
  the throttle logger.
