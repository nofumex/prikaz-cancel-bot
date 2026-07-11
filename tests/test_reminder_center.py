from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.enums import CaseStatus
from app.models import Base, Case, User
from app.services.reminder_center import reminder_counts, reminder_dashboard_text
from app.services.reminder_center import send_manual_reminders


@pytest.mark.asyncio
async def test_reminder_dashboard_counts_pending_and_sent_groups():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        users = [
            User(id=1, platform='telegram', platform_user_id='1'),
            User(id=2, platform='max', platform_user_id='2', first_deadline_reminder_sent_at=datetime.utcnow()),
            User(id=3, platform='telegram', platform_user_id='3'),
            User(id=4, platform='max', platform_user_id='4'),
        ]
        cases = [
            Case(user_id=3, platform='telegram', platform_user_id='3', status=CaseStatus.PAYMENT_PENDING.value),
            Case(user_id=4, platform='max', platform_user_id='4', status=CaseStatus.DELIVERED.value),
        ]
        session.add_all(users + cases)
        await session.commit()
        counts = await reminder_counts(session)
    await engine.dispose()

    assert counts['try'] == {'pending': 1, 'sent': 1}
    assert counts['pay']['pending'] == 1
    assert counts['consultation']['pending'] == 1
    text = reminder_dashboard_text(counts)
    assert 'Попробовать бота — 1' in text
    assert 'Оплатить preview — 1' in text


@pytest.mark.asyncio
async def test_manual_pay_reminder_marks_case_and_emits_crm_event(monkeypatch):
    from app.services import reminder_center as module

    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    events = []
    monkeypatch.setattr(module, '_send_case_message', AsyncMock(return_value=True))
    monkeypatch.setattr(module, 'reminder_settings', lambda: {
        'reminder_try_text': 'try', 'reminder_pay_text': 'pay', 'reminder_consultation_text': 'consult'
    })
    monkeypatch.setattr(module, 'schedule_crm_sync', lambda *args: events.append(args))
    settings = type('Settings', (), {'telegram_bot_token': ''})()
    async with factory() as session:
        user = User(id=1, platform='telegram', platform_user_id='1', telegram_id=1)
        case = Case(user_id=1, platform='telegram', platform_user_id='1', status=CaseStatus.PAYMENT_PENDING.value)
        session.add_all([user, case])
        await session.commit()
        sent, failed = await send_manual_reminders(session, settings, object(), 'pay')
        await session.refresh(case)
        assert case.deadline_reminder_sent_at is not None
    await engine.dispose()
    assert (sent, failed) == (1, 0)
    assert events[0][3] == 'reminder_sent'
