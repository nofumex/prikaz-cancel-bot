from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot

from app.database import SessionLocal
from app.keyboards.common import case_menu
from app.services.cases import due_unpaid_cases
from app.texts import deadline_warning

logger = logging.getLogger(__name__)


async def run_payment_reminders(bot: Bot) -> None:
    while True:
        try:
            async with SessionLocal() as session:
                for case in await due_unpaid_cases(session):
                    await session.refresh(case, ["user"])
                    if case.user.platform != "telegram" or not case.user.telegram_id:
                        continue
                    reminder_no = case.reminders_sent + 1
                    await bot.send_message(
                        case.user.telegram_id,
                        deadline_warning(case.deadline_date, reminder_no),
                        reply_markup=case_menu(can_pay=True, payment_url=case.payment_url),
                    )
                    case.reminders_sent = reminder_no
                    case.last_reminder_at = datetime.utcnow()
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Payment reminder loop failed")
        await asyncio.sleep(300)
