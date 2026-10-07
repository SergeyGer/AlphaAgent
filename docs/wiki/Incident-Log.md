# Incident Log

Real defects found in this system, their root causes, and what changed as a
result. These are not hypotheticals or textbook examples — each one reached a
running system, and several were only caught because the CI pipeline runs against
a clean checkout rather than a developer's working directory.

The purpose of this page is not to catalogue mistakes. It is to show how they
were diagnosed, and to make the case that the most valuable defects are the ones
where **the design was wrong, not the code**.

| ID | Incident | Class | Design change? |
| --- | --- | --- | --- |
| [INC-001](#inc-001--overlapping-sweeps-bought-the-same-stock-twice) | Overlapping sweeps bought the same stock twice | Risk model | **Yes** |
| [INC-002](#inc-002--the-web-tier-silently-served-nothing) | The web tier silently served nothing | Deployment | Yes |
| [INC-003](#inc-003--websocket-dropped-every-90-seconds) | WebSocket dropped every ~90 seconds | Infrastructure | No |
| [INC-004](#inc-004--two-workers-on-one-queue) | Two Celery fleets on one queue | Operations | No |
| [INC-005](#inc-005--a-future-import-broke-the-ai-guardrail) | A `__future__` import broke the AI guardrail | Dependency contract | No |
| [INC-006](#inc-006--a-tool-that-called-itself) | A tool that called itself | Code | No |
| [INC-007](#inc-007--required-env_file-broke-every-fresh-clone) | A required `env_file` broke every fresh clone | Packaging | No |
| [INC-008](#inc-008--sentiment-scored-backwards) | Sentiment scored backwards | Algorithm | No |

---

## INC-001 — Overlapping sweeps bought the same stock twice

**Symptom.** Two `AgentDecisionLog` rows for the same instrument three seconds
apart, both `Purchased AAPL`, each individually within every configured limit.
The portfolio's real exposure was roughly double what any single decision
suggested.

**Impact.** Realised over-concentration in a live portfolio. The risk limits were
respected in full by both trades — which is exactly why this was serious.

**Root cause — a design flaw, not a bug.**
`max_trade_allocation_pct` is a **per-trade** ceiling. Nothing bounded a
*sequence* of trades. Two independent sweeps landed inside the same window:

- Celery Beat fired the scheduled sweep at `14:00:00`.
- A manual trigger fired at `14:00:03`.
- Neither knew about the other. Both read the same cash balance, both sized their
  order against the same ceiling, both passed the guard.

The guard was working correctly. **The risk model was incomplete**, because it
assumed decisions arrive one at a time.

**Fix.** Two Redis-backed guards in `services/throttle.py`, both using atomic
`SET NX` so two workers cannot both win the race:

| Guard | Default | Purpose |
| --- | --- | --- |
| Ticker cooldown | 900 s | One `(portfolio, ticker)` pair runs at most once per window |
| Sweep debounce | 60 s | Prevents duplicate fan-outs, so Beat and a manual trigger cannot both dispatch |

Both **fail open**: if Redis is unavailable the run proceeds with a warning. See
[[Engineering-Decisions|ADR-006]] for why that asymmetry is deliberate.

**Lesson.** A per-item limit is not a rate limit. Any ceiling expressed as "at
most X per trade" needs a companion that bounds the *rate* of trades, or the
aggregate is unbounded. Notably, the correct fix was not to reject more trades —
it was to make the system aware that it had already acted.

A cumulative daily deployment ceiling remains on the [[Roadmap]]; the cooldown
bounds overlapping runs, not deliberate repeated ones.

---

## INC-002 — The web tier silently served nothing

**Symptom.** The container reported healthy. Nothing was reachable from the host.
No error in the logs.

**Root cause.** The container command was written as a YAML folded scalar:

```yaml
# Broken
command: >
  python manage.py migrate --noinput &&
  exec gunicorn config.wsgi:application --bind 0.0.0.0:8000
```

A folded scalar (`>`) joins lines but **preserves the newlines of more-indented
continuation lines**. The command was silently truncated to
`gunicorn config.wsgi:application`. Gunicorn then applied its own defaults —
`127.0.0.1:8000`, a single worker — which is unreachable from outside the
container and would not have served WebSockets even if it had been reachable.

**Fix.** YAML lists, with a comment explaining why, so nobody "tidies" it back:

```yaml
command:
  - sh
  - -c
  - >-
    python manage.py migrate --noinput &&
    exec daphne -b 0.0.0.0 -p 8000 config.asgi:application
```

The same change moved the server from gunicorn to **daphne**, because WSGI cannot
serve the WebSocket feed at all. See [[Real-Time-Layer]].

**Lesson.** Two failures were stacked here, and only one was visible. The
truncation was silent because gunicorn has working defaults — a service that
falls back to a plausible configuration is harder to detect than one that
crashes. The container healthcheck was also checking the wrong thing.

---

## INC-003 — WebSocket dropped every 90 seconds

**Symptom.** The dashboard indicator flapped between `LIVE` and `RECONNECTING`
roughly every ninety seconds. Measured at **26 occurrences in 40 minutes**.

**Root cause — two defaults that happen to be the same number.**

The channel layer's consumer blocks on a Redis pop for **five** seconds
(`BZPOPMIN`, `brpop_timeout=5`). redis-py's `DEFAULT_SOCKET_TIMEOUT` is *also*
**five** seconds, and it applies implicitly when no socket timeout is passed:

```python
redis.connection.DEFAULT_SOCKET_TIMEOUT = 5     # redis-py 8.1.0
channels_redis.core brpop_timeout       = 5     # identical
```

The read deadline therefore raced the blocking window itself. Any scheduling
delay, garbage collection pause or slow round trip tipped it over:

```
redis.exceptions.TimeoutError: Timeout reading from redis:6379
```

That exception escapes `AsyncConsumer` — which catches only `StopConsumer` —
so daphne closed the socket with code `1011`. The client reconnected and
resynced correctly, which is exactly why the symptom looked like flakiness
rather than a defect.

**A second default made it worse.** redis-py's implicit `Retry(retries=10)`
turned each timeout into roughly a **60-second** stall before it surfaced.

**Measured, not assumed.** Reproduced against this checkout:

| Configuration | Result |
| --- | --- |
| `socket_timeout=5`, 5 s block | **4/4 `TimeoutError`**, ~58–60 s each |
| `socket_timeout=5`, 1 s block | clean at 1.04 s |
| `socket_timeout=5`, `Retry(NoBackoff(), 0)` | `TimeoutError` at 5.01 s — proving the ~60 s stall is the retry policy, not Redis |
| `socket_timeout=30` (shipped) | **4/4 clean**, ~5.05 s |

**Fix.** Explicit connection settings on the channel layer, keeping the socket
deadline comfortably above the blocking window:

```python
"hosts": [{
    "address": f"{REDIS_URL}/3",
    "socket_timeout": 30,          # must exceed brpop_timeout (5s)
    "socket_connect_timeout": 10,
    "socket_keepalive": True,
    "health_check_interval": 30,
}]
```

**Verified.** An authenticated socket to `ws://…/ws/portfolio/` handshook in
0.12 s and stayed open for **150 s** with no `1011` close — past the old failure
window. The earlier 120-second check produced zero timeouts against a baseline
of roughly one per ninety seconds.

**Lesson.** Two independent defaults agreeing on the value `5` produced a
race that neither library documents as a constraint. The reconnect logic then
masked it: robust client behaviour is not a substitute for a stable server, and
"it recovers" is not the same as "it works". The general form of this bug is
**a timeout equal to the operation it wraps**, which is worth checking for
whenever two libraries meet at a boundary.

---

## INC-004 — Two workers on one queue

**Symptom.** Intermittent validation errors on otherwise valid proposals, only
for autonomous sweeps.

**Root cause.** A worker was running on the host *and* in a container, both
consuming the same Redis queue. The host worker had been started before a
breaking change to the proposal contract and was still executing the old code.
Which worker picked up a task was effectively random.

**Fix.** One fleet only. The host worker and beat were stopped; the containerised
fleet is the only consumer.

**Lesson.** A shared queue is a shared contract. Two consumers at different code
versions produce non-deterministic, hard-to-reproduce failures that look like
logic bugs. The symptom pointed at the AI layer; the cause was in deployment.
"Have you checked what else is consuming this queue?" is now the first question.

---

## INC-005 — A `__future__` import broke the AI guardrail

**Symptom.**

```
Value error, If return type is annotated, it must be Tuple[bool, Any]
```

**Root cause.** Adding `from __future__ import annotations` to `ai_agent.py` — a
harmless-looking modernisation — activates PEP 563, which turns every annotation
into a **string**. CrewAI's task validator inspects the guardrail callable's
return annotation *at runtime*:

```python
def _proposal_guardrail(output) -> tuple[bool, Any]:   # becomes "tuple[bool, Any]"
```

The validator received the string `"tuple[bool, Any]"` instead of the type, and
rejected it.

**Fix.** Remove the import from that module only, with a comment explaining why,
plus a regression test that asserts the annotation resolves to a real type
(`test_ai_agent.py::test_guardrail_return_annotation_satisfies_crewai_contract`).
The test was confirmed to fail without the fix.

**Lesson.** PEP 563 is not a no-op when a library introspects annotations at
runtime. Framework interop can depend on annotation *objects*, not just their
meaning — and a linting-motivated import can violate a contract no type checker
will catch.

---

## INC-006 — A tool that called itself

**Symptom.** `maximum recursion depth exceeded` when the bear-case agent
requested financial data.

**Root cause.** `mcp_server/server.py` exposes a tool named
`get_financial_health`, and imported the implementation under the same name:

```python
from services.fundamentals import get_financial_health  # shadows itself

...


@mcp.tool()
def get_financial_health(ticker: str) -> str:
    return get_financial_health(ticker)  # calls the decorated wrapper
```

The module-level name referred to the newly defined wrapper, so the tool called
itself.

**Fix.** Explicit aliased imports, with a comment:

```python
from services.fundamentals import get_financial_health as _financial_health
from services.fundamentals import get_price_history as _price_history
```

**Lesson.** A name collision between an exposed tool and its implementation is
invisible at import time and only manifests when that specific tool is called —
so it survived unit tests that exercised the other tools. Naming discipline at a
protocol boundary is not cosmetic.

---

## INC-007 — Required `env_file` broke every fresh clone

**Symptom.** The CI Docker job failed at `Verify compose configuration`:

```
env file /path/.env not found
```

**Root cause.** The Compose service declared `env_file: .env`. Compose validates
that the file exists, and `.env` is gitignored — so `docker compose config` aborts
on **any fresh checkout**, which is precisely the state CI runs in and the state
every new contributor is in before running `cp .env.example .env`.

**Fix.** `required: false`, so compose validates with or without a local `.env`,
while the `:?` guard on `POSTGRES_PASSWORD` still fails loudly when it is
genuinely unset. All three states verified: absent, present, and password unset.

**Lesson.** This was found by CI, not by a developer, because a developer's
working directory always has a `.env`. **Testing the onboarding path requires a
clean checkout** — a class of bug that is structurally invisible on a configured
machine. The same job also revealed that the image smoke test was toothless: it
ran `manage.py --help`, which exits `0` *without loading settings*, so it could
never detect a broken configuration. It now runs `manage.py check` with the
required secrets.

---

## INC-008 — Sentiment scored backwards

**Symptom.** Headlines that were unambiguously negative produced positive
sentiment scores.

**Root cause.** The finance lexicon matched on individual tokens with no
negation handling. `"Apple did not beat earnings expectations"` matched `beat` as
a positive term. Intensifiers had the same problem in reverse: `"fell sharply"`
scored the same as `"fell"`.

**Fix.** A three-token negation window that looks **backward** from a matched
term, and intensifier detection that also looks **forward** for the word it
modifies.

**Lesson.** Lexicon sentiment is a solved-looking problem that is not solved.
The failure was silent and directional — it did not crash, it produced confident
wrong answers that fed a trading decision. It surfaced only by reading the
generated scores against the actual headlines rather than trusting the aggregate.

---

## What these incidents have in common

Four of the eight were **silent**: the system reported success, or recovered
gracefully, while behaving incorrectly. INC-001 executed two trades, INC-002
served nothing, INC-003 reconnected, INC-008 produced confident wrong numbers.
None raised an error a human would have seen.

Two design consequences follow, and both are now load-bearing parts of the
system:

1. **Record everything, including what you declined to do.** INC-001 was found by
   reading audit rows, not by an alert. The `AgentDecisionLog` is written on every
   path, including HOLDs and rejections.
2. **Test the clean-checkout path.** INC-007 was invisible on every developer
   machine by construction. CI runs against a fresh clone with no `.env`, and
   that is what caught it.

## Subsequent review — eight further defects

Documenting this system turned out to be an audit: re-deriving each claim from source
found **eight more defects** — an on-demand sweep that spent every other user's LLM
budget, a debounce key shared across portfolios, a `500` on a malformed `limit`
parameter, a Telegram link code that never expired, a token purge that ignored last
use, an executed trade never pushed to Telegram, a webhook that accepted unsigned
POSTs when unconfigured, and a live snapshot frame whose shape did not match what the
dashboard read. All eight are fixed and pinned by 35 tests in
`core/tests/test_regressions.py`; [[API-Reference]] and [[Operations]] document the
corrected behaviour.

## Related pages

- [[Engineering-Decisions]] — the decisions these incidents shaped, particularly ADR-006
- [[Quality-and-Testing]] — the regression tests that now guard each fix
- [[Operations]] — runbook for the failure modes described here
- [[Roadmap]] — the cumulative exposure limit INC-001 argues for
