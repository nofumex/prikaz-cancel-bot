from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

from app.adapters.max.mapper import IncomingEvent


@pytest.mark.asyncio
async def test_telegram_user_message_is_added_to_crm(monkeypatch):
    from app.handlers import chat as module

    customer = SimpleNamespace(id=11, telegram_id=101, is_manager=False, first_name="Иван", last_name=None, username=None, platform="telegram", platform_user_id="101")
    active_chat = SimpleNamespace(id=7, manager=None)
    case = SimpleNamespace(id=33)
    session = SimpleNamespace(refresh=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(
        text="тест",
        chat=SimpleNamespace(id=555),
        message_id=42,
        date=None,
        from_user=SimpleNamespace(is_bot=False),
        answer=AsyncMock(),
    )
    settings = SimpleNamespace()
    scheduled = []

    monkeypatch.setattr(module, "get_user_active_session", AsyncMock(return_value=active_chat))
    monkeypatch.setattr(module, "save_message", AsyncMock())
    monkeypatch.setattr(module, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(module, "schedule_chat_message_crm_sync", lambda *args, **kwargs: scheduled.append((args, kwargs)))

    await module.relay_chat_message(message, bot, session, customer, settings)

    assert len(scheduled) == 1
    _, kwargs = scheduled[0]
    assert kwargs["platform"] == "telegram"
    assert kwargs["case_id"] == 33
    assert kwargs["text"] == "тест"
    assert kwargs["sender_role"] == "user"
    assert kwargs["chat_session_id"] == 7
    assert kwargs["external_message_id"] == "555:42"


@pytest.mark.asyncio
async def test_max_user_message_is_added_to_crm(monkeypatch):
    from app.adapters.max import chat as module

    customer = SimpleNamespace(id=11, platform_user_id="101", is_manager=False, first_name="Иван", last_name=None, username=None, platform="max")
    active_chat = SimpleNamespace(id=7, manager=None)
    case = SimpleNamespace(id=33)
    session = SimpleNamespace(refresh=AsyncMock())
    client = SimpleNamespace(send_message=AsyncMock())
    event = IncomingEvent(platform_user_id="101", chat_id="100", text="тест", message_id="mid-1")
    settings = SimpleNamespace(max_admin_ids=[])
    scheduled = []

    monkeypatch.setattr(module, "get_user_active_session", AsyncMock(return_value=active_chat))
    monkeypatch.setattr(module, "save_message", AsyncMock())
    monkeypatch.setattr(module, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(module, "get_staff", AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "schedule_chat_message_crm_sync", lambda *args, **kwargs: scheduled.append((args, kwargs)))

    handled = await module._relay_message(client, event, settings, session, customer)

    assert handled is True
    assert len(scheduled) == 1
    _, kwargs = scheduled[0]
    assert kwargs["platform"] == "max"
    assert kwargs["case_id"] == 33
    assert kwargs["text"] == "тест"
    assert kwargs["sender_role"] == "user"
    assert kwargs["external_message_id"] == "mid-1"


@pytest.mark.asyncio
async def test_user_message_crm_sync_is_deduped_in_scheduler(monkeypatch):
    from app.services import chat_crm_sync as module

    module._SCHEDULED_USER_MESSAGES.clear()
    settings = SimpleNamespace(amocrm_enabled=True, crm_sync_background=True)
    user = SimpleNamespace(id=11, is_manager=False, first_name="Иван", last_name=None, username=None, platform="telegram", telegram_id=101, platform_user_id="101")
    scheduled = []
    monkeypatch.setattr(module, "schedule_crm_sync", lambda *args: scheduled.append(args))

    module.schedule_incoming_user_message_crm_sync(
        settings,
        platform="telegram",
        user=user,
        case_id=33,
        text="тест",
        chat_session_id=7,
        external_message_id="555:42",
    )
    module.schedule_incoming_user_message_crm_sync(
        settings,
        platform="telegram",
        user=user,
        case_id=33,
        text="тест",
        chat_session_id=7,
        external_message_id="555:42",
    )

    assert len(scheduled) == 1
    assert scheduled[0][3] == "user_message_received"


def test_user_message_received_dedupe_key_is_stable():
    from app.services.amocrm import crm_event_dedupe_key

    first = crm_event_dedupe_key(
        33,
        "user_message_received",
        {"platform": "telegram", "external_message_id": "555:42"},
    )
    same = crm_event_dedupe_key(
        33,
        "user_message_received",
        {"platform": "telegram", "external_message_id": "555:42", "text": "other"},
    )
    different = crm_event_dedupe_key(
        33,
        "user_message_received",
        {"platform": "max", "external_message_id": "mid-1"},
    )

    assert first == same
    assert first != different


def test_chat_message_payload_contains_real_attachment(tmp_path):
    from app.services.chat_crm_sync import build_chat_message_payload

    attachment = tmp_path / "order.jpg"
    attachment.write_bytes(b"real image bytes")
    user = SimpleNamespace(
        id=11,
        first_name="Иван",
        last_name=None,
        username=None,
        platform="telegram",
        telegram_id=101,
        platform_user_id="101",
    )

    payload = build_chat_message_payload(
        platform="telegram",
        customer=user,
        text="[вложение: фото]",
        sender_role="user",
        chat_session_id=7,
        external_message_id="chat:15",
        attachment_path=str(attachment),
        attachment_name="приказ.jpg",
    )

    assert payload["files"] == [{"path": str(attachment), "caption": "приказ.jpg"}]


@pytest.mark.asyncio
async def test_telegram_chat_attachment_is_downloaded(monkeypatch, tmp_path):
    from app.handlers import chat as module

    async def download(_file_id, *, destination):
        destination.write_bytes(b"telegram photo")

    monkeypatch.chdir(tmp_path)
    bot = SimpleNamespace(download=download)
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_id="photo-file-id")],
        document=None,
        video=None,
        voice=None,
        audio=None,
        sticker=None,
        chat=SimpleNamespace(id=101),
        message_id=55,
    )

    path, name, attachment_type = await module._store_telegram_attachment(bot, message)

    assert Path(path).read_bytes() == b"telegram photo"
    assert name == "photo.jpg"
    assert attachment_type == "photo"


@pytest.mark.asyncio
async def test_telegram_manager_reply_is_added_to_crm(monkeypatch):
    from app.handlers import chat as module

    customer = SimpleNamespace(id=11, telegram_id=101)
    manager = SimpleNamespace(id=22, is_manager=True)
    active_chat = SimpleNamespace(id=7, user=customer)
    case = SimpleNamespace(id=33)
    session = SimpleNamespace(refresh=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(text="?????? ????")
    settings = SimpleNamespace()
    scheduled = []

    monkeypatch.setattr(module, "get_manager_active_session", AsyncMock(return_value=active_chat))
    monkeypatch.setattr(module, "save_message", AsyncMock())
    monkeypatch.setattr(module, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(module, "schedule_chat_message_crm_sync", lambda *args, **kwargs: scheduled.append((args, kwargs)))

    await module.relay_chat_message(message, bot, session, manager, settings)

    assert len(scheduled) == 1
    assert scheduled[0][0][0] is settings
    assert scheduled[0][1]["case_id"] == 33
    assert scheduled[0][1]["sender_role"] == "manager"
    assert scheduled[0][1]["text"] == message.text


@pytest.mark.asyncio
async def test_max_manager_reply_is_added_to_crm(monkeypatch):
    from app.adapters.max import chat as module

    customer = SimpleNamespace(id=11, platform_user_id="101")
    manager = SimpleNamespace(id=22, is_manager=True)
    active_chat = SimpleNamespace(id=7, user=customer)
    case = SimpleNamespace(id=33)
    session = SimpleNamespace(refresh=AsyncMock())
    client = SimpleNamespace(send_message=AsyncMock())
    event = IncomingEvent(platform_user_id="202", chat_id="202", text="Добрый день")
    settings = SimpleNamespace()
    scheduled = []

    monkeypatch.setattr(module, "get_manager_active_session", AsyncMock(return_value=active_chat))
    monkeypatch.setattr(module, "save_message", AsyncMock())
    monkeypatch.setattr(module, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(module, "schedule_chat_message_crm_sync", lambda *args, **kwargs: scheduled.append((args, kwargs)))

    handled = await module._relay_message(client, event, settings, session, manager)

    assert handled is True
    assert scheduled[0][0][0] is settings
    assert scheduled[0][1]["case_id"] == 33
    assert scheduled[0][1]["sender_role"] == "manager"
    assert scheduled[0][1]["text"] == "Добрый день"
