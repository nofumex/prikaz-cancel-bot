from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.database import _sqlite_upgrade_crm_notification_cycle_unique
from app.models import (
    Base,
    Case,
    CrmDealNotification,
    CrmMailingChange,
    CrmMailingCursor,
    MailingAction,
    MailingJob,
    MailingState,
    PavelMessageDelivery,
    User,
)
from app.services import automatic_mailings as mailings
from app.services import crm_mailing_polling as crm_polling


@pytest.fixture
async def mailing_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(mailings, "SessionLocal", sessions)
    monkeypatch.setattr(crm_polling, "SessionLocal", sessions)
    yield sessions
    await engine.dispose()


async def _user(session, user_id: int = 1) -> User:
    user = User(
        id=user_id,
        platform="telegram",
        platform_user_id=str(user_id),
        telegram_id=user_id,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.mark.asyncio
async def test_notification_unique_constraint_migrates_to_deal_type_cycle() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    old_schema = """
        CREATE TABLE crm_deal_notifications (
            id INTEGER PRIMARY KEY,
            amocrm_deal_id BIGINT NOT NULL,
            notification_type VARCHAR(64) NOT NULL,
            cycle INTEGER NOT NULL DEFAULT 1,
            user_id INTEGER NOT NULL,
            case_id INTEGER NOT NULL,
            lawyer_name VARCHAR(255) NOT NULL,
            lawyer_phone VARCHAR(64) NOT NULL,
            message_text TEXT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            claimed_at DATETIME,
            lease_until DATETIME,
            sent_at DATETIME,
            uncertain_at DATETIME,
            error_message TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            UNIQUE (amocrm_deal_id, notification_type)
        )
    """
    insert_sql = """
        INSERT INTO crm_deal_notifications (
            id, amocrm_deal_id, notification_type, cycle, user_id, case_id,
            lawyer_name, lawyer_phone, message_text, status
        ) VALUES (?, 7000, 'lawyer_call', ?, 1, 1, 'Павел', '+7000', 'text', 'sent')
    """
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(old_schema)
            await connection.exec_driver_sql(insert_sql, (1, 1))
            await _sqlite_upgrade_crm_notification_cycle_unique(connection)
            await connection.exec_driver_sql(insert_sql, (2, 2))
        async with engine.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.exec_driver_sql(insert_sql, (3, 2))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_start_creates_one_durable_first_job(mailing_db) -> None:
    started = datetime(2026, 1, 10, 12)
    async with mailing_db() as session:
        user = await _user(session)
        first = await mailings.ensure_mailing_started(session, user, started_at=started)
        second = await mailings.ensure_mailing_started(session, user, started_at=started + timedelta(days=2))
        jobs = list((await session.execute(select(MailingJob))).scalars())

    assert first.id == second.id
    assert len(jobs) == 1
    assert jobs[0].stage == 1
    assert jobs[0].due_at == started + timedelta(days=7)


@pytest.mark.asyncio
async def test_sent_stage_schedules_only_the_next_stage(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    monkeypatch.setattr(mailings, "_send_user_message", AsyncMock(return_value=True))
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(
            session, user, started_at=datetime.utcnow() - timedelta(days=8)
        )
        job_id = await session.scalar(select(MailingJob.id).where(MailingJob.stage == 1))

    await mailings._deliver_job(settings, SimpleNamespace(), job_id)

    async with mailing_db() as session:
        jobs = list((await session.execute(select(MailingJob).order_by(MailingJob.stage))).scalars())
        state = await session.scalar(select(MailingState))
    assert [(job.stage, job.status) for job in jobs] == [(1, "sent"), (2, "pending")]
    assert state.last_sent_stage == 1
    assert state.next_stage == 2
    assert jobs[1].due_at == jobs[0].sent_at + timedelta(days=7)


@pytest.mark.asyncio
async def test_state_is_checked_again_immediately_before_send(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(mailings, "_send_user_message", sender)
    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(
            session, user, started_at=datetime.utcnow() - timedelta(days=8)
        )
        state.participating = False
        state.consultation_completed = True
        await session.commit()
        job_id = await session.scalar(select(MailingJob.id))

    await mailings._deliver_job(settings, None, job_id)

    async with mailing_db() as session:
        job = await session.get(MailingJob, job_id)
    assert job.status == "cancelled"
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_sales_exclusion_during_crm_preflight_cancels_delivery(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    preflight_started = asyncio.Event()
    release_preflight = asyncio.Event()
    sender = AsyncMock(return_value=True)

    async def slow_location(deal_id):
        preflight_started.set()
        await release_preflight.wait()
        return settings.amocrm_pipeline_name, "Подписался на бота"

    monkeypatch.setattr(
        mailings,
        "get_amocrm_service",
        lambda settings: SimpleNamespace(get_lead_location=slow_location),
    )
    monkeypatch.setattr(mailings, "_send_user_message", sender)
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(
            session, user, started_at=datetime.utcnow() - timedelta(days=8)
        )
        session.add(Case(user_id=user.id, platform="telegram", amocrm_lead_id=1200))
        await session.commit()
        job_id = await session.scalar(select(MailingJob.id))

    delivery = asyncio.create_task(mailings._deliver_job(settings, None, job_id))
    await asyncio.wait_for(preflight_started.wait(), timeout=1)
    async with mailing_db() as concurrent:
        state = await concurrent.scalar(select(MailingState))
        state.participating = False
        state.excluded_sales = True
        await concurrent.commit()
    release_preflight.set()
    await asyncio.wait_for(delivery, timeout=2)

    async with mailing_db() as session:
        job = await session.get(MailingJob, job_id)
    assert job.status == "cancelled"
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_consultation_click_does_not_resend_pavel_message(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    sender = AsyncMock(return_value=object())
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        case = Case(user_id=user.id, platform="telegram")
        session.add(case)
        await session.commit()

        assert await mailings.deliver_pavel_message(
            session, settings, user, case, sender
        ) is True
        assert await mailings.deliver_pavel_message(
            session, settings, user, case, sender
        ) is False
        delivery = await session.scalar(select(PavelMessageDelivery))

    assert delivery.status == "sent"
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_parallel_consultation_callbacks_claim_pavel_message_once(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send():
        started.set()
        await release.wait()
        return object()

    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as setup:
        user = await _user(setup)
        await mailings.ensure_mailing_started(setup, user)
        case = Case(user_id=user.id, platform="telegram")
        setup.add(case)
        await setup.commit()
        user_id, case_id = user.id, case.id

    async with mailing_db() as first, mailing_db() as second:
        first_user, first_case = await first.get(User, user_id), await first.get(Case, case_id)
        second_user, second_case = await second.get(User, user_id), await second.get(Case, case_id)
        first_task = asyncio.create_task(
            mailings.deliver_pavel_message(
                first, settings, first_user, first_case, slow_send
            )
        )
        await started.wait()
        assert await mailings.deliver_pavel_message(
            second,
            settings,
            second_user,
            second_case,
            AsyncMock(return_value=object()),
        ) is False
        release.set()
        assert await first_task is True

    async with mailing_db() as session:
        delivery = await session.scalar(select(PavelMessageDelivery))
    assert delivery.status == "sent"
    assert delivery.attempts == 1


@pytest.mark.asyncio
async def test_expired_pavel_claim_becomes_uncertain_without_blind_resend(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    sender = AsyncMock(return_value=object())
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as setup:
        user = await _user(setup)
        state = await mailings.ensure_mailing_started(setup, user)
        case = Case(user_id=user.id, platform="telegram")
        setup.add(case)
        await setup.commit()
        delivery = await mailings._ensure_pavel_delivery(setup, user, case, state)
        delivery.status = "sending"
        delivery.claimed_at = datetime.utcnow() - timedelta(minutes=10)
        delivery.lease_until = datetime.utcnow() - timedelta(seconds=1)
        await setup.commit()
        delivery_id, user_id, case_id = delivery.id, user.id, case.id

    async with mailing_db() as restarted:
        user = await restarted.get(User, user_id)
        case = await restarted.get(Case, case_id)
        assert await mailings.deliver_pavel_message(
            restarted, settings, user, case, sender
        ) is False
        delivery = await restarted.get(PavelMessageDelivery, delivery_id)

    assert delivery.status == "uncertain"
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_pavel_result_requires_explicit_reconciliation(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    uncertain_sender = AsyncMock(return_value=False)
    retry_sender = AsyncMock(return_value=object())
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        case = Case(user_id=user.id, platform="telegram")
        session.add(case)
        await session.commit()

        assert await mailings.deliver_pavel_message(
            session, settings, user, case, uncertain_sender
        ) is False
        delivery = await session.scalar(select(PavelMessageDelivery))
        assert delivery.status == "uncertain"
        assert await mailings.deliver_pavel_message(
            session, settings, user, case, retry_sender
        ) is False
        retry_sender.assert_not_awaited()
        assert await mailings.resolve_uncertain_pavel_delivery(
            session, settings, delivery.id, delivered=False
        ) is True
        assert await mailings.deliver_pavel_message(
            session, settings, user, case, retry_sender
        ) is True

    assert uncertain_sender.await_count == 1
    assert retry_sender.await_count == 1


@pytest.mark.asyncio
async def test_successful_pavel_consultation_still_cancels_future_jobs(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        session.add(
            MailingJob(
                user_id=user.id,
                stage=2,
                due_at=datetime.utcnow() + timedelta(days=7),
                status="pending",
            )
        )
        case = Case(user_id=user.id, platform="telegram")
        session.add(case)
        await session.commit()

        assert await mailings.deliver_pavel_message(
            session, settings, user, case, AsyncMock(return_value=object())
        ) is True
        jobs = list((await session.execute(select(MailingJob))).scalars())
        state = await session.scalar(select(MailingState))

    assert {job.status for job in jobs} == {"cancelled"}
    assert state.consultation_completed is True
    assert state.participating is False


@pytest.mark.asyncio
async def test_crm_sales_is_checked_before_job_send_and_cancels_all_jobs(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    sender = AsyncMock(return_value=True)
    note = AsyncMock(return_value=True)
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(return_value=("Отдел продаж", "Новая заявка"))
    )
    monkeypatch.setattr(mailings, "_send_user_message", sender)
    monkeypatch.setattr(mailings, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(mailings, "record_action", note)
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(
            session, user, started_at=datetime.utcnow() - timedelta(days=8)
        )
        session.add(Case(user_id=user.id, platform="telegram", amocrm_lead_id=777))
        session.add(
            MailingJob(
                user_id=user.id,
                stage=2,
                due_at=datetime.utcnow() + timedelta(days=7),
                status="pending",
            )
        )
        await session.commit()
        job_id = await session.scalar(select(MailingJob.id).where(MailingJob.stage == 1))

    await mailings._deliver_job(settings, None, job_id)

    async with mailing_db() as session:
        jobs = list((await session.execute(select(MailingJob))).scalars())
        state = await session.scalar(select(MailingState))
    assert {job.status for job in jobs} == {"cancelled"}
    assert state.participating is False
    assert state.excluded_sales is True
    sender.assert_not_awaited()
    crm.get_lead_location.assert_awaited_once_with(777)
    notes = [call.args[6] for call in note.await_args_list]
    assert len(notes) == 1
    assert "Регулярная рассылка с кнопкой «Получить консультацию» приостановлена" in notes[0]
    assert "Остальные уведомления продолжают работать" in notes[0]


@pytest.mark.asyncio
async def test_crm_consultation_status_blocks_job_immediately_before_send(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    sender = AsyncMock(return_value=True)
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Консультация")
        )
    )
    monkeypatch.setattr(mailings, "_send_user_message", sender)
    monkeypatch.setattr(mailings, "get_amocrm_service", lambda settings: crm)
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(
            session, user, started_at=datetime.utcnow() - timedelta(days=8)
        )
        session.add(Case(user_id=user.id, platform="telegram", amocrm_lead_id=778))
        await session.commit()
        job_id = await session.scalar(select(MailingJob.id))

    await mailings._deliver_job(settings, None, job_id)

    async with mailing_db() as session:
        job = await session.get(MailingJob, job_id)
        state = await session.scalar(select(MailingState))
    assert job.status == "cancelled"
    assert state.participating is False
    assert state.consultation_completed is True
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_crm_consultation_no_status_still_allows_scheduled_job(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    sender = AsyncMock(return_value=True)
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Консультация - НО")
        )
    )
    monkeypatch.setattr(mailings, "_send_user_message", sender)
    monkeypatch.setattr(mailings, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(
            session, user, started_at=datetime.utcnow() - timedelta(days=8)
        )
        state.consultation_no = True
        session.add(Case(user_id=user.id, platform="telegram", amocrm_lead_id=779))
        await session.commit()
        job_id = await session.scalar(select(MailingJob.id))

    await mailings._deliver_job(settings, None, job_id)

    async with mailing_db() as session:
        job = await session.get(MailingJob, job_id)
    assert job.status == "sent"
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_disable_is_idempotent_and_cancels_all_future_jobs(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        session.add(MailingJob(user_id=user.id, stage=2, due_at=datetime.utcnow(), status="pending"))
        await session.commit()
        assert await mailings.disable_reminders(session, settings, user) is True
        assert await mailings.disable_reminders(session, settings, user) is False
        jobs = list((await session.execute(select(MailingJob))).scalars())
        await session.refresh(state)

    assert state.reminders_disabled is True
    assert state.participating is False
    assert {job.status for job in jobs} == {"cancelled"}


@pytest.mark.asyncio
async def test_restart_recovery_quarantines_claim_without_assuming_delivery(mailing_db, monkeypatch) -> None:
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(mailings, "_send_user_message", sender)
    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        job = await session.scalar(select(MailingJob))
        job.status = "sending"
        job.claimed_at = datetime.utcnow() - timedelta(seconds=1)
        job.lease_until = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()
        await mailings.recover_uncertain_jobs(session)
        await session.refresh(job)
        await session.refresh(state)

    assert job.status == "uncertain"
    assert job.sent_at is None
    assert state.next_stage == 1
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_job_requires_explicit_reconciliation_before_retry(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        job = await session.scalar(select(MailingJob))
        job.status = "uncertain"
        job.uncertain_at = datetime.utcnow()
        await session.commit()
        assert await mailings.resolve_uncertain_job(session, settings, job.id, delivered=False)
        await session.refresh(job)
        assert job.status == "pending"


def test_full_campaign_schedule_uses_previous_delivery_as_anchor() -> None:
    base = datetime(2026, 1, 31, 8)
    assert mailings.due_for_stage(2, base) == datetime(2026, 2, 7, 8)
    assert mailings.due_for_stage(3, base) == datetime(2026, 2, 28, 8)
    assert mailings.due_for_stage(4, base) == datetime(2026, 3, 31, 8)
    assert mailings.due_for_stage(7, base) == datetime(2026, 7, 31, 8)
    assert mailings.due_for_stage(9, base) == datetime(2027, 1, 31, 8)
    assert mailings.due_for_stage(11, base) == datetime(2028, 1, 31, 8)


@pytest.mark.asyncio
async def test_amocrm_polling_reenables_and_sales_excludes_without_duplicates(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    location = ["Судебный приказ", "Консультация-НО"]
    no_leads = [{"id": 777, "responsible_user_id": 10}]
    sales_leads = []
    crm = SimpleNamespace(
        list_leads_in_status=AsyncMock(side_effect=lambda *args: list(no_leads)),
        list_leads_in_pipeline=AsyncMock(side_effect=lambda *args: list(sales_leads)),
        get_lead_location=AsyncMock(side_effect=lambda lead_id: tuple(location)),
        get_lead_lawyer=AsyncMock(return_value=("Анна", "+79990000000")),
    )
    sender = AsyncMock(return_value=True)
    note = AsyncMock(return_value=True)
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", sender)
    monkeypatch.setattr(crm_polling, "record_action", note)
    monkeypatch.setattr(mailings, "record_action", note)

    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        state.participating = False
        state.consultation_completed = True
        state.consultation_cycle = 1
        state.consultation_state = "completed"
        state.next_stage = 2
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=777)
        session.add(case)
        await mailings.cancel_future_jobs(session, user.id)
        await session.commit()

    await crm_polling.reconcile_crm_mailing_once(settings)
    await crm_polling.reconcile_crm_mailing_once(settings)

    async with mailing_db() as session:
        state = await session.scalar(select(MailingState))
        stage_two = await session.scalar(select(MailingJob).where(MailingJob.stage == 2))
        notification = await session.scalar(select(CrmDealNotification))
        assert state.participating is True
        assert state.consultation_no is True
        assert state.consultation_completed is False
        assert stage_two.status == "pending"
        assert notification.status == "sent"
        assert notification.lawyer_name == "Анна"
    assert sender.await_count == 1
    note_texts = [call.args[6] for call in note.await_args_list]
    assert any("с сохраненного шага" in text for text in note_texts)

    location[:] = ["Отдел продаж", "Новая заявка"]
    no_leads.clear()
    sales_leads.append({"id": 777})
    await crm_polling.reconcile_crm_mailing_once(settings)
    await crm_polling.reconcile_crm_mailing_once(settings)

    async with mailing_db() as session:
        state = await session.scalar(select(MailingState))
        stage_two = await session.scalar(select(MailingJob).where(MailingJob.stage == 2))
    assert state.participating is False
    assert state.excluded_sales is True
    assert stage_two.status == "cancelled"
    note_texts = [call.args[6] for call in note.await_args_list]
    assert any("Остальные уведомления продолжают работать" in text for text in note_texts)


@pytest.mark.asyncio
async def test_consultation_no_from_sales_sends_and_restores_exact_future_position(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    preserved_due_at = datetime.utcnow() + timedelta(days=4)
    crm = SimpleNamespace(
        get_lead_lawyer=AsyncMock(return_value=("Павел", "+79230165336")),
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Консультация-НО")
        ),
    )
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", sender)
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))

    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        state.next_stage = 2
        state.participating = False
        state.excluded_sales = True
        state.consultation_cycle = 1
        state.consultation_state = "completed"
        first_job = await session.scalar(select(MailingJob).where(MailingJob.stage == 1))
        first_job.status = "sent"
        session.add(
            MailingJob(
                user_id=user.id,
                stage=2,
                due_at=preserved_due_at,
                status="cancelled",
                cancelled_at=datetime.utcnow(),
            )
        )
        session.add(Case(user_id=user.id, platform="telegram", amocrm_lead_id=902))
        await session.commit()
        await crm_polling._process_consultation_no_lead(
            session, settings, None, {"id": 902, "responsible_user_id": 1}
        )

        await session.refresh(state)
        resumed = await session.scalar(select(MailingJob).where(MailingJob.stage == 2))

    sender.assert_awaited_once()
    assert state.excluded_sales is False
    assert state.participating is True
    assert state.next_stage == 2
    assert resumed.status == "pending"
    assert resumed.due_at == preserved_due_at


@pytest.mark.asyncio
async def test_consultation_no_never_releases_an_overdue_mailing_immediately(
    mailing_db
) -> None:
    resumed_at = datetime.utcnow()
    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        state.next_stage = 1
        job = await session.scalar(select(MailingJob).where(MailingJob.stage == 1))
        job.status = "cancelled"
        job.due_at = resumed_at - timedelta(days=1)
        await crm_polling._resume_saved_mailing_position(session, state, resumed_at)
        await session.commit()
        await session.refresh(job)

    assert job.status == "pending"
    assert job.due_at == mailings.due_for_stage(1, resumed_at)


@pytest.mark.asyncio
async def test_consultation_button_path_never_waits_for_amocrm(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)

    def unexpected_crm_call(settings):
        raise AssertionError("interactive consultation path must not call amoCRM inline")

    monkeypatch.setattr(mailings, "get_amocrm_service", unexpected_crm_call)
    send = AsyncMock(return_value=object())
    async with mailing_db() as session:
        user = await _user(session)
        user.phone = "+79990000000"
        await mailings.ensure_mailing_started(session, user)
        case = Case(user_id=user.id, platform="telegram")
        session.add(case)
        await session.commit()

        selected_case, ready = await mailings.begin_consultation(
            session, settings, user, chat_id="1"
        )
        assert ready is True
        assert await mailings.prepare_consultation(session, settings, user, selected_case)
        assert await mailings.deliver_pavel_message(
            session, settings, user, selected_case, send
        )

    send.assert_awaited_once()


async def _complete_consultation_click(
    session, settings, user: User, case: Case, send: AsyncMock
) -> int:
    selected_case, ready = await mailings.begin_consultation(
        session, settings, user, chat_id=str(user.id)
    )
    assert selected_case.id == case.id
    assert ready is True
    state = await mailings.get_mailing_state(session, user.id)
    cycle = state.consultation_cycle
    assert state.consultation_state == "requested"
    assert await mailings.prepare_consultation(session, settings, user, case)
    assert await mailings.deliver_pavel_message(
        session, settings, user, case, send
    )
    await session.refresh(state)
    assert state.consultation_state == "completed"
    return cycle


@pytest.mark.asyncio
async def test_explicit_click_cycles_resume_stage2_then_stage3(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Консультация-НО")
        ),
        get_lead_lawyer=AsyncMock(return_value=("Павел", "+79230165336")),
    )
    pavel_send = AsyncMock(return_value=object())
    missed_call_send = AsyncMock(return_value=True)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", missed_call_send)

    async with mailing_db() as session:
        user = await _user(session)
        user.phone = "+79990000000"
        state = await mailings.ensure_mailing_started(session, user)
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=8800)
        session.add(case)
        stage1 = await session.scalar(select(MailingJob).where(MailingJob.stage == 1))
        stage1.status = "sent"
        state.next_stage = 2
        session.add(
            MailingJob(
                user_id=user.id,
                stage=2,
                due_at=datetime.utcnow() + timedelta(days=7),
                status="pending",
            )
        )
        await session.commit()

        assert await _complete_consultation_click(
            session, settings, user, case, pavel_send
        ) == 1
        await crm_polling._process_consultation_no_lead(
            session, settings, None, {"id": 8800}
        )
        await session.refresh(state)
        stage2 = await session.scalar(select(MailingJob).where(MailingJob.stage == 2))
        assert state.consultation_cycle == 1
        assert state.consultation_state == "ready"
        assert stage2.status == "pending"

        stage2.status = "sent"
        state.next_stage = 3
        session.add(
            MailingJob(
                user_id=user.id,
                stage=3,
                due_at=datetime.utcnow() + timedelta(days=21),
                status="pending",
            )
        )
        await session.commit()

        assert await _complete_consultation_click(
            session, settings, user, case, pavel_send
        ) == 2
        await crm_polling._process_consultation_no_lead(
            session, settings, None, {"id": 8800}
        )
        await session.refresh(state)
        stage3 = await session.scalar(select(MailingJob).where(MailingJob.stage == 3))
        notifications = list(
            (
                await session.scalars(
                    select(CrmDealNotification)
                    .where(CrmDealNotification.amocrm_deal_id == 8800)
                    .order_by(CrmDealNotification.cycle)
                )
            ).all()
        )

    assert state.consultation_cycle == 2
    assert state.consultation_state == "ready"
    assert stage3.status == "pending"
    assert [row.cycle for row in notifications] == [1, 2]
    assert all(row.status == "sent" for row in notifications)
    assert pavel_send.await_count == 2
    assert missed_call_send.await_count == 2


@pytest.mark.asyncio
async def test_parallel_consultation_clicks_create_exactly_one_cycle(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as setup:
        user = await _user(setup)
        user.phone = "+79990000000"
        await mailings.ensure_mailing_started(setup, user)
        case = Case(user_id=user.id, platform="telegram")
        setup.add(case)
        await setup.commit()
        user_id = user.id

    async with mailing_db() as first, mailing_db() as second:
        first_user = await first.get(User, user_id)
        second_user = await second.get(User, user_id)
        results = await asyncio.gather(
            mailings.begin_consultation(first, settings, first_user, chat_id="1"),
            mailings.begin_consultation(second, settings, second_user, chat_id="1"),
        )

    async with mailing_db() as session:
        state = await session.scalar(select(MailingState))
    assert sorted(ready for _, ready in results) == [False, True]
    assert state.consultation_cycle == 1
    assert state.consultation_state == "requested"


@pytest.mark.asyncio
async def test_disabled_regular_reminders_do_not_block_explicit_consultation_click(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        user = await _user(session)
        user.phone = "+79990000000"
        state = await mailings.ensure_mailing_started(session, user)
        state.reminders_disabled = True
        case = Case(user_id=user.id, platform="telegram")
        session.add(case)
        await session.commit()

        selected_case, ready = await mailings.begin_consultation(
            session, settings, user, chat_id="1"
        )
        await session.refresh(state)

    assert selected_case.id == case.id
    assert ready is True
    assert state.consultation_cycle == 1
    assert state.consultation_state == "requested"


@pytest.mark.asyncio
async def test_click_sales_no_keeps_same_cycle_and_one_notification(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    location = ["Отдел продаж", "Новая заявка"]
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(side_effect=lambda deal_id: tuple(location)),
        get_lead_lawyer=AsyncMock(return_value=("Павел", "+79230165336")),
    )
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", AsyncMock(return_value=True))

    async with mailing_db() as session:
        user = await _user(session)
        user.phone = "+79990000000"
        state = await mailings.ensure_mailing_started(session, user)
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=8801)
        session.add(case)
        await session.commit()
        assert await _complete_consultation_click(
            session, settings, user, case, AsyncMock(return_value=object())
        ) == 1

        await crm_polling._process_sales_lead(session, settings, {"id": 8801})
        await session.refresh(state)
        assert state.excluded_sales is True
        assert state.consultation_cycle == 1

        location[:] = [settings.amocrm_pipeline_name, "Консультация-НО"]
        await crm_polling._process_consultation_no_lead(
            session, settings, None, {"id": 8801}
        )
        await crm_polling._process_consultation_no_lead(
            session, settings, None, {"id": 8801}
        )
        notifications = list(
            (
                await session.scalars(
                    select(CrmDealNotification).where(
                        CrmDealNotification.amocrm_deal_id == 8801
                    )
                )
            ).all()
        )
        await session.refresh(state)

    assert state.consultation_cycle == 1
    assert len(notifications) == 1
    assert notifications[0].cycle == 1
    assert notifications[0].status == "sent"


@pytest.mark.asyncio
async def test_repeated_no_cycles_get_distinct_pavel_delivery_keys(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    send = AsyncMock(return_value=object())
    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        case = Case(user_id=user.id, platform="telegram")
        session.add(case)
        await session.commit()

        state.consultation_no = True
        state.consultation_cycle = 1
        await session.commit()
        assert await mailings.deliver_pavel_message(session, settings, user, case, send)

        state.consultation_no = True
        state.consultation_cycle = 2
        state.consultation_completed = False
        state.participating = True
        await session.commit()
        assert await mailings.deliver_pavel_message(session, settings, user, case, send)
        deliveries = list(
            (await session.execute(select(PavelMessageDelivery).order_by(PavelMessageDelivery.id))).scalars()
        )

    assert send.await_count == 2
    assert len(deliveries) == 2
    assert deliveries[0].consultation_key != deliveries[1].consultation_key


@pytest.mark.asyncio
async def test_polling_processes_deals_in_parallel(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_sent = asyncio.Event()

    async def lawyer(lead):
        if int(lead["id"]) == 1001:
            first_started.set()
            await release_first.wait()
        return "Павел", "+79230165336"

    async def sender(settings, bot, user, text, **kwargs):
        if user.id == 2:
            second_sent.set()
        return True

    crm = SimpleNamespace(
        list_leads_in_status=AsyncMock(return_value=[{"id": 1001}, {"id": 1002}]),
        list_leads_in_pipeline=AsyncMock(return_value=[]),
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Консультация-НО")
        ),
        get_lead_lawyer=lawyer,
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", sender)
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))

    async with mailing_db() as session:
        for user_id, deal_id in ((1, 1001), (2, 1002)):
            user = await _user(session, user_id)
            state = await mailings.ensure_mailing_started(session, user)
            state.consultation_cycle = 1
            state.consultation_state = "completed"
            session.add(Case(user_id=user.id, platform="telegram", amocrm_lead_id=deal_id))
        await session.commit()

    polling = asyncio.create_task(crm_polling.reconcile_crm_mailing_once(settings))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.wait_for(second_sent.wait(), timeout=1)
    release_first.set()
    await asyncio.wait_for(polling, timeout=2)


@pytest.mark.asyncio
async def test_polling_uses_only_local_deals_and_never_lists_pipelines(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(
        list_leads_in_status=AsyncMock(side_effect=AssertionError("pipeline scan")),
        list_leads_in_pipeline=AsyncMock(side_effect=AssertionError("pipeline scan")),
        get_lead_location=AsyncMock(return_value=("Судебный приказ", "Не было коммуникации")),
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)

    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        session.add(Case(user_id=user.id, platform="telegram", amocrm_lead_id=2001))
        await session.commit()

    await crm_polling.reconcile_crm_mailing_once(settings)

    crm.list_leads_in_status.assert_not_awaited()
    crm.list_leads_in_pipeline.assert_not_awaited()
    crm.get_lead_location.assert_awaited_once_with(2001)


@pytest.mark.asyncio
async def test_polling_timeout_isolated_to_one_local_deal(mailing_db, monkeypatch) -> None:
    settings = replace(
        get_settings(), amocrm_enabled=True, crm_sync_timeout_seconds=1
    )
    slow_started = asyncio.Event()
    fast_checked = asyncio.Event()

    async def location(deal_id: int):
        if deal_id == 2002:
            slow_started.set()
            await asyncio.Event().wait()
        fast_checked.set()
        return "Судебный приказ", "Не было коммуникации"

    crm = SimpleNamespace(get_lead_location=AsyncMock(side_effect=location))
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)

    async with mailing_db() as session:
        for user_id, deal_id in ((1, 2002), (2, 2003)):
            user = await _user(session, user_id)
            await mailings.ensure_mailing_started(session, user)
            session.add(Case(user_id=user.id, platform="telegram", amocrm_lead_id=deal_id))
        await session.commit()

    polling = asyncio.create_task(crm_polling.reconcile_crm_mailing_once(settings))
    await asyncio.wait_for(slow_started.wait(), timeout=0.5)
    await asyncio.wait_for(fast_checked.wait(), timeout=0.5)
    await asyncio.wait_for(polling, timeout=1.5)


@pytest.mark.asyncio
async def test_polling_prefers_current_case_over_historical_deals(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(return_value=("Судебный приказ", "Не было коммуникации"))
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)

    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        current = Case(user_id=user.id, platform="telegram", amocrm_lead_id=2004)
        newer_historical = Case(user_id=user.id, platform="telegram", amocrm_lead_id=2005)
        session.add_all([current, newer_historical])
        await session.flush()
        user.amocrm_current_case_id = current.id
        await session.commit()

    await crm_polling.reconcile_crm_mailing_once(settings)

    crm.get_lead_location.assert_awaited_once_with(2004)


async def _add_mailing_deals(session, count: int, *, first_deal_id: int) -> list[int]:
    started_at = datetime.utcnow()
    deal_ids: list[int] = []
    for offset in range(count):
        user_id = offset + 1
        deal_id = first_deal_id + offset
        deal_ids.append(deal_id)
        session.add(
            User(
                id=user_id,
                platform="telegram",
                platform_user_id=str(user_id),
                telegram_id=100000 + user_id,
            )
        )
        session.add(
            MailingState(
                user_id=user_id,
                started_at=started_at,
                participating=True,
            )
        )
        session.add(
            Case(
                user_id=user_id,
                platform="telegram",
                amocrm_lead_id=deal_id,
            )
        )
    await session.commit()
    return deal_ids


@pytest.mark.asyncio
async def test_incremental_sales_is_immediate_with_501_local_deals(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    target_deal_id = 3500
    event_time = int(datetime.utcnow().timestamp())
    crm = SimpleNamespace(
        list_lead_status_changes=AsyncMock(return_value=[{
            "id": "sales-event-1",
            "entity_id": target_deal_id,
            "created_at": event_time,
        }]),
        list_leads_in_status=AsyncMock(side_effect=AssertionError("full scan")),
        list_leads_in_pipeline=AsyncMock(side_effect=AssertionError("full scan")),
        get_lead_location=AsyncMock(return_value=("Отдел продаж", "Новая заявка")),
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))

    async with mailing_db() as session:
        await _add_mailing_deals(session, 501, first_deal_id=target_deal_id)
        target_state = await session.get(MailingState, 1)
        target_state.consultation_cycle = 1
        target_state.consultation_state = "completed"
        target_state.consultation_completed = True
        target_state.participating = False
        await session.commit()

    await asyncio.wait_for(crm_polling.poll_crm_mailing_once(settings), timeout=2)

    async with mailing_db() as session:
        case = await session.scalar(
            select(Case).where(Case.amocrm_lead_id == target_deal_id)
        )
        state = await session.scalar(
            select(MailingState).where(MailingState.user_id == case.user_id)
        )
    assert state.excluded_sales is True
    assert state.participating is False
    assert crm.get_lead_location.await_count == 2  # discovery + final Sales guard
    crm.list_leads_in_status.assert_not_awaited()
    crm.list_leads_in_pipeline.assert_not_awaited()


@pytest.mark.asyncio
async def test_incremental_consultation_no_is_immediate_with_501_local_deals(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    target_deal_id = 4500
    event_time = int(datetime.utcnow().timestamp())
    crm = SimpleNamespace(
        list_lead_status_changes=AsyncMock(return_value=[{
            "id": "no-event-1",
            "entity_id": target_deal_id,
            "created_at": event_time,
        }]),
        list_leads_in_status=AsyncMock(side_effect=AssertionError("full scan")),
        list_leads_in_pipeline=AsyncMock(side_effect=AssertionError("full scan")),
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Консультация-НО")
        ),
        get_lead_lawyer=AsyncMock(return_value=("Павел", "+79230165336")),
    )
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", sender)
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))

    async with mailing_db() as session:
        await _add_mailing_deals(session, 501, first_deal_id=target_deal_id)
        target_state = await session.get(MailingState, 1)
        target_state.consultation_cycle = 1
        target_state.consultation_state = "completed"
        target_state.consultation_completed = True
        target_state.participating = False
        await session.commit()

    await asyncio.wait_for(crm_polling.poll_crm_mailing_once(settings), timeout=2)

    async with mailing_db() as session:
        notification = await session.scalar(
            select(CrmDealNotification).where(
                CrmDealNotification.amocrm_deal_id == target_deal_id
            )
        )
    assert notification.status == "sent"
    assert crm.get_lead_location.await_count == 3
    assert crm.get_lead_location.await_count < 10
    sender.assert_awaited_once()
    crm.list_leads_in_status.assert_not_awaited()
    crm.list_leads_in_pipeline.assert_not_awaited()


@pytest.mark.asyncio
async def test_incremental_overlap_dedupes_same_amocrm_event(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    event = {
        "id": "overlap-event",
        "entity_id": 5500,
        "created_at": int(datetime.utcnow().timestamp()),
    }
    crm = SimpleNamespace(
        list_lead_status_changes=AsyncMock(return_value=[event]),
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Консультация-НО")
        ),
        get_lead_lawyer=AsyncMock(return_value=("Павел", "+79230165336")),
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(crm_polling, "_send_user_message", sender)
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        await _add_mailing_deals(session, 1, first_deal_id=5500)
        state = await session.get(MailingState, 1)
        state.consultation_cycle = 1
        state.consultation_state = "completed"
        state.consultation_completed = True
        state.participating = False
        await session.commit()

    await crm_polling.poll_crm_mailing_once(settings)
    await crm_polling.poll_crm_mailing_once(settings)

    async with mailing_db() as session:
        changes = list((await session.scalars(select(CrmMailingChange))).all())
        cursor = await session.get(CrmMailingCursor, crm_polling.POLLING_CURSOR_NAME)
        notifications = list(
            (await session.scalars(select(CrmDealNotification))).all()
        )
    assert len(changes) == 1
    assert changes[0].status == "completed"
    assert cursor.cursor_at > 0
    assert len(notifications) == 1
    assert notifications[0].cycle == 1
    assert notifications[0].status == "sent"
    sender.assert_awaited_once()
    assert crm.get_lead_location.await_count == 3
    assert crm.list_lead_status_changes.await_count == 2


@pytest.mark.asyncio
async def test_incremental_failed_deal_is_retried_after_cursor_advances(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    event_time = int(datetime.utcnow().timestamp())
    crm = SimpleNamespace(
        list_lead_status_changes=AsyncMock(side_effect=[[
            {"id": "retry-event", "entity_id": 5600, "created_at": event_time}
        ], []]),
        get_lead_location=AsyncMock(side_effect=[
            TimeoutError("temporary timeout"),
            (settings.amocrm_pipeline_name, "Не было коммуникации"),
        ]),
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    async with mailing_db() as session:
        await _add_mailing_deals(session, 1, first_deal_id=5600)

    await crm_polling.poll_crm_mailing_once(settings)
    async with mailing_db() as session:
        first = await session.get(CrmMailingChange, "retry-event")
        first_cursor = (
            await session.get(CrmMailingCursor, crm_polling.POLLING_CURSOR_NAME)
        ).cursor_at
        assert first.status == "pending"

    await crm_polling.poll_crm_mailing_once(settings)
    async with mailing_db() as session:
        retried = await session.get(CrmMailingChange, "retry-event")
        cursor = await session.get(CrmMailingCursor, crm_polling.POLLING_CURSOR_NAME)
    assert retried.status == "completed"
    assert retried.attempts == 2
    assert cursor.cursor_at >= first_cursor


@pytest.mark.asyncio
async def test_incremental_feed_failure_does_not_block_durable_inbox(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(
        list_lead_status_changes=AsyncMock(side_effect=RuntimeError("events unavailable")),
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Не было коммуникации")
        ),
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    async with mailing_db() as session:
        await _add_mailing_deals(session, 1, first_deal_id=5650)
        session.add(
            CrmMailingChange(
                event_id="already-durable",
                amocrm_deal_id=5650,
                changed_at=int(datetime.utcnow().timestamp()),
                status="pending",
            )
        )
        await session.commit()

    with pytest.raises(RuntimeError, match="events unavailable"):
        await crm_polling.poll_crm_mailing_once(settings)

    async with mailing_db() as session:
        change = await session.get(CrmMailingChange, "already-durable")
    assert change.status == "completed"
    crm.get_lead_location.assert_awaited_once_with(5650)


@pytest.mark.asyncio
async def test_one_slow_incremental_deal_does_not_block_another(
    mailing_db, monkeypatch
) -> None:
    settings = replace(
        get_settings(), amocrm_enabled=True, crm_sync_timeout_seconds=1
    )
    now = int(datetime.utcnow().timestamp())
    fast_seen = asyncio.Event()

    async def location(deal_id: int):
        if deal_id == 5700:
            await asyncio.Event().wait()
        fast_seen.set()
        return settings.amocrm_pipeline_name, "Не было коммуникации"

    crm = SimpleNamespace(
        list_lead_status_changes=AsyncMock(return_value=[
            {"id": "slow-change", "entity_id": 5700, "created_at": now},
            {"id": "fast-change", "entity_id": 5701, "created_at": now},
        ]),
        get_lead_location=AsyncMock(side_effect=location),
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    async with mailing_db() as session:
        await _add_mailing_deals(session, 2, first_deal_id=5700)

    polling = asyncio.create_task(crm_polling.poll_crm_mailing_once(settings))
    await asyncio.wait_for(fast_seen.wait(), timeout=0.5)
    await asyncio.wait_for(polling, timeout=1.5)

    async with mailing_db() as session:
        slow = await session.get(CrmMailingChange, "slow-change")
        fast = await session.get(CrmMailingChange, "fast-change")
    assert slow.status == "pending"
    assert fast.status == "completed"


@pytest.mark.asyncio
async def test_stale_sales_list_cannot_exclude_a_deal_already_moved_to_no(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(side_effect=[
            ("Отдел продаж", "Новая заявка"),
            (settings.amocrm_pipeline_name, "Консультация-НО"),
        ]),
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))

    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        session.add(Case(user_id=user.id, platform="telegram", amocrm_lead_id=1003))
        await session.commit()

    await crm_polling.reconcile_crm_mailing_once(settings)

    async with mailing_db() as session:
        state = await session.scalar(select(MailingState))
        job = await session.scalar(select(MailingJob))
    assert state.excluded_sales is False
    assert state.participating is True
    assert job.status == "pending"
    assert crm.get_lead_location.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_or_uncertain_notification_is_never_duplicated_in_same_cycle(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Консультация-НО")
        ),
        get_lead_lawyer=AsyncMock(return_value=("Павел", "+79230165336")),
    )
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", sender)
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))

    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        state.consultation_cycle = 1
        state.consultation_state = "completed"
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=1004)
        session.add(case)
        await session.commit()
        row, _ = await crm_polling._ensure_notification(
            session,
            deal_id=1004,
            case=case,
            user=user,
            lawyer_name="Павел",
            lawyer_phone="+79230165336",
            cycle=1,
        )
        row.status = "cancelled"
        await session.commit()
        await crm_polling._process_consultation_no_lead(session, settings, None, {"id": 1004})
        await session.refresh(row)
        assert row.status == "cancelled"
        assert row.cycle == 1

        row.status = "uncertain"
        state.excluded_sales = True
        state.consultation_no = False
        await session.commit()
        await crm_polling._process_consultation_no_lead(session, settings, None, {"id": 1004})
        await session.refresh(row)

    assert row.status == "uncertain"
    assert row.cycle == 1
    assert sender.await_count == 0


@pytest.mark.asyncio
async def test_sent_notification_is_never_rearmed_from_sales_or_no_flags(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(
            return_value=(settings.amocrm_pipeline_name, "Консультация-НО")
        ),
        get_lead_lawyer=AsyncMock(return_value=("Павел", "+79230165336")),
    )
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", sender)
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))

    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        state.consultation_cycle = 1
        state.consultation_state = "completed"
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=1005)
        session.add(case)
        await session.commit()
        row, _ = await crm_polling._ensure_notification(
            session,
            deal_id=1005,
            case=case,
            user=user,
            lawyer_name="Павел",
            lawyer_phone="+79230165336",
            cycle=1,
        )
        row.status = "sent"
        row.sent_at = datetime.utcnow()
        state.consultation_completed = True
        state.consultation_no = False
        state.excluded_sales = False
        await session.commit()

        await crm_polling._process_consultation_no_lead(session, settings, None, {"id": 1005})
        await session.refresh(row)

    assert row.status == "sent"
    assert row.cycle == 1
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_delivery_resolved_in_sales_does_not_resume_campaign(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(
        get_lead_location=AsyncMock(return_value=("Отдел продаж", "Новая заявка"))
    )
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))

    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        state.consultation_cycle = 1
        state.consultation_state = "completed"
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=1006)
        session.add(case)
        await session.commit()
        row, _ = await crm_polling._ensure_notification(
            session,
            deal_id=1006,
            case=case,
            user=user,
            lawyer_name="Павел",
            lawyer_phone="+79230165336",
            cycle=1,
        )
        row.status = "uncertain"
        row.claimed_at = datetime.utcnow()
        state.participating = False
        state.excluded_sales = True
        job = await session.scalar(select(MailingJob))
        job.status = "cancelled"
        await session.commit()

        assert await crm_polling.resolve_uncertain_notification(
            session, settings, row.id, delivered=True
        )
        await session.refresh(state)
        await session.refresh(job)

    assert state.participating is False
    assert state.excluded_sales is True
    assert state.consultation_state == "ready"
    assert job.status == "cancelled"


@pytest.mark.asyncio
async def test_record_action_reserves_before_external_note(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    started = asyncio.Event()
    release = asyncio.Event()
    sync = AsyncMock()

    async def slow_sync(*args, **kwargs):
        started.set()
        await release.wait()

    sync.side_effect = slow_sync
    monkeypatch.setattr(
        mailings, "get_amocrm_service", lambda settings: SimpleNamespace(sync_case_event=sync)
    )
    async with mailing_db() as setup:
        user = await _user(setup)
        case = Case(user_id=user.id, platform="telegram")
        setup.add(case)
        await setup.commit()
        user_id, case_id = user.id, case.id

    async with mailing_db() as first, mailing_db() as second:
        user1, case1 = await first.get(User, user_id), await first.get(Case, case_id)
        user2, case2 = await second.get(User, user_id), await second.get(Case, case_id)
        task = asyncio.create_task(
            mailings.record_action(first, settings, user1, case1, "same", "mailing_message_sent", "note")
        )
        await started.wait()
        assert await mailings.record_action(
            second, settings, user2, case2, "same", "mailing_message_sent", "note"
        ) is False
        release.set()
        assert await task is True

    async with mailing_db() as session:
        actions = list((await session.execute(select(MailingAction))).scalars())
    assert len(actions) == 1
    assert actions[0].status == "completed"
    assert sync.await_count == 1


@pytest.mark.asyncio
async def test_equal_human_notes_from_distinct_actions_have_distinct_visible_identity(
    mailing_db,
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    async with mailing_db() as session:
        user = await _user(session)
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=321)
        session.add(case)
        await session.commit()
        await mailings.record_action(
            session,
            settings,
            user,
            case,
            "real-event-1",
            "mailing_message_sent",
            "Одинаковый текст",
            execute_immediately=False,
        )
        await mailings.record_action(
            session,
            settings,
            user,
            case,
            "real-event-2",
            "mailing_message_sent",
            "Одинаковый текст",
            execute_immediately=False,
        )
        actions = list(
            (await session.execute(select(MailingAction).order_by(MailingAction.id))).scalars()
        )

    payloads = [json.loads(action.payload_json) for action in actions]
    assert payloads[0]["mailing_note_text"] != payloads[1]["mailing_note_text"]
    assert all("Событие: mailing_" not in payload["mailing_note_text"] for payload in payloads)
    assert all("[mailing:" not in payload["mailing_note_text"] for payload in payloads)


@pytest.mark.asyncio
async def test_crm_actions_run_in_parallel_across_users_but_keep_user_order(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    first_user_started = asyncio.Event()
    release_first_user = asyncio.Event()
    second_user_completed = asyncio.Event()
    calls: list[int] = []

    async def fake_execute(session, settings, action_id):
        calls.append(action_id)
        if action_id == 1:
            first_user_started.set()
            await release_first_user.wait()
        if action_id == 3:
            second_user_completed.set()
        return True

    monkeypatch.setattr(mailings, "_execute_action", fake_execute)
    async with mailing_db() as session:
        user1 = await _user(session, 1)
        user2 = await _user(session, 2)
        case1 = Case(user_id=user1.id, platform="telegram")
        case2 = Case(user_id=user2.id, platform="telegram")
        session.add_all([case1, case2])
        await session.commit()
        session.add_all(
            [
                MailingAction(user_id=1, case_id=case1.id, action_key="a1", event_type="mailing_message_sent", status="pending"),
                MailingAction(user_id=1, case_id=case1.id, action_key="a2", event_type="mailing_message_sent", status="pending"),
                MailingAction(user_id=2, case_id=case2.id, action_key="b1", event_type="mailing_message_sent", status="pending"),
            ]
        )
        await session.commit()
        task = asyncio.create_task(mailings.retry_pending_actions(session, settings))
        await asyncio.wait_for(first_user_started.wait(), timeout=1)
        await asyncio.wait_for(second_user_completed.wait(), timeout=1)
        assert 2 not in calls
        release_first_user.set()
        await asyncio.wait_for(task, timeout=2)

    assert calls.index(1) < calls.index(2)


@pytest.mark.asyncio
async def test_two_pollers_cannot_send_same_deal_notification(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send(*args, **kwargs):
        started.set()
        await release.wait()
        return True

    crm = SimpleNamespace(get_lead_location=AsyncMock(return_value=("Судебный приказ", "Консультация-НО")))
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", slow_send)
    monkeypatch.setattr(crm_polling, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as setup:
        user = await _user(setup)
        await mailings.ensure_mailing_started(setup, user)
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=900)
        setup.add(case)
        await setup.commit()
        row, _ = await crm_polling._ensure_notification(
            setup,
            deal_id=900,
            case=case,
            user=user,
            lawyer_name="Анна",
            lawyer_phone="+79990000000",
            cycle=1,
        )
        notification_id = row.id

    async with mailing_db() as first, mailing_db() as second:
        task = asyncio.create_task(
            crm_polling._deliver_notification(first, settings, None, notification_id)
        )
        await started.wait()
        assert await crm_polling._deliver_notification(
            second, settings, None, notification_id
        ) is False
        release.set()
        assert await task is True

    async with mailing_db() as session:
        row = await session.get(CrmDealNotification, notification_id)
    assert row.status == "sent"
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_notification_pre_send_timeout_returns_to_pending(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(get_lead_location=AsyncMock(side_effect=TimeoutError("amo timeout")))
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", sender)

    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=910)
        session.add(case)
        await session.commit()
        row, _ = await crm_polling._ensure_notification(
            session,
            deal_id=910,
            case=case,
            user=user,
            lawyer_name="Анна",
            lawyer_phone="+79990000000",
            cycle=1,
        )

        assert await crm_polling._deliver_notification(session, settings, None, row.id) is False
        await session.refresh(row)

    assert row.status == "pending"
    assert row.claimed_at is None
    assert row.lease_until is None
    assert row.uncertain_at is None
    assert "pre-delivery check failed" in row.error_message
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_post_send_timeout_becomes_uncertain(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    crm = SimpleNamespace(get_lead_location=AsyncMock(side_effect=[
        (settings.amocrm_pipeline_name, "Консультация-НО"),
        TimeoutError("amo timeout after send"),
    ]))
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(crm_polling, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(crm_polling, "_send_user_message", sender)

    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=911)
        session.add(case)
        await session.commit()
        row, _ = await crm_polling._ensure_notification(
            session,
            deal_id=911,
            case=case,
            user=user,
            lawyer_name="Анна",
            lawyer_phone="+79990000000",
            cycle=1,
        )

        assert await crm_polling._deliver_notification(session, settings, None, row.id) is False
        await session.refresh(row)

    assert row.status == "uncertain"
    assert row.lease_until is None
    assert row.uncertain_at is not None
    assert "requires reconciliation" in row.error_message
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_polling_delivery_lease_is_not_assumed_sent(mailing_db) -> None:
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=901)
        session.add(case)
        await session.commit()
        row, _ = await crm_polling._ensure_notification(
            session,
            deal_id=901,
            case=case,
            user=user,
            lawyer_name="Анна",
            lawyer_phone="+79990000000",
            cycle=1,
        )
        row.status = "sending"
        row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()
        await crm_polling.recover_notification_leases(session)
        await session.refresh(row)

    assert row.status == "uncertain"
    assert row.sent_at is None
