# Data Model

Seven tables in the `core` app hold the platform's complete state. Every
invariant that must survive concurrency is enforced by PostgreSQL 16 — `CHECK`
and `UNIQUE` constraints plus indexes — not only by Python. Models live in
`core/models.py`, the executable schema in `core/migrations/`.

Two decisions shape everything below: **`Transaction` is append-only**, so cash,
cost basis and realised P&L are *derived* rather than stored as mutable counters;
and **one code path writes money**, `services.execution.apply_trade()`, the sole
writer of `Transaction`, `Asset` and `Portfolio.balance_usd`.

No production code updates or deletes a `Transaction`; the only writes are
`Transaction.objects.create()` in `services/execution.py` and `seed_demo.py`.
`0001_initial` created `Portfolio`/`Asset`/`Transaction`/`AgentDecisionLog`,
`0002_telegramlink_portfoliosnapshot_traderecommendation` added
`TelegramLink`/`PortfolioSnapshot`/`TradeRecommendation`, and
`0003_agentdecisionlog_bear_case_and_more` added the debate fields — CI runs
`makemigrations --check --dry-run`, so drift fails the build.

## Portfolio

One row per user — the configuration the execution guard reads on every proposal.

| Field | Type | Notes |
| --- | --- | --- |
| `user` | `OneToOneField(User)` | `related_name="portfolio"` |
| `balance_usd` | `Decimal(12,2)` | Uninvested cash, default `10000.00` |
| `risk_profile` | `CharField(6)` | `RiskProfile`, default `medium`, indexed; feeds AI sizing |
| `is_autonomous` | `BooleanField` | Default `False`; true ⇒ the crew may execute without approval |
| `max_trade_allocation_pct` / `daily_loss_limit_usd` | `Decimal(4,2)` / `Decimal(12,2)` | Defaults `5.00` / `500.00`: share of *cash* per trade, stop-loss per UTC day |
| `created_at` | `DateTimeField` | `auto_now_add`; ordering `-created_at` |

`portfolio_balance_non_negative`, `portfolio_loss_limit_non_negative` and
`portfolio_allocation_pct_range` (`0 ≤ pct ≤ 100`) back the matching
`MinValueValidator`s. `max_trade_budget_usd = balance_usd ×
max_trade_allocation_pct / 100`, quantised to a cent and floored at zero — so the
ceiling moves with the cash balance.

## Asset

The position the API and dashboard read; `portfolio.assets` is prefetched by the
portfolio endpoint, so a lookup is one indexed query instead of a replay.

| Field | Type | Notes |
| --- | --- | --- |
| `portfolio` | `FK(Portfolio)` | `related_name="assets"`, `ON DELETE CASCADE` |
| `ticker` | `CharField(10)` | Indexed |
| `amount` / `avg_purchase_price` | `Decimal(18,8)` / `Decimal(12,2)` | Units (satoshi precision for crypto) and weighted average cost, both default `0` |

`asset_unique_ticker_per_portfolio` (`UNIQUE(portfolio, ticker)`) is what makes
the `get_or_create()` inside `apply_trade()` safe; `asset_amount_non_negative`
and `asset_avg_price_non_negative` guard the arithmetic. `market_value_usd(price=None)`
falls back to average cost when no quote exists — the degradation path used by
`services/portfolio_metrics.py`.

## Transaction

Append-only ledger row: written once, never mutated.

| Field | Type | Notes |
| --- | --- | --- |
| `portfolio` | `FK(Portfolio)` | `related_name="transactions"` |
| `ticker` | `CharField(10)` | Indexed |
| `tx_type` | `CharField(4)` | `TxType` — `BUY` / `SELL` |
| `amount`, `price` | `Decimal(18,8)`, `Decimal(12,2)` | Units traded and execution price per unit |
| `executed_by` | `CharField(10)` | `ExecutedBy` — `AI` from `apply_trade()`, `USER` from `seed_demo` |
| `timestamp` | `DateTimeField` | `auto_now_add`, indexed; ordering `-timestamp` |

`tx_amount_positive` (`amount > 0`) and `tx_price_positive` (`price > 0`) make a
zero-quantity or zero-price row impossible at the database level. Indexes
`tx_portfolio_recent_idx` on `(portfolio, -timestamp)` (trade table, dashboard)
and `tx_portfolio_ticker_idx` on `(portfolio, ticker)` (the replay). Property
`gross_value_usd` is cent-quantised and positive for both sides;
`signed_cash_flow_usd` is negative for `BUY`.

## AgentDecisionLog

One row per agent invocation — **including HOLDs, blocked trades and pipeline
crashes** — plus one row per human decision.

| Field | Type | Notes |
| --- | --- | --- |
| `portfolio` | `FK(Portfolio)` | `related_name="decision_logs"` |
| `transaction` | `OneToOneField(Transaction)` | `null=True`, `ON DELETE SET NULL`, `related_name="decision_log"` |
| `reasoning` | `TextField` | Full chain of thought from the reasoning model |
| `bull_case`, `bear_case` | `TextField` | Debate arguments (migration `0003`), default `""` |
| `action_taken` | `CharField(255)` | Verdict, e.g. `Purchased 3.5 AAPL @ $180.00 ($630.00)` |
| `market_sentiment` | `CharField(20)` | `MarketSentiment`, default `NEUTRAL`, indexed |
| `tokens_used` / `api_cost_usd` | `IntegerField` / `Decimal(8,5)` | Defaults `0` and `0.00000`; cost needs five decimals |
| `created_at` | `DateTimeField` | `auto_now_add`, indexed; ordering `-created_at` |

`log_tokens_non_negative`, `log_cost_non_negative` and index
`log_portfolio_recent_idx` on `(portfolio, -created_at)`. The `OneToOne` to
`Transaction` makes a decision *attributable*: the dashboard renders the exact
reasoning behind a specific ledger row, and `_log_human_decision()` links the
human verdict to the executed trade while the original AI row keeps a null link.
`purge_decision_logs` refuses to delete rows linked to an executed trade.

## PortfolioSnapshot

The ledger can reconstruct *positions*; it cannot reconstruct what they were
*worth* in the past. This table is that time series and nothing else.

| Field | Type | Notes |
| --- | --- | --- |
| `portfolio` | `FK(Portfolio)` | `related_name="snapshots"` |
| `total_equity_usd`, `cash_balance_usd`, `positions_value_usd` | `Decimal(16,2)` | Cash + marked-to-market positions at capture time |
| `unrealised_pnl_usd`, `realised_pnl_today_usd` | `Decimal(16,2)` | Default `0.00`; the second comes from the ledger replay |
| `captured_at` | `DateTimeField` | `auto_now_add`, indexed; ordering `-captured_at` |

Index `snap_portfolio_recent_idx` on `(portfolio, -captured_at)`. There are no
`CHECK` constraints: the values come from
`services/portfolio_metrics.compute_metrics()` and are a record, not a source of
truth. Rows are written after every executed trade and every 15 minutes per
portfolio; retention is `prune_snapshots(keep_days=90)` or
`purge_decision_logs --prune-snapshots`.

## TradeRecommendation

The advisory counterpart to autonomous execution. Approving one is **not** a
bypass: `approve_recommendation()` re-runs the full guard against live prices and
the current ledger before anything reaches `apply_trade()`.

| Field | Type | Notes |
| --- | --- | --- |
| `portfolio` | `FK(Portfolio)` | `related_name="recommendations"` |
| `ticker` / `action` | `CharField(10)` / `CharField(4)` | Indexed; `action` is `TxType` (`BUY`/`SELL`) |
| `amount`, `price`, `notional_usd` | `Decimal(18,8)`, `(12,2)`, `(16,2)` | As proposed; overwritten with executed values on approval, since the guard may clamp |
| `sentiment` / `reasoning` | `CharField(20)` / `TextField` | `MarketSentiment` (default `NEUTRAL`) and the CIO verdict, which may be empty |
| `status` / `decided_via` | `CharField(10)` ×2 | `RecommendationStatus` (default `PENDING`, indexed) and `DecidedVia` (blank until decided) |
| `decided_at`, `expires_at` | `DateTimeField` | Nullable; `expires_at = now + RECOMMENDATION_TTL_MINUTES` (default 60) |
| `decision_log` | `FK(AgentDecisionLog)` | `null=True`, `ON DELETE SET NULL` |
| `transaction` | `OneToOneField(Transaction)` | `null=True`, `ON DELETE SET NULL`, `related_name="recommendation"` |
| `created_at` | `DateTimeField` | `auto_now_add`, indexed; ordering `-created_at` |

`reco_amount_positive` and `reco_price_positive` — a recommendation always
carries a usable price — plus index `reco_portfolio_status_idx` on
`(portfolio, status, -created_at)`. Two invariants live in
`services/recommendations.py` rather than in the schema:
`create_recommendation()` returns `None` when a `PENDING` row for the same
`(portfolio, ticker)` exists (a chatty sweep cannot spam duplicates), and
`is_actionable` means `status == PENDING and not expired`;
`expire_stale_recommendations_task` closes abandoned proposals every 30 minutes.

## TelegramLink

| Field | Type | Notes |
| --- | --- | --- |
| `user` | `OneToOneField(User)` | `related_name="telegram_link"` |
| `chat_id` | `BigIntegerField` | `unique=True`, indexed — Telegram ids exceed 32 bits |
| `telegram_username` / `link_code` | `CharField(64)` / `CharField(32)` | Display name; pairing code, indexed (deliberately not unique) |
| `link_code_issued_at` | `DateTimeField` | Nullable. When the current `link_code` was issued; cleared on redemption |
| `is_active` | `BooleanField` | Default `True`; a revoked link stops notifications |
| `notify_trades`, `notify_recommendations` | `BooleanField` | Default `True` |
| `linked_at`, `last_seen_at` | `DateTimeField` | `auto_now_add`, nullable; ordering `-linked_at` |

The bot resolves the acting user by `chat_id`, never by a chat-supplied
identifier, so a Telegram message cannot address another user's portfolio.

### Link codes expire — and the expiry is enforced

`LINK_CODE_TTL` is 15 minutes. A code is redeemable only inside that window:

```python
TelegramLink.redeemable("ABC12345")  # filters on the code AND the issue time
```

This is worth stating explicitly because the column was added to fix a real gap.
The API had always returned an `expires_at` fifteen minutes out, and the bot's
rejection message read *"not valid **or has expired**"* — but the lookup filtered
on `is_active` alone. An unused code therefore stayed valid indefinitely, which
meant a leaked code was a **permanent** credential rather than a 15-minute one.

The window now lives in exactly one place: the issuing view derives `expires_at`
from `LINK_CODE_TTL`, and redemption filters on the same value, so the advertised
window and the enforced one cannot drift apart again.

`link_code_issued_at` is nulled alongside the code on successful redemption, so a
consumed code fails both checks — the explicit `is_valid` guard and the query.

## Enumerations

All are `models.TextChoices`: the stored value and the human label are declared
once and validated by Django before the database sees them.

| Enum | Values | Used by | Meaning |
| --- | --- | --- | --- |
| `RiskProfile` | `low`, `medium`, `high` | `Portfolio.risk_profile` | Investor appetite; shapes AI position sizing |
| `TxType` | `BUY`, `SELL` | `Transaction.tx_type`, `TradeRecommendation.action` | Ledger direction. `HOLD` is not stored — it writes an audit row and no transaction |
| `ExecutedBy` | `USER`, `AI` | `Transaction.executed_by` | Provenance of the decision; `apply_trade()` always stamps `AI`, only `seed_demo` writes `USER` |
| `MarketSentiment` | `BULLISH`, `BEARISH`, `NEUTRAL` | `AgentDecisionLog.market_sentiment`, `TradeRecommendation.sentiment` | The crew's market read, propagated from the proposal |
| `RecommendationStatus` | `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `EXECUTED`, `BLOCKED` | `TradeRecommendation.status` | `PENDING` → `EXECUTED` (guard passed) or `BLOCKED` (guard refused) on approval; `REJECTED` on decline; `EXPIRED` after the TTL or from the sweep |
| `DecidedVia` | `WEB`, `TELEGRAM`, `API`, `AUTO` | `TradeRecommendation.decided_via` | Which surface decided; `AUTO` comes from the expiry sweep |

`APPROVED` and `DecidedVia.API` are declared but never written by current code:
approval either executes (`EXECUTED`) or is refused by the guard (`BLOCKED`), and
every REST approval arrives as `DecidedVia.WEB`. Treat them as reserved names.

## The ledger is derived, not stored

`services/ledger.py` replays every `Transaction` for a portfolio in
`(timestamp, id)` order and folds it into a `PositionState` per ticker using
**weighted-average cost**:

```python
for ticker, tx_type, amount, price, timestamp in rows:
    state = states.setdefault(ticker, PositionState(ticker=ticker))
    if tx_type == TxType.BUY:
        new_amount = state.amount + amount
        if new_amount > 0:
            state.avg_cost = ((state.amount * state.avg_cost) + (amount * price)) / new_amount
        state.amount = new_amount
    else:  # SELL: realise against the running average cost, which does not move
        realised = amount * (price - state.avg_cost)
        state.realised_pnl_total += realised
        state.amount = max(state.amount - amount, ZERO)
        if state.amount == 0:
            state.avg_cost = ZERO
        if timestamp >= day_start:  # utc_day_start() = midnight UTC
            state.realised_pnl_today += realised
```

Worked example, checked against `core/tests/test_services.py`:

| Step | Row | `amount` after | `avg_cost` after | Realised P&L on this row |
| --- | --- | --- | --- | --- |
| 1 | BUY 10 @ 100.00 | 10 | 100.00 | — |
| 2 | BUY 10 @ 120.00 | 20 | 110.00 | — |
| 3 | SELL 4 @ 130.00 | 16 | 110.00 | +80.00 |
| 4 | SELL 16 @ 90.00 | 0 | 0.00 | −320.00 |

A full exit resets the basis to zero so a later re-entry starts clean, and
`realised_pnl_today` uses the same UTC-day boundary as the daily stop-loss in
`evaluate_proposal()`.

| Concern | Mutable counters | Replay from the ledger |
| --- | --- | --- |
| Auditability | The number exists; its derivation does not | Any past state is reproducible from the rows |
| Drift | Two writers diverge silently | One writer; recomputation is deterministic and idempotent |
| Partial failure | A half-applied update leaves wrong money | A rolled-back transaction leaves no trace |
| Cost | O(1) read | O(rows), streamed with `.iterator(chunk_size=2000)` |

The trade-off is CPU on the read path. So positions come from `Asset` (a
projection maintained inside the same locked transaction, using the identical
formula) while realised P&L and the stop-loss decision come from the replay;
`services/portfolio_metrics.py` composes both into one `PortfolioMetrics`. If the
two ever disagreed, the ledger is the one to trust.

## Concurrency: row locks

`apply_trade()` is one `transaction.atomic()` block that locks rows before
reading anything it is about to change:

| Operation | Lock | Protects against |
| --- | --- | --- |
| `apply_trade()` → `Portfolio.objects.select_for_update()` | Portfolio row | Two trades passing the cash pre-check and overdrawing the balance |
| `apply_trade()` → `Asset.objects.select_for_update().get_or_create()` | Asset row | Concurrent BUYs on one ticker losing a cost-basis update |
| `approve_/reject_recommendation()` → `TradeRecommendation.objects.select_for_update()` | Recommendation row | Two operators (dashboard + Telegram) deciding the same proposal; the second sees a non-`PENDING` status and stops |

`evaluate_proposal()` is deliberately a *pure function* — no I/O, no locks — so
it runs in tests, management commands and the API alike. The authoritative checks
happen again inside the lock, where the values cannot move: a BUY re-tests
`notional > portfolio.balance_usd` and a SELL re-tests `amount > asset.amount`,
each raising `DatabaseError("Balance changed under lock: …")` or
`DatabaseError("Position changed under lock: …")`. That aborts the transaction, so
the ledger row, the balance and the position roll back together — the ledger never
records a trade that did not happen. `run_alpha_agent_task()` catches
`DatabaseError`/`OperationalError`, marks the decision rejected with
`"Execution failed and was rolled back: …"`, and still writes the audit row.

Lock ordering is consistent (portfolio → asset; the approval path takes the
recommendation row first), so two executions cannot deadlock. PostgreSQL runs at
its default `READ COMMITTED`, which suffices because every read that feeds a write
is re-read under the lock. The Redis-backed cooldown in `services/throttle.py`
complements locking by stopping two overlapping sweeps from each trading inside
its own ceiling (`ALPHA_TICKER_COOLDOWN_SECONDS`, default 900): throttling is
admission control, locking is correctness.

## Why the AI never writes to the database

* **The tool set has no database access.** `ai_agent.py` builds
  `MarketPriceTool`, `NewsSentimentTool`, `FinancialHealthTool` and
  `PriceHistoryTool` — read-only market-data calls returning JSON: no SQL tool,
  no ORM handle, no shell.
* **The model returns a proposal, not a mutation.** Crew output is parsed into a
  Pydantic `TradeProposal` (`action`, `amount`, `sentiment`, `reasoning`); it
  cannot name a table, a column or a row id.
* **The guard is plain Python.** `evaluate_proposal()` applies the allocation,
  cash, position and stop-loss ceilings without consulting the model, so a prompt
  injection in a headline can change what the crew *wants* to do, never what it is
  *allowed* to do.
* **Exactly one writer, shared by every surface.** `apply_trade()` is the only path
  from proposal to ledger row (stamping `executed_by=AI` for provenance), and
  REST, Telegram and Celery all call it — there is no trusted route around the
  limits. `AgentDecisionLog` rows are written by application code in `tasks.py`.

Related: [[Execution-Guard]] for the rules in detail, [[Architecture]] for how the
pieces fit, [[API-Reference]] for the endpoints that read these tables, and
[[Real-Time-Layer]] for how changes reach the dashboard.
