# AlphaAgent — Technical Documentation

> An autonomous multi-agent investment platform where **AI agents propose
> trades and deterministic code decides whether they may execute.**

This wiki is the engineering documentation. The [README](https://github.com/SergeyGer/AlphaAgent#readme)
is the overview aimed at a general reader; everything below assumes you want the
implementation detail and the reasoning behind it.

---

## If you are evaluating this project

Three pages will tell you the most in the least time:

| Page | What it demonstrates |
| --- | --- |
| [[Engineering-Decisions]] | Eight decision records with the alternatives rejected and the consequences accepted. Start here if you want to judge engineering judgement rather than code volume. |
| [[Incident-Log]] | Real defects found in this system, their root causes, and the fixes. Includes one where the design itself was wrong, not just the code. |
| [[Execution-Guard]] | The risk model — the part of the system that is deliberately *not* AI, and why. |

## If you are running it

| Page | Contents |
| --- | --- |
| [[Configuration]] | Every environment variable, its default and whether it is required |
| [[Operations]] | Runbook: services, scheduled jobs, throttling, failure modes |
| [[Quality-and-Testing]] | Test strategy, the CI pipeline, and how to run everything locally |

## If you are extending it

| Page | Contents |
| --- | --- |
| [[Architecture]] | Service topology, the decision pipeline, layer responsibilities |
| [[Data-Model]] | Schema, the derived ledger, concurrency control |
| [[API-Reference]] | Every endpoint with request and response shapes |
| [[Agent-Debate]] | How the three agents are prompted, and why adversarial beats sequential |
| [[Real-Time-Layer]] | WebSocket authentication, event model, reconnection behaviour |
| [[MCP-Tool-Server]] | The read-only tool server and how external AI clients consume it |
| [[Telegram-Bot]] | Bot wiring, commands, and the callback security model |

---

## The system in one paragraph

Celery Beat wakes every fifteen minutes and asks which portfolios are autonomous.
For each one it dispatches an independent subtask per instrument. That subtask
gathers market context over read-only tools and runs a CrewAI **debate**: a bull
analyst argues the case to buy, a short seller argues the case to sell from
different evidence, and a chief investment officer adjudicates. The verdict is a
structured proposal — and that is all the AI is allowed to produce. A pure,
unit-tested **execution guard** then checks the proposal against allocation,
cash, concentration and daily-loss limits. Only if it passes does a transaction
touch the ledger. Every run, approved or not, is written to an audit trail with
both arguments, the token cost and the reasoning, then streamed to a live
dashboard and to Telegram for optional human approval.

```
Celery Beat ─► fan-out ─► [ gather context ─► debate ─► GUARD ─► ledger ]
                                                            │
                                              audit trail ◄─┴─► live dashboard
```

## Design principles

**The model proposes; code decides.**
No language model can widen a risk limit. Limits live in a pure function with no
I/O and no model dependency, which makes every branch exhaustively testable and
immune to prompt injection, model upgrades or provider outages.

**Nothing reaches a decision unchallenged.**
A single analyst feeding a single decision-maker is a hallucination amplifier.
Two agents working from different evidence, with explicit instructions to concede
when their case is weak, produce disagreement that is real rather than staged.

**Explainability is a requirement, not a feature.**
Every decision is reconstructible after the fact — including the decisions *not*
to act, which are usually the more interesting ones.

**Degrade, never disappear.**
Missing market data becomes `HOLD`. A cache outage is tolerated. With no LLM
credential at all, a deterministic engine produces the same output contract so
the entire pipeline remains exercisable offline.

---

## Repository map

| Path | Responsibility |
| --- | --- |
| `config/` | Django settings, ASGI application, Celery app, URL routing |
| `core/` | Models, REST views, WebSocket consumer, authentication, management commands, tests |
| `services/` | Market data, news, sentiment, ledger, metrics, execution guard, recommendations, snapshots, events, throttling, provider hooks |
| `mcp_server/` | Model Context Protocol tool server (stdio + streamable HTTP) |
| `ai_agent.py` | The three-agent debate, output contract, deterministic fallback |
| `tasks.py` | Celery tasks, fan-out orchestration, scheduling entry points |
| `telegram_bot.py` | Bot API client and inline-keyboard handlers |
| `frontend/` | React + TypeScript single-page dashboard |
| `docs/` | Screenshots, demo recording, social preview |

---

*MIT licensed. Built and maintained by [SergeyGer](https://github.com/SergeyGer).*
