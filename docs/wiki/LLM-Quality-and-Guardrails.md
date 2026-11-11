# LLM Quality and Guardrails

The agent in this project is an LLM, and LLMs are unreliable in a specific and
dangerous way: they produce confident, well-formatted, plausible output that is
sometimes wrong. In a trading system "sometimes wrong" costs money, so the design
does not ask *how do we make the model reliable* — it asks **how do we build a
system that is safe when the model is not**.

Two separate questions are answered here, and they are often conflated:

- **How is output quality measured?** → [§9](#9-measuring-quality-the-outcome-ledger).
  Not by inspection, and not by asking the model. By scoring every decision against
  what the market did next.
- **How are unreliable results prevented?** → [§1](#1-the-principle-the-llm-proposes-code-decides)
  through [§8](#8-layer-7--prior-belief-so-a-reversal-must-be-argued). Seven layers, most of which do
  not involve the model at all.

---

## 1. The principle: the LLM proposes, code decides

Every tool and agent in this system is read-only. Nothing an LLM produces can
mutate state. It produces **one** structure — a
[`TradeProposal`](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L97) — and a deterministic function
([`evaluate_proposal`](https://github.com/SergeyGer/AlphaAgent/blob/main/services/execution.py#L155)) decides whether
that becomes a trade.

```python
class TradeProposal(BaseModel):
    """The **only** structure the AI layer is allowed to emit.

    Tools and agents never mutate state; they produce this payload and the
    Celery execution guard in ``tasks.py`` decides whether it becomes a trade.
    """
```

That inversion is the whole architecture. A compromised model prompt, a
hallucinated quantity, or a hundred million dollars of "buy" all arrive at the same
place and meet the same arithmetic.

---

## 2. Layer 1 — Schema validation at the type level

`TradeProposal` is a Pydantic model, and the constraints are declared rather than
checked afterwards, so an invalid proposal cannot be constructed at all.

| Field | Constraint | What it stops |
| --- | --- | --- |
| `action` | `Literal["BUY", "SELL", "HOLD"]` ([L106](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L106)) | Invented verbs — `"ACQUIRE"`, `"MAYBE"`, `"buy"` |
| `sentiment` | `Literal["BULLISH", "BEARISH", "NEUTRAL"]` ([L112](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L112)) | Free-text mood labels that nothing downstream can compare |
| `amount` | `ge=0.0` ([L107](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L107)) | Negative quantities — a "BUY −5" that would read as a sell |
| `reasoning` | `min_length=1` ([L113](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L113)) | A decision with no stated reason, which cannot be audited later |
| all | `extra="ignore"` | Additional keys a model invents are dropped, not propagated |

Three `mode="before"` validators recover from *formatting* mistakes without
loosening the *semantic* ones — a distinction worth being explicit about:

- [`_upper`](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L139) — `"buy"` becomes `"BUY"`. Casing is a
  formatting artefact, not a different decision.
- [`_coerce_amount`](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L144) — `"$1,234.50"` becomes
  `1234.50`. A currency-formatted string is the same number in a different font.
- [`_coerce_reasoning`](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L153) — collapses whitespace.

What those validators deliberately do **not** do is guess. They never repair an
action, never default an unknown sentiment, and never invent a quantity. A model
that says `"ACCUMULATE"` is rejected, not mapped to `BUY` — because the mapping
would be this system's invention, recorded as the model's decision.

---

## 3. Layer 2 — Parsing, with several attempts and one honest failure

Models do not always return clean JSON: they wrap it in prose, in a markdown fence,
or in an explanatory sentence. [`parse_proposal`](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L170)
tries successively weaker interpretations:

1. already a `TradeProposal` → return it;
2. a Pydantic model → `model_validate(raw.model_dump())`;
3. a `dict` → `model_validate(raw)`;
4. a string containing JSON → find the first `{…}` block and parse it;
5. a string containing a fenced code block → strip the fence and parse;
6. anything else → **`None`**.

Step 6 is the design decision. The function returns `None` rather than a
defaulted proposal. A default of `HOLD` would be indistinguishable from the model
genuinely deciding to hold — and the audit trail would record a *decision the model
never made*. `None` propagates to the fallback in §4 and is logged at error level
with the first 300 characters of the offending output.

---

## 4. Layer 3 — A deterministic fallback that is not an LLM

When the model fails to produce anything usable — parsing failure, provider outage,
missing key — the system does not stop and does not retry indefinitely. It switches
to [`HeuristicDecisionEngine`](https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L421), a fully offline,
deterministic decision engine.

This is a deliberate choice about what a fallback should be. A fallback that is also
an LLM has the same failure modes as the thing it is replacing, and its failures
are harder to distinguish. A deterministic engine can be reasoned about, unit-tested
exactly, and — importantly — **the switch is recorded**: a Phase 2 fallback is a
separate `DecisionRun` with `mode="heuristic"` and `source="heuristic_fallback"`,
and the task result carries `agent_source`. The legacy `AgentDecisionLog` has no
`source` column, so on that path the distinction lives in the run and the event
payload, not in the audit row itself.

---

## 5. Layer 4 — The execution guard

`evaluate_proposal` is where the model stops being special. It is pure arithmetic
over `Decimal`, it does not know or care that an LLM produced the proposal, and it
can do four things:

| Outcome | When |
| --- | --- |
| **Approve** | The request fits every limit |
| **Clamp** | It fits after sizing down; the note names which ceiling bound it |
| **Reject** | It cannot be sized to fit; the reason names the limit |
| **Halt** | A kill switch is engaged; new risk is frozen |

The limits are covered in [[Execution-Guard]]: a per-trade budget, the cash
balance, a daily loss limit, the position-size check, and the two portfolio-level
ceilings added in ADR-0015 ([`_portfolio_headroom`](https://github.com/SergeyGer/AlphaAgent/blob/main/services/execution.py#L100)).
Every message names the constraint that bound, so an audit reader is not sent to
the wrong one.

**The model's number is an input, never an instruction.** A proposal for a
$10,000 position against a $500 budget is not "mostly fine" — it becomes a $500
position or a refusal, with the model's original intent preserved in
`AgentDecisionLog.reasoning` for comparison.

---

## 6. Layer 5 — Grounding: numbers before opinions

Asking an LLM "is this news good or bad for AAPL" yields an answer that changes
between runs, cannot be reproduced, and cannot be audited. So sentiment is not
asked for. It is computed, by
[`services/sentiment.py`](https://github.com/SergeyGer/AlphaAgent/blob/main/services/sentiment.py), from a finance-tuned
lexicon with negation and intensifier handling, and the result is handed to the
model **as evidence**.

```python
"""Deterministic financial sentiment scoring.

The news-analyst agent needs a *reproducible* numeric signal. An LLM alone is
non-deterministic and may be unavailable, so headlines are scored with a
finance-tuned lexicon (with negation and intensifier handling) and the result is
handed to the LLM as grounding evidence rather than being replaced by it.
"""
```

The aggregate ([`aggregate_sentiment`](https://github.com/SergeyGer/AlphaAgent/blob/main/services/sentiment.py#L297))
carries a **confidence** defined as:

```
confidence = min(1.0, agreement × 0.7 + |normalised_score| × 0.3)
```

where `agreement` is the share of non-neutral articles whose sign matches the mean.
That is a measurable statement about *disagreement in the source material* — 10
headlines split 5/5 produce low confidence even if the mean is large. An LLM asked
for a confidence score would produce a plausible number with no such basis.

The agent still reasons about the news. It just cannot invent the number.

---

## 7. Layer 6 — Structured disagreement

A single agent asked to justify a decision will justify it. The crew therefore runs
a debate: a bull case, a bear case, and a CIO that must choose between them, with
the opposing arguments supplied in the prompt rather than produced on request.

Two details make this more than theatre:

**The bear case is instructed to be specific.** A prompt that says "argue the other
side" produces a token objection. The prompt requires the strongest quantifiable
counter-argument and explicitly permits the agent to report that no credible case
exists. The synthetic Phase 1 disagreement fixture records the shape of that
exchange — a `dispute` and a `qualify`, each with a stated position change — but it
is a fixture, not a live transcript, and no live transcript is committed.

**Absence is stated, not left blank.** When no prior thesis exists, the prompt says
so explicitly (`_thesis_block` in `ai_agent.py`) rather than omitting the section.
An omitted section is something a model will fill in confidently; an explicit "no
prior thesis recorded" is a fact it must work with.

---

## 8. Layer 7 — Prior belief, so a reversal must be argued

The crew runs stateless, so without help an agent could sell at 10:00 and rebuy at
10:15 without noticing. [[Position-Thesis]] records one live thesis per
instrument — why it is held and what would falsify it — and the next run receives
it as context.

The prompt requires the CIO to engage with the prior thesis, which converts a
reversal from a silent decision into a stated one. The thesis is also *not edited*:
superseding creates a new row and closes the old one, so the history reads as a
sequence of beliefs rather than a single mutable claim.

---

## 9. Measuring quality: the outcome ledger

Everything above prevents bad decisions. This is how the system finds out whether
the decisions were any good — and it is the part that most projects of this kind
skip.

Every decision is scored, after the fact, against what the market actually did.
The mechanism is [[Decision-Attribution]]: `DecisionOutcome` rows
([`core/models.py`](https://github.com/SergeyGer/AlphaAgent/blob/main/core/models.py#L483)) at 1, 7 and 30-day horizons,
written by `evaluate_pending` and summarised by
[`build_scorecard`](https://github.com/SergeyGer/AlphaAgent/blob/main/services/attribution.py#L305).

### The scoring rules are load-bearing

Three details decide whether the resulting number means anything:

**A SELL is correct when the price falls.**
[`directional_hit`](https://github.com/SergeyGer/AlphaAgent/blob/main/services/attribution.py#L90) signs the forward
return by the action. Scoring every action against "did the price rise" would mark
every correct exit as a failure and make the whole exercise worthless.

**A HOLD is not a directional call.** Its `directional_hit` is `NULL`, not `False`.
A cautious model that holds frequently would otherwise drag the hit rate down while
making no claims that could be wrong. `NULL` is excluded from the denominator;
`False` would be counted.

**Scoring starts at the executed price, not the close.** The first version of this
measured forward returns from the day's closing price, while trades executed at the
live quote. The bias was not random: whenever the close sat below the entry it
flattered a BUY.
[`_start_price`](https://github.com/SergeyGer/AlphaAgent/blob/main/services/attribution.py#L147) now prefers the
transaction's own price and falls back to the close only for a HOLD, which traded
nothing.

Correcting it removed a bias toward flattering BUY calls. The change and the
direction of the bias are recorded in `services/attribution.py` and `CHANGELOG.md`;
this page does not quote before/after figures because they came from a live
database and are not a committed fixture.

### What the ledger reports

The dashboard scorecard is computed at request time from `DecisionOutcome` rows
(`build_scorecard` in `services/attribution.py`) and is not committed, so no
figure is quoted here: it changes as outcomes mature and cannot be checked from
the repository. The feature exists to make the system's performance measurable,
and a measurement that can only produce good news is not a measurement. A
committed measurement report does exist, but it is the synthetic Phase 3 fixture
report, and it explicitly says it is engineering validation only.

### Why alpha versus a benchmark

A raw hit rate cannot distinguish skill from exposure to a rising market. Every
outcome also records SPY's return over the same window, so the scorecard reports
**alpha** — the decision's return minus the benchmark's. A 60% hit rate during a
bull market is not evidence of anything; a positive alpha is at least a candidate.

A missing benchmark is recorded as `NULL`, not zero. Zero would claim the benchmark
did nothing, which is a different and stronger statement than "we could not measure
it".

---

## 10. What this does **not** do

Worth stating plainly, because the alternative is a page that reads as a guarantee:

- **It does not make the model reliable.** It makes its failures bounded. Clamping a
  bad trade to the budget is safer than executing it, and still not the same as
  making a good decision.
- **It cannot detect a plausible wrong reason.** The guard checks whether a trade is
  *affordable*, not whether the argument for it is sound. A model citing a real
  headline to support a bad inference passes every layer above.
- **The measured performance is short-sample.** Read a rate with its per-horizon
  denominator. The dashboard publishes `evaluated` and `directional_calls` beside
  `hit_rate_pct`; without those counts the rate is not an estimate of anything.
- **A 30-day column is not a result until it matures.** The scorecard renders an
  unscored horizon as `NULL` rather than `0%`, which is the difference between
  "not yet known" and "known to be zero".
- **Grounding depends on the lexicon.** Deterministic sentiment is reproducible but
  not necessarily correct; it can be confidently wrong on sarcasm, context, or
  domain-specific phrasing. Being auditable is not the same as being right.

---

## 11. Where to look in the code

| Concern | Location |
| --- | --- |
| The only structure the AI may emit | `ai_agent.py` L97 — `TradeProposal` |
| Constraint recovery (casing, currency) | `ai_agent.py` L139–L158 — `_upper`, `_coerce_amount`, `_coerce_reasoning` |
| Parsing with honest failure | `ai_agent.py` L170 — `parse_proposal` |
| Offline fallback | `ai_agent.py` L421 — `HeuristicDecisionEngine` |
| Prompt construction and debate | `ai_agent.py` — `_thesis_block`, `debate`, CIO prompt |
| Deterministic sentiment | `services/sentiment.py` — `score_text`, `aggregate_sentiment` |
| Deterministic execution limits | `services/execution.py` L155 — `evaluate_proposal` |
| Portfolio ceilings | `services/execution.py` L100 — `_portfolio_headroom` |
| Outcome scoring | `services/attribution.py` — `directional_hit` L90, `_start_price` L147, `build_scorecard` L305 |
| Outcome storage | `core/models.py` L483 — `DecisionOutcome` |
| Prior belief | `core/models.py` L398 — `PositionThesis` |
| Cost ceiling | `services/budget.py` — `budget_state` |

Tests that pin the above: `core/tests/test_guardrails.py`,
`test_exposure_limits.py`, `test_attribution.py`, `test_thesis.py`,
`test_model_compatibility.py`, `test_service_exposure.py`.

---

## Related pages

- [[Execution-Guard]] — the deterministic limits in full
- [[Decision-Attribution]] — how the scorecard is built
- [[Position-Thesis]] — prior belief as context
- [[Agent-Debate]] — the bull/bear/CIO structure
- [[Quality-and-Testing]] — what the test suite covers
