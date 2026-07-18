from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import get_settings
from app.enums import CaseStatus
from app.handlers.case_flow import _extract_and_process_order
from app.keyboards.common import envelope_choice, order_rephoto_menu
from app.models import Case, User
from app.services.legal_data import normalize_order_data
from app.adapters.max.keyboards import envelope_choice as max_envelope_choice, order_rephoto_menu as max_order_rephoto_menu
from app.adapters.max.mapper import parse_update, sanitize_raw_update


def _make_settings(**kwargs):
    settings = get_settings()
    return settings.__class__(**{**settings.__dict__, **kwargs})


def _case(**kwargs) -> Case:
    base = dict(
        id=1,
        user_id=1,
        platform="telegram",
        status=CaseStatus.PROCESSING.value,
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
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
    )
    base.update(kwargs)
    return Case(**base)


def test_max_callback_inline_keyboard_is_not_attachment():
    event = parse_update({
        'update_type': 'message_callback',
        'callback': {'payload': 'chat:end', 'user': {'user_id': 17572856}},
        'message': {
            'recipient': {'chat_id': 999},
            'sender': {'is_bot': True},
            'body': {'attachments': [{'type': 'inline_keyboard'}]},
        },
    })
    assert event is not None
    assert event.callback_data == 'chat:end'
    assert event.platform_user_id == '17572856'
    assert event.chat_id == '999'
    assert event.has_raw_attachment is False
    assert event.photo_url is None
    assert event.document_url is None


def test_max_file_attachment_reads_top_level_filename_and_camelcase_id():
    event = parse_update({
        'update_type': 'message_created',
        'message': {
            'recipient': {'chat_id': 999},
            'sender': {'user_id': 17572856, 'is_bot': False},
            'body': {'attachments': [{
                'type': 'file',
                'filename': 'IMG_20260711_125455.jpg',
                'payload': {'url': 'https://example.test/file.jpg', 'token': 'secret', 'fileId': 4216717167},
            }]},
        },
    })
    assert event is not None
    assert event.document_name == 'IMG_20260711_125455.jpg'
    assert event.document_url == 'https://example.test/file.jpg'
    assert event.document_token == 'secret'
    assert event.attachment_id == '4216717167'


@pytest.mark.asyncio
async def test_after_known_date_requests_phone_before_preview_and_payment(monkeypatch):
    from app.handlers.case_flow import CaseStates
    settings = _make_settings(show_user_confirmation_step=False, amocrm_enabled=False)
    case = _case()
    user = User(id=1, platform="telegram", platform_user_id="1")
    message = SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock()))
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock(), set_state=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())
    mock_generate = AsyncMock(return_value=True)
    monkeypatch.setattr("app.handlers.case_flow._generate_documents_flow", mock_generate)
    monkeypatch.setattr(
        "app.handlers.case_flow.extract_order_data",
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
    monkeypatch.setattr("app.handlers.case_flow.get_amocrm_service", lambda settings: SimpleNamespace(build_ocr_note=AsyncMock(return_value="note")))

    await _extract_and_process_order(message, state, session, settings, case, user)

    assert mock_generate.await_count == 0
    assert state.set_state.await_count == 1
    assert state.set_state.await_args.args[0].state == CaseStates.waiting_payment_contact.state
    assert message.answer.await_args.args[0] == '<b>Укажите свой номер телефона для связи с судом</b>\n\nНажмите кнопку \"Поделиться контактом\" снизу'


@pytest.mark.asyncio
async def test_telegram_order_without_received_date_prompts_for_date(monkeypatch):
    from app.handlers.case_flow import CaseStates

    settings = _make_settings(show_user_confirmation_step=False, amocrm_enabled=False)
    case = _case(received_date=None, deadline_date=None)
    user = User(id=1, platform="telegram", platform_user_id="1")
    message = SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock()))
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock(), set_state=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())
    mock_generate = AsyncMock(return_value=True)
    monkeypatch.setattr("app.handlers.case_flow._generate_documents_flow", mock_generate)
    monkeypatch.setattr(
        "app.handlers.case_flow.extract_order_data",
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
    monkeypatch.setattr("app.handlers.case_flow.get_amocrm_service", lambda settings: SimpleNamespace(build_ocr_note=AsyncMock(return_value="note")))

    await _extract_and_process_order(message, state, session, settings, case, user)

    assert mock_generate.await_count == 0
    assert state.set_state.await_count == 1
    assert state.set_state.await_args.args[0].state == CaseStates.waiting_manual_date.state

    text = message.answer.await_args.args[0]
    assert "<b>✅ Приказ распознан.</b>" in text
    assert "Укажите дату получения судебного приказа." in text
    assert "<code>" in text
    assert "</code>" in text
    assert "Введите дату получения" not in text
    
    assert message.answer_document.await_count == 0


@pytest.mark.asyncio
async def test_missing_required_fields_asks_rephoto_not_manual_edit(monkeypatch):
    settings = _make_settings(show_user_confirmation_step=False, amocrm_enabled=False)
    case = _case(
        extracted_json=json.dumps({"court_name": "x", "debtor_full_name": "x", "creditor_name": "x", "order_date": "01.01.2020", "debt_amount": "1 руб. 00 коп."}),
    )
    user = User(id=1, platform="telegram", platform_user_id="1")
    message = SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock()))
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock(), set_state=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr("app.handlers.case_flow.extract_order_data", AsyncMock(return_value={}))
    monkeypatch.setattr("app.handlers.case_flow.get_amocrm_service", lambda settings: SimpleNamespace(build_ocr_note=AsyncMock(return_value="note")))

    await _extract_and_process_order(message, state, session, settings, case, user)

    assert state.set_state.await_count == 1
    assert order_rephoto_menu().inline_keyboard[0][0].callback_data == "case:rephoto_order"


@pytest.mark.asyncio
async def test_telegram_case_new_starts_fresh_case(monkeypatch):
    from app.handlers.case_flow import start_case

    settings = _make_settings(amocrm_enabled=False)
    previous = _case(received_date=date(2026, 7, 10), deadline_date=date(2026, 7, 20))
    fresh = _case(id=2, received_date=None, deadline_date=None)
    user = User(id=1, platform="telegram", platform_user_id="1")
    event = SimpleNamespace(chat=SimpleNamespace(id="chat-1"), answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock(), set_state=AsyncMock())
    session = SimpleNamespace()
    get_or_create = AsyncMock(return_value=fresh)

    monkeypatch.setattr("app.handlers.case_flow.latest_open_case", AsyncMock(return_value=previous))
    monkeypatch.setattr("app.handlers.case_flow.get_or_create_active_case", get_or_create)
    monkeypatch.setattr("app.handlers.case_flow.schedule_crm_sync", lambda *args, **kwargs: None)

    await start_case(event, state, session, user, settings)

    get_or_create.assert_awaited_once_with(session, user, chat_id="chat-1", force_new=True)
    assert state.set_state.await_count == 1


@pytest.mark.asyncio
async def test_telegram_photo_without_state_auto_creates_and_processes_case(monkeypatch):
    from app.handlers import case_flow

    settings = _make_settings(amocrm_enabled=False)
    fresh = _case(
        id=108,
        status=CaseStatus.WAITING_ORDER_PHOTO.value,
        order_photo_path=None,
        received_date=None,
        deadline_date=None,
    )
    user = User(id=1, platform="telegram", platform_user_id="42")
    message = SimpleNamespace(chat=SimpleNamespace(id=99))
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
    session = SimpleNamespace()
    bot = SimpleNamespace()
    create_case = AsyncMock(return_value=fresh)
    process_photo = AsyncMock()

    monkeypatch.setattr(case_flow, "latest_open_case", AsyncMock(side_effect=[None, None]))
    monkeypatch.setattr(case_flow, "get_or_create_active_case", create_case)
    monkeypatch.setattr(case_flow, "receive_order_photo", process_photo)
    monkeypatch.setattr(case_flow, "schedule_crm_sync", lambda *args, **kwargs: None)

    await case_flow.receive_unscoped_order_photo(message, bot, state, session, settings, user)

    create_case.assert_awaited_once_with(session, user, chat_id="99")
    assert state.set_state.await_args.args[0].state == case_flow.CaseStates.waiting_order_photo.state
    process_photo.assert_awaited_once_with(message, bot, state, session, settings, user)


@pytest.mark.asyncio
async def test_unscoped_photo_after_generated_case_starts_new_case(monkeypatch):
    from app.handlers import case_flow

    previous = _case(
        id=114,
        status=CaseStatus.PAYMENT_PENDING.value,
        order_photo_path="storage/photos/old.jpg",
        received_date=date(2026, 7, 18),
    )
    fresh = _case(
        id=115,
        status=CaseStatus.WAITING_ORDER_PHOTO.value,
        order_photo_path=None,
        received_date=None,
    )
    user = User(id=1, platform="telegram", platform_user_id="42")
    message = SimpleNamespace(chat=SimpleNamespace(id=99))
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
    session = SimpleNamespace()
    create_case = AsyncMock(return_value=fresh)

    monkeypatch.setattr(case_flow, "latest_open_case", AsyncMock(return_value=previous))
    monkeypatch.setattr(case_flow, "get_or_create_active_case", create_case)
    monkeypatch.setattr(case_flow, "schedule_crm_sync", lambda *args, **kwargs: None)

    result = await case_flow._ensure_case_for_unscoped_order_upload(
        message, state, session, _make_settings(amocrm_enabled=False), user
    )

    assert result is fresh
    create_case.assert_awaited_once_with(session, user, chat_id="99", force_new=True)
    assert fresh.received_date is None


@pytest.mark.asyncio
async def test_unscoped_photo_does_not_reuse_rephoto_case_with_old_preview(monkeypatch):
    from app.handlers import case_flow

    previous = _case(
        id=114,
        status=CaseStatus.WAITING_ORDER_REPHOTO.value,
        order_photo_path="storage/photos/rejected.jpg",
        received_date=date(2026, 7, 18),
        preview_pdf_path="storage/documents/case_114/preview.pdf",
    )
    fresh = _case(id=115, status=CaseStatus.WAITING_ORDER_PHOTO.value, received_date=None)
    user = User(id=1, platform="telegram", platform_user_id="42")
    message = SimpleNamespace(chat=SimpleNamespace(id=99))
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
    create_case = AsyncMock(return_value=fresh)

    monkeypatch.setattr(case_flow, "latest_open_case", AsyncMock(return_value=previous))
    monkeypatch.setattr(case_flow, "get_or_create_active_case", create_case)
    monkeypatch.setattr(case_flow, "schedule_crm_sync", lambda *args, **kwargs: None)

    result = await case_flow._ensure_case_for_unscoped_order_upload(
        message, state, SimpleNamespace(), _make_settings(amocrm_enabled=False), user
    )

    assert result is fresh
    create_case.assert_awaited_once()
    assert create_case.await_args.kwargs["force_new"] is True


def test_user_confirmation_step_disabled_by_default():
    get_settings.cache_clear()
    assert not get_settings().show_user_confirmation_step


def test_user_confirmation_step_can_be_enabled_by_env(monkeypatch):
    monkeypatch.setenv("SHOW_USER_CONFIRMATION_STEP", "true")
    get_settings.cache_clear()
    assert get_settings().show_user_confirmation_step is True
    monkeypatch.delenv("SHOW_USER_CONFIRMATION_STEP", raising=False)
    get_settings.cache_clear()


def test_envelope_unreadable_offers_rephoto_or_manual_date():
    assert envelope_choice().inline_keyboard[0][0].text == "📷 Перефотографировать конверт"
    assert envelope_choice().inline_keyboard[1][0].text == "✍️ Ввести дату вручную"
    assert max_envelope_choice()[0][0].text == "📷 Перефотографировать конверт"
    assert max_order_rephoto_menu()[0][0].callback_data == "case:rephoto_order"


def test_telegram_and_max_share_same_auto_flow():
    assert order_rephoto_menu().inline_keyboard[0][0].callback_data == max_order_rephoto_menu()[0][0].callback_data
    assert envelope_choice().inline_keyboard[0][0].text == max_envelope_choice()[0][0].text


def test_admin_edit_fields_still_available():
    from app.keyboards.common import edit_fields_menu

    labels = [button.text for row in edit_fields_menu().inline_keyboard for button in row]
    assert "👤 Должник" in labels
    assert "⚖️ Госпошлина" in labels

@pytest.mark.asyncio
async def test_max_photo_event_without_text_keeps_case_flow(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False, admin_ids=[], max_admin_ids=[])
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock())
    user = User(id=1, platform="max", platform_user_id="42")
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", photo_url="https://example.test/order.jpg")
    handle_order = AsyncMock()

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=max_bot.STATE_ORDER_PHOTO))
    monkeypatch.setattr(max_bot, "_handle_order_image", handle_order)

    await max_bot.handle_update(client, event, settings)

    assert handle_order.await_count == 1
    assert client.send_message.await_count == 0


@pytest.mark.asyncio
async def test_max_manual_date_requests_phone_before_order_processing(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False, admin_ids=[], max_admin_ids=[])
    case = _case(platform="max", platform_user_id="42", received_date=None, deadline_date=None)
    user = User(id=1, platform="max", platform_user_id="42")
    session = SimpleNamespace(get=AsyncMock(return_value=case), commit=AsyncMock())
    client = SimpleNamespace(send_message=AsyncMock())
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", text="19.06.2026")
    generate = AsyncMock()

    async def fake_save_received_date(session_arg, settings_arg, case_arg, user_arg, received):
        case_arg.received_date = received
        case_arg.deadline_date = date(2026, 6, 29)

    monkeypatch.setattr(max_bot, "_state_data", AsyncMock(return_value={"case_id": 1}))
    monkeypatch.setattr(max_bot, "_set_state", AsyncMock())
    monkeypatch.setattr(max_bot, 'save_received_date', fake_save_received_date)
    monkeypatch.setattr(max_bot, "schedule_crm_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(max_bot, '_generate_documents', generate)
    monkeypatch.setattr(max_bot, 'start_document_preparation', lambda *args: None)

    await max_bot._handle_manual_date(client, event, session, settings, user, "19.06.2026")

    assert case.received_date == date(2026, 6, 19)
    assert generate.await_count == 0
    assert max_bot._set_state.await_args.args[2] == max_bot.STATE_PAYMENT_CONTACT
    assert client.send_message.await_args.kwargs["text"] == '<b>Укажите свой номер телефона для связи с судом</b>\n\nНажмите кнопку \"Поделиться контактом\" снизу'

@pytest.mark.asyncio
async def test_max_lost_state_photo_recovers_latest_waiting_case(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False, admin_ids=[], max_admin_ids=[])
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock())
    user = User(id=1, platform="max", platform_user_id="42")
    case = _case(id=77, platform="max", platform_user_id="42", status=CaseStatus.WAITING_ORDER_PHOTO.value, order_photo_path=None)
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", photo_url="https://example.test/order.jpg")
    handle_order = AsyncMock()

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=None))
    monkeypatch.setattr(max_bot, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(max_bot, "_set_state", AsyncMock())
    monkeypatch.setattr(max_bot, "_handle_order_image", handle_order)

    await max_bot.handle_update(client, event, settings)

    assert handle_order.await_count == 1


@pytest.mark.asyncio
async def test_max_attachment_without_active_case_auto_creates_and_processes_case(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import parse_update

    settings = _make_settings(amocrm_enabled=False, admin_ids=[], max_admin_ids=[])
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock())
    user = User(id=1, platform="max", platform_user_id="185607445")
    case = _case(
        id=81,
        platform="max",
        platform_user_id="185607445",
        status=CaseStatus.WAITING_ORDER_PHOTO.value,
        order_photo_path=None,
    )
    event = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": "185607445"},
                "recipient": {"chat_id": "chat-1"},
                "body": {"attachments": [{"type": "image", "payload": {"url": "https://example.test/order.jpg"}}]},
            },
        }
    )

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=None))
    monkeypatch.setattr(max_bot, "latest_open_case", AsyncMock(return_value=None))
    create_case = AsyncMock(return_value=case)
    handle_order = AsyncMock()
    monkeypatch.setattr(max_bot, "get_or_create_active_case", create_case)
    monkeypatch.setattr(max_bot, "_set_state", AsyncMock())
    monkeypatch.setattr(max_bot, "_handle_order_image", handle_order)
    monkeypatch.setattr(max_bot, "schedule_crm_sync", lambda *args, **kwargs: None)

    await max_bot.handle_update(client, event, settings)

    create_case.assert_awaited_once_with(session, user, chat_id="chat-1")
    assert handle_order.await_count == 1
    assert client.send_message.await_count == 0


@pytest.mark.asyncio
async def test_max_lost_state_manual_date_recovers_after_order_photo(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False)
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock())
    user = User(id=1, platform="max", platform_user_id="42")
    case = _case(
        id=78,
        platform="max",
        platform_user_id="42",
        status=CaseStatus.WAITING_ENVELOPE.value,
        order_photo_path="storage/max/order.jpg",
        received_date=None,
        deadline_date=None,
    )
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", text="19.06.2026")
    handle_date = AsyncMock()

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=None))
    monkeypatch.setattr(max_bot, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(max_bot, "_set_state", AsyncMock())
    monkeypatch.setattr(max_bot, "_handle_manual_date", handle_date)

    await max_bot.handle_update(client, event, settings)

    assert handle_date.await_count == 1
    assert client.send_message.await_count == 0


@pytest.mark.asyncio
async def test_max_case_new_starts_fresh_case(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False)
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock())
    user = User(id=1, platform="max", platform_user_id="42")
    case = _case(id=79, platform="max", platform_user_id="42", status=CaseStatus.WAITING_ORDER_PHOTO.value, order_photo_path=None)
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", callback_data="case:new", callback_id="cb-1")
    get_or_create = AsyncMock(return_value=case)

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=None))
    monkeypatch.setattr(max_bot, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(max_bot, "get_or_create_active_case", get_or_create)
    monkeypatch.setattr(max_bot, "_set_state", AsyncMock())
    monkeypatch.setattr(max_bot, "schedule_crm_sync", lambda *args, **kwargs: None)

    await max_bot.handle_update(client, event, settings)

    get_or_create.assert_awaited_once_with(session, user, chat_id="chat-1", force_new=True)
    assert client.send_message.await_count == 1

def test_max_parse_update_accepts_photo_attachment_variants():
    from app.adapters.max.mapper import parse_update

    event = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": "42", "username": "client"},
                "recipient": {"chat_id": "chat-1"},
                "body": {
                    "attachments": [
                        {
                            "type": "photo",
                            "payload": {
                                "photos": [
                                    {"url": "https://example.test/small.jpg"},
                                    {"url": "https://example.test/large.jpg"},
                                ],
                                "token": "photo-token",
                            },
                        }
                    ]
                },
            },
        }
    )

    assert event is not None
    assert event.photo_url == "https://example.test/large.jpg"
    assert event.photo_token == "photo-token"


def test_max_image_payload_token_downloads_file():
    event = parse_update(
        {
            "message": {
                "sender": {"user_id": "42"},
                "recipient": {"chat_id": "chat-1"},
                "body": {"attachments": [{"type": "image", "payload": {"photo": {"token": "token-123"}}}]},
            }
        }
    )

    assert event is not None
    assert event.photo_token == "token-123"
    assert event.has_raw_attachment is True


def test_max_image_payload_url_downloads_file():
    event = parse_update(
        {
            "message": {
                "sender": {"user_id": "42"},
                "recipient": {"chat_id": "chat-1"},
                "attachments": [{"type": "image", "payload": {"image": {"url": "https://example.test/order.jpg"}}}],
            }
        }
    )

    assert event is not None
    assert event.photo_url == "https://example.test/order.jpg"
    assert event.has_raw_attachment is True


def test_max_nested_attachment_downloads_file():
    event = parse_update(
        {
            "message": {
                "sender": {"user_id": "42"},
                "recipient": {"chat_id": "chat-1"},
                "body": {
                    "attachments": [
                        {
                            "attachment_type": "file",
                            "payload": {
                                "video_thumbnail": {"file_id": "thumb-file-id"},
                                "file": {"name": "order.jpg", "url": "https://example.test/order.jpg"},
                            },
                        }
                    ]
                },
            }
        }
    )

    assert event is not None
    assert event.document_url == "https://example.test/order.jpg"
    assert event.document_name == "order.jpg"
    assert event.has_raw_attachment is True


def test_max_debug_raw_update_redacts_tokens():
    cleaned = sanitize_raw_update(
        {
            "message": {
                "body": {
                    "attachments": [
                        {"payload": {"token": "secret-token", "file_id": "file-123", "nested": {"photo_token": "photo-secret"}}}
                    ]
                }
            }
        }
    )

    payload = cleaned["message"]["body"]["attachments"][0]["payload"]
    assert payload["token"] == "***"
    assert payload["nested"]["photo_token"] == "***"
    assert payload["file_id"] == "file-123"


def test_max_exact_image_raw_update_fixture_is_parsed():
    event = parse_update(
        {
            "message": {
                "body": {
                    "attachments": [
                        {
                            "type": "image",
                            "payload": {
                                "photo_id": 24276560405,
                                "token": "redacted",
                                "url": "https://i.oneme.ru/i?r=test",
                            },
                        }
                    ]
                }
            },
            "update_type": "message_created",
        }
    )

    assert event is not None
    assert event.photo_url == "https://i.oneme.ru/i?r=test"
    assert event.photo_token == "redacted"
    assert event.has_raw_attachment is True


@pytest.mark.asyncio
async def test_max_photo_token_counts_as_attachment(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False)
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock())
    user = User(id=1, platform="max", platform_user_id="42")
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", photo_token="token-only")
    handle_order = AsyncMock()

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=max_bot.STATE_ORDER_PHOTO))
    monkeypatch.setattr(max_bot, "_handle_order_image", handle_order)

    await max_bot.handle_update(client, event, settings)

    assert handle_order.await_count == 1
    assert client.send_message.await_count == 0


@pytest.mark.asyncio
async def test_max_photo_is_accepted_as_order(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False)
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock())
    user = User(id=1, platform="max", platform_user_id="42")
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", photo_url="https://example.test/order.jpg")
    handle_order = AsyncMock()

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=max_bot.STATE_ORDER_PHOTO))
    monkeypatch.setattr(max_bot, "_handle_order_image", handle_order)

    await max_bot.handle_update(client, event, settings)

    assert handle_order.await_count == 1
    assert client.send_message.await_count == 0


@pytest.mark.asyncio
async def test_max_active_order_state_does_not_fall_back_to_main_menu(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False)
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock())
    user = User(id=1, platform="max", platform_user_id="42")
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1")

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=max_bot.STATE_ORDER_PHOTO))

    await max_bot.handle_update(client, event, settings)

    assert client.send_message.await_count == 1
    text = client.send_message.await_args.kwargs["text"]
    assert "Нужно фото судебного приказа" in text
    assert settings.company_name not in text

@pytest.mark.asyncio
async def test_max_open_waiting_case_blocks_unknown_command_main_menu(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False)
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock())
    user = User(id=1, platform="max", platform_user_id="42")
    case = _case(id=80, platform="max", platform_user_id="42", status=CaseStatus.WAITING_ORDER_PHOTO.value, order_photo_path=None)
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", text="/unknown")

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=None))
    monkeypatch.setattr(max_bot, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(max_bot, "_set_state", AsyncMock())

    await max_bot.handle_update(client, event, settings)

    assert client.send_message.await_count == 1
    text = client.send_message.await_args.kwargs["text"]
    assert "Нужно фото судебного приказа" in text
    assert settings.company_name not in text
