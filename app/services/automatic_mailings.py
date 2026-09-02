from __future__ import annotations

import asyncio
import logging
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.max import keyboards as max_keyboards
from app.database import SessionLocal
from app.keyboards.common import automatic_mailing_menu
from app.models import Case, MailingAction, MailingJob, MailingState, User
from app.services.amocrm import get_amocrm_service
from app.services.cases import ensure_user_has_case
from app.services.reminders import _send_user_message
from app.utils import normalize_phone

logger = logging.getLogger(__name__)

PAVEL_PHONE = "+79230165336"
PAVEL_MESSAGE = (
    f"<b>Скоро вам позвонит юрисконсульт Павел с номера {PAVEL_PHONE}</b>\n\n"
    "<b>Сохраните СЕЙЧАС этот номер</b>, иначе не поймете кто вам звонит! Либо сами ему позвоните"
)
CONSULTATION_NO_MESSAGE = f"Вам звонил юрисконсульт Павел с номера {PAVEL_PHONE}, перезвоните ему"
PHONE_REQUEST_TEXT = "Укажите свой номер телефона для связи"
REMINDERS_DISABLED_TEXT = "Напоминания отключены"
CONSULTATION_STATUS = "Консультация"
CONSULTATION_LOCATION = "СУДЕБНЫЙ ПРИКАЗ / Консультация"

FIRST_TEXT = (
    "<b>Мы можем помочь вам с долгами по судебному приказу!</b>\n\n"
    "Нажмите кнопку ниже и мы бесплатно проконсультируем вас!"
)
SECOND_TEXT = (
    "<b>Давайте мы уже поможем вам с долгами по судебному приказу!</b>\n\n"
    "Нажмите кнопку ниже и мы проконсультируем вас!"
)
FOLLOWUP_TEXT = (
    "Сегодня у нас день консультаций по решению проблем с долгами!\n\n"
    "Нажмите кнопку ниже и мы проконсультируем вас!"
)
LAST_STAGE = 12


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def due_for_stage(stage: int, previous_at: datetime) -> datetime:
    if stage in {1, 2}:
        return previous_at + timedelta(days=7)
    if stage == 3:
        return _add_months(previous_at, 1)
    if 4 <= stage <= 6:
        return _add_months(previous_at, 2)
    if 7 <= stage <= 8:
        return _add_months(previous_at, 6)
    if 9 <= stage <= 10:
        return _add_months(previous_at, 12)
    return _add_months(previous_at, 24)


def stage_text(stage: int) -> str:
    return FIRST_TEXT if stage == 1 else SECOND_TEXT if stage == 2 else FOLLOWUP_TEXT


async def get_mailing_state(session: AsyncSession, user_id: int) -> MailingState | None:
    return await session.scalar(select(MailingState).where(MailingState.user_id == user_id))


async def _schedule_job(session: AsyncSession, state: MailingState, stage: int, due_at: datetime) -> MailingJob | None:
    if stage > LAST_STAGE or state.reminders_disabled or state.excluded_sales or not state.participating:
        return None
    job = await session.scalar(
        select(MailingJob).where(MailingJob.user_id == state.user_id, MailingJob.stage == stage)
    )
    if job is None:
        job = MailingJob(user_id=state.user_id, stage=stage, due_at=due_at, status="pending")
        session.add(job)
    elif job.status == "cancelled":
        job.status = "pending"
        job.due_at = due_at
        job.cancelled_at = None
        job.error_message = None
    return job


async def ensure_mailing_started(
    session: AsyncSession, user: User, *, started_at: datetime | None = None
) -> MailingState:
    """Idempotently register the first explicit /start and its first durable job."""
    state = await get_mailing_state(session, user.id)
    if state is not None:
        return state
    started_at = started_at or datetime.utcnow()
    state = MailingState(user_id=user.id, started_at=started_at, next_stage=1)
    session.add(state)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await get_mailing_state(session, user.id)
        if existing is None:
            raise
        return existing
    await _schedule_job(session, state, 1, due_for_stage(1, started_at))
    await session.commit()
    return state


async def cancel_future_jobs(session: AsyncSession, user_id: int, *, now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    result = await session.execute(
        update(MailingJob)
        .where(MailingJob.user_id == user_id, MailingJob.status == "pending")
        .values(status="cancelled", cancelled_at=now)
    )
    return int(result.rowcount or 0)


async def _case_for_user(session: AsyncSession, user: User, chat_id: str | None = None) -> Case:
    case, _ = await ensure_user_has_case(session, user, chat_id=chat_id)
    return case


async def record_action(
    session: AsyncSession,
    settings,
    user: User,
    case: Case,
    action_key: str,
    event_type: str,
    note: str,
    *,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Write an amoCRM note once, then persist the same idempotency key locally."""
    exists = await session.scalar(
        select(MailingAction.id).where(
            MailingAction.user_id == user.id, MailingAction.action_key == action_key
        )
    )
    if exists is not None:
        return False
    payload = {"note": note, "mailing_action_key": action_key, **(extra or {})}
    crm = get_amocrm_service(settings)
    await crm.sync_case_event(session, case, user, event_type, payload)
    session.add(
        MailingAction(
            user_id=user.id,
            case_id=case.id,
            action_key=action_key,
            event_type=event_type,
        )
    )
    await session.commit()
    return True


async def begin_consultation(
    session: AsyncSession, settings, user: User, *, chat_id: str | None
) -> tuple[Case, bool]:
    state = await get_mailing_state(session, user.id)
    if state is None:
        state = await ensure_mailing_started(session, user)
    case = await _case_for_user(session, user, chat_id)
    await record_action(
        session,
        settings,
        user,
        case,
        f"consult-click:{state.next_stage}",
        "mailing_consultation_clicked",
        "Система рассылок: пользователь нажал «Получить консультацию»",
    )
    if state.consultation_completed and not state.consultation_no:
        return case, False
    if not user.phone:
        state.awaiting_phone = True
        await session.commit()
        await record_action(
            session,
            settings,
            user,
            case,
            f"phone-request:{state.next_stage}",
            "mailing_phone_requested",
            "Система рассылок: запрошен номер телефона",
        )
        return case, False
    return case, True


async def save_campaign_phone(
    session: AsyncSession, settings, user: User, case: Case, raw_phone: str
) -> str:
    phone = normalize_phone(raw_phone)
    if not phone:
        raise ValueError("invalid phone")
    user.phone = phone
    state = await get_mailing_state(session, user.id)
    if state:
        state.awaiting_phone = False
    await session.commit()
    await record_action(
        session,
        settings,
        user,
        case,
        f"phone-received:{phone}",
        "mailing_phone_received",
        "Система рассылок: получен номер телефона из Telegram",
    )
    return phone


async def prepare_consultation(
    session: AsyncSession, settings, user: User, case: Case
) -> bool:
    """Persist and verify CRM phone and lead stage before Telegram confirmation."""
    state = await get_mailing_state(session, user.id)
    if state and state.consultation_completed and not state.consultation_no:
        return False
    crm = get_amocrm_service(settings)
    if settings.amocrm_enabled:
        contact_id = await crm.create_or_update_contact(user)
        if not contact_id or not await crm.verify_contact_phone(contact_id, user.phone or ""):
            raise RuntimeError("amoCRM contact phone was not persisted")
        user.amocrm_contact_id = contact_id
        case.amocrm_contact_id = contact_id
        await session.commit()
    await record_action(
        session,
        settings,
        user,
        case,
        f"phone-saved-amocrm:{user.phone}",
        "mailing_phone_saved_amocrm",
        "Система рассылок: номер телефона записан в контакт amoCRM",
    )
    await record_action(
        session,
        settings,
        user,
        case,
        f"consultation-stage:{state.next_stage if state else 1}",
        "mailing_consultation_stage",
        f"Система рассылок: сделка переведена в «{CONSULTATION_LOCATION}»",
        extra={"status_name_override": CONSULTATION_STATUS, "force_status": True},
    )
    if settings.amocrm_enabled and not await crm.verify_lead_status(case, CONSULTATION_STATUS):
        raise RuntimeError("amoCRM lead status was not persisted")
    return True


async def finish_consultation(
    session: AsyncSession, settings, user: User, case: Case
) -> None:
    state = await get_mailing_state(session, user.id)
    if state is None:
        state = await ensure_mailing_started(session, user)
    state.participating = False
    state.consultation_completed = True
    state.consultation_no = False
    state.awaiting_phone = False
    cancelled = await cancel_future_jobs(session, user.id)
    await session.commit()
    await record_action(
        session,
        settings,
        user,
        case,
        f"pavel-message:{state.next_stage}",
        "mailing_pavel_message_sent",
        "Система рассылок: отправлено сообщение с номером юрисконсульта Павла",
    )
    await record_action(
        session,
        settings,
        user,
        case,
        f"jobs-cancelled-consultation:{state.next_stage}",
        "mailing_jobs_cancelled",
        f"Система рассылок: отменены будущие задания рассылки ({cancelled})",
    )


async def disable_reminders(session: AsyncSession, settings, user: User) -> bool:
    state = await get_mailing_state(session, user.id)
    if state is None:
        state = await ensure_mailing_started(session, user)
    if state.reminders_disabled:
        return False
    state.reminders_disabled = True
    state.participating = False
    case = await _case_for_user(session, user)
    cancelled = await cancel_future_jobs(session, user.id)
    await session.commit()
    await record_action(
        session,
        settings,
        user,
        case,
        "reminders-disabled",
        "mailing_reminders_disabled",
        "Система рассылок: пользователь отключил напоминания",
    )
    await record_action(
        session,
        settings,
        user,
        case,
        "jobs-cancelled-disabled",
        "mailing_jobs_cancelled",
        f"Система рассылок: отменены будущие задания рассылки ({cancelled})",
    )
    return True


def _eligible(state: MailingState | None) -> bool:
    return bool(
        state
        and state.participating
        and not state.reminders_disabled
        and not state.consultation_completed
        and not state.excluded_sales
    )


async def _deliver_job(settings, bot: Bot | None, job_id: int) -> None:
    async with SessionLocal() as session:
        job = await session.get(MailingJob, job_id)
        if job is None or job.status != "pending":
            return
        claimed_at = datetime.utcnow()
        claim = await session.execute(
            update(MailingJob)
            .where(MailingJob.id == job_id, MailingJob.status == "pending")
            .values(
                status="sending",
                claimed_at=claimed_at,
                attempts=MailingJob.attempts + 1,
            )
        )
        await session.commit()
        if not claim.rowcount:
            return
        await session.refresh(job)
        state = await get_mailing_state(session, job.user_id)
        user = await session.get(User, job.user_id)
        if user is None or not _eligible(state):
            job.status = "cancelled"
            job.cancelled_at = datetime.utcnow()
            await session.commit()
            return
        telegram_markup = automatic_mailing_menu(allow_disable=job.stage >= 2)
        max_markup = max_keyboards.automatic_mailing_menu(allow_disable=job.stage >= 2)
        sent = await _send_user_message(
            settings,
            bot,
            user,
            stage_text(job.stage),
            telegram_markup=telegram_markup,
            max_keyboard=max_markup,
        )
        if not sent:
            job.status = "pending"
            job.due_at = datetime.utcnow() + timedelta(minutes=5)
            job.error_message = "delivery failed"
            await session.commit()
            return
        sent_at = datetime.utcnow()
        job.status = "sent"
        job.sent_at = sent_at
        job.error_message = None
        state.last_sent_stage = job.stage
        state.last_sent_at = sent_at
        state.next_stage = job.stage + 1
        await _schedule_job(session, state, state.next_stage, due_for_stage(state.next_stage, sent_at))
        await session.commit()
        case = await _case_for_user(session, user)
        await record_action(
            session,
            settings,
            user,
            case,
            f"message:{job.stage}",
            "mailing_message_sent",
            f"Система рассылок: отправлено сообщение №{job.stage}",
        )
        logger.info("Система рассылок: отправлено сообщение №%s user_id=%s", job.stage, user.id)


async def recover_uncertain_jobs(session: AsyncSession) -> None:
    """Advance old claimed sends without risking a duplicate Telegram message."""
    jobs = list(
        (await session.execute(select(MailingJob).where(MailingJob.status == "sending"))).scalars()
    )
    for job in jobs:
        state = await get_mailing_state(session, job.user_id)
        job.status = "sent"
        job.sent_at = job.claimed_at or datetime.utcnow()
        if state and state.next_stage <= job.stage:
            state.last_sent_stage = job.stage
            state.last_sent_at = job.sent_at
            state.next_stage = job.stage + 1
            await _schedule_job(session, state, state.next_stage, due_for_stage(state.next_stage, job.sent_at))
    await session.commit()


async def run_automatic_mailings(settings, bot: Bot | None = None) -> None:
    async with SessionLocal() as session:
        await recover_uncertain_jobs(session)
    while True:
        try:
            async with SessionLocal() as session:
                ids = list(
                    (await session.execute(
                        select(MailingJob.id)
                        .where(MailingJob.status == "pending", MailingJob.due_at <= datetime.utcnow())
                        .order_by(MailingJob.due_at, MailingJob.id)
                        .limit(100)
                    )).scalars()
                )
            for job_id in ids:
                await _deliver_job(settings, bot, job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic mailing worker failed")
        await asyncio.sleep(30)


def extract_amocrm_lead_ids(payload: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    leads = payload.get("leads") if isinstance(payload, dict) else None
    if isinstance(leads, dict):
        for group in leads.values():
            values = group.values() if isinstance(group, dict) else group if isinstance(group, list) else []
            for item in values:
                if isinstance(item, dict) and str(item.get("id") or "").isdigit():
                    result.add(int(item["id"]))
    direct = payload.get("lead_id") or payload.get("id")
    if str(direct or "").isdigit():
        result.add(int(direct))
    for key, value in payload.items():
        if str(key).startswith("leads[") and str(key).endswith("[id]") and str(value).isdigit():
            result.add(int(value))
    return result


async def process_amocrm_status_webhook(payload: dict[str, Any], bot: Bot | None, settings) -> None:
    """Reconcile campaign state from the actual amoCRM lead location."""
    for lead_id in extract_amocrm_lead_ids(payload):
        async with SessionLocal() as session:
            case = await session.scalar(
                select(Case).where(
                    (Case.amocrm_lead_id == lead_id) | (Case.amo_lead_id == lead_id)
                ).order_by(Case.id.desc()).limit(1)
            )
            if case is None:
                continue
            user = await session.get(User, case.user_id)
            state = await get_mailing_state(session, case.user_id)
            if user is None or state is None:
                continue
            crm = get_amocrm_service(settings)
            pipeline_name, status_name = await crm.get_lead_location(lead_id)
            if status_name == "Консультация - НО":
                if state.consultation_no:
                    continue
                sent = await _send_user_message(settings, bot, user, CONSULTATION_NO_MESSAGE)
                if not sent:
                    raise RuntimeError(f"could not deliver Consultation-NO message user_id={user.id}")
                state.participating = not state.reminders_disabled
                state.consultation_completed = False
                state.consultation_no = True
                state.excluded_sales = False
                if state.participating:
                    await _schedule_job(
                        session, state, state.next_stage, due_for_stage(state.next_stage, datetime.utcnow())
                    )
                await session.commit()
                await record_action(
                    session, settings, user, case, "consultation-no", "mailing_consultation_no",
                    "Система рассылок: сделка переведена в «Консультация - НО», пользователь возвращен в рассылку",
                )
                await record_action(
                    session, settings, user, case, "consultation-no-message", "mailing_consultation_no_message",
                    "Система рассылок: отправлено сообщение «Вам звонил юрисконсульт Павел...»",
                )
            elif pipeline_name == "Отдел продаж":
                if state.excluded_sales:
                    continue
                state.participating = False
                state.consultation_no = False
                state.excluded_sales = True
                await cancel_future_jobs(session, user.id)
                await session.commit()
                await record_action(
                    session, settings, user, case, "sales-excluded", "mailing_sales_excluded",
                    "Система рассылок: пользователь исключен из рассылки из-за перехода в «Отдел продаж»",
                )
                await record_action(
                    session, settings, user, case, "jobs-cancelled-sales", "mailing_jobs_cancelled",
                    "Система рассылок: отменены будущие задания рассылки",
                )
