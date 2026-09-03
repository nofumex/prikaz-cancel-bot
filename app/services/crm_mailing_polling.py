from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Case, CrmDealNotification, MailingJob, MailingState, User
from app.services.amocrm import get_amocrm_service
from app.services.automatic_mailings import (
    _schedule_job,
    due_for_stage,
    exclude_user_for_sales,
    get_mailing_state,
    record_action,
)
from app.services.reminders import _send_user_message
from app.utils import h

logger = logging.getLogger(__name__)

LAWYER_CALL = "lawyer_call"
POLL_INTERVAL_SECONDS = 5
DELIVERY_LEASE = timedelta(minutes=5)
NO_STATUS_NAMES = ("Консультация-НО", "Консультация - НО")
POLLING_CONCURRENCY = 8


def _is_consultation_no_location(settings, pipeline_name: str | None, status_name: str | None) -> bool:
    return pipeline_name == settings.amocrm_pipeline_name and status_name in NO_STATUS_NAMES


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
    new_cycle: bool = False,
) -> tuple[CrmDealNotification, bool]:
    existing = await session.scalar(
        select(CrmDealNotification).where(
            CrmDealNotification.amocrm_deal_id == deal_id,
            CrmDealNotification.notification_type == LAWYER_CALL,
        )
    )
    if existing is not None:
        should_rearm = existing.status == "cancelled" or (
            new_cycle and existing.status == "sent"
        )
        if should_rearm:
            next_message = (
                f"Вам звонил юрисконсульт {h(lawyer_name)} с номера {h(lawyer_phone)}. "
                "Перезвоните ему."
            )
            rearmed = await session.execute(
                update(CrmDealNotification)
                .where(
                    CrmDealNotification.id == existing.id,
                    CrmDealNotification.status == existing.status,
                )
                .values(
                    cycle=CrmDealNotification.cycle + 1,
                    lawyer_name=lawyer_name,
                    lawyer_phone=lawyer_phone,
                    message_text=next_message,
                    status="pending",
                    claimed_at=None,
                    lease_until=None,
                    sent_at=None,
                    uncertain_at=None,
                    error_message=None,
                )
            )
            await session.commit()
            await session.refresh(existing)
            return existing, bool(rearmed.rowcount)
        return existing, False
    message = (
        f"Вам звонил юрисконсульт {h(lawyer_name)} с номера {h(lawyer_phone)}. "
        "Перезвоните ему."
    )
    row = CrmDealNotification(
        amocrm_deal_id=deal_id,
        notification_type=LAWYER_CALL,
        cycle=1,
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
    await session.refresh(state)
    if state.reminders_disabled:
        row.status = "cancelled"
        row.lease_until = None
        await session.commit()
        return False

    crm = get_amocrm_service(settings)
    pipeline_name, status_name = await crm.get_lead_location(row.amocrm_deal_id)
    if not _is_consultation_no_location(settings, pipeline_name, status_name):
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

    # The deal may move while the Bot API request is in flight. The one-off
    # message remains delivered, but the regular campaign is resumed only if
    # amoCRM still confirms Consultation-NO.
    pipeline_name, status_name = await crm.get_lead_location(row.amocrm_deal_id)
    await _mark_notification_sent(
        session,
        settings,
        row,
        case,
        user,
        state,
        datetime.utcnow(),
        resume_campaign=_is_consultation_no_location(settings, pipeline_name, status_name),
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
    *,
    resume_campaign: bool = True,
) -> None:
    await session.refresh(state)
    row.status = "sent"
    row.sent_at = sent_at
    row.lease_until = None
    row.error_message = None
    if resume_campaign:
        state.participating = not state.reminders_disabled
        state.consultation_completed = False
        state.consultation_no = True
        state.consultation_cycle = row.cycle
        state.excluded_sales = False
        if state.participating:
            await _resume_saved_mailing_position(session, state, sent_at)
    await session.commit()
    await record_action(
        session,
        settings,
        user,
        case,
        f"poll-lawyer-call-sent:{row.amocrm_deal_id}:cycle:{row.cycle}",
        "mailing_consultation_no_message",
        "Клиенту отправлено уведомление после пропущенного звонка юрисконсульта.\n\n"
        f"Текст сообщения:\n{row.message_text}",
        execute_immediately=False,
    )


async def _resume_saved_mailing_position(
    session: AsyncSession, state: MailingState, resumed_at: datetime
) -> None:
    """Resume the exact next stage without ever firing an overdue message immediately."""
    job = await session.scalar(
        select(MailingJob).where(
            MailingJob.user_id == state.user_id,
            MailingJob.stage == state.next_stage,
        )
    )
    safe_due_at = due_for_stage(state.next_stage, resumed_at)
    if job is None:
        await _schedule_job(session, state, state.next_stage, safe_due_at)
        return
    if job.status == "cancelled":
        job.status = "pending"
        job.cancelled_at = None
        job.error_message = None
        job.claimed_at = None
        job.lease_until = None
    if job.status == "pending" and job.due_at <= resumed_at:
        job.due_at = safe_due_at


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
    pipeline_name, status_name = await get_amocrm_service(settings).get_lead_location(
        row.amocrm_deal_id
    )
    await _mark_notification_sent(
        session,
        settings,
        row,
        case,
        user,
        state,
        delivered_at or row.claimed_at or datetime.utcnow(),
        resume_campaign=_is_consultation_no_location(settings, pipeline_name, status_name),
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
    if state.reminders_disabled:
        return
    crm = get_amocrm_service(settings)
    pipeline_name, status_name = await crm.get_lead_location(deal_id)
    if not _is_consultation_no_location(settings, pipeline_name, status_name):
        return
    lawyer_name, lawyer_phone = await crm.get_lead_lawyer(lead)
    row, created = await _ensure_notification(
        session,
        deal_id=deal_id,
        case=case,
        user=user,
        lawyer_name=lawyer_name,
        lawyer_phone=lawyer_phone,
        # A sent notification starts a new cycle only after a confirmed sales
        # exclusion. consultation_no=False alone is insufficient: the fast
        # consultation callback changes it before its background CRM stage move.
        new_cycle=state.excluded_sales,
    )
    if created:
        await record_action(
            session,
            settings,
            user,
            case,
            f"poll-consultation-no-detected:{deal_id}:cycle:{row.cycle}",
            "mailing_consultation_no",
            "Сделка переведена в этап «Консультация-НО». Клиенту отправляется просьба перезвонить, "
            "после чего регулярная рассылка продолжится с сохраненного шага.",
            execute_immediately=False,
        )
    elif row.status == "sent":
        return
    await _deliver_notification(session, settings, bot, row.id)


async def _process_sales_lead(session: AsyncSession, settings, lead: dict) -> None:
    deal_id = int(lead["id"])
    case, user, state = await _local_deal(session, deal_id)
    if case is None or user is None or state is None:
        return
    pipeline_name, _ = await get_amocrm_service(settings).get_lead_location(deal_id)
    if pipeline_name != "Отдел продаж":
        return
    await exclude_user_for_sales(session, settings, user, case, state, deal_id)
    await session.execute(
        update(CrmDealNotification)
        .where(
            CrmDealNotification.amocrm_deal_id == deal_id,
            CrmDealNotification.status == "pending",
        )
        .values(status="cancelled", lease_until=None)
    )
    await session.commit()


async def poll_crm_mailing_once(settings, bot: Bot | None = None) -> None:
    crm = get_amocrm_service(settings)
    no_leads, sales_leads = await asyncio.gather(
        crm.list_leads_in_status(settings.amocrm_pipeline_name, NO_STATUS_NAMES),
        crm.list_leads_in_pipeline("Отдел продаж"),
    )

    async with SessionLocal() as session:
        await recover_notification_leases(session)

    semaphore = asyncio.Semaphore(POLLING_CONCURRENCY)

    async def process_lead(kind: str, lead: dict) -> None:
        async with semaphore:
            try:
                async with SessionLocal() as session:
                    if kind == "consultation_no":
                        await _process_consultation_no_lead(session, settings, bot, lead)
                    else:
                        await _process_sales_lead(session, settings, lead)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "amoCRM mailing lead processing failed kind=%s deal_id=%s",
                    kind,
                    lead.get("id"),
                )

    # Every deal owns an independent session. One slow lead no longer blocks
    # other client notifications or sales exclusions in the same poll cycle.
    await asyncio.gather(
        *(process_lead("consultation_no", lead) for lead in no_leads),
        *(process_lead("sales", lead) for lead in sales_leads),
    )


async def run_crm_mailing_polling(settings, bot: Bot | None = None) -> None:
    while True:
        try:
            await poll_crm_mailing_once(settings, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("amoCRM mailing polling cycle failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
