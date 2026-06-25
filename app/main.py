from __future__ import annotations

import asyncio
import logging
import sys

from app.adapters.max.bot import run_max_bot
from app.adapters.max.webhook import run_max_webhook
from app.adapters.telegram.bot import run_telegram_bot
from app.config import get_settings
from app.database import close_db, init_db
from app.services.payment_web import run_payment_webhook
from app.services.reminders import run_payment_reminders


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)


async def main() -> None:
    setup_logging()
    settings = get_settings()
    await init_db()
    tasks = []
    if settings.run_telegram and settings.telegram_bot_token:
        tasks.append(run_telegram_bot(settings))
    else:
        logging.info("Telegram skipped: RUN_TELEGRAM=%s token_set=%s", settings.run_telegram, bool(settings.telegram_bot_token))
    if settings.run_max and settings.max_bot_token:
        tasks.append(run_max_webhook(settings) if settings.max_use_webhook else run_max_bot(settings))
    else:
        logging.info("MAX skipped: RUN_MAX=%s token_set=%s", settings.run_max, bool(settings.max_bot_token))

    telegram_active = settings.run_telegram and bool(settings.telegram_bot_token)
    max_active = settings.run_max and bool(settings.max_bot_token)
    payment_web_configured = bool(settings.yoomoney_receiver or settings.yoomoney_notification_secret or settings.payment_public_base_url)
    if max_active and not telegram_active:
        tasks.append(run_payment_reminders(None))
        if payment_web_configured:
            tasks.append(run_payment_webhook(None, settings))

    if not tasks:
        await close_db()
        raise RuntimeError("No bot token configured. Fill TG_BOT_TOKEN and/or MAX_BOT_TOKEN.")
    try:
        await asyncio.gather(*tasks)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
