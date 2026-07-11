import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.enums import CaseStatus
from app.models import Base, Case, User
from app.services.cases import get_or_create_active_case
from app.services.received_date import validate_received_date
from app.utils import parse_russian_date


@pytest.mark.parametrize('raw', [
    '10 07 2026',
    '10/07/26',
    '10/07/2026',
    '10,07,26',
    '10,07,2026',
    '10.07.2026',
    '10-07-2026',
])
def test_received_date_supported_formats(raw):
    assert parse_russian_date(raw) == date(2026, 7, 10)


def test_received_date_ambiguous_keeps_day_month_order():
    assert parse_russian_date('07/10/2026') == date(2026, 10, 7)


@pytest.mark.parametrize('raw', ['07/13/2026', '31.02.2026', '2026-07-10'])
def test_received_date_rejects_invalid_or_iso(raw):
    assert parse_russian_date(raw) is None


def test_received_date_rejects_before_order_and_future():
    case = SimpleNamespace(extracted_json=json.dumps({'order_date': '11.07.2026'}))
    _, error = validate_received_date(case, '10.07.2026', today=date(2026, 7, 11))
    assert 'раньше даты судебного приказа' in error
    case.extracted_json = json.dumps({'order_date': '01.07.2026'})
    _, error = validate_received_date(case, '12.07.2026', today=date(2026, 7, 11))
    assert 'не может быть в будущем' in error


@pytest.mark.asyncio
async def test_new_case_does_not_inherit_previous_received_date():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            user = User(platform="telegram", platform_user_id="1")
            session.add(user)
            await session.flush()
            old_case = Case(
                user_id=user.id,
                platform="telegram",
                platform_user_id="1",
                status=CaseStatus.PROCESSING.value,
                received_date=date(2026, 7, 10),
                deadline_date=date(2026, 7, 20),
                extracted_json=json.dumps({"restore_reason": "old reason"}, ensure_ascii=False),
                full_doc_path="storage/documents/old/full.docx",
                full_pdf_path="storage/documents/old/full.pdf",
                preview_pdf_path="storage/documents/old/preview.pdf",
                preview_doc_path="storage/documents/old/preview.docx",
                instruction_path="storage/documents/old/instruction.docx",
                payment_label="old-payment",
                payment_url="https://example.test/pay",
            )
            session.add(old_case)
            await session.commit()

            fresh_case = await get_or_create_active_case(session, user, force_new=True)

            assert fresh_case.id != old_case.id
            assert fresh_case.received_date is None
            assert fresh_case.deadline_date is None
            assert fresh_case.extracted_json is None
            assert fresh_case.full_doc_path is None
            assert fresh_case.full_pdf_path is None
            assert fresh_case.preview_pdf_path is None
            assert fresh_case.preview_doc_path is None
            assert fresh_case.instruction_path is None
            assert fresh_case.payment_label is None
            assert fresh_case.payment_url is None
            assert old_case.status == CaseStatus.SUPERSEDED.value
    finally:
        await engine.dispose()
