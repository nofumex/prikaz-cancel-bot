from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.max import bot as max_bot
from app.adapters.max.keyboards import phone_request_keyboard as max_phone_request_keyboard, to_attachments
from app.adapters.max.mapper import IncomingEvent, parse_update
from app.config import get_settings
from app.handlers import case_flow
from app.keyboards.common import phone_request_keyboard
from app.models import Case, User
from app.services.amocrm import AmoCrmService


def _settings(**kwargs):
    settings = get_settings()
    return settings.__class__(**{**settings.__dict__, **kwargs})


def _case(platform: str) -> Case:
    return Case(
        id=11,
        user_id=7,
        platform=platform,
        platform_user_id="42",
        received_date=date(2026, 7, 14),
        deadline_date=date(2026, 7, 24),
        extracted_json=json.dumps({"court_name": "Суд"}, ensure_ascii=False),
    )


def test_phone_share_keyboards_use_native_contact_buttons() -> None:
    telegram_button = phone_request_keyboard().keyboard[0][0]
    assert telegram_button.request_contact is True
    assert telegram_button.text == "Поделиться контактом"

    max_attachment = to_attachments(max_phone_request_keyboard())
    assert max_attachment[0]["payload"]["buttons"][0][0] == {
        "type": "request_contact",
        "text": "Поделиться контактом",
    }


def test_max_mapper_reads_phone_from_incoming_contact_vcard() -> None:
    event = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 42},
                "recipient": {"chat_id": 99},
                "body": {
                    "attachments": [
                        {
                            "type": "contact",
                            "payload": {
                                "vcf_info": (
                                    "BEGIN:VCARD\n"
                                    "VERSION:3.0\n"
                                    "FN:Егор\n"
                                    "TEL;TYPE=CELL:+7 (999) 123-45-67\n"
                                    "END:VCARD"
                                ),
                                "max_info": {"user_id": 42, "name": "Егор"},
                                "hash": "signed",
                            },
                        }
                    ]
                },
            },
        }
    )

    assert event is not None
    assert event.contact_phone == "+7 (999) 123-45-67"


@pytest.mark.asyncio
async def test_telegram_contact_shows_progress_then_starts_generation(monkeypatch) -> None:
    settings = _settings(show_user_confirmation_step=False, amocrm_enabled=False)
    case = _case("telegram")
    user = User(id=7, platform="telegram", platform_user_id="42")
    message = SimpleNamespace(
        contact=SimpleNamespace(phone_number="8 (999) 123-45-67"),
        text=None,
        answer=AsyncMock(),
        bot=SimpleNamespace(),
    )
    state = SimpleNamespace(get_data=AsyncMock(return_value={"case_id": case.id}), clear=AsyncMock())
    session = SimpleNamespace(get=AsyncMock(return_value=case), commit=AsyncMock())
    generate = AsyncMock(return_value=True)
    scheduled = []

    monkeypatch.setattr(case_flow, "_generate_documents_flow", generate)
    monkeypatch.setattr(case_flow, "schedule_crm_sync", lambda *args: scheduled.append(args))

    await case_flow.receive_payment_contact(message, state, session, settings, user)

    assert user.phone == "+79991234567"
    session.commit.assert_awaited_once()
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "<b>🔄 Заявление составляется, нужно немного подождать...</b>"
    assert message.answer.await_args.kwargs["reply_markup"].remove_keyboard is True
    assert scheduled[0][3] == "phone_provided"
    assert generate.await_args.kwargs["remove_phone_keyboard"] is True


@pytest.mark.asyncio
async def test_saved_telegram_phone_skips_contact_prompt(monkeypatch) -> None:
    settings = _settings(show_user_confirmation_step=False, amocrm_enabled=False)
    case = _case("telegram")
    case.missing_fields = "[]"
    user = User(id=7, platform="telegram", platform_user_id="42", phone="+79991234567")
    message = SimpleNamespace(answer=AsyncMock(), bot=SimpleNamespace())
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock(), clear=AsyncMock())
    session = SimpleNamespace()
    generate = AsyncMock(return_value=True)

    monkeypatch.setattr(case_flow, "_generate_documents_flow", generate)

    await case_flow._continue_after_received_date(message, state, session, settings, user, case)

    generate.assert_awaited_once()
    assert state.set_state.await_count == 0
    assert all("Поделиться контактом" not in str(call) for call in message.answer.await_args_list)


@pytest.mark.asyncio
async def test_saved_max_phone_skips_contact_prompt_after_date(monkeypatch) -> None:
    settings = _settings(show_user_confirmation_step=False, amocrm_enabled=False)
    case = _case("max")
    case.missing_fields = "[]"
    user = User(id=7, platform="max", platform_user_id="42", phone="+79991234567")
    event = IncomingEvent(platform_user_id="42", chat_id="99", text="14.07.2026")
    client = SimpleNamespace(send_message=AsyncMock())
    session = SimpleNamespace(get=AsyncMock(return_value=case), commit=AsyncMock())
    generate = AsyncMock()

    monkeypatch.setattr(max_bot, "_state_data", AsyncMock(return_value={"case_id": case.id}))
    monkeypatch.setattr(max_bot, "save_received_date", AsyncMock())
    monkeypatch.setattr(max_bot, "_generate_documents", generate)

    await max_bot._handle_manual_date(client, event, session, settings, user, "14.07.2026")

    generate.assert_awaited_once()
    assert all("Поделиться контактом" not in str(call) for call in client.send_message.await_args_list)


@pytest.mark.asyncio
async def test_max_contact_is_saved_then_generation_starts_without_confirmation_message(monkeypatch) -> None:
    settings = _settings(show_user_confirmation_step=False, amocrm_enabled=False)
    case = _case("max")
    user = User(id=7, platform="max", platform_user_id="42")
    event = IncomingEvent(platform_user_id="42", chat_id="99", contact_phone="+7 999 123-45-67")
    client = SimpleNamespace(send_message=AsyncMock())
    session = SimpleNamespace(get=AsyncMock(return_value=case), commit=AsyncMock())
    generate = AsyncMock()
    scheduled = []

    monkeypatch.setattr(max_bot, "_state_data", AsyncMock(return_value={"case_id": case.id}))
    monkeypatch.setattr(max_bot, "_generate_documents", generate)
    monkeypatch.setattr(max_bot, "schedule_crm_sync", lambda *args: scheduled.append(args))

    await max_bot._handle_payment_contact(client, event, session, settings, user, event.contact_phone)

    assert user.phone == "+79991234567"
    session.commit.assert_awaited_once()
    assert client.send_message.await_count == 0
    assert scheduled[0][3] == "phone_provided"
    generate.assert_awaited_once_with(client, event, session, settings, user, case)


@pytest.mark.asyncio
async def test_max_incoming_contact_vcard_runs_the_phone_flow_end_to_end(monkeypatch) -> None:
    settings = _settings(show_user_confirmation_step=False, amocrm_enabled=False)
    case = _case("max")
    user = User(id=7, platform="max", platform_user_id="42")
    event = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 42},
                "recipient": {"chat_id": 99},
                "body": {
                    "attachments": [
                        {
                            "type": "contact",
                            "payload": {
                                "vcf_info": "BEGIN:VCARD\nVERSION:4.0\nFN:Егор\nTEL;VALUE=uri:tel:+79991234567\nEND:VCARD",
                                "max_info": {"user_id": 42, "name": "Егор"},
                            },
                        }
                    ]
                },
            },
        }
    )
    session = SimpleNamespace(get=AsyncMock(return_value=case), commit=AsyncMock())

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = SimpleNamespace(send_message=AsyncMock(), answer_callback=AsyncMock())
    generate = AsyncMock()

    monkeypatch.setattr(max_bot, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(max_bot, "get_or_create_platform_user", AsyncMock(return_value=user))
    monkeypatch.setattr(max_bot, "_state", AsyncMock(return_value=max_bot.STATE_PAYMENT_CONTACT))
    monkeypatch.setattr(max_bot, "_state_data", AsyncMock(return_value={"case_id": case.id}))
    monkeypatch.setattr(max_bot, "_generate_documents", generate)
    monkeypatch.setattr(max_bot, "schedule_crm_sync", lambda *args: None)

    await max_bot.handle_update(client, event, settings)

    assert user.phone == "+79991234567"
    assert client.send_message.await_count == 0
    generate.assert_awaited_once_with(client, event, session, settings, user, case)


@pytest.mark.asyncio
async def test_phone_event_updates_existing_amocrm_contact(monkeypatch) -> None:
    settings = _settings(amocrm_enabled=True)
    service = AmoCrmService(settings)
    case = _case("telegram")
    case.amocrm_lead_id = 501
    case.amocrm_contact_id = 601
    user = User(
        id=7,
        platform="telegram",
        platform_user_id="42",
        phone="+79991234567",
        amocrm_contact_id=601,
        amocrm_current_case_id=case.id,
    )
    update_contact = AsyncMock(return_value=601)

    monkeypatch.setattr(service, "create_or_update_contact", update_contact)
    monkeypatch.setattr(service, "add_lead_note", AsyncMock())

    await service.sync_case_event(None, case, user, "phone_provided")

    update_contact.assert_awaited_once_with(user)
    assert case.amocrm_contact_id == 601
