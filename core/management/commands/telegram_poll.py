"""Long-poll the Telegram Bot API (local development).

Production uses the webhook at ``/api/telegram/webhook/``; polling needs no
public URL, which makes it the right choice for a laptop or a sandbox::

    python manage.py telegram_poll
    python manage.py telegram_poll --once     # drain the backlog and exit
"""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand, CommandError

from telegram_bot import TelegramError, bot_enabled, get_client, handle_update

logger = logging.getLogger("alphaagent.telegram")


class Command(BaseCommand):
    help = "Run the Telegram bot in long-polling mode (development)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process the current backlog once and exit.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=30,
            help="Long-poll timeout in seconds (default: 30).",
        )

    def handle(self, *args, **options) -> None:
        if not bot_enabled():
            raise CommandError("TELEGRAM_BOT_TOKEN is not set. Add it to .env to enable the bot.")

        client = get_client()
        try:
            me = client.get_me()
        except TelegramError as exc:
            raise CommandError(f"Telegram rejected the bot token: {exc}") from exc

        username = me.get("username", "unknown")
        self.stdout.write(self.style.SUCCESS(f"Polling as @{username} (Ctrl-C to stop)"))

        # A webhook and getUpdates are mutually exclusive.
        try:
            client.delete_webhook()
            self.stdout.write("Webhook cleared; using long polling.")
        except TelegramError as exc:
            self.stdout.write(self.style.WARNING(f"Could not clear webhook: {exc}"))

        offset: int | None = None
        while True:
            try:
                updates = client.get_updates(offset=offset, timeout=options["timeout"])
            except TelegramError as exc:
                logger.warning("getUpdates failed: %s", exc)
                time.sleep(3)
                continue
            except KeyboardInterrupt:  # pragma: no cover - interactive
                self.stdout.write("\nStopped.")
                return

            for update in updates:
                offset = update.get("update_id", 0) + 1
                try:
                    handle_update(update)
                except Exception as exc:
                    logger.exception("Update handling failed: %s", exc)

            if options["once"]:
                self.stdout.write(f"Processed {len(updates)} update(s); exiting.")
                return
