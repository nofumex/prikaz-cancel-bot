from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot

from app.adapters.max import keyboards as max_keyboards
from app.adapters.max.client import MaxApiError, MaxBotClient
from app.config import get_settings
from app.database import SessionLocal
from app.enums import CaseStatus
from app.keyboards.common import case_menu, consultation_menu, main_menu
from app.models import Case, User
from app.services.cases import (
    due_case_consultation_reminders,
    due_no_order_cases,
    due_paid_followup_cases,
    due_started_users_without_cases,
    due_unpaid_cases,
)
from app.services.crm_background import schedule_crm_sync
from app.services.app_settings import reminder_settings
from app.texts import no_order_deadline_reminder_text, post_payment_court_followup_text, unpaid_document_reminder_text

logger = logging.getLogger(__name__)


async def run_payment_reminders(bot: Bot | None = None) -> None:
    settings = get_settings()
    while True:
        try:
            async with SessionLocal() as session:
                now = datetime.utcnow()
                reminder_config = reminder_settings()

                for user in await due_started_users_without_cases(session):
                    sent = await _send_user_message(
                        settings,
                        bot,
                        user,
                        reminder_config['reminder_try_text'],
                        telegram_markup=main_menu(),
                        max_keyboard=max_keyboards.main_menu(),
                    )
                    if sent:
                        user.first_deadline_reminder_sent_at = now

                for case in await due_no_order_cases(session):
                    await session.refresh(case, ["user"])
                    sent = await _send_case_message(
                        settings,
                        bot,
                        case,
                        reminder_config['reminder_try_text'],
                        telegram_markup=main_menu(),
                        max_keyboard=max_keyboards.main_menu(),
                    )
                    if sent:
                        case.deadline_reminder_sent_at = now
                        case.last_reminder_at = now
                        case.reminders_sent = max(case.reminders_sent, 1)
                        schedule_crm_sync(
                            settings,
                            case.id,
                            case.user.id,
                            "reminder_sent",
                            {"note": "\u041d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u0435 \u0447\u0435\u0440\u0435\u0437 \u0441\u0443\u0442\u043a\u0438: \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u043d\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u043b \u0441\u0443\u0434\u0435\u0431\u043d\u044b\u0439 \u043f\u0440\u0438\u043a\u0430\u0437"},
                        )

                for case in await due_unpaid_cases(session):
                    await session.refresh(case, ["user"])
                    if case.status != CaseStatus.PAYMENT_PENDING.value:
                        continue
                    sent = await _send_case_message(
                        settings,
                        bot,
                        case,
                        reminder_config['reminder_pay_text'],
                        telegram_markup=case_menu(can_pay=True, payment_url=case.payment_url),
                        max_keyboard=max_keyboards.case_menu(can_pay=True, payment_url=case.payment_url),
                    )
                    if sent:
                        case.deadline_reminder_sent_at = now
                        case.last_reminder_at = now
                        case.reminders_sent = max(case.reminders_sent, 1)
                        schedule_crm_sync(
                            settings,
                            case.id,
                            case.user.id,
                            "reminder_sent",
                            {"note": "\u041d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u0435 \u0447\u0435\u0440\u0435\u0437 \u0441\u0443\u0442\u043a\u0438: \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u043d\u0435 \u043e\u043f\u043b\u0430\u0442\u0438\u043b \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043b\u0435\u043d\u043d\u044b\u0439 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442"},
                        )

                for case in await due_paid_followup_cases(session):
                    await session.refresh(case, ["user"])
                    sent = await _send_case_message(
                        settings,
                        bot,
                        case,
                        post_payment_court_followup_text(),
                    )
                    if sent:
                        case.post_payment_followup_sent_at = now
                        schedule_crm_sync(
                            settings,
                            case.id,
                            case.user.id,
                            "paid_court_followup_sent",
                            {"note": "\u0412\u043e\u043f\u0440\u043e\u0441 \u0447\u0435\u0440\u0435\u0437 \u0434\u0432\u043e\u0435 \u0441\u0443\u0442\u043e\u043a \u043f\u043e\u0441\u043b\u0435 \u043e\u043f\u043b\u0430\u0442\u044b: \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043b\u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u044f\u0432\u043b\u0435\u043d\u0438\u0435 \u0432 \u0441\u0443\u0434"},
                        )

                for case in await due_case_consultation_reminders(session):
                    await session.refresh(case, ["user"])
                    sent = await _send_case_message(
                        settings,
                        bot,
                        case,
                        reminder_config['reminder_consultation_text'],
                        telegram_markup=consultation_menu(),
                        max_keyboard=max_keyboards.consultation_menu(),
                    )
                    if sent:
                        case.consultation_reminder_sent_at = now
                        schedule_crm_sync(
                            settings,
                            case.id,
                            case.user.id,
                            "consultation_offer_sent",
                            {"note": "\u041f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0430 \u043a\u043e\u043d\u0441\u0443\u043b\u044c\u0442\u0430\u0446\u0438\u044f \u043f\u043e \u0441\u0438\u0442\u0443\u0430\u0446\u0438\u0438 \u0438 \u0431\u0430\u043d\u043a\u0440\u043e\u0442\u0441\u0442\u0432\u0443"},
                        )

                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Payment reminder loop failed")
        await asyncio.sleep(300)


async def _send_case_message(settings, bot: Bot | None, case: Case, text: str, *, telegram_markup=None, max_keyboard=None) -> bool:
    try:
        if case.platform == "max":
            chat_id = case.platform_chat_id or case.platform_user_id or case.user.platform_user_id
            if not chat_id:
                return False
            await _send_max_message(settings, text, max_keyboard, chat_id=chat_id)
            return True
        if bot is not None and case.user.telegram_id:
            await bot.send_message(case.user.telegram_id, text, reply_markup=telegram_markup)
            return True
        return False
    except MaxApiError as exc:
        if _is_terminal_max_delivery_error(exc):
            case.reminder_delivery_blocked_at = datetime.utcnow()
            case.reminder_delivery_error = str(exc)[:1000]
            case.user.reminder_delivery_blocked_at = case.reminder_delivery_blocked_at
            case.user.reminder_delivery_error = case.reminder_delivery_error
            schedule_crm_sync(settings, case.id, case.user.id, 'reminder_delivery_failed', {'note': str(exc)[:1000]})
            logger.warning('MAX reminder disabled for case_id=%s user_id=%s: %s', case.id, case.user.platform_user_id, exc)
        else:
            logger.exception('MAX reminder delivery failed case_id=%s', case.id)
        return False
    except Exception:
        logger.exception('Reminder delivery failed case_id=%s', case.id)
        return False


async def _send_user_message(settings, bot: Bot | None, user: User, text: str, *, telegram_markup=None, max_keyboard=None) -> bool:
    try:
        if user.platform == "max" and user.platform_user_id:
            await _send_max_message(settings, text, max_keyboard, user_id=user.platform_user_id)
            return True
        if bot is not None and user.telegram_id:
            await bot.send_message(user.telegram_id, text, reply_markup=telegram_markup)
            return True
        return False
    except MaxApiError as exc:
        if _is_terminal_max_delivery_error(exc):
            user.reminder_delivery_blocked_at = datetime.utcnow()
            user.reminder_delivery_error = str(exc)[:1000]
            schedule_crm_sync(settings, None, user.id, 'reminder_delivery_failed', {'note': str(exc)[:1000]})
            logger.warning('MAX reminders disabled for user_id=%s: %s', user.platform_user_id, exc)
        else:
            logger.exception('MAX reminder delivery failed user_id=%s', user.platform_user_id)
        return False
    except Exception:
        logger.exception('Reminder delivery failed user_id=%s', user.id)
        return False


def _is_terminal_max_delivery_error(exc: MaxApiError) -> bool:
    text = str(exc).lower()
    return exc.status == 403 and (exc.code == 'chat.denied' or 'dialog.suspended' in text)


async def _send_max_message(settings, text: str, keyboard=None, *, chat_id: str | int | None = None, user_id: str | int | None = None) -> None:
    async with MaxBotClient(
        settings.max_bot_token,
        settings.max_api_base_url,
        upload_retry_attempts=settings.max_upload_retry_attempts,
        upload_retry_base_seconds=settings.max_upload_retry_base_seconds,
    ) as client:
        await client.send_message(chat_id=chat_id, user_id=user_id, text=text, keyboard=keyboard)
