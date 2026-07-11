from __future__ import annotations

from datetime import datetime
import logging

from aiogram import Bot

from sqlalchemy import func, select

from app.adapters.max import keyboards as max_keyboards
from app.enums import CaseStatus
from app.keyboards.common import case_menu, consultation_menu, main_menu
from app.models import Case, User
from app.services.app_settings import reminder_settings
from app.services.crm_background import schedule_crm_sync
from app.services.reminders import _send_case_message, _send_user_message

KINDS = {'try', 'pay', 'consultation'}
logger = logging.getLogger(__name__)


async def _safe_send(awaitable) -> bool:
    try:
        return bool(await awaitable)
    except Exception:
        logger.exception('Manual reminder delivery failed')
        return False


async def reminder_counts(session) -> dict[str, dict[str, int]]:
    no_case = ~select(Case.id).where(Case.user_id == User.id).exists()
    regular = User.is_admin.is_(False), User.is_manager.is_(False)
    result = {
        'try': {
            'pending': int(await session.scalar(select(func.count(User.id)).where(*regular, no_case, User.first_deadline_reminder_sent_at.is_(None))) or 0),
            'sent': int(await session.scalar(select(func.count(User.id)).where(*regular, no_case, User.first_deadline_reminder_sent_at.is_not(None))) or 0),
        },
        'pay': {
            'pending': int(await session.scalar(select(func.count(Case.id)).where(Case.status == CaseStatus.PAYMENT_PENDING.value, Case.deadline_reminder_sent_at.is_(None))) or 0),
            'sent': int(await session.scalar(select(func.count(Case.id)).where(Case.status == CaseStatus.PAYMENT_PENDING.value, Case.deadline_reminder_sent_at.is_not(None))) or 0),
        },
        'consultation': {
            'pending': int(await session.scalar(select(func.count(Case.id)).where(Case.status.in_([CaseStatus.PAID.value, CaseStatus.DELIVERED.value]), Case.consultation_reminder_sent_at.is_(None))) or 0),
            'sent': int(await session.scalar(select(func.count(Case.id)).where(Case.status.in_([CaseStatus.PAID.value, CaseStatus.DELIVERED.value]), Case.consultation_reminder_sent_at.is_not(None))) or 0),
        },
    }
    return result


def reminder_dashboard_text(counts: dict) -> str:
    try_pending = counts['try']['pending']
    pay_pending = counts['pay']['pending']
    consultation_pending = counts['consultation']['pending']
    try_sent = counts['try']['sent']
    pay_sent = counts['pay']['sent']
    consultation_sent = counts['consultation']['sent']
    return (
        '<b>📣 Рассылки и напоминания</b>\n\n<b>Еще не получали:</b>\n'
        f'• Попробовать бота — {try_pending}\n'
        f'• Оплатить preview — {pay_pending}\n'
        f'• Предложение консультации — {consultation_pending}\n\n'
        '<b>Уже получали:</b>\n'
        f'• Попробовать бота — {try_sent}\n'
        f'• Оплатить preview — {pay_sent}\n'
        f'• Предложение консультации — {consultation_sent}'
    )


async def send_manual_reminders(session, settings, bot, kind: str) -> tuple[int, int]:
    if kind not in KINDS:
        raise ValueError('Unknown reminder kind')
    config = reminder_settings()
    owned_bot = False
    if bot is None and settings.telegram_bot_token:
        bot = Bot(settings.telegram_bot_token)
        owned_bot = True
    now = datetime.utcnow()
    sent = failed = 0
    if kind == 'try':
        no_case = ~select(Case.id).where(Case.user_id == User.id).exists()
        users = list((await session.execute(select(User).where(User.is_admin.is_(False), User.is_manager.is_(False), no_case, User.first_deadline_reminder_sent_at.is_(None)))).scalars())
        for user in users:
            ok = await _safe_send(_send_user_message(settings, bot, user, config['reminder_try_text'], telegram_markup=main_menu(), max_keyboard=max_keyboards.main_menu()))
            if ok:
                user.first_deadline_reminder_sent_at = now
                sent += 1
            else:
                failed += 1
    else:
        if kind == 'pay':
            query = select(Case).where(Case.status == CaseStatus.PAYMENT_PENDING.value, Case.deadline_reminder_sent_at.is_(None))
        else:
            query = select(Case).where(Case.status.in_([CaseStatus.PAID.value, CaseStatus.DELIVERED.value]), Case.consultation_reminder_sent_at.is_(None))
        cases = list((await session.execute(query)).scalars())
        for case in cases:
            await session.refresh(case, ['user'])
            if kind == 'pay':
                ok = await _safe_send(_send_case_message(settings, bot, case, config['reminder_pay_text'], telegram_markup=case_menu(True, case.payment_url), max_keyboard=max_keyboards.case_menu(True, case.payment_url)))
                event = 'reminder_sent'
                note = 'Ручное напоминание оплатить подготовленный документ'
            else:
                ok = await _safe_send(_send_case_message(settings, bot, case, config['reminder_consultation_text'], telegram_markup=consultation_menu(), max_keyboard=max_keyboards.consultation_menu()))
                event = 'consultation_offer_sent'
                note = 'Ручное предложение консультации'
            if ok:
                if kind == 'pay':
                    case.deadline_reminder_sent_at = now
                    case.last_reminder_at = now
                    case.reminders_sent = max(case.reminders_sent, 1)
                else:
                    case.consultation_reminder_sent_at = now
                schedule_crm_sync(settings, case.id, case.user.id, event, {'note': note})
                sent += 1
            else:
                failed += 1
    await session.commit()
    if owned_bot:
        await bot.session.close()
    return sent, failed
