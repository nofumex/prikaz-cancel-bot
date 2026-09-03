from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import (
    Case,
    CrmDealNotification,
    CrmMailingChange,
    CrmMailingCursor,
    MailingJob,
    MailingState,
    User,
)
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
POLLING_CURSOR_NAME = "lead_status_changes"
POLLING_CURSOR_OVERLAP_SECONDS = 3
POLLING_CHANGE_LEASE = timedelta(minutes=2)
POLLING_CHANGE_BATCH = 1000
POLLING_CHANGE_RETENTION_SECONDS = 7 * 24 * 60 * 60


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
    cycle: int,
) -> tuple[CrmDealNotification, bool]:
    existing = await session.scalar(
        select(CrmDealNotification).where(
            CrmDealNotification.amocrm_deal_id == deal_id,
            CrmDealNotification.notification_type == LAWYER_CALL,
            CrmDealNotification.cycle == cycle,
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
        cycle=cycle,
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
                CrmDealNotification.cycle == cycle,
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
    # A confirmed delivery completes this explicit consultation cycle even if
    # amoCRM moved the deal while the Bot API call was in flight. Only campaign
    # resumption and Sales flags remain conditional on the confirmed location.
    state.consultation_state = "ready"
    if resume_campaign:
        state.participating = not state.reminders_disabled
        state.consultation_completed = False
        state.consultation_no = True
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
    cycle = int(state.consultation_cycle)
    if cycle <= 0:
        # NO without a user-created consultation cycle is not a notification trigger.
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
        cycle=cycle,
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
    await session.refresh(row)
    if row.status == "pending":
        raise RuntimeError(
            f"CRM notification {row.id} was not delivered and must be retried"
        )


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


async def _process_current_deal(
    settings, bot: Bot | None, crm, deal_id: int
) -> None:
    location = await _crm_call(settings, crm.get_lead_location(deal_id))
    pipeline_name, status_name = location
    async with SessionLocal() as session:
        lead = {"id": deal_id}
        if pipeline_name == "Отдел продаж":
            await _process_sales_lead(
                session, settings, lead, known_location=location
            )
        elif _is_consultation_no_location(settings, pipeline_name, status_name):
            await _process_consultation_no_lead(
                session,
                settings,
                bot,
                lead,
                known_location=location,
            )


async def _run_deals_bounded(settings, bot: Bot | None, deal_ids: list[int]) -> bool:
    crm = get_amocrm_service(settings)
    semaphore = asyncio.Semaphore(POLLING_CONCURRENCY)
    successful = True

    async def process_deal(deal_id: int) -> None:
        nonlocal successful
        async with semaphore:
            try:
                await _process_current_deal(settings, bot, crm, deal_id)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                successful = False
                logger.warning("amoCRM mailing deal timed out deal_id=%s", deal_id)
            except Exception:
                successful = False
                logger.exception("amoCRM mailing deal processing failed deal_id=%s", deal_id)

    await asyncio.gather(*(process_deal(deal_id) for deal_id in deal_ids))
    return successful


async def reconcile_crm_mailing_once(settings, bot: Bot | None = None) -> None:
    """One bounded startup reconciliation of locally known active deals."""
    async with SessionLocal() as session:
        await recover_notification_leases(session)
        deal_ids = await _local_polling_deal_ids(session)
    await _run_deals_bounded(settings, bot, deal_ids)


async def _ensure_incremental_cursor(initial_at: int) -> CrmMailingCursor:
    async with SessionLocal() as session:
        cursor = await session.get(CrmMailingCursor, POLLING_CURSOR_NAME)
        if cursor is not None:
            return cursor
        try:
            async with session.begin_nested():
                cursor = CrmMailingCursor(
                    name=POLLING_CURSOR_NAME, cursor_at=int(initial_at)
                )
                session.add(cursor)
                await session.flush()
            await session.commit()
            return cursor
        except IntegrityError:
            await session.rollback()
            cursor = await session.get(CrmMailingCursor, POLLING_CURSOR_NAME)
            if cursor is None:
                raise
            return cursor


async def _ingest_incremental_changes(settings) -> None:
    observed_now = int(time.time())
    cursor = await _ensure_incremental_cursor(
        observed_now - POLLING_CURSOR_OVERLAP_SECONDS
    )
    # Never move the high-water mark backwards if the host clock is adjusted.
    window_to = max(observed_now, int(cursor.cursor_at))
    window_from = max(
        0, int(cursor.cursor_at) - POLLING_CURSOR_OVERLAP_SECONDS
    )
    crm = get_amocrm_service(settings)
    events = await _crm_call(
        settings, crm.list_lead_status_changes(window_from, window_to)
    )

    async with SessionLocal() as session:
        local_deal_ids = set(await _local_polling_deal_ids(session))
        seen_event_ids: set[str] = set()
        for event in events:
            event_id = str(event.get("id") or "").strip()
            try:
                deal_id = int(event.get("entity_id") or 0)
                changed_at = int(event.get("created_at") or 0)
            except (TypeError, ValueError):
                logger.warning("Ignoring malformed amoCRM event id=%s", event_id)
                continue
            if (
                not event_id
                or event_id in seen_event_ids
                or deal_id not in local_deal_ids
                or changed_at <= 0
            ):
                continue
            seen_event_ids.add(event_id)
            try:
                async with session.begin_nested():
                    session.add(
                        CrmMailingChange(
                            event_id=event_id,
                            amocrm_deal_id=deal_id,
                            changed_at=changed_at,
                            status="pending",
                        )
                    )
                    await session.flush()
            except IntegrityError:
                # Expected for the overlap window or another polling process.
                pass
        await session.execute(
            update(CrmMailingCursor)
            .where(
                CrmMailingCursor.name == POLLING_CURSOR_NAME,
                CrmMailingCursor.cursor_at < window_to,
            )
            .values(cursor_at=window_to, updated_at=datetime.utcnow())
        )
        await session.commit()


async def _process_pending_changes(settings, bot: Bot | None = None) -> None:
    now = datetime.utcnow()
    async with SessionLocal() as session:
        await session.execute(
            delete(CrmMailingChange).where(
                CrmMailingChange.status == "completed",
                CrmMailingChange.changed_at
                < int(time.time()) - POLLING_CHANGE_RETENTION_SECONDS,
            )
        )
        await session.execute(
            update(CrmMailingChange)
            .where(
                CrmMailingChange.status == "processing",
                or_(
                    CrmMailingChange.lease_until.is_(None),
                    CrmMailingChange.lease_until < now,
                ),
            )
            .values(status="pending", lease_until=None, claim_token=None)
        )
        await session.commit()
        deal_ids = list(
            (
                await session.scalars(
                    select(CrmMailingChange.amocrm_deal_id)
                    .where(CrmMailingChange.status == "pending")
                    .group_by(CrmMailingChange.amocrm_deal_id)
                    .order_by(func.min(CrmMailingChange.changed_at))
                    .limit(POLLING_CHANGE_BATCH)
                )
            ).all()
        )
        changes = list(
            (
                await session.scalars(
                    select(CrmMailingChange)
                    .where(
                        CrmMailingChange.status == "pending",
                        CrmMailingChange.amocrm_deal_id.in_(deal_ids),
                    )
                    .order_by(
                        CrmMailingChange.changed_at,
                        CrmMailingChange.event_id,
                    )
                )
            ).all()
        ) if deal_ids else []

    by_deal: dict[int, list[str]] = {}
    for change in changes:
        by_deal.setdefault(int(change.amocrm_deal_id), []).append(change.event_id)
    semaphore = asyncio.Semaphore(POLLING_CONCURRENCY)
    crm = get_amocrm_service(settings)

    async def process_deal(deal_id: int, event_ids: list[str]) -> None:
        claim_token = uuid.uuid4().hex
        async with semaphore:
            async with SessionLocal() as session:
                claimed = await session.execute(
                    update(CrmMailingChange)
                    .where(
                        CrmMailingChange.event_id.in_(event_ids),
                        CrmMailingChange.status == "pending",
                    )
                    .values(
                        status="processing",
                        attempts=CrmMailingChange.attempts + 1,
                        lease_until=datetime.utcnow() + POLLING_CHANGE_LEASE,
                        claim_token=claim_token,
                        error_message=None,
                    )
                )
                await session.commit()
                if not claimed.rowcount:
                    return
            try:
                await _process_current_deal(settings, bot, crm, deal_id)
            except asyncio.CancelledError:
                async with SessionLocal() as session:
                    await session.execute(
                        update(CrmMailingChange)
                        .where(CrmMailingChange.claim_token == claim_token)
                        .values(
                            status="pending",
                            lease_until=None,
                            claim_token=None,
                            error_message="incremental processing cancelled",
                        )
                    )
                    await session.commit()
                raise
            except Exception as exc:
                logger.exception(
                    "amoCRM incremental mailing change failed deal_id=%s", deal_id
                )
                async with SessionLocal() as session:
                    await session.execute(
                        update(CrmMailingChange)
                        .where(CrmMailingChange.claim_token == claim_token)
                        .values(
                            status="pending",
                            lease_until=None,
                            claim_token=None,
                            error_message=str(exc)[:2000],
                        )
                    )
                    await session.commit()
                return
            async with SessionLocal() as session:
                await session.execute(
                    update(CrmMailingChange)
                    .where(CrmMailingChange.claim_token == claim_token)
                    .values(
                        status="completed",
                        lease_until=None,
                        claim_token=None,
                        processed_at=datetime.utcnow(),
                        error_message=None,
                    )
                )
                await session.commit()

    await asyncio.gather(
        *(process_deal(deal_id, event_ids) for deal_id, event_ids in by_deal.items())
    )


async def poll_crm_mailing_once(settings, bot: Bot | None = None) -> None:
    """Ingest and process only amoCRM lead status changes since the cursor."""
    ingest_error: Exception | None = None
    try:
        await _ingest_incremental_changes(settings)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A temporary feed outage must not prevent already durable inbox rows
        # from being retried in this cycle.
        ingest_error = exc
    await _process_pending_changes(settings, bot)
    if ingest_error is not None:
        raise ingest_error


async def run_crm_mailing_polling(settings, bot: Bot | None = None) -> None:
    startup_at = int(time.time())
    await _ensure_incremental_cursor(startup_at)

    async def startup_reconciliation() -> None:
        try:
            await reconcile_crm_mailing_once(settings, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("amoCRM mailing startup reconciliation failed")

    reconciliation_task = asyncio.create_task(
        startup_reconciliation(), name="crm-mailing-startup-reconciliation"
    )
    try:
        while True:
            try:
                await poll_crm_mailing_once(settings, bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("amoCRM mailing polling cycle failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        if not reconciliation_task.done():
            reconciliation_task.cancel()
        await asyncio.gather(reconciliation_task, return_exceptions=True)
