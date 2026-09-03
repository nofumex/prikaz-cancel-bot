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
POLLING_CRM_TIMEOUT_SECONDS = 5


def _is_consultation_no_location(settings, pipeline_name: str | None, status_name: str | None) -> bool:
    return pipeline_name == settings.amocrm_pipeline_name and status_name in NO_STATUS_NAMES


async def _crm_call(settings, awaitable):
    """Bound one amoCRM operation so a single lead cannot stall a poll cycle."""
    timeout = max(
        1,
        int(getattr(settings, "crm_sync_timeout_seconds", POLLING_CRM_TIMEOUT_SECONDS)),
    )
    return await asyncio.wait_for(awaitable, timeout=timeout)


async def _local_polling_deal_ids(session: AsyncSession) -> list[int]:
    """Return one current local CRM deal for every active mailing lifecycle."""
    rows = (
        await session.execute(
            select(
                User.id,
                User.amocrm_current_case_id,
                Case.id,
                Case.amocrm_lead_id,
                Case.amo_lead_id,
            )
            .join(MailingState, MailingState.user_id == User.id)
            .join(Case, Case.user_id == User.id)
            .where(
                MailingState.reminders_disabled.is_(False),
                or_(
                    MailingState.participating.is_(True),
                    MailingState.consultation_completed.is_(True),
                    MailingState.consultation_no.is_(True),
                    MailingState.excluded_sales.is_(True),
                ),
                or_(
                    Case.amocrm_lead_id.is_not(None),
                    Case.amo_lead_id.is_not(None),
                ),
            )
            .order_by(User.id, Case.id.desc())
        )
    ).all()
    by_user: dict[int, tuple[int, int]] = {}
    for user_id, current_case_id, case_id, amocrm_lead_id, amo_lead_id in rows:
        deal_id = amocrm_lead_id or amo_lead_id
        if not deal_id:
            continue
        selected = by_user.get(int(user_id))
        is_current = current_case_id is not None and int(case_id) == int(current_case_id)
        if selected is None or is_current:
            by_user[int(user_id)] = (int(case_id), int(deal_id))
    return sorted({deal_id for _, deal_id in by_user.values()})


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
    async def release(status: str, error: str) -> None:
        # Recover a session whose transaction may have failed while finalizing
        # the delivery, then update only a still-claimed notification.
        await session.rollback()
        values = {
            "status": status,
            "lease_until": None,
            "error_message": error[:2000],
        }
        if status == "pending":
            values.update(claimed_at=None, uncertain_at=None)
        else:
            values.update(uncertain_at=datetime.utcnow())
        await session.execute(
            update(CrmDealNotification)
            .where(
                CrmDealNotification.id == notification_id,
                CrmDealNotification.status == "sending",
            )
            .values(**values)
        )
        await session.commit()

    try:
        row = await session.get(CrmDealNotification, notification_id)
        if row is None:
            raise RuntimeError(f"CRM notification missing id={notification_id}")
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
        pipeline_name, status_name = await _crm_call(
            settings, crm.get_lead_location(row.amocrm_deal_id)
        )
        if not _is_consultation_no_location(settings, pipeline_name, status_name):
            row.status = "cancelled"
            row.lease_until = None
            await session.commit()
            return False
    except asyncio.CancelledError:
        await release("pending", "delivery cancelled before Bot API call")
        raise
    except Exception as exc:
        await release("pending", f"pre-delivery check failed: {exc}")
        logger.exception("CRM notification pre-delivery check failed id=%s", notification_id)
        return False

    try:
        sent = await _send_user_message(settings, bot, user, row.message_text)
        if not sent:
            await release("uncertain", "delivery outcome was not confirmed")
            return False

        # The deal may move while the Bot API request is in flight. The one-off
        # message remains delivered, but the regular campaign is resumed only if
        # amoCRM still confirms Consultation-NO.
        pipeline_name, status_name = await _crm_call(
            settings, crm.get_lead_location(row.amocrm_deal_id)
        )
        await _mark_notification_sent(
            session,
            settings,
            row,
            case,
            user,
            state,
            datetime.utcnow(),
            resume_campaign=_is_consultation_no_location(
                settings, pipeline_name, status_name
            ),
        )
    except asyncio.CancelledError:
        await release("uncertain", "delivery interrupted during or after Bot API call")
        raise
    except Exception as exc:
        await release("uncertain", f"delivery outcome requires reconciliation: {exc}")
        logger.exception("CRM notification delivery became uncertain id=%s", notification_id)
        return False
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
    pipeline_name, status_name = await _crm_call(
        settings,
        get_amocrm_service(settings).get_lead_location(row.amocrm_deal_id),
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
    session: AsyncSession,
    settings,
    bot: Bot | None,
    lead: dict,
    *,
    known_location: tuple[str | None, str | None] | None = None,
) -> None:
    deal_id = int(lead["id"])
    case, user, state = await _local_deal(session, deal_id)
    if case is None or user is None or state is None:
        return
    if state.reminders_disabled:
        return
    crm = get_amocrm_service(settings)
    pipeline_name, status_name = known_location or await _crm_call(
        settings, crm.get_lead_location(deal_id)
    )
    if not _is_consultation_no_location(settings, pipeline_name, status_name):
        return
    get_lead_details = getattr(crm, "get_lead_details", None)
    if callable(get_lead_details):
        lead = await _crm_call(settings, get_lead_details(deal_id))
    lawyer_name, lawyer_phone = await _crm_call(settings, crm.get_lead_lawyer(lead))
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


async def _process_sales_lead(
    session: AsyncSession,
    settings,
    lead: dict,
    *,
    known_location: tuple[str | None, str | None] | None = None,
) -> None:
    deal_id = int(lead["id"])
    case, user, state = await _local_deal(session, deal_id)
    if case is None or user is None or state is None:
        return
    # The discovery result may already be stale. Confirm Sales immediately
    # before mutating the mailing state.
    pipeline_name, _ = await _crm_call(
        settings, get_amocrm_service(settings).get_lead_location(deal_id)
    )
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
    async with SessionLocal() as session:
        await recover_notification_leases(session)
        deal_ids = await _local_polling_deal_ids(session)

    semaphore = asyncio.Semaphore(POLLING_CONCURRENCY)

    async def process_deal(deal_id: int) -> None:
        async with semaphore:
            try:
                location = await _crm_call(settings, crm.get_lead_location(deal_id))
                pipeline_name, status_name = location
                async with SessionLocal() as session:
                    lead = {"id": deal_id}
                    if pipeline_name == "Отдел продаж":
                        await _process_sales_lead(
                            session, settings, lead, known_location=location
                        )
                    elif _is_consultation_no_location(
                        settings, pipeline_name, status_name
                    ):
                        await _process_consultation_no_lead(
                            session,
                            settings,
                            bot,
                            lead,
                            known_location=location,
                        )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "amoCRM mailing deal timed out deal_id=%s",
                    deal_id,
                )
            except Exception:
                logger.exception(
                    "amoCRM mailing deal processing failed deal_id=%s",
                    deal_id,
                )

    # Every deal owns an independent session. Discovery is strictly local;
    # amoCRM is queried only for the exact deal ids already known by the bot.
    await asyncio.gather(*(process_deal(deal_id) for deal_id in deal_ids))


async def run_crm_mailing_polling(settings, bot: Bot | None = None) -> None:
    while True:
        try:
            await poll_crm_mailing_once(settings, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("amoCRM mailing polling cycle failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
