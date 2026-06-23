from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class IncomingEvent:
    platform_user_id: str
    chat_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    text: str | None = None
    callback_data: str | None = None
    callback_id: str | None = None
    raw_update: dict[str, Any] | None = None


def _dig(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def parse_update(update: dict[str, Any]) -> IncomingEvent | None:
    update_type = update.get("update_type")
    user = _dig(update, "message", "sender") or _dig(update, "callback", "user") or update.get("user") or {}
    platform_user_id = user.get("user_id") or user.get("id")
    chat_id = _dig(update, "message", "recipient", "chat_id") or _dig(update, "message", "chat_id") or update.get("chat_id") or platform_user_id
    if not platform_user_id or not chat_id:
        return None
    callback = update.get("callback") or {}
    return IncomingEvent(
        platform_user_id=str(platform_user_id),
        chat_id=str(chat_id),
        username=user.get("username"),
        first_name=user.get("first_name") or user.get("name"),
        last_name=user.get("last_name"),
        text="/start" if update_type == "bot_started" else (_dig(update, "message", "body", "text") or _dig(update, "message", "text")),
        callback_data=(callback.get("payload") or callback.get("data")) if update_type == "message_callback" else None,
        callback_id=callback.get("callback_id") or callback.get("id"),
        raw_update=update,
    )
