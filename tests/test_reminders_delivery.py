from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.enums import CaseStatus, PaymentStatus
from app.handlers.case_flow import deliver_full_documents
from app.models import Base, Case, Payment, User
from app.services.cases import due_unpaid_cases
from app.services.document_delivery import delivery_instruction_text
from app.texts import deadline_warning, payment_text


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
    assert sent_file.path == str(docx)
    assert "Инструкция по подаче" in caption
    assert "Срок подачи: до 29.06.2026" in caption
    assert case.status == CaseStatus.DELIVERED.value


def test_delivery_instruction_text_is_caption_sized() -> None:
    text = delivery_instruction_text(SimpleNamespace(deadline_date=date(2026, 6, 29)))

    assert "Полный вариант заявления DOCX во вложении" in text
    assert "Инструкция по подаче" in text
    assert len(text) < 1024
