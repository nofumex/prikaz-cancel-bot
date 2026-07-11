from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Case, CrmSyncLog, Payment, User
from app.services.admin_reporting import client_path_text, problem_case_error_text, problem_cases_page
from app.services.payments import net_payment_totals, record_manual_refund
from app.services.received_date import received_date_prompt_text
from app.keyboards.common import admin_panel
from app.adapters.max.keyboards import admin_panel as max_admin_panel


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_received_date_prompt_uses_today(monkeypatch):
    from app.services import received_date as received_date_module

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 11)

    monkeypatch.setattr(received_date_module, "date", FixedDate)
    text = received_date_prompt_text()
    assert "<b>✅ Приказ распознан.</b>" in text
    assert "<code>11.07.2026</code>" in text
    assert "10.07.2026" not in text


def test_admin_keyboard_uses_problem_cases_button():
    keyboard = admin_panel()
    payloads = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "admin:problem_cases:0" in payloads
    assert "⚠️ Проблемные заявки" in texts


def test_max_admin_keyboard_uses_problem_cases_button():
    keyboard = max_admin_panel()
    payloads = [button.callback_data for row in keyboard for button in row]
    texts = [button.text for row in keyboard for button in row]
    assert "admin:problem_cases:0" in payloads
    assert "⚠️ Проблемные заявки" in texts


@pytest.mark.asyncio
async def test_problem_cases_page_returns_problem_cases_and_errors(session_factory):
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1")
        session.add(user)
        await session.flush()
        ok_case = Case(user_id=user.id, platform="telegram", platform_user_id="1", created_at=datetime(2026, 7, 9, 12, 0))
        bad_case = Case(user_id=user.id, platform="telegram", platform_user_id="1", created_at=datetime(2026, 7, 10, 12, 0))
        session.add_all([ok_case, bad_case])
        await session.flush()
        session.add(
            CrmSyncLog(
                case_id=bad_case.id,
                user_id=user.id,
                event_type="document_qa_failed",
                request_payload='{"payload":{"note":"OCR не смог прочитать дату"}}',
                success=True,
            )
        )
        session.add(
            CrmSyncLog(
                case_id=ok_case.id,
                user_id=user.id,
                event_type="user_started_bot",
                success=True,
            )
        )
        await session.commit()

        cases, total, errors = await problem_cases_page(session, 0, 5)

        assert total == 1
        assert [case.id for case in cases] == [bad_case.id]
        assert errors[bad_case.id] == "OCR не смог прочитать дату"
        assert await problem_case_error_text(session, bad_case.id) == "OCR не смог прочитать дату"


@pytest.mark.asyncio
async def test_manual_refund_updates_totals_once_and_logs_event(session_factory):
    async with session_factory() as session:
        admin = User(platform="telegram", platform_user_id="100", is_admin=True)
        user = User(platform="telegram", platform_user_id="1")
        session.add_all([admin, user])
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1")
        session.add(case)
        await session.flush()
        payment = Payment(
            case_id=case.id,
            label="pay-1",
            amount=990,
            provider="yookassa",
            status="paid",
            paid_at=datetime.utcnow() - timedelta(minutes=5),
        )
        session.add(payment)
        await session.commit()

        refunded_payment, applied = await record_manual_refund(session, case, admin)
        assert applied is True
        assert refunded_payment is not None
        assert refunded_payment.refunded_at is not None

        second_payment, second_applied = await record_manual_refund(session, case, admin)
        assert second_payment is not None
        assert second_applied is False

        payment_count, payment_sum, yookassa_count, yookassa_sum = await net_payment_totals(session)
        assert payment_count == 0
        assert payment_sum == 0
        assert yookassa_count == 0
        assert yookassa_sum == 0

        log = await session.scalar(select(CrmSyncLog).where(CrmSyncLog.event_type == "refund_recorded"))
        assert log is not None
        path = await client_path_text(session, case.id)
        assert "Возврат учтен админом" in path
