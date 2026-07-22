from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.config import Settings
from app.models import User
from app.services.crm_background import schedule_crm_sync
from app.utils import full_name, platform_id_text

logger = logging.getLogger(__name__)

USER_MESSAGE_CRM_EVENT = "user_message_received"

_SERVICE_COMMANDS = {
    "/start",
    "/admin",
    "/tutor",
    "/endchat",
    "/help",
    "/cancel",
    "/profile",
    "/new",
    "/manager",
}

_SCHEDULED_USER_MESSAGES: set[str] = set()


def _normalize_command(text: str) -> str:
    command = text.strip().split(maxsplit=1)[0].lower()
    if "@" in command:
        command = command.split("@", 1)[0]
    return command


def _build_external_message_id(platform: str, external_message_id: str | None) -> str | None:
    if not external_message_id:
        return None
    return f"{platform}:{external_message_id}"


def _format_note(
    *,
    platform: str,
    user: User,
    text: str,
    message_datetime: datetime | None,
    chat_session_id: int | None,
) -> str:
    aware = message_datetime
    if aware is not None and aware.tzinfo is None:
        aware = aware.replace(tzinfo=UTC)
    timestamp = (aware or datetime.now(tz=UTC)).strftime("%d.%m.%Y %H:%M:%S")
    lines = [
        f"Входящее сообщение ({platform})",
        f"Пользователь: {full_name(user)} (id: {platform_id_text(user)})",
        f"Текст: {text[:500]}",
        f"Дата: {timestamp}",
    ]
    if chat_session_id is not None:
        lines.append(f"Сессия чата: {chat_session_id}")
    return "\n".join(lines)


def schedule_incoming_user_message_crm_sync(
    settings: Settings,
    *,
    platform: str,
    user: User,
    case_id: int | None,
    text: str | None,
    chat_session_id: int | None,
    external_message_id: str | None,
    message_datetime: datetime | None = None,
    is_bot: bool = False,
) -> None:
    event = USER_MESSAGE_CRM_EVENT
    if is_bot:
        logger.info("CRM sync skipped event=%s reason=bot_message platform=%s", event, platform)
        return
    normalized_text = (text or "").strip()
    if not normalized_text:
        logger.info("CRM sync skipped event=%s reason=empty_message platform=%s", event, platform)
        return
    if normalized_text.startswith("/"):
        command = _normalize_command(normalized_text)
        if command in _SERVICE_COMMANDS or command.startswith("/"):
            logger.info("CRM sync skipped event=%s reason=service_command platform=%s command=%s", event, platform, command)
            return
    if getattr(user, "is_manager", False):
        logger.info("CRM sync skipped event=%s reason=manager_message platform=%s user_id=%s", event, platform, user.id)
        return
    if case_id is None:
        logger.info(
            "CRM sync skipped event=%s reason=no_case platform=%s user_id=%s external_message_id=%s",
            event,
            platform,
            user.id,
            external_message_id,
        )
        return
    dedupe_key = _build_external_message_id(platform, external_message_id)
    if dedupe_key and dedupe_key in _SCHEDULED_USER_MESSAGES:
        logger.info(
            "CRM sync skipped event=%s reason=duplicate case_id=%s platform=%s external_message_id=%s",
            event,
            case_id,
            platform,
            external_message_id,
        )
        return
    if dedupe_key:
        _SCHEDULED_USER_MESSAGES.add(dedupe_key)
    payload = {
        "platform": platform,
        "direction": "incoming",
        "user_name": full_name(user),
        "user_id": platform_id_text(user),
        "text": normalized_text[:500],
        "datetime": (message_datetime or datetime.now(tz=UTC)).isoformat(),
        "chat_session_id": chat_session_id,
        "external_message_id": external_message_id,
        "note": _format_note(
            platform=platform,
            user=user,
            text=normalized_text,
            message_datetime=message_datetime,
            chat_session_id=chat_session_id,
        ),
    }
    schedule_crm_sync(settings, case_id, user.id, event, payload)
