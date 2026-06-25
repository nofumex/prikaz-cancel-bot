from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot

from app.adapters.max import keyboards as max_keyboards
from app.adapters.max.client import MaxBotClient
from app.config import get_settings
from app.database import SessionLocal
from app.enums import CaseStatus
from app.keyboards.common import case_menu
from app.services.cases import due_unpaid_cases
from app.services.crm_background import schedule_crm_sync
from app.texts import deadline_warning

logger = logging.getLogger(__name__)


async def run_payment_reminders(bot: Bot | None = None) -> None:
    settings = get_settings()
    while True:
        try:
            async with SessionLocal() as session:
                for case in await due_unpaid_cases(session):
                    await session.refresh(case, ["user"])
                    if case.status != CaseStatus.PAYMENT_PENDING.value:
                        continue
                    reminder_no = case.reminders_sent + 1
                    text = deadline_warning(case.deadline_date, reminder_no)
                    if case.platform == "max":
                        await _send_max_reminder(settings, case, text)
                    elif bot is not None and case.user.telegram_id:
                        await bot.send_message(
                            case.user.telegram_id,
                            text,
                            reply_markup=case_menu(can_pay=True, payment_url=case.payment_url),
                        )
                    else:
                        continue
                    case.reminders_sent = reminder_no
                    case.last_reminder_at = datetime.utcnow()
                    schedule_crm_sync(settings, case.id, case.user.id, "reminder_sent", {"note": f"Напоминание {reminder_no}/3"})
                    if reminder_no >= 3:
                        schedule_crm_sync(settings, case.id, case.user.id, "payment_abandoned", {"note": "Пользователь не оплатил после 3 напоминаний"})
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Payment reminder loop failed")
        await asyncio.sleep(300)


async def _send_max_reminder(settings, case, text: str) -> None:
    chat_id = case.platform_chat_id or case.platform_user_id or case.user.platform_user_id
    async with MaxBotClient(
        settings.max_bot_token,
        settings.max_api_base_url,
        upload_retry_attempts=settings.max_upload_retry_attempts,
        upload_retry_base_seconds=settings.max_upload_retry_base_seconds,
    ) as client:
        await client.send_message(chat_id=chat_id, text=text, keyboard=max_keyboards.case_menu(can_pay=True, payment_url=case.payment_url))

