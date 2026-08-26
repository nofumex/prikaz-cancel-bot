from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.max.bot import handle_update
from app.adapters.max.mapper import IncomingEvent


@pytest.mark.asyncio
async def test_max_outgoing_bot_message_is_ignored_before_creating_user(monkeypatch) -> None:
    session_factory = AsyncMock()
    monkeypatch.setattr("app.adapters.max.bot.SessionLocal", session_factory)
    client = SimpleNamespace(send_message=AsyncMock())
    event = IncomingEvent(
        platform_user_id="331077416",
        chat_id="chat-1",
        message_id="outgoing-1",
        update_type="message_created",
        sender_is_bot=True,
    )

    await handle_update(client, event, SimpleNamespace())

    session_factory.assert_not_called()
