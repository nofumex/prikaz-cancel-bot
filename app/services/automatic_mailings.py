from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from calendar import monthrange
from datetime import datetime, timedelta
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.max import keyboards as max_keyboards
from app.database import SessionLocal
from app.keyboards.common import automatic_mailing_menu
from app.models import Case, MailingAction, MailingJob, MailingState, PavelMessageDelivery, User
from app.services.amocrm import (
    AmoNoteVerificationPending,
    PIPELINE_STATUSES,
    get_amocrm_service,
    sanitize_visible_mailing_note,
)
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
CONSULTATION_NO_STATUSES = {"Консультация-НО", "Консультация - НО"}
SALES_PIPELINE = "Отдел продаж"

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
ACTION_LEASE = timedelta(minutes=5)
JOB_LEASE = timedelta(minutes=5)
PAVEL_DELIVERY_LEASE = timedelta(minutes=5)
MAILING_DELIVERY_CONCURRENCY = 10
MAILING_ACTION_CONCURRENCY = 8


def _visible_mailing_note(note: str, occurred_at: datetime, action_id: int) -> str:
    """Give each real event a readable, stable identity without technical markers."""
    timestamp = occurred_at.strftime("%d.%m.%Y %H:%M:%S UTC")
    note = sanitize_visible_mailing_note(note)
    return (
        f"{note.strip()}\n\n"
        f"Время события: {timestamp}\n"
        f"Запись рассылки №{action_id}"
    )


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
    execute_immediately: bool = True,
) -> bool:
    """Atomically reserve a human-readable amoCRM note and optionally send it now."""
    occurred_at = datetime.utcnow()
    payload = {
        "note": note,
        "mailing_event_at": occurred_at.isoformat(timespec="microseconds"),
        "mailing_action_key": action_key,
        **(extra or {}),
    }
    action = await session.scalar(
        select(MailingAction).where(
            MailingAction.user_id == user.id, MailingAction.action_key == action_key
        )
    )
    if action is None:
        action = MailingAction(
            user_id=user.id,
            case_id=case.id,
            action_key=action_key,
            event_type=event_type,
            note_text=note,
            payload_json=None,
            status="pending",
        )
        session.add(action)
        try:
            await session.flush()
            payload["mailing_note_text"] = _visible_mailing_note(
                note, occurred_at, action.id
            )
            action.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            action = await session.scalar(
                select(MailingAction).where(
                    MailingAction.user_id == user.id, MailingAction.action_key == action_key
                )
            )
            if action is None:
                raise
    if not execute_immediately:
        return True
    return await _execute_action(session, settings, action.id)


async def _execute_action(session: AsyncSession, settings, action_id: int) -> bool:
    now = datetime.utcnow()
    lease_until = now + ACTION_LEASE
    current = await session.get(MailingAction, action_id)
    verification_only = bool(current and current.status == "verifying")
    claim = await session.execute(
        update(MailingAction)
        .where(
            MailingAction.id == action_id,
            MailingAction.status != "completed",
            or_(
                MailingAction.status == "pending",
                MailingAction.lease_until.is_(None),
                MailingAction.lease_until < now,
            ),
        )
        .values(
            status="processing",
            lease_until=lease_until,
            attempts=MailingAction.attempts + 1,
            error_message=None,
        )
    )
    await session.commit()
    if not claim.rowcount:
        return False
    action = await session.get(MailingAction, action_id)
    case = await session.get(Case, action.case_id) if action and action.case_id else None
    user = await session.get(User, action.user_id) if action else None
    if action is None or case is None or user is None:
        raise RuntimeError(f"mailing action target is missing action_id={action_id}")
    try:
        payload = json.loads(action.payload_json or "{}")
        if not payload.get("mailing_note_text"):
            # Upgrade already queued actions from older deployments. created_at
            # is stable, so retries verify the same visible note every time.
            occurred_at = action.created_at or datetime.utcnow()
            payload["mailing_note_text"] = _visible_mailing_note(
                str(payload.get("note") or action.note_text or "Обновление рассылки."),
                occurred_at,
                action.id,
            )
            payload.pop("mailing_marker", None)
            action.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            await session.commit()
        crm = get_amocrm_service(settings)
        if verification_only:
            note_text = sanitize_visible_mailing_note(
                str(payload.get("mailing_note_text") or "")
            )
            lead_id = case.amocrm_lead_id or case.amo_lead_id
            if not note_text or not lead_id or not await crm.lead_note_has_text(int(lead_id), note_text):
                await session.execute(
                    update(MailingAction)
                    .where(MailingAction.id == action_id)
                    .values(status="verifying", lease_until=datetime.utcnow() + ACTION_LEASE)
                )
                await session.commit()
                return False
        else:
            await crm.sync_case_event(session, case, user, action.event_type, payload)
    except AmoNoteVerificationPending as exc:
        await session.execute(
            update(MailingAction)
            .where(MailingAction.id == action_id)
            .values(
                status="verifying",
                lease_until=datetime.utcnow() + ACTION_LEASE,
                error_message=str(exc)[:2000],
            )
        )
        await session.commit()
        return False
    except Exception as exc:
        await session.execute(
            update(MailingAction)
            .where(MailingAction.id == action_id, MailingAction.status == "processing")
            .values(
                status="pending",
                lease_until=None,
                error_message=str(exc)[:2000],
            )
        )
        await session.commit()
        raise
    await session.execute(
        update(MailingAction)
        .where(MailingAction.id == action_id)
        .values(
            status="completed",
            completed_at=datetime.utcnow(),
            lease_until=None,
            error_message=None,
        )
    )
    await session.commit()
    return True


async def retry_pending_actions(session: AsyncSession, settings, *, limit: int = 100) -> None:
    now = datetime.utcnow()
    rows = list(
        (await session.execute(
            select(MailingAction.id, MailingAction.user_id)
            .where(
                MailingAction.status != "completed",
                or_(
                    MailingAction.status == "pending",
                    MailingAction.lease_until.is_(None),
                    MailingAction.lease_until < now,
                ),
            )
            .order_by(MailingAction.id)
            .limit(limit)
        )).all()
    )
    by_user: dict[int, list[int]] = {}
    for action_id, user_id in rows:
        by_user.setdefault(int(user_id), []).append(int(action_id))
    semaphore = asyncio.Semaphore(MAILING_ACTION_CONCURRENCY)

    async def retry_user_actions(user_id: int, action_ids: list[int]) -> None:
        async with semaphore, SessionLocal() as action_session:
            # Keep causal order for one client, while unrelated clients sync in parallel.
            for action_id in action_ids:
                try:
                    await _execute_action(action_session, settings, action_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Mailing CRM action retry failed user_id=%s action_id=%s",
                        user_id,
                        action_id,
                    )

    await asyncio.gather(
        *(retry_user_actions(user_id, action_ids) for user_id, action_ids in by_user.items())
    )


async def resolve_verifying_action(
    session: AsyncSession, action_id: int, *, note_exists: bool
) -> bool:
    """Resolve an amoCRM POST whose transport outcome could not be proven."""
    action = await session.get(MailingAction, action_id)
    if action is None or action.status != "verifying":
        return False
    action.status = "completed" if note_exists else "pending"
    action.completed_at = datetime.utcnow() if note_exists else None
    action.lease_until = None
    action.error_message = None
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
        "Клиент нажал кнопку «Получить консультацию». Заявка передана юрисконсульту.",
        execute_immediately=False,
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
            "Для консультации у клиента запрошен номер телефона.",
            execute_immediately=False,
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
        f"Клиент указал номер телефона для консультации: {phone}.",
        execute_immediately=False,
    )
    return phone


async def prepare_consultation(
    session: AsyncSession, settings, user: User, case: Case
) -> bool:
    """Queue CRM updates without delaying the confirmation shown to the client."""
    state = await get_mailing_state(session, user.id)
    if state and state.consultation_completed and not state.consultation_no:
        return False
    await record_action(
        session,
        settings,
        user,
        case,
        f"phone-saved-amocrm:{user.phone}",
        "mailing_phone_saved_amocrm",
        f"Номер телефона клиента для консультации: {user.phone}.",
        execute_immediately=False,
    )
    await record_action(
        session,
        settings,
        user,
        case,
        f"consultation-stage:{state.next_stage if state else 1}",
        "mailing_consultation_stage",
        f"Клиент запросил консультацию. Сделка переводится в этап «{CONSULTATION_STATUS}».",
        extra={"status_name_override": CONSULTATION_STATUS, "force_status": True},
        execute_immediately=False,
    )
    return True


async def finish_consultation(
    session: AsyncSession,
    settings,
    user: User,
    case: Case,
    *,
    consultation_key: str | None = None,
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
        f"pavel-message:{consultation_key or state.next_stage}",
        "mailing_pavel_message_sent",
        "Клиенту отправлено уведомление о звонке юрисконсульта.\n\n"
        f"Текст сообщения:\n{plain_message_text(PAVEL_MESSAGE)}",
        execute_immediately=False,
    )
    await record_action(
        session,
        settings,
        user,
        case,
        f"jobs-cancelled-consultation:{consultation_key or state.next_stage}",
        "mailing_jobs_cancelled",
        f"Регулярная рассылка с предложением консультации приостановлена. Отменено заданий: {cancelled}.",
        execute_immediately=False,
    )


def pavel_consultation_key(state: MailingState, case: Case) -> str:
    cycle = state.consultation_cycle if state.consultation_no else 0
    return f"case:{case.id}:stage:{state.next_stage}:consultation-cycle:{cycle}"


async def _ensure_pavel_delivery(
    session: AsyncSession,
    user: User,
    case: Case,
    state: MailingState,
) -> PavelMessageDelivery:
    consultation_key = pavel_consultation_key(state, case)
    row = await session.scalar(
        select(PavelMessageDelivery).where(
            PavelMessageDelivery.user_id == user.id,
            PavelMessageDelivery.consultation_key == consultation_key,
        )
    )
    if row is not None:
        return row
    row = PavelMessageDelivery(
        user_id=user.id,
        case_id=case.id,
        consultation_key=consultation_key,
        status="pending",
    )
    session.add(row)
    try:
        await session.commit()
        return row
    except IntegrityError:
        await session.rollback()
        row = await session.scalar(
            select(PavelMessageDelivery).where(
                PavelMessageDelivery.user_id == user.id,
                PavelMessageDelivery.consultation_key == consultation_key,
            )
        )
        if row is None:
            raise
        return row


async def recover_pavel_delivery_leases(session: AsyncSession) -> None:
    now = datetime.utcnow()
    await session.execute(
        update(PavelMessageDelivery)
        .where(
            PavelMessageDelivery.status == "sending",
            or_(
                PavelMessageDelivery.lease_until.is_(None),
                PavelMessageDelivery.lease_until < now,
            ),
        )
        .values(
            status="uncertain",
            uncertain_at=now,
            lease_until=None,
            error_message="worker lease expired during Pavel message delivery",
        )
    )
    await session.commit()


async def deliver_pavel_message(
    session: AsyncSession,
    settings,
    user: User,
    case: Case,
    send: Callable[[], Awaitable[Any]],
) -> bool:
    """Send PAVEL_MESSAGE once, then complete the consultation durably."""
    state = await get_mailing_state(session, user.id)
    if state is None:
        state = await ensure_mailing_started(session, user)
    row = await _ensure_pavel_delivery(session, user, case, state)
    now = datetime.utcnow()
    await session.execute(
        update(PavelMessageDelivery)
        .where(
            PavelMessageDelivery.id == row.id,
            PavelMessageDelivery.status == "sending",
            or_(
                PavelMessageDelivery.lease_until.is_(None),
                PavelMessageDelivery.lease_until < now,
            ),
        )
        .values(
            status="uncertain",
            uncertain_at=now,
            lease_until=None,
            error_message="worker lease expired during Pavel message delivery",
        )
    )
    await session.commit()
    await session.refresh(row)
    if row.status == "sent":
        await finish_consultation(
            session,
            settings,
            user,
            case,
            consultation_key=row.consultation_key,
        )
        return False
    if row.status != "pending":
        return False

    claimed_at = datetime.utcnow()
    claim = await session.execute(
        update(PavelMessageDelivery)
        .where(
            PavelMessageDelivery.id == row.id,
            PavelMessageDelivery.status == "pending",
        )
        .values(
            status="sending",
            claimed_at=claimed_at,
            lease_until=claimed_at + PAVEL_DELIVERY_LEASE,
            attempts=PavelMessageDelivery.attempts + 1,
            error_message=None,
        )
    )
    await session.commit()
    if not claim.rowcount:
        return False
    await session.refresh(row)
    try:
        result = await send()
    except Exception as exc:
        row.status = "uncertain"
        row.uncertain_at = datetime.utcnow()
        row.lease_until = None
        row.error_message = str(exc)[:2000]
        await session.commit()
        logger.exception("Pavel message delivery outcome is uncertain delivery_id=%s", row.id)
        return False
    if not result:
        row.status = "uncertain"
        row.uncertain_at = datetime.utcnow()
        row.lease_until = None
        row.error_message = "delivery outcome was not confirmed"
        await session.commit()
        return False

    row.status = "sent"
    row.sent_at = datetime.utcnow()
    row.lease_until = None
    row.uncertain_at = None
    row.error_message = None
    await session.commit()
    await finish_consultation(
        session,
        settings,
        user,
        case,
        consultation_key=row.consultation_key,
    )
    return True


async def resolve_uncertain_pavel_delivery(
    session: AsyncSession,
    settings,
    delivery_id: int,
    *,
    delivered: bool,
    delivered_at: datetime | None = None,
) -> bool:
    row = await session.get(PavelMessageDelivery, delivery_id)
    if row is None or row.status != "uncertain":
        return False
    if not delivered:
        row.status = "pending"
        row.uncertain_at = None
        row.error_message = None
        await session.commit()
        return True
    row.status = "sent"
    row.sent_at = delivered_at or row.claimed_at or datetime.utcnow()
    row.uncertain_at = None
    row.lease_until = None
    row.error_message = None
    await session.commit()
    user = await session.get(User, row.user_id)
    case = await session.get(Case, row.case_id)
    if user is None or case is None:
        raise RuntimeError(f"Pavel delivery target is missing delivery_id={delivery_id}")
    await finish_consultation(
        session,
        settings,
        user,
        case,
        consultation_key=row.consultation_key,
    )
    return True


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
        "Клиент отключил регулярную рассылку с предложением консультации.",
        execute_immediately=False,
    )
    await record_action(
        session,
        settings,
        user,
        case,
        "jobs-cancelled-disabled",
        "mailing_jobs_cancelled",
        f"Будущие сообщения регулярной рассылки отменены. Отменено заданий: {cancelled}.",
        execute_immediately=False,
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


def plain_message_text(text: str) -> str:
    """Render a bot HTML message as readable plain text for an amoCRM note."""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


async def exclude_user_for_sales(
    session: AsyncSession,
    settings,
    user: User,
    case: Case,
    state: MailingState,
    deal_id: int,
) -> None:
    """Persist the sales exclusion and its CRM notes idempotently."""
    state.participating = False
    state.consultation_no = False
    state.excluded_sales = True
    await cancel_future_jobs(session, user.id)
    await session.commit()
    actions = ((
        f"poll-sales-excluded:{deal_id}",
        "mailing_sales_excluded",
        "Регулярная рассылка с кнопкой «Получить консультацию» приостановлена, "
        "потому что сделка переведена в воронку «Отдел продаж». Остальные уведомления продолжают работать.",
    ),)
    for action_key, event_type, note in actions:
        try:
            await record_action(
                session, settings, user, case, action_key, event_type, note,
                execute_immediately=False,
            )
        except Exception:
            # record_action has already persisted a retryable outbox row.
            logger.exception(
                "Sales exclusion CRM note failed deal_id=%s action_key=%s",
                deal_id,
                action_key,
            )


async def _crm_case_for_user(session: AsyncSession, user: User) -> Case | None:
    if user.amocrm_current_case_id:
        current = await session.get(Case, user.amocrm_current_case_id)
        if current is not None and (current.amocrm_lead_id or current.amo_lead_id):
            return current
    return await session.scalar(
        select(Case)
        .where(
            Case.user_id == user.id,
            or_(Case.amocrm_lead_id.is_not(None), Case.amo_lead_id.is_not(None)),
        )
        .order_by(Case.id.desc())
        .limit(1)
    )


async def _crm_allows_job_delivery(
    session: AsyncSession,
    settings,
    job: MailingJob,
    user: User,
    state: MailingState,
) -> bool:
    if not settings.amocrm_enabled:
        return True
    case = await _crm_case_for_user(session, user)
    if case is None:
        raise RuntimeError(f"amoCRM deal is missing for mailing user_id={user.id}")
    deal_id = int(case.amocrm_lead_id or case.amo_lead_id)
    pipeline_name, status_name = await get_amocrm_service(settings).get_lead_location(deal_id)
    if not pipeline_name or not status_name:
        raise RuntimeError(f"amoCRM location is incomplete for deal_id={deal_id}")

    allowed_statuses = (set(PIPELINE_STATUSES) - {CONSULTATION_STATUS}) | CONSULTATION_NO_STATUSES
    sales_exclusion = pipeline_name == SALES_PIPELINE
    other_exclusion = (
        pipeline_name != settings.amocrm_pipeline_name or status_name not in allowed_statuses
    )
    if not sales_exclusion and not other_exclusion:
        return True

    job.status = "cancelled"
    job.cancelled_at = datetime.utcnow()
    job.lease_until = None
    if sales_exclusion:
        await exclude_user_for_sales(session, settings, user, case, state, deal_id)
    else:
        state.participating = False
        if status_name == CONSULTATION_STATUS:
            state.consultation_completed = True
            state.consultation_no = False
        await cancel_future_jobs(session, user.id)
        await session.commit()
    return False


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
                lease_until=claimed_at + JOB_LEASE,
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
            job.lease_until = None
            await session.commit()
            return
        try:
            if not await _crm_allows_job_delivery(session, settings, job, user, state):
                return
        except Exception as exc:
            # No external message was attempted, so this claim can be retried safely.
            job.status = "pending"
            job.claimed_at = None
            job.lease_until = None
            job.error_message = str(exc)[:2000]
            await session.commit()
            logger.exception("Mailing CRM preflight failed job_id=%s", job_id)
            return
        # The CRM preflight can take seconds. Re-read local state immediately
        # before the irreversible Bot API call so a concurrent disable,
        # consultation click or sales exclusion wins the race.
        await session.refresh(state)
        if not _eligible(state):
            job.status = "cancelled"
            job.cancelled_at = datetime.utcnow()
            job.lease_until = None
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
            # A transport error cannot prove whether Telegram/MAX accepted the
            # message. Blind retry could duplicate it, so quarantine for an
            # explicit delivered/not-delivered reconciliation.
            job.status = "uncertain"
            job.uncertain_at = datetime.utcnow()
            job.lease_until = None
            job.error_message = "delivery outcome was not confirmed"
            await session.commit()
            return
        await _complete_job(session, settings, job, user, state, datetime.utcnow())


async def _complete_job(
    session: AsyncSession,
    settings,
    job: MailingJob,
    user: User,
    state: MailingState,
    sent_at: datetime,
) -> None:
    job.status = "sent"
    job.sent_at = sent_at
    job.lease_until = None
    job.uncertain_at = None
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
        f"Клиенту отправлено сообщение регулярной рассылки №{job.stage}.\n\n"
        f"Текст сообщения:\n{plain_message_text(stage_text(job.stage))}",
        execute_immediately=False,
    )
    logger.info("Система рассылок: отправлено сообщение №%s user_id=%s", job.stage, user.id)


async def recover_uncertain_jobs(session: AsyncSession) -> None:
    """Quarantine expired leases; never infer delivery merely from a claim."""
    now = datetime.utcnow()
    jobs = list((await session.execute(
        select(MailingJob).where(
            MailingJob.status == "sending",
            or_(MailingJob.lease_until.is_(None), MailingJob.lease_until < now),
        )
    )).scalars())
    for job in jobs:
        job.status = "uncertain"
        job.uncertain_at = now
        job.lease_until = None
        job.error_message = "worker lease expired during external delivery"
    await session.commit()


async def resolve_uncertain_job(
    session: AsyncSession,
    settings,
    job_id: int,
    *,
    delivered: bool,
    delivered_at: datetime | None = None,
) -> bool:
    """Operator/reconciliation hook for the unavoidable Bot API crash window."""
    job = await session.get(MailingJob, job_id)
    if job is None or job.status != "uncertain":
        return False
    if not delivered:
        job.status = "pending"
        job.due_at = datetime.utcnow()
        job.uncertain_at = None
        job.error_message = None
        await session.commit()
        return True
    user = await session.get(User, job.user_id)
    state = await get_mailing_state(session, job.user_id)
    if user is None or state is None:
        raise RuntimeError(f"mailing job target is missing job_id={job_id}")
    await _complete_job(
        session, settings, job, user, state, delivered_at or job.claimed_at or datetime.utcnow()
    )
    return True


async def run_automatic_mailings(settings, bot: Bot | None = None) -> None:
    while True:
        try:
            async with SessionLocal() as session:
                await recover_uncertain_jobs(session)
                await recover_pavel_delivery_leases(session)
                ids = list(
                    (await session.execute(
                        select(MailingJob.id)
                        .where(MailingJob.status == "pending", MailingJob.due_at <= datetime.utcnow())
                        .order_by(MailingJob.due_at, MailingJob.id)
                        .limit(100)
                    )).scalars()
                )

            async def retry_actions() -> None:
                async with SessionLocal() as action_session:
                    await retry_pending_actions(action_session, settings)

            delivery_semaphore = asyncio.Semaphore(MAILING_DELIVERY_CONCURRENCY)

            async def deliver_job_safely(job_id: int) -> None:
                async with delivery_semaphore:
                    try:
                        await _deliver_job(settings, bot, job_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Automatic mailing delivery failed job_id=%s", job_id)

            # Slow amoCRM note synchronization must never hold up due client
            # deliveries. Each delivery owns its DB session and runs independently.
            await asyncio.gather(
                retry_actions(),
                *(deliver_job_safely(job_id) for job_id in ids),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic mailing worker failed")
        await asyncio.sleep(30)
