from __future__ import annotations

from datetime import datetime
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ChatStatus
from app.models import ChatMessage, ChatSession, User

logger = logging.getLogger(__name__)


def add_inactivity_notification_ref(chat: ChatSession, platform: str, message_id, chat_id) -> None:
    refs = json.loads(chat.inactivity_notification_refs or "[]")
    refs.append({"platform": platform, "message_id": str(message_id), "chat_id": str(chat_id)})
    chat.inactivity_notification_refs = json.dumps(refs)


async def delete_inactivity_notifications(chat: ChatSession, settings, *, bot=None, max_client=None) -> None:
    refs = json.loads(chat.inactivity_notification_refs or "[]")
    own_bot = None
    own_max = None
    try:
        for ref in refs:
            try:
                if ref["platform"] == "telegram":
                    if bot is None:
                        from aiogram import Bot
                        own_bot = own_bot or Bot(settings.bot_token)
                    await (bot or own_bot).delete_message(int(ref["chat_id"]), int(ref["message_id"]))
                else:
                    if max_client is None:
                        from app.adapters.max.client import MaxBotClient
                        own_max = own_max or MaxBotClient(settings.max_bot_token, settings.max_api_base_url)
                        await own_max.__aenter__()
                    await (max_client or own_max).delete_message(ref["message_id"])
            except Exception:
                logger.exception("Failed to delete inactivity staff notification")
    finally:
        if own_bot:
            await own_bot.session.close()
        if own_max:
            await own_max.__aexit__(None, None, None)
    chat.inactivity_notification_refs = None


async def close_inactivity_sessions(session: AsyncSession, user_id: int, settings) -> None:
    result = await session.execute(select(ChatSession).where(ChatSession.user_id == user_id, ChatSession.inactivity_notification_refs.is_not(None)))
    for chat in result.scalars():
        await delete_inactivity_notifications(chat, settings)
        if chat.status in ("open", "active"):
            chat.status = "closed"
            chat.closed_at = datetime.utcnow()


async def open_session(session: AsyncSession, user: User) -> ChatSession:
    existing = await get_user_active_session(session, user.id)
    if existing:
        return existing
    chat = ChatSession(user_id=user.id, status=ChatStatus.OPEN.value)
    session.add(chat)
    await session.commit()
    await session.refresh(chat)
    return chat


async def get_session(session: AsyncSession, session_id: int) -> ChatSession | None:
    return await session.get(ChatSession, session_id)


async def get_user_active_session(session: AsyncSession, user_id: int) -> ChatSession | None:
    result = await session.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user_id, ChatSession.status.in_([ChatStatus.OPEN.value, ChatStatus.ACTIVE.value]))
        .order_by(ChatSession.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_manager_active_session(session: AsyncSession, manager_id: int) -> ChatSession | None:
    result = await session.execute(
        select(ChatSession)
        .where(ChatSession.manager_id == manager_id, ChatSession.status == ChatStatus.ACTIVE.value)
        .order_by(ChatSession.connected_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def connect_manager(session: AsyncSession, chat: ChatSession, manager: User) -> tuple[ChatSession, bool, bool]:
    busy = await get_manager_active_session(session, manager.id)
    if busy and busy.id != chat.id:
        return chat, False, True
    await session.refresh(chat)
    if chat.manager_id and chat.manager_id != manager.id:
        return chat, False, False
    chat.manager_id = manager.id
    chat.status = ChatStatus.ACTIVE.value
    chat.connected_at = chat.connected_at or datetime.utcnow()
    await session.commit()
    await session.refresh(chat)
    return chat, True, False


async def close_session(session: AsyncSession, chat: ChatSession) -> None:
    chat.status = ChatStatus.CLOSED.value
    chat.closed_at = datetime.utcnow()
    await session.commit()


async def save_message(session: AsyncSession, chat: ChatSession, sender: User, text: str, role: str) -> None:
    session.add(ChatMessage(session_id=chat.id, sender_id=sender.id, text=text, sender_role=role))
    await session.commit()
