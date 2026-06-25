from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _dig_attachments(data: dict[str, Any]) -> list[dict] | None:
    attachments = data.get("attachments") or []
    return [a for a in attachments if a.get("type") in {"image", "file"}]


def _get_photo_url(attachments: list[dict] | None) -> str | None:
    for att in (attachments or []):
        if att.get("type") == "image":
            return att.get("payload", {}).get("url")
    return None


def _dig(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


@dataclass(slots=True)
class IncomingEvent:
    platform_user_id: str
    chat_id: str
    message_id: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    text: str | None = None
    callback_data: str | None = None
    callback_id: str | None = None
    photo_url: str | None = None
    photo_token: str | None = None
    document_url: str | None = None
    document_token: str | None = None
    document_name: str | None = None
    document_mime: str | None = None
    raw_update: dict[str, Any] | None = None


def parse_update(update: dict[str, Any]) -> IncomingEvent | None:
    update_type = update.get("update_type")
    user = _dig(update, "message", "sender") or _dig(update, "callback", "user") or update.get("user") or {}
    platform_user_id = user.get("user_id") or user.get("id")
    chat_id = _dig(update, "message", "recipient", "chat_id") or _dig(update, "message", "chat_id") or update.get("chat_id") or platform_user_id
    if not platform_user_id or not chat_id:
        return None
    callback = update.get("callback") or {}
    message = update.get("message") or {}
    attachments = _dig(message, "body", "attachments") or _dig(message, "attachments") or []
    
    photo_url = None
    photo_token = None
    document_url = None
    document_token = None
    document_name = None
    document_mime = None
    
    for att in attachments:
        if att.get("type") == "image":
            payload = att.get("payload") or {}
            if not photo_url:
                photo_url = payload.get("url")
            if not photo_token:
                photo_token = payload.get("token")
        elif att.get("type") == "file":
            payload = att.get("payload") or {}
            document_token = payload.get("token")
            document_mime = _dig(att, "payload", "mime_type")
            document_name = _dig(att, "payload", "file_name") or _dig(att, "payload", "name")
            if not document_url:
                document_url = payload.get("url")
    
    return IncomingEvent(
        platform_user_id=str(platform_user_id),
        chat_id=str(chat_id),
        message_id=str(message.get("message_id") or message.get("id") or ""),
        username=user.get("username"),
        first_name=user.get("first_name") or user.get("name"),
        last_name=user.get("last_name"),
        text="/start" if update_type == "bot_started" else (message.get("body", {}).get("text") or message.get("text")),
        callback_data=(callback.get("payload") or callback.get("data")) if update_type == "message_callback" else None,
        callback_id=callback.get("callback_id") or callback.get("id"),
        photo_url=photo_url,
        photo_token=photo_token,
        document_url=document_url,
        document_token=document_token,
        document_name=document_name,
        document_mime=document_mime,
        raw_update=update,
    )
