from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.max.mapper import IncomingEvent


@pytest.mark.asyncio
async def test_telegram_manager_reply_is_added_to_crm(monkeypatch):
    from app.handlers import chat as module

    customer = SimpleNamespace(id=11, telegram_id=101)
    manager = SimpleNamespace(id=22, is_manager=True)
    active_chat = SimpleNamespace(user=customer)
    case = SimpleNamespace(id=33)
    session = SimpleNamespace(refresh=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(text="Добрый день")
    settings = SimpleNamespace()
    scheduled = []

    monkeypatch.setattr(module, "get_manager_active_session", AsyncMock(return_value=active_chat))
    monkeypatch.setattr(module, "save_message", AsyncMock())
    monkeypatch.setattr(module, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(module, "schedule_crm_sync", lambda *args: scheduled.append(args))

    await module.relay_chat_message(message, bot, session, manager, settings)

    assert scheduled == [
        (settings, 33, 11, "manager_reply_sent", {"note": "Сообщение менеджера: Добрый день"})
    ]


@pytest.mark.asyncio
async def test_max_manager_reply_is_added_to_crm(monkeypatch):
    from app.adapters.max import chat as module

    customer = SimpleNamespace(id=11, platform_user_id="101")
    manager = SimpleNamespace(id=22, is_manager=True)
    active_chat = SimpleNamespace(user=customer)
    case = SimpleNamespace(id=33)
    session = SimpleNamespace(refresh=AsyncMock())
    client = SimpleNamespace(send_message=AsyncMock())
    event = IncomingEvent(platform_user_id="202", chat_id="202", text="Добрый день")
    settings = SimpleNamespace()
    scheduled = []

    monkeypatch.setattr(module, "get_manager_active_session", AsyncMock(return_value=active_chat))
    monkeypatch.setattr(module, "save_message", AsyncMock())
    monkeypatch.setattr(module, "latest_open_case", AsyncMock(return_value=case))
    monkeypatch.setattr(module, "schedule_crm_sync", lambda *args: scheduled.append(args))

    handled = await module._relay_message(client, event, settings, session, manager)

    assert handled is True
    assert scheduled == [
        (settings, 33, 11, "manager_reply_sent", {"note": "Сообщение менеджера: Добрый день"})
    ]
