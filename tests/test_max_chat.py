from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.max.chat import handle_chat_update
from app.adapters.max.mapper import IncomingEvent


@pytest.mark.asyncio
async def test_max_chat_start_opens_session_and_notifies_staff(monkeypatch):
    from app.adapters.max import chat as module

    chat = SimpleNamespace(id=7)
    user = SimpleNamespace(id=1, is_manager=False, first_name='Иван', last_name=None, username=None, platform_user_id='42')
    client = SimpleNamespace(send_message=AsyncMock())
    session = SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(module, 'open_session', AsyncMock(return_value=chat))
    monkeypatch.setattr(module, '_notify_staff', AsyncMock())
    monkeypatch.setattr(module, 'latest_open_case', AsyncMock(return_value=None))
    event = IncomingEvent(platform_user_id='42', chat_id='100', callback_data='chat:start', update_type='message_callback')

    handled = await handle_chat_update(client, event, SimpleNamespace(), session, user)

    assert handled is True
    module.open_session.assert_awaited_once()
    module._notify_staff.assert_awaited_once()
    assert 'Чат с менеджером открыт' in client.send_message.await_args.kwargs['text']


@pytest.mark.asyncio
async def test_max_chat_end_closes_session_and_returns_menu(monkeypatch):
    from app.adapters.max import chat as module

    user = SimpleNamespace(id=1, is_manager=False, platform_user_id='42')
    active = SimpleNamespace(id=7, user=user, manager=None)
    session = SimpleNamespace(refresh=AsyncMock(), execute=AsyncMock())
    client = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(module, 'get_user_active_session', AsyncMock(return_value=active))
    monkeypatch.setattr(module, 'close_session', AsyncMock())
    event = IncomingEvent(platform_user_id='42', chat_id='100', callback_data='chat:end', update_type='message_callback')

    handled = await handle_chat_update(client, event, SimpleNamespace(), session, user)

    assert handled is True
    module.close_session.assert_awaited_once_with(session, active)
    assert 'Чат завершен' in client.send_message.await_args.kwargs['text']
