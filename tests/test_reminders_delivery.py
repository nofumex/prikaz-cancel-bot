from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.max import keyboards as max_keyboards
from app.adapters.max.client import MaxApiError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.enums import CaseStatus, PaymentStatus
from app.handlers.case_flow import deliver_full_documents
from app.keyboards.common import consultation_menu as telegram_consultation_menu
from app.models import Base, Case, Payment, User
from app.services.cases import (
    due_case_consultation_reminders,
    due_no_order_cases,
    due_paid_followup_cases,
    due_started_users_without_cases,
    due_unpaid_cases,
    due_user_consultation_reminders,
)
from app.services.document_delivery import delivery_instruction_text
from app.services.reminders import _send_user_message
from app.texts import consultation_offer_text, deadline_warning, no_order_deadline_reminder_text, payment_text, post_payment_court_followup_text, unpaid_document_reminder_text


@pytest.mark.parametrize("reminder_no", [1, 2, 3])
def test_deadline_warning_starts_with_prepared_statement(reminder_no: int) -> None:
    text = deadline_warning(date(2026, 6, 29), reminder_no)
    expected = "<b>\u0412\u0430\u0448\u0435 \u0437\u0430\u044f\u0432\u043b\u0435\u043d\u0438\u0435 \u0443\u0436\u0435 \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043b\u0435\u043d\u043e.</b>"

    assert text.startswith(expected)
    assert "preview PDF" not in text
    assert "/3" not in text


def test_payment_text_promises_docx_and_text_instruction_only() -> None:
    case = SimpleNamespace(deadline_date=date(2026, 6, 29))

    text = payment_text(case, 2000)

    assert "полный DOCX" in text
    assert "инструкцию по отправке в суд текстом" in text
    assert "полный PDF" not in text


@pytest.mark.asyncio
async def test_due_unpaid_cases_uses_payment_age_and_reminder_gap() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()

    async with session_factory() as session:
        user = User(id=1, platform="telegram", platform_user_id="1", telegram_id=1)
        fresh = Case(id=1, user_id=1, platform="telegram", status=CaseStatus.PAYMENT_PENDING.value, reminders_sent=0, created_at=now)
        due_first = Case(id=2, user_id=1, platform="telegram", status=CaseStatus.PAYMENT_PENDING.value, reminders_sent=0, created_at=now)
        no_catchup = Case(
            id=3,
            user_id=1,
            platform="telegram",
            status=CaseStatus.PAYMENT_PENDING.value,
            reminders_sent=1,
            last_reminder_at=now - timedelta(hours=2),
            created_at=now,
        )
        due_second = Case(
            id=4,
            user_id=1,
            platform="telegram",
            status=CaseStatus.PAYMENT_PENDING.value,
            reminders_sent=1,
            last_reminder_at=now - timedelta(hours=24),
            created_at=now,
        )
        session.add_all(
            [
                user,
                fresh,
                due_first,
                no_catchup,
                due_second,
                Payment(case_id=1, label="fresh", amount=2000, status=PaymentStatus.PENDING.value, created_at=now - timedelta(hours=23, minutes=30)),
                Payment(case_id=2, label="due-1", amount=2000, status=PaymentStatus.PENDING.value, created_at=now - timedelta(hours=24, minutes=1)),
                Payment(case_id=3, label="no-catchup", amount=2000, status=PaymentStatus.PENDING.value, created_at=now - timedelta(hours=60)),
                Payment(case_id=4, label="due-2", amount=2000, status=PaymentStatus.PENDING.value, created_at=now - timedelta(hours=49)),
            ]
        )
        await session.commit()

        due_ids = {case.id for case in await due_unpaid_cases(session)}

    await engine.dispose()
    assert due_ids == {4}



@pytest.mark.asyncio
async def test_due_no_order_cases_after_one_day() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()

    async with session_factory() as session:
        users = [User(id=i, platform="telegram", platform_user_id=str(i), telegram_id=i) for i in range(1, 5)]
        fresh = Case(user_id=1, platform="telegram", platform_user_id="1", status=CaseStatus.WAITING_ORDER_PHOTO.value, created_at=now - timedelta(hours=23))
        due = Case(user_id=2, platform="telegram", platform_user_id="2", status=CaseStatus.WAITING_ORDER_PHOTO.value, created_at=now - timedelta(hours=25))
        sent = Case(user_id=3, platform="telegram", platform_user_id="3", status=CaseStatus.WAITING_ORDER_PHOTO.value, created_at=now - timedelta(hours=26), deadline_reminder_sent_at=now - timedelta(hours=1))
        with_order = Case(user_id=4, platform="telegram", platform_user_id="4", status=CaseStatus.WAITING_ENVELOPE.value, order_photo_path="order.jpg", created_at=now - timedelta(hours=26))
        session.add_all([*users, fresh, due, sent, with_order])
        await session.commit()

        due_cases = await due_no_order_cases(session)

    await engine.dispose()
    assert [case.id for case in due_cases] == [due.id]


@pytest.mark.asyncio
async def test_due_started_users_without_cases_after_one_day() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()

    async with session_factory() as session:
        fresh = User(id=1, platform="telegram", platform_user_id="1", telegram_id=1, created_at=now - timedelta(hours=23))
        due = User(id=2, platform="telegram", platform_user_id="2", telegram_id=2, created_at=now - timedelta(hours=25))
        with_case = User(id=3, platform="telegram", platform_user_id="3", telegram_id=3, created_at=now - timedelta(hours=25))
        session.add_all([fresh, due, with_case])
        await session.flush()
        session.add(Case(user_id=with_case.id, platform="telegram", platform_user_id="3", status=CaseStatus.WAITING_ORDER_PHOTO.value, created_at=now - timedelta(hours=25)))
        await session.commit()

        users = await due_started_users_without_cases(session)

    await engine.dispose()
    assert [user.id for user in users] == [due.id]


@pytest.mark.asyncio
async def test_due_paid_followup_cases_after_two_days() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()

    async with session_factory() as session:
        user = User(id=1, platform="telegram", platform_user_id="1")
        fresh = Case(user_id=1, platform="telegram", platform_user_id="1", status=CaseStatus.PAID.value, paid_at=now - timedelta(hours=47))
        due = Case(user_id=1, platform="telegram", platform_user_id="1", status=CaseStatus.PAID.value, paid_at=now - timedelta(hours=49))
        already_sent = Case(user_id=1, platform="telegram", platform_user_id="1", status=CaseStatus.DELIVERED.value, paid_at=now - timedelta(hours=49), post_payment_followup_sent_at=now)
        session.add_all([user, fresh, due, already_sent])
        await session.commit()

        cases = await due_paid_followup_cases(session)

    await engine.dispose()
    assert [case.id for case in cases] == [due.id]


@pytest.mark.asyncio
async def test_due_consultation_reminders_one_day_after_prior_followup() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()

    async with session_factory() as session:
        user = User(id=1, platform="telegram", platform_user_id="1")
        fresh = Case(user_id=1, platform="telegram", platform_user_id="1", status=CaseStatus.PAYMENT_PENDING.value, deadline_reminder_sent_at=now - timedelta(hours=23))
        due = Case(user_id=1, platform="telegram", platform_user_id="1", status=CaseStatus.PAYMENT_PENDING.value, deadline_reminder_sent_at=now - timedelta(hours=25))
        paid_due = Case(user_id=1, platform="telegram", platform_user_id="1", status=CaseStatus.PAID.value, post_payment_followup_sent_at=now - timedelta(hours=25))
        already_sent = Case(user_id=1, platform="telegram", platform_user_id="1", status=CaseStatus.PAYMENT_PENDING.value, deadline_reminder_sent_at=now - timedelta(hours=25), consultation_reminder_sent_at=now)
        session.add_all([user, fresh, due, paid_due, already_sent])
        await session.commit()

        cases = await due_case_consultation_reminders(session)

    await engine.dispose()
    assert {case.id for case in cases} == {due.id, paid_due.id}


@pytest.mark.asyncio
async def test_due_user_consultation_reminders_skips_users_with_cases() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()

    async with session_factory() as session:
        due = User(id=1, platform="telegram", platform_user_id="1", first_deadline_reminder_sent_at=now - timedelta(hours=25))
        with_case = User(id=2, platform="telegram", platform_user_id="2", first_deadline_reminder_sent_at=now - timedelta(hours=25))
        fresh = User(id=3, platform="telegram", platform_user_id="3", first_deadline_reminder_sent_at=now - timedelta(hours=23))
        session.add_all([due, with_case, fresh])
        await session.flush()
        session.add(Case(user_id=with_case.id, platform="telegram", platform_user_id="2", status=CaseStatus.WAITING_ORDER_PHOTO.value))
        await session.commit()

        users = await due_user_consultation_reminders(session)

    await engine.dispose()
    assert [user.id for user in users] == [due.id]


def test_new_reminder_texts_render_readable_russian() -> None:
    no_order_text = no_order_deadline_reminder_text()
    unpaid_text = unpaid_document_reminder_text()
    paid_text = post_payment_court_followup_text()
    consult_text = consultation_offer_text()

    assert "\u0421\u0440\u043e\u043a\u0438 \u043d\u0430 \u043e\u0442\u043c\u0435\u043d\u0443" in no_order_text
    assert "\u043e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0435\u0433\u043e \u0432 \u0431\u043e\u0442" in no_order_text
    assert "\u0412\u0430\u0448 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442 \u0443\u0436\u0435 \u0433\u043e\u0442\u043e\u0432" in unpaid_text
    assert "\u0417\u0430\u0432\u0435\u0440\u0448\u0438\u0442\u0435 \u043e\u043f\u043b\u0430\u0442\u0443" in unpaid_text
    assert "\u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043b\u0438 \u043f\u0435\u0440\u0435\u0434\u0430\u0442\u044c" in paid_text
    assert "\u0421\u0438\u043d\u0430\u0439" in consult_text
    assert "???" not in no_order_text + unpaid_text + paid_text + consult_text


def test_consultation_reminder_keyboards_open_manager_chat() -> None:
    telegram_keyboard = telegram_consultation_menu()
    max_keyboard = max_keyboards.consultation_menu()

    tg_rows = telegram_keyboard.inline_keyboard
    assert len(tg_rows) == 1
    assert len(tg_rows[0]) == 1
    assert tg_rows[0][0].text == "\U0001f4ac \u0421\u0432\u044f\u0437\u0430\u0442\u044c\u0441\u044f \u0441 \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440\u043e\u043c"
    assert tg_rows[0][0].callback_data == "chat:start"

    assert len(max_keyboard) == 1
    assert len(max_keyboard[0]) == 1
    assert max_keyboard[0][0].text == "\U0001f4ac \u0421\u0432\u044f\u0437\u0430\u0442\u044c\u0441\u044f \u0441 \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440\u043e\u043c"
    assert max_keyboard[0][0].callback_data == "chat:start"


@pytest.mark.asyncio
async def test_deliver_full_documents_sends_only_docx_with_instruction(tmp_path) -> None:
    docx = tmp_path / "statement.docx"
    pdf = tmp_path / "statement.pdf"
    instruction = tmp_path / "instruction.docx"
    docx.write_bytes(b"docx")
    pdf.write_bytes(b"pdf")
    instruction.write_bytes(b"instruction")
    case = Case(
        id=10,
        user_id=1,
        platform="telegram",
        status=CaseStatus.PAID.value,
        full_doc_path=str(docx),
        full_pdf_path=str(pdf),
        instruction_path=str(instruction),
        deadline_date=date(2026, 6, 29),
    )
    message = SimpleNamespace(answer_document=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())

    await deliver_full_documents(message, session, case)

    message.answer_document.assert_awaited_once()
    sent_file = message.answer_document.await_args.args[0]
    caption = message.answer_document.await_args.kwargs["caption"]
    reply_markup = message.answer_document.await_args.kwargs["reply_markup"]
    assert sent_file.path == str(docx)
    assert "Инструкция по подаче" in caption
    assert "Срок подачи: до 29.06.2026" in caption
    assert reply_markup.inline_keyboard[0][0].text == "❌ Данные в заявлении неверные"
    assert reply_markup.inline_keyboard[0][0].callback_data == "paid:correction:start:10"
    assert case.status == CaseStatus.DELIVERED.value


def test_delivery_instruction_text_is_caption_sized() -> None:
    text = delivery_instruction_text(SimpleNamespace(deadline_date=date(2026, 6, 29)))

    assert "Полный вариант заявления DOCX во вложении" in text
    assert "Инструкция по подаче" in text
    assert len(text) < 1024


@pytest.mark.asyncio
async def test_max_suspended_dialog_isolated_and_disabled(monkeypatch) -> None:
    async def denied(*args, **kwargs):
        raise MaxApiError(403, {'code': 'chat.denied', 'message': 'error.dialog.suspended'})

    monkeypatch.setattr('app.services.reminders._send_max_message', denied)
    monkeypatch.setattr('app.services.reminders.schedule_crm_sync', lambda *args, **kwargs: None)
    user = User(id=7, platform='max', platform_user_id='98496219')

    sent = await _send_user_message(SimpleNamespace(), None, user, 'test')

    assert sent is False
    assert user.reminder_delivery_blocked_at is not None
    assert 'chat.denied' in user.reminder_delivery_error
