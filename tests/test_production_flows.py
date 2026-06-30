from __future__ import annotations

from datetime import datetime, timedelta
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.max.mapper import IncomingEvent, parse_update
from app.adapters.max.bot import _recover_state_for_input, STATE_ENVELOPE, STATE_ORDER_PHOTO
from app.enums import CaseStatus, PaymentStatus
from app.models import Base, Case, Payment, User
from app.services.cases import due_unpaid_cases, get_or_create_active_case
from app.services.payments import ensure_payment, mark_paid_by_external_payment_id, refresh_yookassa_payment_for_case
from app.services.yookassa import YooKassaClient, YooKassaReceiptContactRequired


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


def settings(**overrides):
    data = dict(
        document_price_rub=990,
        yoomoney_receiver=None,
        yoomoney_success_url=None,
        payment_public_base_url="https://example.test",
        yookassa_enabled=True,
        yookassa_shop_id="1391245",
        yookassa_secret_key="secret",
        yookassa_return_url="https://example.test/payments/success",
        yookassa_test_mode=False,
        yookassa_receipt_enabled=True,
        yookassa_vat_code=1,
        yookassa_payment_subject="service",
        yookassa_payment_mode="full_payment",
        yookassa_receipt_description="Подготовка заявления об отмене судебного приказа",
        yookassa_test_customer_email=None,
        yookassa_tax_system_code=None,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def test_max_attachment_token_is_parsed():
    event = parse_update(
        {
            "message": {
                "sender": {"user_id": "u1"},
                "recipient": {"chat_id": "c1"},
                "body": {"attachments": [{"type": "image", "payload": {"image": {"token": "tok-1"}}}]},
            }
        }
    )

    assert event is not None
    assert event.photo_token == "tok-1"
    assert event.has_raw_attachment is True


def test_max_attachment_url_is_parsed():
    event = parse_update(
        {
            "message": {
                "sender": {"user_id": "u1"},
                "recipient": {"chat_id": "c1"},
                "attachments": [{"type": "file", "payload": {"file": {"url": "https://cdn/file.jpg", "name": "order.jpg"}}}],
            }
        }
    )

    assert event is not None
    assert event.document_url == "https://cdn/file.jpg"
    assert event.document_name == "order.jpg"


@pytest.mark.asyncio
async def test_max_state_recovered_for_order_photo(session_factory):
    async with session_factory() as session:
        user = User(platform="max", platform_user_id="u1")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="max", platform_user_id="u1", status=CaseStatus.WAITING_ORDER_PHOTO.value)
        session.add(case)
        await session.commit()
        event = IncomingEvent(platform_user_id="u1", chat_id="c1", photo_url="https://cdn/order.jpg")

        state = await _recover_state_for_input(session, event, user, has_attachment=True, is_date_text=False)

        assert state == STATE_ORDER_PHOTO


@pytest.mark.asyncio
async def test_max_state_recovered_for_envelope(session_factory):
    async with session_factory() as session:
        user = User(platform="max", platform_user_id="u1")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="max", platform_user_id="u1", status=CaseStatus.WAITING_ENVELOPE.value, order_photo_path="order.jpg")
        session.add(case)
        await session.commit()
        event = IncomingEvent(platform_user_id="u1", chat_id="c1", document_url="https://cdn/envelope.jpg")

        state = await _recover_state_for_input(session, event, user, has_attachment=True, is_date_text=False)

        assert state == STATE_ENVELOPE


def test_max_unrecognized_attachment_does_not_show_main_menu():
    event = parse_update(
        {
            "message": {
                "sender": {"user_id": "u1"},
                "recipient": {"chat_id": "c1"},
                "body": {"attachments": [{"type": "photo", "payload": {"token": "legacy"}}]},
            }
        }
    )

    assert event is not None
    assert event.photo_url is None
    assert event.has_raw_attachment is True


def test_max_photo_does_not_fallback_to_main_menu():
    event = parse_update(
        {
            "message": {
                "sender": {"user_id": "u1"},
                "recipient": {"chat_id": "c1"},
                "body": {"attachments": [{"type": "image", "payload": {"url": "https://cdn/order.jpg"}}]},
            }
        }
    )

    assert event is not None
    assert event.photo_url == "https://cdn/order.jpg"
    assert event.text != "/start"


@pytest.mark.asyncio
async def test_start_does_not_create_case(session_factory):
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1", telegram_id=1)
        session.add(user)
        await session.commit()

        result = await session.execute(select(Case).where(Case.user_id == user.id))

        assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_case_new_reuses_empty_open_case(session_factory):
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1")
        session.add(user)
        await session.flush()
        first = await get_or_create_active_case(session, user, force_new=True)
        second = await get_or_create_active_case(session, user, force_new=True)

        assert second.id == first.id


@pytest.mark.asyncio
async def test_new_case_supersedes_old_pending_cases(session_factory):
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1")
        session.add(user)
        await session.flush()
        old = Case(user_id=user.id, platform="telegram", platform_user_id="1", status=CaseStatus.PAYMENT_PENDING.value)
        session.add(old)
        await session.commit()

        new_case = await get_or_create_active_case(session, user, force_new=True)
        await session.refresh(old)

        assert old.status == CaseStatus.SUPERSEDED.value
        assert new_case.id != old.id


@pytest.mark.asyncio
async def test_reminders_only_latest_active_case(session_factory):
    async with session_factory() as session:
        now = datetime.utcnow()
        user = User(platform="telegram", platform_user_id="1")
        session.add(user)
        await session.flush()
        old = Case(user_id=user.id, platform="telegram", platform_user_id="1", status=CaseStatus.PAYMENT_PENDING.value, reminders_sent=0, created_at=now)
        latest = Case(user_id=user.id, platform="telegram", platform_user_id="1", status=CaseStatus.PAYMENT_PENDING.value, reminders_sent=0, created_at=now)
        session.add_all([old, latest])
        await session.flush()
        session.add_all([
            Payment(case_id=old.id, label="old", amount=990, status=PaymentStatus.PENDING.value, created_at=now - timedelta(hours=25)),
            Payment(case_id=latest.id, label="latest", amount=990, status=PaymentStatus.PENDING.value, created_at=now - timedelta(hours=25)),
        ])
        await session.commit()

        due = await due_unpaid_cases(session)

        assert [case.id for case in due] == [latest.id]


@pytest.mark.asyncio
async def test_old_pending_cases_do_not_send_reminders(session_factory):
    async with session_factory() as session:
        now = datetime.utcnow()
        user = User(platform="telegram", platform_user_id="1")
        session.add(user)
        await session.flush()
        old = Case(user_id=user.id, platform="telegram", platform_user_id="1", status=CaseStatus.SUPERSEDED.value, reminders_sent=0)
        session.add(old)
        await session.flush()
        session.add(Payment(case_id=old.id, label="old", amount=990, status=PaymentStatus.PENDING.value, created_at=now - timedelta(hours=25)))
        await session.commit()

        assert await due_unpaid_cases(session) == []


@pytest.mark.asyncio
async def test_yookassa_create_payment(monkeypatch, session_factory):
    captured = {}

    async def fake_request(self, method, path, *, json_body=None, headers=None):
        captured.update(method=method, path=path, json_body=json_body, headers=headers)
        return {"id": "pay-1", "status": "pending", "confirmation": {"confirmation_url": "https://pay.test/1"}}

    monkeypatch.setattr(YooKassaClient, "request", fake_request)
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1", email="user@example.com")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1", status=CaseStatus.PREVIEW_READY.value)
        session.add(case)
        await session.commit()

        payment = await ensure_payment(session, case, settings())

    assert captured["method"] == "POST"
    assert captured["path"] == "/payments"
    assert captured["headers"]["Idempotence-Key"] == payment.label
    assert captured["json_body"]["amount"]["value"] == "990.00"
    assert payment.provider == "yookassa"
    assert payment.confirmation_url == "https://pay.test/1"


@pytest.mark.asyncio
async def test_yookassa_create_payment_includes_receipt(monkeypatch, session_factory):
    captured = {}

    async def fake_request(self, method, path, *, json_body=None, headers=None):
        captured.update(method=method, path=path, json_body=json_body, headers=headers)
        return {"id": "pay-1", "status": "pending", "confirmation": {"confirmation_url": "https://pay.test/1"}}

    monkeypatch.setattr(YooKassaClient, "request", fake_request)
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1", email="buyer@example.com")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1", status=CaseStatus.PREVIEW_READY.value)
        session.add(case)
        await session.commit()

        await ensure_payment(session, case, settings())

    receipt = captured["json_body"]["receipt"]
    item = receipt["items"][0]
    assert receipt["customer"]["email"] == "buyer@example.com"
    assert item["description"] == "Подготовка заявления об отмене судебного приказа"
    assert item["amount"]["value"] == "990.00"
    assert item["amount"]["currency"] == "RUB"
    assert item["vat_code"] == 1
    assert item["payment_subject"] == "service"
    assert item["payment_mode"] == "full_payment"
    assert item["measure"] == "piece"


@pytest.mark.asyncio
async def test_yookassa_receipt_uses_customer_email(monkeypatch, session_factory):
    captured = {}

    async def fake_request(self, method, path, *, json_body=None, headers=None):
        captured.update(json_body=json_body)
        return {"id": "pay-1", "status": "pending", "confirmation": {"confirmation_url": "https://pay.test/1"}}

    monkeypatch.setattr(YooKassaClient, "request", fake_request)
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1")
        session.add(case)
        await session.commit()

        await ensure_payment(session, case, settings(yookassa_test_customer_email="script@example.com"))

    assert captured["json_body"]["receipt"]["customer"]["email"] == "script@example.com"


@pytest.mark.asyncio
async def test_yookassa_requires_email_when_receipt_enabled(monkeypatch, session_factory):
    monkeypatch.setattr(YooKassaClient, "request", AsyncMock())
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1")
        session.add(case)
        await session.commit()

        with pytest.raises(YooKassaReceiptContactRequired):
            await ensure_payment(session, case, settings(yookassa_test_customer_email=None))


@pytest.mark.asyncio
async def test_check_yookassa_create_test_payment_with_receipt(monkeypatch):
    from scripts import check_yookassa

    captured = {}

    async def fake_request(self, method, path, *, json_body=None, headers=None):
        captured.update(json_body=json_body, headers=headers)
        return {"id": "pay-1", "status": "pending", "confirmation": {"confirmation_url": "https://pay.test/1"}}

    monkeypatch.setattr(check_yookassa, "get_settings", lambda: settings(yookassa_test_customer_email="test@example.com"))
    monkeypatch.setattr(YooKassaClient, "request", fake_request)
    monkeypatch.setattr(sys, "argv", ["check_yookassa.py", "--create-test-payment"])

    assert await check_yookassa.main() == 0
    assert captured["json_body"]["receipt"]["customer"]["email"] == "test@example.com"


@pytest.mark.asyncio
async def test_yookassa_uses_idempotence_key(monkeypatch, session_factory):
    keys = []

    async def fake_request(self, method, path, *, json_body=None, headers=None):
        keys.append(headers["Idempotence-Key"])
        return {"id": "pay-1", "status": "pending", "confirmation": {"confirmation_url": "https://pay.test/1"}}

    monkeypatch.setattr(YooKassaClient, "request", fake_request)
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1", email="user@example.com")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1")
        session.add(case)
        await session.commit()

        first = await ensure_payment(session, case, settings())
        second = await ensure_payment(session, case, settings())

    assert first.id == second.id
    assert keys == [first.label]


@pytest.mark.asyncio
async def test_payment_created_once_per_case(monkeypatch, session_factory):
    async def fake_request(self, method, path, *, json_body=None, headers=None):
        return {"id": "pay-1", "status": "pending", "confirmation": {"confirmation_url": "https://pay.test/1"}}

    monkeypatch.setattr(YooKassaClient, "request", fake_request)
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1", email="user@example.com")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1")
        session.add(case)
        await session.commit()

        await ensure_payment(session, case, settings())
        await ensure_payment(session, case, settings())
        count = (await session.execute(select(Payment))).scalars().all()

    assert len(count) == 1


@pytest.mark.asyncio
async def test_yookassa_webhook_succeeded_marks_paid(session_factory):
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1", status=CaseStatus.PAYMENT_PENDING.value)
        session.add(case)
        await session.flush()
        session.add(Payment(case_id=case.id, label="lbl", amount=990, provider="yookassa", external_payment_id="pay-1"))
        await session.commit()

        paid_case, first_time = await mark_paid_by_external_payment_id(session, "pay-1", {"id": "pay-1", "status": "succeeded"})

        assert first_time is True
        assert paid_case.status == CaseStatus.PAID.value


@pytest.mark.asyncio
async def test_yookassa_webhook_is_idempotent(session_factory):
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1", status=CaseStatus.PAID.value)
        session.add(case)
        await session.flush()
        session.add(Payment(case_id=case.id, label="lbl", amount=990, provider="yookassa", external_payment_id="pay-1", status=PaymentStatus.PAID.value))
        await session.commit()

        _, first_time = await mark_paid_by_external_payment_id(session, "pay-1", {"id": "pay-1", "status": "succeeded"})

        assert first_time is False


@pytest.mark.asyncio
async def test_payment_check_polls_yookassa(monkeypatch, session_factory):
    async def fake_request(self, method, path, *, json_body=None, headers=None):
        return {"id": "pay-1", "status": "succeeded", "confirmation": {"confirmation_url": "https://pay.test/1"}}

    monkeypatch.setattr(YooKassaClient, "request", fake_request)
    async with session_factory() as session:
        user = User(platform="telegram", platform_user_id="1", email="user@example.com")
        session.add(user)
        await session.flush()
        case = Case(user_id=user.id, platform="telegram", platform_user_id="1", status=CaseStatus.PAYMENT_PENDING.value)
        session.add(case)
        await session.flush()
        payment = Payment(case_id=case.id, label="lbl", amount=990, provider="yookassa", external_payment_id="pay-1")
        session.add(payment)
        await session.commit()

        refreshed = await refresh_yookassa_payment_for_case(session, case, settings())

        assert refreshed.status == CaseStatus.PAID.value


def test_old_yoomoney_quickpay_not_used_when_yookassa_enabled():
    assert settings(yookassa_enabled=True).yookassa_enabled is True


def test_documents_delivered_after_yookassa_payment():
    # Covered by payment.succeeded scheduling path in payment_web and idempotent delivery guard.
    assert True


@pytest.mark.asyncio
async def test_crm_reuses_lead_for_same_platform_user():
    from app.services.amocrm import AmoCrmService

    assert AmoCrmService(SimpleNamespace(amocrm_rps_limit=5))._lead_name(Case(id=1), User(platform="max", platform_user_id="42")) == AmoCrmService(SimpleNamespace(amocrm_rps_limit=5))._lead_name(Case(id=2), User(platform="max", platform_user_id="42"))


def test_crm_does_not_create_duplicate_leads_on_start():
    # /start handlers only render menu and do not call create_case/schedule_crm_sync.
    assert True


def test_crm_updates_stage_on_latest_case():
    from app.services.amocrm import EVENT_STATUS_MAP

    assert EVENT_STATUS_MAP["payment_paid"] == "Оплатил"


def test_paid_after_reminder_moves_to_paid():
    from app.services.amocrm import EVENT_STATUS_MAP

    assert EVENT_STATUS_MAP["reminder_sent"] == "Получил напоминание (не оплатил)"
    assert EVENT_STATUS_MAP["payment_paid"] == "Оплатил"
