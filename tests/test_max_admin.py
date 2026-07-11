from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.max.admin import handle_admin_update
from app.adapters.max.mapper import IncomingEvent


def _event(*, text=None, callback_data=None):
    return IncomingEvent(
        platform_user_id='100',
        chat_id='200',
        text=text,
        callback_data=callback_data,
    )


@pytest.mark.asyncio
async def test_max_admin_command_opens_panel_for_admin():
    client = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(is_admin=True, is_manager=True)

    handled = await handle_admin_update(client, _event(text='/admin'), SimpleNamespace(), None, user)

    assert handled is True
    kwargs = client.send_message.await_args.kwargs
    assert kwargs['chat_id'] == '200'
    assert 'Админ-панель' in kwargs['text']
    payloads = [button.callback_data for row in kwargs['keyboard'] for button in row]
    assert 'admin:cases:0' in payloads
    assert 'admin:stats' in payloads


@pytest.mark.asyncio
async def test_max_admin_callback_is_denied_for_regular_user():
    client = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(is_admin=False, is_manager=False)

    handled = await handle_admin_update(
        client,
        _event(callback_data='admin:stats'),
        SimpleNamespace(),
        None,
        user,
    )

    assert handled is True
    assert 'только администратору' in client.send_message.await_args.kwargs['text']


@pytest.mark.asyncio
async def test_max_non_admin_update_is_not_consumed():
    client = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(is_admin=False, is_manager=False)

    handled = await handle_admin_update(client, _event(text='привет'), SimpleNamespace(), None, user)

    assert handled is False
    client.send_message.assert_not_awaited()
