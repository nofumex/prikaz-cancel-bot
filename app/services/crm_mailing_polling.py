from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Case, CrmDealNotification, MailingState, User
from app.services.amocrm import get_amocrm_service
from app.services.automatic_mailings import (
    _schedule_job,
    cancel_future_jobs,
    due_for_stage,
    get_mailing_state,
    record_action,
)
from app.services.reminders import _send_user_message
from app.utils import h

logger = logging.getLogger(__name__)

LAWYER_CALL = "lawyer_call"
POLL_INTERVAL_SECONDS = 60
DELIVERY_LEASE = timedelta(minutes=5)
NO_STATUS_NAMES = ("Консультация-НО", "Консультация - НО")


async def _local_deal(
    session: AsyncSession, deal_id: int
) -> tuple[Case | None, User | None, MailingState | None]:
    case = await session.scalar(
        select(Case)
        .where((Case.amocrm_lead_id == deal_id) | (Case.amo_lead_id == deal_id))
        .order_by(Case.id.desc())
        .limit(1)
    )
    if case is None:
        return None, None, None
    return case, await session.get(User, case.user_id), await get_mailing_state(session, case.user_id)


async def _ensure_notification(
    session: AsyncSession,
    *,
    deal_id: int,
    case: Case,
    user: User,
    lawyer_name: str,
    lawyer_phone: str,
) -> tuple[CrmDealNotification, bool]:
    existing = await session.scalar(
        select(CrmDealNotification).where(
            CrmDealNotification.amocrm_deal_id == deal_id,
            CrmDealNotification.notification_type == LAWYER_CALL,
        )
    )
    if existing is not None:
        return existing, False
    message = (
        f"Вам звонил юрисконсульт {h(lawyer_name)} с номера {h(lawyer_phone)}. "
        "Перезвоните ему."
    )
    row = CrmDealNotification(
        amocrm_deal_id=deal_id,
        notification_type=LAWYER_CALL,
        user_id=user.id,
        case_id=case.id,
        lawyer_name=lawyer_name,
        lawyer_phone=lawyer_phone,
        message_text=message,
        status="pending",
    )
    session.add(row)
    try:
        await session.commit()
        return row, True
    except IntegrityError:
        await session.rollback()
        row = await session.scalar(
            select(CrmDealNotification).where(
                CrmDealNotification.amocrm_deal_id == deal_id,
                CrmDealNotification.notification_type == LAWYER_CALL,
            )
        )
        if row is None:
            raise
        return row, False


async def _deliver_notification(
    session: AsyncSession,
    settings,
    bot: Bot | None,
    notification_id: int,
) -> bool:
    now = datetime.utcnow()
    claim = await session.execute(
        update(CrmDealNotification)
        .where(
            CrmDealNotification.id == notification_id,
            CrmDealNotification.status == "pending",
        )
        .values(
            status="sending",
            claimed_at=now,
            lease_until=now + DELIVERY_LEASE,
            attempts=CrmDealNotification.attempts + 1,
            error_message=None,
        )
    )
    await session.commit()
    if not claim.rowcount:
        return False
    row = await session.get(CrmDealNotification, notification_id)
    case = await session.get(Case, row.case_id)
    user = await session.get(User, row.user_id)
    state = await get_mailing_state(session, row.user_id)
    if case is None or user is None or state is None:
        raise RuntimeError(f"CRM notification target missing id={notification_id}")
    if state.reminders_disabled or state.excluded_sales:
        row.status = "cancelled"
        row.lease_until = None
        await session.commit()
        return False

    crm = get_amocrm_service(settings)
    pipeline_name, status_name = await crm.get_lead_location(row.amocrm_deal_id)
    if status_name not in NO_STATUS_NAMES or pipeline_name == "Отдел продаж":
        row.status = "cancelled"
        row.lease_until = None
        await session.commit()
        return False

    sent = await _send_user_message(settings, bot, user, row.message_text)
    if not sent:
        row.status = "uncertain"
        row.uncertain_at = datetime.utcnow()
        row.lease_until = None
        row.error_message = "delivery outcome was not confirmed"
        await session.commit()
        return False

    await _mark_notification_sent(
        session, settings, row, case, user, state, datetime.utcnow()
    )
    return True


async def _mark_notification_sent(
    session: AsyncSession,
    settings,
    row: CrmDealNotification,
    case: Case,
    user: User,
    state: MailingState,
    sent_at: datetime,
) -> None:
    row.status = "sent"
    row.sent_at = sent_at
    row.lease_until = None
    row.error_message = None
    state.participating = not state.reminders_disabled
    state.consultation_completed = False
    state.consultation_no = True
    state.excluded_sales = False
    if state.participating:
        await _schedule_job(session, state, state.next_stage, due_for_stage(state.next_stage, sent_at))
    await session.commit()
    await record_action(
        session,
        settings,
        user,
        case,
        f"poll-lawyer-call-sent:{row.amocrm_deal_id}",
        "mailing_consultation_no_message",
        f"Система рассылок: отправлено уведомление пользователю по сделке {row.amocrm_deal_id}",
    )


async def resolve_uncertain_notification(
    session: AsyncSession,
    settings,
    notification_id: int,
    *,
    delivered: bool,
    delivered_at: datetime | None = None,
) -> bool:
    """Explicitly reconcile the transport crash window without a blind resend."""
    row = await session.get(CrmDealNotification, notification_id)
    if row is None or row.status != "uncertain":
        return False
    if not delivered:
        row.status = "pending"
        row.uncertain_at = None
        row.error_message = None
        await session.commit()
        return True
    case = await session.get(Case, row.case_id)
    user = await session.get(User, row.user_id)
    state = await get_mailing_state(session, row.user_id)
    if case is None or user is None or state is None:
        raise RuntimeError(f"CRM notification target missing id={notification_id}")
    await _mark_notification_sent(
        session,
        settings,
        row,
        case,
        user,
        state,
        delivered_at or row.claimed_at or datetime.utcnow(),
    )
    return True


async def recover_notification_leases(session: AsyncSession) -> None:
    now = datetime.utcnow()
    await session.execute(
        update(CrmDealNotification)
        .where(
            CrmDealNotification.status == "sending",
            or_(
                CrmDealNotification.lease_until.is_(None),
                CrmDealNotification.lease_until < now,
            ),
        )
        .values(
            status="uncertain",
            uncertain_at=now,
            lease_until=None,
            error_message="polling worker lease expired during external delivery",
        )
    )
    await session.commit()


async def _process_consultation_no_lead(
    session: AsyncSession, settings, bot: Bot | None, lead: dict
) -> None:
    deal_id = int(lead["id"])
    case, user, state = await _local_deal(session, deal_id)
    if case is None or user is None or state is None:
        return
    if state.reminders_disabled or state.excluded_sales:
        return
    crm = get_amocrm_service(settings)
    lawyer_name, lawyer_phone = await crm.get_lead_lawyer(lead)
    row, created = await _ensure_notification(
        session,
        deal_id=deal_id,
        case=case,
        user=user,
        lawyer_name=lawyer_name,
        lawyer_phone=lawyer_phone,
    )
    if created:
        await record_action(
            session,
            settings,
            user,
            case,
            f"poll-consultation-no-detected:{deal_id}",
            "mailing_consultation_no",
            f"Система рассылок: обнаружена сделка {deal_id} в «Консультация-НО»",
        )
    elif row.status == "sent":
        await record_action(
            session,
            settings,
            user,
            case,
            f"poll-lawyer-call-skipped:{deal_id}",
            "mailing_consultation_no_duplicate",
            f"Система рассылок: уведомление по сделке {deal_id} пропущено — уже отправлено",
        )
        return
    await _deliver_notification(session, settings, bot, row.id)


async def _process_sales_lead(session: AsyncSession, settings, lead: dict) -> None:
    deal_id = int(lead["id"])
    case, user, state = await _local_deal(session, deal_id)
    if case is None or user is None or state is None or state.excluded_sales:
        return
    state.participating = False
    state.consultation_no = False
    state.excluded_sales = True
    await cancel_future_jobs(session, user.id)
    await session.execute(
        update(CrmDealNotification)
        .where(
            CrmDealNotification.amocrm_deal_id == deal_id,
            CrmDealNotification.status == "pending",
        )
        .values(status="cancelled", lease_until=None)
    )
    await session.commit()
    await record_action(
        session,
        settings,
        user,
        case,
        f"poll-sales-excluded:{deal_id}",
        "mailing_sales_excluded",
        "Система рассылок: пользователь исключен из рассылки из-за перехода в «Отдел продаж»",
    )


async def poll_crm_mailing_once(settings, bot: Bot | None = None) -> None:
    crm = get_amocrm_service(settings)
    no_leads = await crm.list_leads_in_status(settings.amocrm_pipeline_name, NO_STATUS_NAMES)
    sales_leads = await crm.list_leads_in_pipeline("Отдел продаж")
    async with SessionLocal() as session:
        await recover_notification_leases(session)
        for lead in no_leads:
            await _process_consultation_no_lead(session, settings, bot, lead)
        for lead in sales_leads:
            await _process_sales_lead(session, settings, lead)


async def run_crm_mailing_polling(settings, bot: Bot | None = None) -> None:
    while True:
        try:
            await poll_crm_mailing_once(settings, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("amoCRM mailing polling cycle failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
