from __future__ import annotations

import asyncio
import logging

from app.adapters.max import keyboards
from app.adapters.max.client import MaxBotClient
from app.adapters.max.mapper import IncomingEvent, parse_update
from app.config import Settings
from app.database import SessionLocal
from app.services.users import get_or_create_platform_user
from app.texts import profile_text, welcome_text

logger = logging.getLogger(__name__)


async def _send(client: MaxBotClient, event: IncomingEvent, text: str, keyboard=None) -> None:
    await client.send_message(event.chat_id, text, keyboard=keyboard)


async def handle_update(client: MaxBotClient, event: IncomingEvent, settings: Settings) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_platform_user(
            session,
            "max",
            event.platform_user_id,
            settings,
            username=event.username,
            first_name=event.first_name,
            last_name=event.last_name,
        )
        data = event.callback_data
        if event.callback_id:
            await client.answer_callback(event.callback_id)
        if event.text in {"/start", None} or data == "menu:main":
            await _send(client, event, welcome_text(settings.company_name), keyboards.main_menu())
            return
        if data == "profile:show":
            from app.services.cases import latest_case

            await _send(client, event, profile_text(user, await latest_case(session, user.id)), keyboards.main_menu())
            return
        if data == "case:new":
            await _send(
                client,
                event,
                "MAX-версия подключена как интерфейсный каркас. Для полноценной обработки фото сейчас используйте Telegram-бота; "
                "логика документов, оплаты и CRM уже общая и готова к расширению MAX-загрузок.",
                keyboards.main_menu(),
            )
            return
        if data == "chat:start":
            await _send(client, event, settings.manager_contact_text, keyboards.chat_end_menu())
            return
        await _send(client, event, welcome_text(settings.company_name), keyboards.main_menu())


async def run_max_bot(settings: Settings) -> None:
    marker: int | None = None
    async with MaxBotClient(settings.max_bot_token) as client:
        logger.info("MAX polling started")
        while True:
            try:
                payload = await client.get_updates(marker=marker)
                marker = payload.get("marker", marker)
                for raw in payload.get("updates", []):
                    event = parse_update(raw)
                    if event:
                        await handle_update(client, event, settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MAX polling error")
                await asyncio.sleep(3)
