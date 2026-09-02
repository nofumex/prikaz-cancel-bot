from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base, Case, MailingJob, MailingState, User
from app.services import automatic_mailings as mailings


@pytest.fixture
async def mailing_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(mailings, "SessionLocal", sessions)
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
async def test_restart_recovery_does_not_resend_claimed_job(mailing_db, monkeypatch) -> None:
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(mailings, "_send_user_message", sender)
    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        job = await session.scalar(select(MailingJob))
        job.status = "sending"
        job.claimed_at = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()
        await mailings.recover_uncertain_jobs(session)
        await session.refresh(job)
        await session.refresh(state)
        next_job = await session.scalar(select(MailingJob).where(MailingJob.stage == 2))

    assert job.status == "sent"
    assert state.next_stage == 2
    assert next_job is not None
    sender.assert_not_awaited()


def test_full_campaign_schedule_uses_previous_delivery_as_anchor() -> None:
    base = datetime(2026, 1, 31, 8)
    assert mailings.due_for_stage(2, base) == datetime(2026, 2, 7, 8)
    assert mailings.due_for_stage(3, base) == datetime(2026, 2, 28, 8)
    assert mailings.due_for_stage(4, base) == datetime(2026, 3, 31, 8)
    assert mailings.due_for_stage(7, base) == datetime(2026, 7, 31, 8)
    assert mailings.due_for_stage(9, base) == datetime(2027, 1, 31, 8)
    assert mailings.due_for_stage(11, base) == datetime(2028, 1, 31, 8)


@pytest.mark.asyncio
async def test_amocrm_no_reenables_and_sales_excludes_without_webhook_duplicates(
    mailing_db, monkeypatch
) -> None:
    settings = replace(get_settings(), amocrm_enabled=True)
    location = ["Судебный приказ", "Консультация - НО"]
    crm = SimpleNamespace(get_lead_location=AsyncMock(side_effect=lambda lead_id: tuple(location)))
    sender = AsyncMock(return_value=True)
    note = AsyncMock(return_value=True)
    monkeypatch.setattr(mailings, "get_amocrm_service", lambda settings: crm)
    monkeypatch.setattr(mailings, "_send_user_message", sender)
    monkeypatch.setattr(mailings, "record_action", note)

    async with mailing_db() as session:
        user = await _user(session)
        state = await mailings.ensure_mailing_started(session, user)
        state.participating = False
        state.consultation_completed = True
        state.next_stage = 2
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=777)
        session.add(case)
        await mailings.cancel_future_jobs(session, user.id)
        await session.commit()

    payload = {"leads[status][0][id]": "777"}
    await mailings.process_amocrm_status_webhook(payload, None, settings)
    await mailings.process_amocrm_status_webhook(payload, None, settings)

    async with mailing_db() as session:
        state = await session.scalar(select(MailingState))
        stage_two = await session.scalar(select(MailingJob).where(MailingJob.stage == 2))
        assert state.participating is True
        assert state.consultation_no is True
        assert state.consultation_completed is False
        assert stage_two.status == "pending"
    assert sender.await_count == 1

    location[:] = ["Отдел продаж", "Новая заявка"]
    await mailings.process_amocrm_status_webhook(payload, None, settings)
    await mailings.process_amocrm_status_webhook(payload, None, settings)

    async with mailing_db() as session:
        state = await session.scalar(select(MailingState))
        stage_two = await session.scalar(select(MailingJob).where(MailingJob.stage == 2))
    assert state.participating is False
    assert state.excluded_sales is True
    assert stage_two.status == "cancelled"
