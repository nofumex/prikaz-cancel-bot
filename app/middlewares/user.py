from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import Settings
from app.database import SessionLocal
from app.services.cases import ensure_user_has_case
from app.services.crm_background import schedule_crm_sync
from app.services.users import get_or_create_telegram_user


class DbUserMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = None
        if isinstance(event, Message):
            tg_user = event.from_user
        elif isinstance(event, CallbackQuery):
            tg_user = event.from_user
        async with SessionLocal() as session:
            data["session"] = session
            if tg_user:
                user = await get_or_create_telegram_user(session, tg_user, self.settings)
                data["current_user"] = user
                if self.settings.amocrm_enabled and not user.is_admin and not user.is_manager:
                    case, created = await ensure_user_has_case(
                        session,
                        user,
                        chat_id=str(getattr(getattr(event, "chat", None), "id", "") or user.platform_user_id),
                    )
                    if created or not (case.amocrm_lead_id or case.amo_lead_id):
                        schedule_crm_sync(
                            self.settings,
                            case.id,
                            user.id,
                            "user_started_bot" if created else "crm_reconciliation",
                            {"note": "Telegram: пользователь зарегистрирован в боте"},
                        )
            return await handler(event, data)
