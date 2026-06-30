from __future__ import annotations

from dataclasses import dataclass
from typing import Any

IMAGE_ATTACHMENT_TYPES = {"image", "photo"}
FILE_ATTACHMENT_TYPES = {"file", "document"}
ATTACHMENT_TYPES = IMAGE_ATTACHMENT_TYPES | FILE_ATTACHMENT_TYPES
URL_KEYS = ("url", "download_url", "file_url", "photo_url", "image_url", "media_url")
TOKEN_KEYS = ("token", "file_token", "photo_token", "image_token", "media_token", "file_id", "photo_id", "image_id", "media_id")


def _dig(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_text(message: dict[str, Any]) -> str | None:
    body = message.get("body")
    if isinstance(body, dict):
        text = body.get("text")
        if text:
            return str(text)
    elif isinstance(body, str) and body:
        return body
    text = message.get("text")
    return str(text) if text else None


def _select_update_user(update_type: str | None, update: dict[str, Any], callback: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    message_sender = _as_dict(_dig(message, "sender"))
    callback_user = _as_dict(_dig(callback, "user"))
    update_user = _as_dict(update.get("user"))

    if update_type == "message_callback":
        return callback_user or update_user or message_sender
    if update_type == "message_created":
        return message_sender or callback_user or update_user
    if update_type == "bot_started":
        return update_user or message_sender or callback_user
    return message_sender or callback_user or update_user


def _attachment_candidates(update: dict[str, Any], message: dict[str, Any]) -> list[Any]:
    body = _as_dict(message.get("body"))
    candidates: list[Any] = []
    for source in (
        body.get("attachments"),
        body.get("attachment"),
        message.get("attachments"),
        message.get("attachment"),
        update.get("attachments"),
        update.get("attachment"),
    ):
        if isinstance(source, list):
            candidates.extend(source)
        elif isinstance(source, dict):
            candidates.append(source)
    return candidates


def _walk_attachment_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        payload = value.get("payload")
        if isinstance(payload, dict):
            nodes.extend(_walk_attachment_nodes(payload))
        for key in ("photo", "image", "file", "media", "video_thumbnail", "thumbnail"):
            nested = value.get(key)
            if isinstance(nested, dict):
                nodes.extend(_walk_attachment_nodes(nested))
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                nodes.extend(_walk_attachment_nodes(nested))
    elif isinstance(value, list):
        for item in value:
            nodes.extend(_walk_attachment_nodes(item))
    return nodes


def _normalized_attachments(update: dict[str, Any], message: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for candidate in _attachment_candidates(update, message):
        if not isinstance(candidate, dict):
            continue
        att_type = str(candidate.get("type") or candidate.get("attachment_type") or "").lower()
        if att_type in ATTACHMENT_TYPES:
            attachments.append(candidate)
            continue
        payload = _as_dict(candidate.get("payload")) or candidate
        if _payload_url(payload) or _payload_token(payload):
            attachments.append(candidate)
    return attachments


def _payload_url(payload: dict[str, Any]) -> str | None:
    for key in URL_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    for key in ("photo", "image", "file", "media", "video_thumbnail", "thumbnail"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            value = _payload_url(nested)
            if value:
                return value
    photos = payload.get("photos") or payload.get("sizes") or payload.get("variants") or []
    if isinstance(photos, dict):
        photos = list(photos.values())
    if isinstance(photos, list):
        for item in reversed(photos):
            if isinstance(item, dict):
                value = _payload_url(item)
                if value:
                    return value
            elif item:
                return str(item)
    return None


def _payload_token(payload: dict[str, Any]) -> str | None:
    for key in TOKEN_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    for key in ("photo", "image", "file", "media", "video_thumbnail", "thumbnail"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            value = _payload_token(nested)
            if value:
                return value
    return None


def sanitize_raw_update(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret in lowered for secret in ("token", "secret", "authorization")):
                cleaned[key] = "***" if item else item
            else:
                cleaned[key] = sanitize_raw_update(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_raw_update(item) for item in value]
    return value


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
    has_raw_attachment: bool = False
    raw_update: dict[str, Any] | None = None


def parse_update(update: dict[str, Any]) -> IncomingEvent | None:
    update_type = update.get("update_type")
    callback = update.get("callback") or {}
    message = update.get("message") or callback.get("message") or {}
    user = _select_update_user(update_type, update, callback, message)
    platform_user_id = user.get("user_id") or user.get("id")
    chat_id = _dig(message, "recipient", "chat_id") or message.get("chat_id") or update.get("chat_id") or platform_user_id
    if update_type == "message_callback":
        chat_id = _dig(message, "recipient", "chat_id") or chat_id
    raw_attachments = _attachment_candidates(update, message)
    if not platform_user_id and raw_attachments:
        platform_user_id = str(message.get("message_id") or update.get("message_id") or "unknown")
    if not chat_id and raw_attachments:
        chat_id = str(update.get("chat_id") or platform_user_id or message.get("message_id") or "unknown")
    if not platform_user_id or not chat_id:
        return None

    photo_url = None
    photo_token = None
    document_url = None
    document_token = None
    document_name = None
    document_mime = None

    for att in _normalized_attachments(update, message):
        att_type = str(att.get("type") or att.get("attachment_type") or "").lower()
        payload = _as_dict(att.get("payload")) or att
        payload_nodes = _walk_attachment_nodes(payload)
        if payload_nodes:
            for node in payload_nodes:
                if not photo_url:
                    photo_url = _payload_url(node)
                if not photo_token:
                    photo_token = _payload_token(node)
                if not document_url:
                    document_url = _payload_url(node)
                if not document_token:
                    document_token = _payload_token(node)
        if att_type in IMAGE_ATTACHMENT_TYPES:
            if not photo_url:
                photo_url = _payload_url(payload)
            if not photo_token:
                photo_token = _payload_token(payload)
        elif att_type in FILE_ATTACHMENT_TYPES:
            document_token = _payload_token(payload)
            document_mime = payload.get("mime_type") or payload.get("mime") or att.get("mime_type")
            nested_file = payload.get("file") if isinstance(payload.get("file"), dict) else {}
            document_name = (
                payload.get("file_name")
                or payload.get("filename")
                or payload.get("name")
                or nested_file.get("file_name")
                or nested_file.get("filename")
                or nested_file.get("name")
                or att.get("name")
            )
            if not document_url:
                document_url = _payload_url(payload)

    return IncomingEvent(
        platform_user_id=str(platform_user_id),
        chat_id=str(chat_id),
        message_id=str(message.get("message_id") or message.get("id") or ""),
        username=user.get("username"),
        first_name=user.get("first_name") or user.get("name"),
        last_name=user.get("last_name"),
        text="/start" if update_type == "bot_started" else _message_text(message),
        callback_data=(callback.get("payload") or callback.get("data")) if update_type == "message_callback" else None,
        callback_id=callback.get("callback_id") or callback.get("id"),
        photo_url=photo_url,
        photo_token=photo_token,
        document_url=document_url,
        document_token=document_token,
        document_name=document_name,
        document_mime=document_mime,
        has_raw_attachment=bool(raw_attachments),
        raw_update=update,
    )
