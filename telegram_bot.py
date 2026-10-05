"""Telegram bot: interactive approvals and reports in the messenger.

Design notes
------------
* **English UI only**, as required.
* Transport is the raw Bot API over ``requests`` - two endpoints
  (``sendMessage`` / ``answerCallbackQuery``) do not justify a framework
  dependency, and a thin client is easier to test with a mocked session.
* Supports **both** delivery models: a webhook (production, ``/api/telegram/webhook/``)
  and long polling (local development, ``manage.py telegram_poll``). Both funnel
  into :func:`handle_update`, so behaviour is identical.
* Every callback is authorised against the chat's linked user. A user can only
  ever see or act on **their own** portfolio and recommendations.
* Nothing here raises into the caller: a Telegram outage must not fail a trade.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("alphaagent.telegram")

__all__ = [
    "TelegramClient",
    "TelegramError",
    "handle_update",
    "main_menu_keyboard",
    "notify_recommendation",
    "notify_trade",
    "recommendation_keyboard",
]

_API_ROOT = "https://api.telegram.org/bot{token}/{method}"
_MAX_TEXT = 3900  # Bot API limit is 4096; leave room for our own wrapper text.


class TelegramError(RuntimeError):
    """Raised when the Bot API returns an error."""


def _config() -> dict[str, Any]:
    return getattr(settings, "TELEGRAM_CONFIG", {}) or {}


def bot_enabled() -> bool:
    cfg = _config()
    return bool(cfg.get("BOT_TOKEN")) and bool(cfg.get("ENABLED", True))


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
@dataclass
class TelegramClient:
    """Minimal Bot API client."""

    token: str
    timeout: int = 15
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        """Invoke a Bot API method. Raises :class:`TelegramError` on failure."""
        url = _API_ROOT.format(token=self.token, method=method)
        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)  # type: ignore[union-attr]
        except requests.RequestException as exc:
            raise TelegramError(f"transport error calling {method}: {exc}") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method} returned non-JSON ({response.status_code})") from exc

        if not body.get("ok"):
            description = body.get("description", "unknown error")
            raise TelegramError(f"{method} failed: {description}")

        return body.get("result", {})

    # -- convenience wrappers ---------------------------------------------
    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:_MAX_TEXT],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", **payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:_MAX_TEXT],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("editMessageText", **payload)

    def answer_callback_query(
        self, callback_query_id: str, text: str = "", *, show_alert: bool = False
    ) -> dict[str, Any]:
        return self.call(
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text[:200],
            show_alert=show_alert,
        )

    def set_webhook(self, url: str, secret_token: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": url,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True,
        }
        if secret_token:
            payload["secret_token"] = secret_token
        return self.call("setWebhook", **payload)

    def delete_webhook(self) -> dict[str, Any]:
        return self.call("deleteWebhook", drop_pending_updates=True)

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", **payload)
        return result if isinstance(result, list) else []


def get_client() -> TelegramClient:
    cfg = _config()
    token = cfg.get("BOT_TOKEN", "")
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
    return TelegramClient(token=token, timeout=int(cfg.get("REQUEST_TIMEOUT", 15)))


# ---------------------------------------------------------------------------
# Keyboards (English labels)
# ---------------------------------------------------------------------------
def main_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Balance Report", "callback_data": "report:balance"},
                {"text": "📈 Open Positions", "callback_data": "report:positions"},
            ],
            [
                {"text": "🧠 Latest AI Reasoning", "callback_data": "report:thoughts"},
                {"text": "🗂 Pending Approvals", "callback_data": "report:pending"},
            ],
            [
                {"text": "🔍 Analyse Now", "callback_data": "action:analyse"},
                {"text": "🔄 Refresh", "callback_data": "report:balance"},
            ],
        ]
    }


def recommendation_keyboard(recommendation_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve Trade", "callback_data": f"ap:{recommendation_id}"},
                {"text": "❌ Reject Trade", "callback_data": f"rj:{recommendation_id}"},
            ],
            [
                {"text": "🧠 Full Reasoning", "callback_data": f"why:{recommendation_id}"},
                {"text": "🗂 All Pending", "callback_data": "report:pending"},
            ],
        ]
    }


def autonomy_keyboard(is_autonomous: bool) -> dict[str, Any]:
    toggle = (
        {"text": "⏸ Pause Autopilot", "callback_data": "action:pause"}
        if is_autonomous
        else {"text": "▶️ Resume Autopilot", "callback_data": "action:resume"}
    )
    return {
        "inline_keyboard": [[toggle, {"text": "📊 Balance", "callback_data": "report:balance"}]]
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def money(value: Decimal | str | float | None) -> str:
    if value is None:
        return "n/a"
    return f"${Decimal(str(value)):,.2f}"


def signed_money(value: Decimal | str | float | None) -> str:
    if value is None:
        return "n/a"
    amount = Decimal(str(value))
    return f"{'+' if amount >= 0 else '-'}${abs(amount):,.2f}"


def emoji_for_sentiment(sentiment: str) -> str:
    return {"BULLISH": "🟢", "BEARISH": "🔴"}.get((sentiment or "").upper(), "⚪")


def emoji_for_action(action: str) -> str:
    return {"BUY": "🟢 BUY", "SELL": "🔴 SELL"}.get((action or "").upper(), "⚪ HOLD")


def status_emoji(status: str) -> str:
    return {
        "PENDING": "⏳",
        "EXECUTED": "✅",
        "REJECTED": "❌",
        "EXPIRED": "⌛",
        "BLOCKED": "🛡",
        "APPROVED": "👍",
    }.get((status or "").upper(), "•")


# ---------------------------------------------------------------------------
# Content builders
# ---------------------------------------------------------------------------
def build_balance_message(portfolio) -> str:
    from services.portfolio_metrics import build_price_map, compute_metrics

    fallbacks = {a.ticker.upper(): a.avg_purchase_price for a in portfolio.assets.all()}
    price_map = build_price_map(fallbacks.keys(), fallbacks=fallbacks)
    m = compute_metrics(portfolio, price_map)

    pnl_icon = "📈" if m.unrealised_pnl_usd >= 0 else "📉"
    lines = [
        "📊 <b>Portfolio Balance</b>",
        "",
        f"💼 <b>Total Equity</b>: {money(m.total_equity_usd)}",
        f"💵 Cash: {money(m.cash_balance_usd)}",
        f"📦 Positions: {money(m.positions_value_usd)}",
        "",
        f"{pnl_icon} Unrealised P&L: {signed_money(m.unrealised_pnl_usd)} "
        f"({m.unrealised_pnl_pct:+.2f}%)",
        f"🧾 Realised today: {signed_money(m.realised_pnl_today_usd)}",
        "",
        f"🛡 Daily loss budget: {money(m.daily_loss_used_usd)} used of "
        f"{money(m.daily_loss_limit_usd)}",
        f"🎯 Max per trade: {money(m.max_trade_budget_usd)}",
        f"🤖 Autopilot: {'ON' if portfolio.is_autonomous else 'OFF'}",
    ]
    if m.is_autonomy_blocked and m.block_reason:
        lines += ["", f"⛔️ <b>Blocked</b>: {esc(m.block_reason)}"]
    return "\n".join(lines)


def build_positions_message(portfolio) -> str:
    from services.portfolio_metrics import build_price_map

    assets = list(portfolio.assets.all())
    if not assets:
        return "📈 <b>Open Positions</b>\n\nNo open positions yet."

    fallbacks = {a.ticker.upper(): a.avg_purchase_price for a in assets}
    price_map = build_price_map(fallbacks.keys(), fallbacks=fallbacks)

    lines = ["📈 <b>Open Positions</b>", ""]
    for asset in assets:
        quote = price_map.get(asset.ticker.upper())
        price = quote.price if quote else asset.avg_purchase_price
        value = (asset.amount * price).quantize(Decimal("0.01"))
        pnl = value - asset.cost_basis_usd
        icon = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{icon} <b>{esc(asset.ticker)}</b>  {asset.amount.normalize()}\n"
            f"    Value {money(value)} · Avg {money(asset.avg_purchase_price)} · "
            f"P&L {signed_money(pnl)}"
        )
    return "\n".join(lines)


def build_thoughts_message(portfolio, limit: int = 3) -> str:
    logs = portfolio.decision_logs.select_related("transaction").order_by("-created_at")[:limit]
    if not logs:
        return "🧠 <b>Latest AI Reasoning</b>\n\nNo agent activity yet."

    lines = ["🧠 <b>Latest AI Reasoning</b>", ""]
    for log in logs:
        stamp = timezone.localtime(log.created_at).strftime("%d %b %H:%M")
        lines.append(
            f"{emoji_for_sentiment(log.market_sentiment)} <b>{esc(log.action_taken)}</b>\n"
            f"<i>{stamp} · {log.tokens_used:,} tokens · ${log.api_cost_usd}</i>\n"
            f"{esc(log.reasoning[:600])}"
        )
        lines.append("─" * 18)
    return "\n".join(lines)


def build_recommendation_message(recommendation) -> str:
    return "\n".join(
        [
            f"{status_emoji(recommendation.status)} <b>Trade Approval Required</b>",
            "",
            f"<b>{emoji_for_action(recommendation.action)} {esc(recommendation.ticker)}</b>",
            f"Quantity: {recommendation.amount.normalize()}",
            f"Price: {money(recommendation.price)}",
            f"Notional: {money(recommendation.notional_usd)}",
            f"Sentiment: {emoji_for_sentiment(recommendation.sentiment)} "
            f"{esc(recommendation.sentiment)}",
            "",
            f"🧠 <i>{esc(recommendation.reasoning[:500])}</i>",
            "",
            "⏳ This proposal expires automatically.",
        ]
    )


def build_pending_message(portfolio) -> str:
    from core.models import RecommendationStatus

    pending = portfolio.recommendations.filter(status=RecommendationStatus.PENDING).order_by(
        "-created_at"
    )
    if not pending.exists():
        return "🗂 <b>Pending Approvals</b>\n\nNothing awaiting your decision. ✅"

    lines = ["🗂 <b>Pending Approvals</b>", ""]
    for reco in pending[:5]:
        lines.append(
            f"{status_emoji(reco.status)} #{reco.id} — "
            f"<b>{emoji_for_action(reco.action)} {esc(reco.ticker)}</b> "
            f"{reco.amount.normalize()} @ {money(reco.price)}"
        )
    lines += ["", "Use /pending to approve or reject each proposal."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command / callback handling
# ---------------------------------------------------------------------------
HELP_TEXT = "\n".join(
    [
        "🤖 <b>AlphaAgent</b> — autonomous portfolio control",
        "",
        "<b>Commands</b>",
        "/start &lt;code&gt; — link this chat to your account",
        "/balance — portfolio valuation",
        "/positions — open positions",
        "/thoughts — latest AI reasoning",
        "/pending — approve or reject proposals",
        "/analyse — run an advisory AI sweep now",
        "/pause — disable autonomous trading",
        "/resume — enable autonomous trading",
        "/unlink — disconnect this chat",
        "/help — this message",
    ]
)


def _reply(chat_id: int, text: str, markup: dict | None = None) -> None:
    try:
        get_client().send_message(chat_id, text, reply_markup=markup)
    except TelegramError as exc:
        logger.warning("Failed to reply to chat %s: %s", chat_id, exc)


def _get_link(chat_id: int):
    from core.models import TelegramLink

    return (
        TelegramLink.objects.select_related("user").filter(chat_id=chat_id, is_active=True).first()
    )


def _require_link(chat_id: int, client: TelegramClient | None = None) -> Any | None:
    link = _get_link(chat_id)
    if link is None:
        _reply(
            chat_id,
            "🔒 This chat is not linked to an AlphaAgent account.\n\n"
            "Open the dashboard, generate a link code, then send:\n"
            "<code>/start YOUR-CODE</code>",
        )
        return None
    return link


def _portfolio_for(link):
    from core.models import Portfolio

    portfolio, _ = Portfolio.objects.get_or_create(user=link.user)
    return portfolio


# -- commands ---------------------------------------------------------------
def _cmd_start(chat_id: int, args: str, from_user: dict) -> None:
    from core.models import TelegramLink

    code = (args or "").strip().upper()
    if not code:
        existing = _get_link(chat_id)
        if existing:
            _reply(
                chat_id,
                f"👋 Welcome back, <b>{esc(existing.user.username)}</b>.\n\n"
                "Use the buttons below to control your portfolio.",
                main_menu_keyboard(),
            )
        else:
            _reply(
                chat_id,
                "👋 <b>Welcome to AlphaAgent</b>\n\n"
                "To link this chat, open the dashboard and generate a link code, "
                "then send:\n<code>/start YOUR-CODE</code>",
            )
        return

    # Enforces the 15-minute window the API advertises. This previously filtered
    # on `is_active` alone, so an unused code never expired and a leaked one was
    # a permanent credential.
    candidate = TelegramLink.redeemable(code).select_related("user").first()
    if candidate is None:
        _reply(chat_id, "❌ That link code is not valid or has expired.")
        return

    # A chat may only ever be bound to one account.
    TelegramLink.objects.filter(chat_id=chat_id).exclude(pk=candidate.pk).delete()
    candidate.chat_id = chat_id
    candidate.telegram_username = (from_user or {}).get("username", "")[:64]
    candidate.link_code = ""
    candidate.link_code_issued_at = None
    candidate.last_seen_at = timezone.now()
    candidate.save(
        update_fields=[
            "chat_id",
            "telegram_username",
            "link_code",
            "link_code_issued_at",
            "last_seen_at",
        ]
    )

    logger.info("Telegram chat %s linked to user %s", chat_id, candidate.user.username)
    _reply(
        chat_id,
        f"✅ Linked to <b>{esc(candidate.user.username)}</b>.\n\n"
        "You will receive trade approvals here.",
        main_menu_keyboard(),
    )


def _cmd_balance(chat_id: int) -> None:
    link = _require_link(chat_id)
    if not link:
        return
    portfolio = _portfolio_for(link)
    _reply(chat_id, build_balance_message(portfolio), autonomy_keyboard(portfolio.is_autonomous))


def _cmd_positions(chat_id: int) -> None:
    link = _require_link(chat_id)
    if not link:
        return
    _reply(chat_id, build_positions_message(_portfolio_for(link)), main_menu_keyboard())


def _cmd_thoughts(chat_id: int) -> None:
    link = _require_link(chat_id)
    if not link:
        return
    _reply(chat_id, build_thoughts_message(_portfolio_for(link)), main_menu_keyboard())


def _cmd_pending(chat_id: int) -> None:
    link = _require_link(chat_id)
    if not link:
        return
    portfolio = _portfolio_for(link)
    from core.models import RecommendationStatus

    pending = list(
        portfolio.recommendations.filter(status=RecommendationStatus.PENDING).order_by(
            "-created_at"
        )[:5]
    )
    if not pending:
        _reply(chat_id, build_pending_message(portfolio), main_menu_keyboard())
        return

    _reply(chat_id, build_pending_message(portfolio))
    for reco in pending:
        _reply(
            chat_id,
            build_recommendation_message(reco),
            recommendation_keyboard(reco.id),
        )


def _cmd_analyse(chat_id: int) -> None:
    link = _require_link(chat_id)
    if not link:
        return
    portfolio = _portfolio_for(link)
    try:
        from tasks import advisory_sweep_task

        advisory_sweep_task.delay(portfolio.id)
        _reply(
            chat_id,
            "🔍 Advisory sweep queued.\n\n"
            "The AI is analysing your watchlist. Proposals will arrive here for approval.",
        )
    except Exception as exc:
        logger.exception("Failed to queue advisory sweep: %s", exc)
        _reply(chat_id, "⚠️ Could not queue the analysis. Please try again shortly.")


def _set_autonomy(chat_id: int, enabled: bool) -> None:
    link = _require_link(chat_id)
    if not link:
        return
    portfolio = _portfolio_for(link)
    portfolio.is_autonomous = enabled
    portfolio.save(update_fields=["is_autonomous"])

    from services.events import emit_autonomy_changed

    emit_autonomy_changed(portfolio.user_id, portfolio)

    state = "enabled ▶️" if enabled else "paused ⏸"
    warning = (
        "\n\n⚠️ The AI may now execute trades automatically within your limits."
        if enabled
        else "\n\nNew proposals will require your approval."
    )
    _reply(
        chat_id,
        f"🤖 Autopilot {state}.{warning}",
        autonomy_keyboard(portfolio.is_autonomous),
    )


def _cmd_unlink(chat_id: int) -> None:
    link = _get_link(chat_id)
    if link:
        link.is_active = False
        link.save(update_fields=["is_active"])
        logger.info("Telegram chat %s unlinked from %s", chat_id, link.user.username)
    _reply(chat_id, "🔓 This chat has been unlinked.")


# -- callbacks --------------------------------------------------------------
def _handle_recommendation_action(
    chat_id: int, message_id: int | None, callback_id: str, action: str, reco_id: int
) -> None:
    from core.models import DecidedVia, TradeRecommendation
    from services.recommendations import approve_recommendation, reject_recommendation

    link = _require_link(chat_id)
    if not link:
        get_client().answer_callback_query(callback_id, "Not linked", show_alert=True)
        return

    # Ownership check: a user may only act on their own recommendations.
    reco = (
        TradeRecommendation.objects.select_related("portfolio", "portfolio__user")
        .filter(pk=reco_id, portfolio__user=link.user)
        .first()
    )
    if reco is None:
        get_client().answer_callback_query(callback_id, "Not found", show_alert=True)
        return

    if not reco.is_actionable:
        get_client().answer_callback_query(
            callback_id, f"Already {reco.status.lower()}", show_alert=True
        )
        if message_id:
            _safe_edit(chat_id, message_id, reco)
        return

    try:
        if action == "approve":
            outcome = approve_recommendation(reco.id, via=DecidedVia.TELEGRAM)
        else:
            outcome = reject_recommendation(reco.id, via=DecidedVia.TELEGRAM)
    except Exception as exc:
        logger.exception("Telegram %s failed for recommendation %s: %s", action, reco_id, exc)
        get_client().answer_callback_query(callback_id, "Action failed", show_alert=True)
        return

    get_client().answer_callback_query(callback_id, outcome.message[:190], show_alert=False)
    reco.refresh_from_db()
    if message_id:
        _safe_edit(chat_id, message_id, reco, note=outcome.message)


def _safe_edit(chat_id: int, message_id: int, reco, note: str = "") -> None:
    kind = (
        "✅ <b>Executed</b>"
        if reco.status == "EXECUTED"
        else f"{status_emoji(reco.status)} <b>{esc(reco.status)}</b>"
    )
    body = f"{kind}\n\n{esc(note)}" if note else kind
    try:
        get_client().edit_message_text(
            chat_id,
            message_id,
            body,
            reply_markup={
                "inline_keyboard": [[{"text": "📊 Balance", "callback_data": "report:balance"}]]
            },
        )
    except TelegramError as exc:
        logger.debug("Could not edit Telegram message: %s", exc)


def _handle_callback(callback: dict) -> None:
    callback_id = callback.get("id", "")
    data = callback.get("data", "") or ""
    message = callback.get("message", {}) or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None:
        return

    try:
        if data.startswith(("ap:", "rj:", "why:")):
            verb, _, raw_id = data.partition(":")
            try:
                reco_id = int(raw_id)
            except ValueError:
                get_client().answer_callback_query(callback_id, "Bad request", show_alert=True)
                return
            if verb == "why":
                _show_reasoning(chat_id, callback_id, reco_id)
                return
            _handle_recommendation_action(
                chat_id, message_id, callback_id, "approve" if verb == "ap" else "reject", reco_id
            )
            return

        get_client().answer_callback_query(callback_id)

        if data == "report:balance":
            _cmd_balance(chat_id)
        elif data == "report:positions":
            _cmd_positions(chat_id)
        elif data == "report:thoughts":
            _cmd_thoughts(chat_id)
        elif data == "report:pending":
            _cmd_pending(chat_id)
        elif data == "action:analyse":
            _cmd_analyse(chat_id)
        elif data == "action:pause":
            _set_autonomy(chat_id, False)
        elif data == "action:resume":
            _set_autonomy(chat_id, True)
        else:
            _reply(chat_id, "🤔 Unknown action.", main_menu_keyboard())

    except TelegramError as exc:
        logger.warning("Telegram callback failed: %s", exc)
    except Exception as exc:
        logger.exception("Unhandled Telegram callback error: %s", exc)


def _show_reasoning(chat_id: int, callback_id: str, reco_id: int) -> None:
    from core.models import TradeRecommendation

    link = _get_link(chat_id)
    if link is None:
        get_client().answer_callback_query(callback_id, "Not linked", show_alert=True)
        return
    reco = TradeRecommendation.objects.filter(pk=reco_id, portfolio__user=link.user).first()
    if reco is None:
        get_client().answer_callback_query(callback_id, "Not found", show_alert=True)
        return

    get_client().answer_callback_query(callback_id)
    _reply(
        chat_id,
        f"🧠 <b>Full reasoning — {esc(reco.ticker)}</b>\n\n{esc(reco.reasoning) or '(none)'}",
        recommendation_keyboard(reco.id) if reco.is_actionable else None,
    )


# ---------------------------------------------------------------------------
# Update dispatch
# ---------------------------------------------------------------------------
def handle_update(update: dict) -> None:
    """Route one Telegram update. Never raises."""
    try:
        if "callback_query" in update:
            _handle_callback(update["callback_query"])
            return

        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text:
            return

        from_user = message.get("from") or {}
        command, _, args = text.partition(" ")
        command = command.split("@")[0].lower()

        handlers = {
            "/start": lambda: _cmd_start(chat_id, args, from_user),
            "/help": lambda: _reply(chat_id, HELP_TEXT, main_menu_keyboard()),
            "/balance": lambda: _cmd_balance(chat_id),
            "/positions": lambda: _cmd_positions(chat_id),
            "/thoughts": lambda: _cmd_thoughts(chat_id),
            "/pending": lambda: _cmd_pending(chat_id),
            "/analyse": lambda: _cmd_analyse(chat_id),
            "/pause": lambda: _set_autonomy(chat_id, False),
            "/resume": lambda: _set_autonomy(chat_id, True),
            "/unlink": lambda: _cmd_unlink(chat_id),
        }
        handler = handlers.get(command)
        if handler:
            handler()
        elif command.startswith("/"):
            _reply(chat_id, "🤔 Unknown command.\n\n" + HELP_TEXT, main_menu_keyboard())
        else:
            _reply(chat_id, HELP_TEXT, main_menu_keyboard())

    except Exception as exc:
        logger.exception("Failed to handle Telegram update: %s", exc)


# ---------------------------------------------------------------------------
# Outbound notifications (called from Celery)
# ---------------------------------------------------------------------------
def notify_recommendation(recommendation_id: int) -> dict[str, Any]:
    """Push a pending recommendation to the owner's chat with action buttons."""
    from core.models import TradeRecommendation

    if not bot_enabled():
        return {"status": "disabled"}

    try:
        reco = TradeRecommendation.objects.select_related("portfolio__user").get(
            pk=recommendation_id
        )
    except TradeRecommendation.DoesNotExist:
        return {"status": "missing"}

    link = getattr(reco.portfolio.user, "telegram_link", None)
    if link is None or not link.is_active or not link.notify_recommendations:
        return {"status": "not_linked"}

    try:
        get_client().send_message(
            link.chat_id,
            build_recommendation_message(reco),
            reply_markup=recommendation_keyboard(reco.id),
        )
    except TelegramError as exc:
        logger.warning("Could not push recommendation %s: %s", recommendation_id, exc)
        return {"status": "error", "error": str(exc)}

    return {"status": "sent", "chat_id": link.chat_id}


def notify_trade(transaction_id: int) -> dict[str, Any]:
    """Tell a linked user that a trade executed."""
    from core.models import Transaction

    if not bot_enabled():
        return {"status": "disabled"}

    try:
        tx = Transaction.objects.select_related("portfolio__user").get(pk=transaction_id)
    except Transaction.DoesNotExist:
        return {"status": "missing"}

    link = getattr(tx.portfolio.user, "telegram_link", None)
    if link is None or not link.is_active or not link.notify_trades:
        return {"status": "not_linked"}

    body = "\n".join(
        [
            "⚡️ <b>Trade executed</b>",
            "",
            f"<b>{emoji_for_action(tx.tx_type)} {esc(tx.ticker)}</b>",
            f"Quantity: {tx.amount.normalize()}",
            f"Price: {money(tx.price)}",
            f"Notional: {money(tx.gross_value_usd)}",
            f"Executed by: {esc(tx.executed_by)}",
        ]
    )
    try:
        get_client().send_message(link.chat_id, body, reply_markup=main_menu_keyboard())
    except TelegramError as exc:
        logger.warning("Could not push trade %s: %s", transaction_id, exc)
        return {"status": "error", "error": str(exc)}

    return {"status": "sent", "chat_id": link.chat_id}
