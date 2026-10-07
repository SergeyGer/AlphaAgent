# Telegram Bot

`telegram_bot.py` is the operator surface for [[Execution-Guard]] decisions: it
pushes trade proposals to a linked chat, and lets the operator approve or reject
them without opening the dashboard. It is deliberately thin — the raw Bot API over
`requests`, English-only labels, and no business logic of its own.

Related pages: [[API-Reference]] · [[Execution-Guard]] · [[Data-Model]] · [[Operations]] · [[Configuration]]

## How it is wired

Both delivery models funnel into one function, `handle_update(update)`, so a button
behaves identically in production and on a laptop:

| | Production — webhook | Development — long polling |
| --- | --- | --- |
| Entry point | `POST /api/telegram/webhook/` (`TelegramWebhookView`) | `python manage.py telegram_poll` |
| Needs a public HTTPS URL | Yes | No |
| Auth | `X-Telegram-Bot-Api-Secret-Token` header, compared with `secrets.compare_digest` | Bot token only |
| Update source | Telegram pushes; `request.data` is passed straight to `handle_update` | `getUpdates` with an `offset`, looped |
| Dispatcher | `handle_update` → `_handle_callback` / command handlers | identical |

The webhook view is unauthenticated **by design** — Telegram cannot present a DRF
token — and verified instead by the shared secret from `TELEGRAM_WEBHOOK_SECRET`.
Behaviour, in the order the view evaluates it:

1. No `TELEGRAM_BOT_TOKEN` configured → return `{"ok": true}`, 200, do nothing.
2. Secret configured and header absent/mismatched → log
   `Rejected Telegram webhook with bad secret token`, return 403.
3. Otherwise call `handle_update`, swallowing any exception, and **always return
   200** so Telegram does not retry the same update forever.
4. A blank `TELEGRAM_WEBHOOK_SECRET` with a bot token configured makes the endpoint
return `503` (fail-closed) instead of accepting unsigned posts. Set it in
   production; `.env.example` calls it required there.

`set_webhook` sends `allowed_updates: ["message", "callback_query"]` and
`drop_pending_updates: true`, so the bot only ever receives what it handles and
never replays a backlog it missed. `delete_webhook` drops pending updates too.

`telegram_poll` long-polls for local development:

```bash
python manage.py telegram_poll            # loop until Ctrl-C
python manage.py telegram_poll --once     # drain the backlog and exit
python manage.py telegram_poll --timeout 10
```

It calls `getMe` first (a rejected token exits with
`Telegram rejected the bot token`), clears any registered webhook — `getUpdates` and
a webhook are mutually exclusive — then loops with `offset = update_id + 1`, sleeping
3 s after a transport error. Per-update failures are logged and skipped, never fatal.

## Setup

1. Create the bot with **@BotFather** and keep the token out of the repository.

2. Add the configuration to `.env` (placeholders — never commit real values):

   ```bash
   TELEGRAM_BOT_TOKEN=<token-from-BotFather>
   TELEGRAM_WEBHOOK_SECRET=<random-32-byte-string>
   TELEGRAM_WEBHOOK_URL=https://<your-host>/api/telegram/webhook/
   TELEGRAM_REQUEST_TIMEOUT=15
   ```

   Leaving `TELEGRAM_BOT_TOKEN` empty disables the integration completely:
   `TELEGRAM_CONFIG["ENABLED"]` is derived from it, the webhook acknowledges without
   work, and `POST /api/telegram/link/` returns 503. The rest of the platform is
   unaffected.

3. Recreate the services that read the token — `web` (webhook, link API) and
   `worker` (outbound notifications):

   ```bash
   docker compose up -d --force-recreate web worker
   ```

4. Register the webhook. There is **no management command** for this; use the Bot API
   directly (the payload mirrors `TelegramClient.set_webhook`):

   ```bash
   curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
     -H 'Content-Type: application/json' \
     -d "{\"url\":\"${TELEGRAM_WEBHOOK_URL}\",\"secret_token\":\"${TELEGRAM_WEBHOOK_SECRET}\",
          \"allowed_updates\":[\"message\",\"callback_query\"],\"drop_pending_updates\":true}"
   ```

   The equivalent from inside the app:

   ```bash
   docker compose exec web python manage.py shell -c \
     "from telegram_bot import get_client; print(get_client().call('getWebhookInfo'))"
   ```

5. Link a chat to an account. Mint a code from the authenticated API, then send it to
   the bot:

   ```bash
   curl -sS -X POST http://127.0.0.1:8000/api/telegram/link/ \
     -H "Authorization: Token ${ALPHAAGENT_API_TOKEN}"
   # -> {"link_code": "<16-hex-code>", "expires_at": "...", "instructions": "..."}
   ```

   In Telegram: `/start <16-hex-code>`. The chat is now bound; `/balance` and the
   main menu work.

6. For a laptop or a sandbox, skip step 4 and run the poller instead
   (`python manage.py telegram_poll`); no public URL is involved.

Verify the pairing from the server side:

```bash
docker compose exec web python manage.py shell -c \
  "from core.models import TelegramLink; print(list(TelegramLink.objects.values_list('user__username','chat_id','is_active')))"
```

## Command reference

Commands are matched case-insensitively after stripping a `@botname` suffix.
Unknown `/commands` get "Unknown command" plus the help text; any non-command text
gets the help text and the main menu.

| Command | Handler | Effect |
| --- | --- | --- |
| `/start` | `_cmd_start` | Linked chat: welcome back + main menu. Unlinked: instructions to obtain a code |
| `/start <CODE>` | `_cmd_start` | Binds this chat to the account owning an active `link_code`; clears the code, records `telegram_username` and `last_seen_at` |
| `/balance` | `_cmd_balance` | Equity, cash, positions value, unrealised and realised P&L, daily-loss budget, per-trade budget, autopilot state, plus the pause/resume buttons |
| `/positions` | `_cmd_positions` | One block per asset: quantity, value, average price, P&L |
| `/thoughts` | `_cmd_thoughts` | Last 3 `AgentDecisionLog` rows: action, timestamp, tokens, cost, 600 chars of reasoning |
| `/pending` | `_cmd_pending` | Lists up to 5 pending proposals; each gets its own message with Approve/Reject/Reasoning buttons |
| `/analyse` | `_cmd_analyse` | Queues `advisory_sweep_task` for the linked portfolio — analysis only, nothing executes |
| `/pause` | `_set_autonomy(False)` | Sets `is_autonomous=False`, emits an autonomy event, warns that proposals now need approval |
| `/resume` | `_set_autonomy(True)` | Sets `is_autonomous=True` and warns that the AI may now execute within limits |
| `/unlink` | `_cmd_unlink` | Sets the link's `is_active=False`; the chat can be re-paired later |
| `/help` | inline | The English command list with the main menu |

## Inline keyboards

Labels are English-only; every button carries a short `callback_data` string. The
test suite asserts every `callback_data` stays within Telegram's 64-byte limit.

**Main menu** (`main_menu_keyboard`):

| Button | `callback_data` |
| --- | --- |
| 📊 Balance Report | `report:balance` |
| 📈 Open Positions | `report:positions` |
| 🧠 Latest AI Reasoning | `report:thoughts` |
| 🗂 Pending Approvals | `report:pending` |
| 🔍 Analyse Now | `action:analyse` |
| 🔄 Refresh | `report:balance` |

**Per recommendation** (`recommendation_keyboard(id)`):

| Button | `callback_data` | Handler |
| --- | --- | --- |
| ✅ Approve Trade | `ap:<id>` | `_handle_recommendation_action` → `approve_recommendation(..., via=DecidedVia.TELEGRAM)` |
| ❌ Reject Trade | `rj:<id>` | `_handle_recommendation_action` → `reject_recommendation(..., via=DecidedVia.TELEGRAM)` |
| 🧠 Full Reasoning | `why:<id>` | `_show_reasoning` |
| 🗂 All Pending | `report:pending` | `_cmd_pending` |

**Autonomy toggle** (`autonomy_keyboard`): `⏸ Pause Autopilot` (`action:pause`) or
`▶️ Resume Autopilot` (`action:resume`), plus `📊 Balance` (`report:balance`).
Unknown callback data replies `🤔 Unknown action.` with the main menu.

## The security model

A callback payload is **attacker-controllable input**, never an authority. Anyone who
can reach the update path can forge `ap:1`; the code therefore treats `callback_data`
as a *request* and re-derives every fact it needs from the server-side session (the
chat link) and the database.

| Supplied by the payload | Derived server-side and trusted |
| --- | --- |
| The recommendation id in `ap:` / `rj:` / `why:` | The acting user — from `chat_id` → active `TelegramLink.user` |
| *(nothing else)* | Ownership — `TradeRecommendation.filter(pk=reco_id, portfolio__user=link.user)` |
| | Actionability — `reco.is_actionable` re-read at click time |
| | Trade validity — `evaluate_proposal()` re-run against live prices |

Every path enforces the same order:

1. **Link required.** `_require_link(chat_id)` resolves an *active* `TelegramLink`;
   otherwise the bot replies "This chat is not linked to an AlphaAgent account" and
   the callback is answered with a `Not linked` alert. No report is rendered.
2. **Ownership re-verified.** The recommendation is fetched with
   `portfolio__user=link.user`. Another user's id yields `Not found`, never data.
3. **State re-checked.** `is_actionable` guards against double approval: a decided or
   expired proposal answers `Already <status>` and, where possible, edits the original
   message to show the final state.
4. **Approval is not a bypass.** `approve_recommendation()` takes a row lock, replays
   positions, fetches a fresh quote and runs the **same** execution guard used by the
   autonomous path. A proposal that no longer fits is stored as `BLOCKED` with the
   guard reason instead of executing — see [[Execution-Guard]].
5. **Attribution.** The decision is recorded with `decided_via=TELEGRAM` and written
   into the audit trail by `_log_human_decision`, so the operator's action sits in the
   same timeline as the AI reasoning.

Supporting controls:

* **Pairing.** `POST /api/telegram/link/` mints `secrets.token_hex(8).upper()`; the
  code is cleared on first successful use, and a chat is bound to at most one account
  (any other `TelegramLink` row for that `chat_id` is deleted). `chat_id` is unique
  and non-null, so an unpaired row carries a synthetic negative id (`-user.id`).
  Expiry is enforced, not merely advertised: `_cmd_start` resolves the code through
  `TelegramLink.redeemable()`, which requires `link_code`, `is_active` **and** a
  `link_code_issued_at` inside `LINK_CODE_TTL` (15 minutes). This closed a real gap —
  the lookup previously filtered on `is_active` alone, so an unused code never expired
  and a leaked one was a permanent credential. Treat the code as a secret and let it
  be consumed or replaced.
* **Webhook authenticity.** The `X-Telegram-Bot-Api-Secret-Token` header is compared
  with `secrets.compare_digest`; a mismatch is logged and rejected with 403. If the
  secret is unset while a bot token *is* configured, the endpoint fails closed with
  `503` rather than accepting unsigned posts — an unconfigured deployment must not be
  less safe than a configured one.
* **Output hardening.** All model- and user-derived text goes through `html.escape`
  (`esc`) before HTML `parse_mode` rendering; messages are truncated at 3900 chars and
  callback answers at 200, so a long reasoning string cannot break a reply.
* **Fail-soft, never fail-closed on Telegram.** `handle_update` catches everything,
  `_safe_edit` logs at debug, and notification helpers return a status dict instead of
  raising — a Telegram outage must never fail a trade.

## Outbound notifications

| Function | Trigger | Gating | Returns |
| --- | --- | --- | --- |
| `notify_recommendation(id)` | `tasks.notify_recommendation_task`, queued from `run_alpha_agent_task` after a PENDING proposal is created | `bot_enabled()`, active link, `link.notify_recommendations` | `sent` / `disabled` / `missing` / `not_linked` / `error` |
| `notify_trade(id)` | `tasks.notify_trade_task`, enqueued after execution on both the autonomous path and the human-approval path | same, plus `link.notify_trades` | same shape |

`notify_recommendation` sends the proposal card with Approve/Reject buttons;
`notify_trade` (implemented, not yet dispatched) would send the execution summary with
the main menu. A transport failure is logged as a warning and returned as `error`, and
the Celery wrapper in `tasks.py` catches as well — a Telegram problem never marks a
successful trade as failed. `/pending` is the recovery path when a push is missed.

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `Telegram rejected the bot token` on `telegram_poll` | Token wrong, revoked, or from another bot | Re-copy from @BotFather; `/start` from the bot's own chat to confirm it is alive |
| Webhook log: `Rejected Telegram webhook with bad secret token` | `TELEGRAM_WEBHOOK_SECRET` does not match the `secret_token` registered with `setWebhook` | Re-register the webhook (setup step 4), then recreate `web` |
| Bot silent in production | Webhook never registered, or registered with a URL Telegram cannot reach | `getWebhookInfo`; a webhook URL must be public HTTPS — terminate TLS in front and enable `DJANGO_SECURE_SSL_REDIRECT`/`DJANGO_USE_X_FORWARDED_PROTO` |
| Bot silent during development | Webhook still registered, so `getUpdates` returns nothing | `telegram_poll` clears it automatically; otherwise call `deleteWebhook` |
| `Conflict: terminated by other getUpdates request` | Two pollers on the same token | Run one; the exception is caught and retried after 3 s, which hides the cause |
| `/start <CODE>` → "That link code is not valid or has expired" | Code mistyped, already consumed, or the link was deactivated | Mint a new code; codes are 16 hex characters, uppercase |
| Buttons answer `Not linked` | The chat was unlinked or re-paired to another account | Mint a new code in the dashboard and send `/start <CODE>` again |
| Buttons answer `Already executed` / `Already expired` | Correct behaviour on a stale message | Use `/pending` for the current list |
| Approval results in `Blocked by guardrail: ...` | The trade stopped fitting between proposal and click (price, cash, stop-loss) | Expected; the reason is shown and logged — see [[Execution-Guard]] |
| Long reasoning appears cut off | Intentional truncation (`build_thoughts_message` 600 chars, messages 3900) | Read the full text in the dashboard audit trail |
