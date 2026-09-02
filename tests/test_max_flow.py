from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.max.bot import _download_event_image
from app.adapters.max.mapper import IncomingEvent
from app.config import get_settings
from app.enums import CaseStatus
from app.models import Case, User
from app.services.consultations import consultation_notification_ids
from app.services.legal_data import normalize_order_data


def _make_settings(**kwargs):
    settings = get_settings()
    return settings.__class__(**{**settings.__dict__, **kwargs})


def _case(**kwargs) -> Case:
    base = dict(
        id=1,
        user_id=1,
        platform="max",
        status=CaseStatus.PROCESSING.value,
        received_date=None,
        deadline_date=None,
        extracted_json=json.dumps(
            normalize_order_data(
                {
                    "court_name": "судебный участок №5 города Ессентуки",
                    "debtor_full_name": "Иванов Иван Иванович",
                    "creditor_name": "АО «Почта Банк»",
                    "case_number": "2-146-09-434/2021",
                    "uid": "26MS0031-01-2021-000169-72",
                    "order_date": "18.01.2021",
                    "debt_amount": "78 472 руб. 87 коп.",
                    "state_duty": "1 277 руб. 00 коп.",
                    "total_amount": "79 749 руб. 87 коп.",
                }
            ),
            ensure_ascii=False,
        ),
        order_rephoto_attempts=0,
        order_photo_path="storage/max/order.jpg",
    )
    base.update(kwargs)
    return Case(**base)


@pytest.mark.asyncio
async def test_max_automatic_consultation_notifies_three_telegram_accounts(
    monkeypatch,
) -> None:
    from app.adapters.max import bot as max_bot

    settings = _make_settings(admin_ids={123456789}, telegram_bot_token="test-token")
    expected_ids = consultation_notification_ids(settings, test_mode=False)
    assert len(expected_ids) == 3
    sent_to = []

    class FakeSession:
        async def close(self):
            return None

    class FakeTelegramBot:
        def __init__(self, token):
            self.session = FakeSession()

        async def send_message(self, chat_id, text, parse_mode=None):
            sent_to.append(chat_id)

    monkeypatch.setattr("aiogram.Bot", FakeTelegramBot)
    max_client = SimpleNamespace(send_message=AsyncMock())

    await max_bot._notify_consultation_staff_max(
        max_client, settings, "new consultation", test_mode=False
    )

    assert set(sent_to) == expected_ids
    max_client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_max_automatic_consultation_notifies_staff_after_durable_send(
    monkeypatch,
) -> None:
    from app.adapters.max import bot as max_bot

    settings = _make_settings()
    user = User(id=1, platform="max", platform_user_id="42")
    case = Case(id=5, user_id=user.id, platform="max")
    event = IncomingEvent(
        platform_user_id="42", chat_id="chat-1", contact_phone="+79990000000"
    )
    session = SimpleNamespace(get=AsyncMock(return_value=case), commit=AsyncMock())
    notifier = AsyncMock()
    monkeypatch.setattr(
        max_bot,
        "_state_data",
        AsyncMock(
            return_value={
                "automatic_mailing_consultation": True,
                "consultation_case_id": case.id,
            }
        ),
    )
    monkeypatch.setattr(max_bot, "save_campaign_phone", AsyncMock())
    monkeypatch.setattr(max_bot, "prepare_consultation", AsyncMock(return_value=True))
    monkeypatch.setattr(max_bot, "deliver_pavel_message", AsyncMock(return_value=True))
    monkeypatch.setattr(max_bot, "_notify_consultation_staff_max", notifier)
    monkeypatch.setattr(max_bot, "_clear_state", AsyncMock())

    await max_bot._handle_consultation_phone(
        SimpleNamespace(), event, session, settings, user
    )

    notifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_rephoto_downloads_use_unique_paths(tmp_path) -> None:
    async def download(url, destination):
        destination.write_bytes(url.encode())
        return destination

    client = SimpleNamespace(download_external_url=download)
    settings = SimpleNamespace(max_download_dir=str(tmp_path))
    first = IncomingEvent(platform_user_id='1', chat_id='1', message_id='mid.first', photo_url='first')
    second = IncomingEvent(platform_user_id='1', chat_id='1', message_id='mid.second', photo_url='second')

    first_path = await _download_event_image(client, first, 71, 'order', settings)
    second_path = await _download_event_image(client, second, 71, 'order', settings)

    assert first_path != second_path
    assert first_path.read_bytes() == b'first'
    assert second_path.read_bytes() == b'second'


@pytest.mark.asyncio
async def test_max_order_without_received_date_prompts_for_date(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False)
    case = _case()
    user = User(id=1, platform="max", platform_user_id="42")
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", photo_url="https://example.test/order.jpg")
    client = SimpleNamespace(send_message=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())
    generate = AsyncMock()

    monkeypatch.setattr(max_bot, "_clear_state", AsyncMock())
    monkeypatch.setattr(max_bot, "_set_state", AsyncMock())
    monkeypatch.setattr(max_bot, "_generate_documents", generate)
    monkeypatch.setattr(
        max_bot,
        "extract_order_data",
        AsyncMock(
            return_value={
                "court_name": "судебный участок №5 города Ессентуки",
                "debtor_full_name": "Иванов Иван Иванович",
                "creditor_name": "АО «Почта Банк»",
                "case_number": "2-146-09-434/2021",
                "uid": "26MS0031-01-2021-000169-72",
                "order_date": "18.01.2021",
                "debt_amount": "78 472 руб. 87 коп.",
                "state_duty": "1 277 руб. 00 коп.",
                "total_amount": "79 749 руб. 87 коп.",
            }
        ),
    )
    monkeypatch.setattr(max_bot, "schedule_crm_sync", lambda *args, **kwargs: None)

    await max_bot._extract_and_process_order(client, event, session, settings, user, case)

    assert generate.await_count == 0
    assert max_bot._set_state.await_count == 1
    assert max_bot.DATE_PROMPT in client.send_message.await_args_list[-1].kwargs["text"]
    assert max_bot.STATE_MANUAL_DATE in [call.args[2] for call in max_bot._set_state.await_args_list]


@pytest.mark.asyncio
async def test_max_precheck_failure_immediately_requests_order_rephoto(monkeypatch):
    from app.adapters.max import bot as max_bot

    settings = _make_settings(amocrm_enabled=False)
    case = _case()
    user = User(id=1, platform="max", platform_user_id="42")
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1")
    client = SimpleNamespace(send_message=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(max_bot, "_clear_state", AsyncMock())
    monkeypatch.setattr(max_bot, "_set_state", AsyncMock())
    monkeypatch.setattr(
        max_bot,
        "extract_order_data",
        AsyncMock(return_value={"_document_kind": "other", "_preflight_blocked": "1"}),
    )
    monkeypatch.setattr(max_bot, "schedule_crm_sync", lambda *args, **kwargs: None)

    await max_bot._extract_and_process_order(client, event, session, settings, user, case)

    assert case.status == CaseStatus.WAITING_ORDER_REPHOTO.value
    assert json.loads(case.missing_fields) == ["not_court_order"]
    max_bot._set_state.assert_awaited_once_with(
        session, event, max_bot.STATE_ORDER_REPHOTO, {"case_id": case.id}
    )
    assert "судебный приказ" in client.send_message.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_max_generation_passes_custom_restore_reason(monkeypatch):
    from app.adapters.max import bot as max_bot

    reason = "Причина пропуска срока: не смогла войти в Госуслуги из-за сбоя интернета"
    case = _case(
        received_date=date(2021, 2, 1),
        deadline_date=date(2021, 2, 11),
        extracted_json=json.dumps(
            {
                **json.loads(_case().extracted_json),
                "restore_reason": reason,
            },
            ensure_ascii=False,
        ),
    )
    user = User(id=1, platform="max", platform_user_id="42")
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", text=reason)
    client = SimpleNamespace(send_message=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())
    settings = _make_settings(amocrm_enabled=False, admin_ids=set(), max_admin_ids=set())
    create_documents = AsyncMock(
        return_value=SimpleNamespace(ok=False, artifacts=None, admin_report="test stop")
    )

    monkeypatch.setattr(max_bot, "_send_pending_ocr_confirmation", AsyncMock(return_value=False))
    monkeypatch.setattr(max_bot, "missing_order_fields", lambda *_args: [])
    monkeypatch.setattr(max_bot, "create_case_documents_reviewed", create_documents)
    monkeypatch.setattr(max_bot, "schedule_crm_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(max_bot, "_notify_admin_document_review_failure", AsyncMock())

    await max_bot._generate_documents(client, event, session, settings, user, case)

    assert create_documents.await_args.kwargs["restore_reason"] == reason
