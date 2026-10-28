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
| [INC-005](#inc-005--a-__future__-import-broke-the-ai-guardrail) | A `__future__` import broke the AI guardrail | Dependency contract | No |
| [INC-006](#inc-006--a-tool-that-called-itself) | A tool that called itself | Code | No |
| [INC-007](#inc-007--required-env_file-broke-every-fresh-clone) | A required `env_file` broke every fresh clone | Packaging | No |
| [INC-008](#inc-008--sentiment-scored-backwards) | Sentiment scored backwards | Algorithm | No |
| [INC-009](#inc-009--redis-ran-with-no-authentication-on-a-published-port) | Redis ran unauthenticated on a published port | Exposure | Partly |
| [INC-010](#inc-010--both-databases-were-reachable-from-the-public-internet) | Both databases were reachable from the public internet | Exposure | **Yes** |
| [INC-011](#inc-011--one-run-in-a-hundred-failed-on-a-token-cap) | One run in a hundred failed on a token cap | Model budget | **Yes** |

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

## INC-009 — Redis ran with no authentication, on a published port

**Severity:** high (unauthenticated infrastructure) · **Detected by:** a security
review of the compose configuration · **Fixed in:** the Redis authentication
commit

### What happened

`docker-compose.yml` started Redis with no `--requirepass` and published
`6379:6379` to the host. Everything sharing that instance — the Celery broker, the
task result backend, the Django cache and the WebSocket channel layer — was
therefore reachable, readable and writable by anything that could route to the
port. Redis is a well-known target for exactly this reason.

### Root cause

The stack was built for a local demo and the default was never revisited. Redis
ships with authentication disabled, so "no password" is not a misconfiguration
anyone has to make — it is what you get by not deciding. The published port was
added so host-run tooling (`make test`, the seed command) could reach it, which is
a real requirement; not securing it was not.

### Fix

- `redis-server --requirepass` in both compose files, and in the demo file too —
  a demo password is still a password.
- `config/settings.py` builds all four connection URLs from `REDIS_HOST`,
  `REDIS_PORT` and `REDIS_PASSWORD`, percent-encoding the password.
- The healthcheck authenticates via `REDISCLI_AUTH`, not `redis-cli -a`, which
  would expose the secret in the process list.
- The port stays published, because the documented host workflow needs it. With a
  password it is a protected port rather than an open one.

  > **This conclusion was wrong, and [INC-010](#inc-010--both-databases-were-reachable-from-the-public-internet)
  > supersedes it.** "Protected by a password" and "not exposed" are different
  > properties, and only the second bounds what an attacker can attempt. The port
  > was reachable from the public internet, not merely from the host, so the
  > password was doing work that binding to loopback should have done. Left in
  > place because the reasoning is the useful part: it is a plausible argument that
  > a security review accepted, and it took a second look at the host's listening
  > sockets to overturn it.

### What the fix exposed

Two failures that configuration review alone would not have caught:

1. **A stale URL survived the password change.** `.env` still held
   `CELERY_BROKER_URL=redis://127.0.0.1:6379/0`, which overrode the builder. The
   cache and channel layer picked up credentials while the broker kept the open
   URL — so the change looked complete and was not. This is why the wiki says to
   leave those two variables empty.
2. **The channel layer authenticated late.** After the settings change the
   dashboard showed RECONNECTING: the running container was still the image built
   before the change, so `address` was still `redis://redis:6379/3`. A rebuild
   fixed it. Worth remembering that settings live in the image, and a compose
   restart alone does not pick up a code change.

### Verification

Anonymous access is refused (`NOAUTH Authentication required.`), authenticated
access answers (`PONG`), the dashboard reports **LIVE** with all seven metric
cards populated, a Celery task round-trips through the authenticated broker, and
428 backend plus 82 frontend tests pass.

### Lesson

An unauthenticated dependency is invisible until someone looks for it, because
nothing fails. `RedisAuthTests` now asserts the URL construction and the compose
configuration, since CI cannot exercise the authenticated path — GitHub service
containers accept docker flags but not a command, so `--requirepass` cannot be
passed there.

## Related pages

- [[Engineering-Decisions]] — the decisions these incidents shaped, particularly ADR-006
- [[Quality-and-Testing]] — the regression tests that now guard each fix
- [[Operations]] — runbook for the failure modes described here
- [[Roadmap]] — the cumulative exposure limit INC-001 argues for

---

## INC-010 — Both databases were reachable from the public internet

**Severity:** high (internet-exposed stateful services) · **Detected by:** checking
which addresses the host was listening on, while investigating something unrelated
· **Fixed in:** the loopback-binding commit

### What happened

```
188.245.2.148:5432   PostgreSQL — answered an SSLRequest from the public internet
188.245.2.148:6379   Redis      — accepted a connection
```

Neither was behind a firewall — `ufw` was inactive. PostgreSQL proceeded from the
`SSLRequest` to authentication, so a scanner reaching that port was talking to a
real database login prompt. Both had been reachable this way for **two weeks**.

Both are password-protected, so this was brute-force exposure rather than an open
door. That distinction is real but narrow: an internet-facing database is scanned
continuously, nothing rate-limits the attempts, and the password in `.env` was the
entire distance between a scanner and the data.

### Root cause — a default that is safe on a laptop and dangerous on a server

```yaml
ports:
  - "5432:5432"     # Docker reads this as 0.0.0.0:5432
```

A bare `"host:container"` mapping binds **every interface**. Docker's short syntax
cannot express the difference, so the file did not say which the author meant — and
the short form is what everyone writes, because it is tidier and works perfectly
until the machine has a routable address.

This is the same compose file, written on a laptop, deployed to a server with a
public IP. Nothing forced that change of context to be noticed.

**It is also a correction to INC-009.** That incident secured Redis with a password
and then concluded, in writing, that "the port stays published… with a password it
is a protected port rather than an open one." That reasoning was accepted at the
time and is wrong: authenticated and unexposed are different properties, and only
the second bounds what can be attempted. The password was doing work that binding
to loopback should have been doing.

### Fix

```yaml
ports:
  - "127.0.0.1:5432:5432"
  - "127.0.0.1:6379:6379"
```

Nothing legitimate was lost: `psql` on the host, a GUI client over an SSH tunnel,
and the devcontainer all still reach the databases. Only the public route is gone.
Verified after the change — both ports refuse connections from the public address,
the dashboard still answers `200`, and the database still reads.

### The design change — the part that matters more than the fix

The one-line correction is the easy half. **The exposure returned because nothing
in the project could tell the two mappings apart**, and a reviewer reading the file
saw nothing worth flagging.

`core/tests/test_service_exposure.py` now **resolves** the compose configuration
with `docker compose config --format json` and asserts that every published
data-service port binds loopback. It reads Compose's own interpretation rather than
parsing the YAML, because that is the only way to test what Docker will do instead
of what the file appears to say.

**The guard is proven to fail.** Reverting the binding to `"5432:5432"` produces:

```
db: service 'db' publishes 5432 on all interfaces. That is the public internet on
a machine with a routable IP. Bind it to 127.0.0.1 - the host, an SSH tunnel and
the devcontainer all still work.
```

A third test pins the interpretation the other two depend on — that Compose reports
an implicit `0.0.0.0` for a bare mapping — so a future Compose release cannot make
the assertions pass while the exposure quietly returns.

### Lesson

The lesson is not "bind to loopback". It is that **the development environment and
the deployed environment had different threat models and shared one configuration
file**, and the safe-on-a-laptop default travelled with it.

Every setting whose safety depends on network topology — published ports,
`ALLOWED_HOSTS`, CORS, TLS verification — is now treated as needing an explicit
decision rather than an inherited default. The explicit form is longer and looks
noisier, which is the point: it is a place where somebody had to decide.

---

## INC-011 — One run in a hundred failed on a token cap

**Symptom.** Decisions occasionally appeared in the audit trail as
`ERROR - agent pipeline failed` instead of a trade, a HOLD, or a heuristic
fallback. Sixteen of them, scattered across two weeks, with no pattern a reader
could see from the feed.

**Impact.** Roughly **1% of agent runs produced no decision at all** (16 of 1,532).
The guard was never bypassed — a failed run simply does nothing — but a system that
silently declines to decide one time in a hundred is harder to trust than one that
refuses loudly.

**Root cause.** The worker log named it on the line before every failure:

```
Anthropic API usage: {'input_tokens': 7586, 'output_tokens': 2048, ...}
ERROR crewai: Invalid response from LLM call - None or empty.
```

`2048` was `max_tokens`, exactly. The 5.x Anthropic models think adaptively by
default, and that thinking is billed from the **same** allowance as the answer. A
run could therefore reason until the budget was gone and return no text. CrewAI
surfaces that as a `ValueError`, which reached the Celery task and was logged as a
crashed pipeline.

Across the log history: **26 runs consumed the cap exactly, and 24 of them failed.**

**Fix.** The cap defaults to **8192** and the example `.env` says why. Cost rises
about 1.4× — $0.0145 to $0.019–0.025 per run — because the model is no longer cut
off mid-thought. One percent of runs failing was the wrong way to save three cents.

**What was tried first and does not work.** Disabling thinking would have been
cheaper and more precise. CrewAI accepts the parameter and serialises it as
`{"type": "disabled", "budget_tokens": None}`; the API rejects that with
`thinking.disabled.budget_tokens: Extra inputs are not permitted`, because the two
fields are mutually exclusive and CrewAI cannot express the valid form. Recorded in
the code so the attempt is not repeated.

**Design change.** A run whose output consumes the entire budget now logs an
explicit truncation error naming the cap. Before this the failure was invisible in
two different ways: it either crashed inside CrewAI, or it produced a proposal from
a partial reply that looked like a short answer. The number matters less than the
fact that the condition is now stated.

**Lesson.** The interesting part is not that a constant was too small. It is that
**the failure had a signature — `output_tokens == max_tokens` — and nothing was
looking for it.** Twenty-six runs hit that signature before anyone connected it to
the errors. A metric that equals its own ceiling is not a coincidence; it is a
truncation, and systems should say so at the moment it happens.
