# Execution Guard

The execution guard is the part of AlphaAgent that is deliberately **not** AI.
It is the single authority on whether a proposal may become a transaction, and
it is a pure function: no I/O, no clock beyond what is passed in, no model.

Source: [`services/execution.py`](https://github.com/SergeyGer/AlphaAgent/blob/main/services/execution.py)

---

## Why it exists

An autonomous financial system is only as trustworthy as its ability to
constrain itself. The design decision — recorded as
[[Engineering-Decisions|ADR-001]] — is that risk limits are enforced in code the
model cannot influence, rather than stated in a prompt the model is asked to
respect.

The consequences of that choice drive everything on this page:

| Because the guard is… | …it is possible to |
| --- | --- |
| A pure function | Exhaustively unit-test every branch, with no mocks and no fixtures beyond plain objects |
| Not model-aware | Reason about risk without reasoning about model behaviour, prompt drift or provider changes |
| A single choke point | Guarantee that **no** code path reaches the ledger without passing it |
| Returning a typed decision | Persist *why* a trade was refused, not merely that it was |

> The most common way an AI trading project loses money is not a bad model. It is
> a limit that existed only in a system prompt.

## The decision object

```python
@dataclass
class GuardDecision:
    approved: bool
    reason: str  # human-readable, persisted to the audit trail
    action: str  # BUY | SELL | HOLD
    amount: Decimal = ZERO  # may differ from the proposal if clamped
    price: Decimal | None = None
    notional: Decimal = ZERO
    clamped: bool = False  # the guard resized the request
    notes: list[str] = ...  # audit detail for every adjustment made
```

`clamped` and `notes` exist so that a reviewer can see not just the verdict but
every deviation between what the model asked for and what was permitted.

## The rules, in evaluation order

`evaluate_proposal(portfolio, proposal, price, position, realised_pnl_today)`

| # | Rule | Outcome when violated |
| --- | --- | --- |
| 1 | `HOLD` requires no execution | Returns not-approved with a benign reason; nothing is written |
| 2 | A live price must exist and be positive | **Reject** — no execution without a reliable price |
| 3 | The amount must parse and quantize to a positive number | **Reject** — invalid or non-positive size |
| 4 | The per-trade budget (`max_trade_allocation_pct` of cash) must resolve above zero | **Reject** — a zero budget is a misconfiguration, not a trade |
| 5 | **BUY only:** the daily loss limit must not already be breached | **Block new risk, allow de-risking** — see below |
| 6 | **BUY only:** notional must fit the per-trade ceiling | **Clamp** down to the ceiling; reject if nothing fits |
| 7 | **BUY only:** notional must not exceed available cash | **Reject** |
| 8 | **SELL only:** an open position must exist | **Reject** |
| 9 | **SELL only:** the amount must not exceed the position | **Clamp** down to the position size |

### Rule 5 — the asymmetry that matters

Once realised losses for the day breach `daily_loss_limit_usd`, the guard freezes
**new risk** but continues to permit **de-risking**:

```
Blocked by daily loss limit: realised P&L today $-612.40 breaches -$500.
New risk is frozen.
```

Blocking sells as well would trap the portfolio in exactly the position that is
losing money. The limit is on *increasing exposure*, not on activity.

### Rules 6 and 9 — clamp rather than reject

When a model overshoots a limit, the guard resizes the order to the maximum
permitted and records the adjustment:

```
Clamped 12 -> 3.358 AAPL units to respect the $1127.95 ceiling
(max_trade_allocation_pct=5.00%).
```

This is a deliberate trade-off. Rejecting an oversized proposal discards an
otherwise-valid trading signal over a sizing error the model is bad at anyway —
language models are notably poor at arithmetic against a running balance. The
clamp is recorded in `notes`, so the adjustment is never silent.

The exception is when *nothing* fits: if the ceiling is smaller than one unit,
the proposal is rejected outright rather than executed at a nonsensical size.

### Precision

Amounts quantize to eight decimal places (`SATOSHI`) with `ROUND_DOWN`, and
notional to cents. Rounding **down** on both is intentional — the guard never
rounds a position *up* past a limit. Eight decimals exists because the platform
trades fractional crypto alongside equities.

## The only mutating path

Everything above is evaluation. Execution happens in exactly one function:

```python
def apply_trade(portfolio, proposal, decision, price) -> Transaction
```

It runs inside `transaction.atomic()` with `select_for_update()` row locks on the
portfolio and the asset, and **re-checks the money invariants under the lock**
before writing. The re-check matters: `evaluate_proposal` runs outside the lock,
so a concurrent task could have changed the balance in between. Locking without
re-validating would be a false sense of safety.

See [[Data-Model]] for the concurrency model.

## Human approval

When a portfolio is not autonomous, the proposal becomes a `TradeRecommendation`
instead of a trade. Approving it from the dashboard or Telegram **re-runs
`evaluate_proposal` against a freshly fetched price** before executing.

This is deliberate: a recommendation approved twenty minutes after it was
produced was evaluated against a twenty-minute-old price. Approval is a decision
to proceed *subject to the same limits*, not a bypass of them. If the market has
moved such that the trade is no longer permissible, the approval fails with the
guard's reason.

## Testing

The guard's purity is what makes the risk behaviour credible rather than
aspirational. `core/tests/test_guardrails.py` exercises every rule and every
interaction between them, including:

- each rejection reason and its exact message
- clamping boundaries, and the case where clamping rounds to zero
- the stop-loss asymmetry (BUY blocked, SELL permitted)
- non-positive prices, unparseable amounts, and zero-budget portfolios
- concurrency: two simultaneous buys cannot both drain the same cash

Because there is no I/O, these are fast, deterministic tests rather than
integration tests with mocks. See [[Quality-and-Testing]].

## Related pages

- [[Engineering-Decisions]] — ADR-001, and ADR-003 on the derived ledger
- [[Agent-Debate]] — what produces the proposal the guard evaluates
- [[Data-Model]] — locking, invariants and the transaction history
- [[Operations]] — throttling, the other layer of protection against over-trading
