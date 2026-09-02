from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import (
    Base,
    Case,
    CrmDealNotification,
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
    assert notes.count("Система рассылок: отменены будущие задания рассылки") == 1


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
        state.next_stage = 2
        case = Case(user_id=user.id, platform="telegram", amocrm_lead_id=777)
        session.add(case)
        await mailings.cancel_future_jobs(session, user.id)
        await session.commit()

    await crm_polling.poll_crm_mailing_once(settings)
    await crm_polling.poll_crm_mailing_once(settings)

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
    assert (
        "Система рассылок: сделка переведена в «Консультация - НО», пользователь возвращен в рассылку"
        in note_texts
    )

    location[:] = ["Отдел продаж", "Новая заявка"]
    no_leads.clear()
    sales_leads.append({"id": 777})
    await crm_polling.poll_crm_mailing_once(settings)
    await crm_polling.poll_crm_mailing_once(settings)

    async with mailing_db() as session:
        state = await session.scalar(select(MailingState))
        stage_two = await session.scalar(select(MailingJob).where(MailingJob.stage == 2))
    assert state.participating is False
    assert state.excluded_sales is True
    assert stage_two.status == "cancelled"
    note_texts = [call.args[6] for call in note.await_args_list]
    assert "Система рассылок: отменены будущие задания рассылки" in note_texts


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
        )
        row.status = "sending"
        row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()
        await crm_polling.recover_notification_leases(session)
        await session.refresh(row)

    assert row.status == "uncertain"
    assert row.sent_at is None
