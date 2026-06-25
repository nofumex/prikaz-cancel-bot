from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from app.adapters.max.client import MaxBotClient
from app.config import Settings
from app.database import SessionLocal
from app.enums import CaseStatus
from app.models import Case
from app.services.crm_background import schedule_crm_sync

logger = logging.getLogger(__name__)


def schedule_document_delivery(case_id: int, settings: Settings, telegram_bot: Bot | None = None) -> None:
    task = asyncio.create_task(deliver_documents_to_case_platform(case_id, settings, telegram_bot=telegram_bot))
    task.add_done_callback(_consume_delivery_exception)


def _consume_delivery_exception(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Document delivery task crashed")


async def deliver_documents_to_case_platform(case_id: int, settings: Settings, telegram_bot: Bot | None = None) -> None:
    async with SessionLocal() as session:
        case = await session.get(Case, case_id)
        if not case:
            raise RuntimeError(f"Case {case_id} not found")
        await session.refresh(case, ["user"])
        if case.platform == "max":
            await _deliver_to_max(case, settings)
        else:
            if telegram_bot is None:
                raise RuntimeError("Telegram bot object is required for Telegram delivery")
            await _deliver_to_telegram(case, telegram_bot)
        case.status = CaseStatus.DELIVERED.value
        from datetime import datetime
        case.delivered_at = datetime.utcnow()
        await session.commit()
        schedule_crm_sync(settings, case.id, case.user.id, "payment_paid", {"note": "Оплата подтверждена"})
        schedule_crm_sync(settings, case.id, case.user.id, "documents_delivered", {"note": "Клиенту выдан полный комплект документов"})


async def _deliver_to_telegram(case: Case, bot: Bot) -> None:
    if not case.user.telegram_id:
        raise RuntimeError("Telegram user id is empty")
    await bot.send_message(case.user.telegram_id, "Оплата подтверждена. Отправляю полный комплект документов.")
    for path, caption in _delivery_files(case):
        await bot.send_document(case.user.telegram_id, FSInputFile(path), caption=caption)
    await bot.send_message(case.user.telegram_id, "Готово. Не забудьте поставить подпись перед отправкой.")


async def _deliver_to_max(case: Case, settings: Settings) -> None:
    chat_id = case.platform_chat_id or case.platform_user_id or case.user.platform_user_id
    async with MaxBotClient(
        settings.max_bot_token,
        settings.max_api_base_url,
        upload_retry_attempts=settings.max_upload_retry_attempts,
        upload_retry_base_seconds=settings.max_upload_retry_base_seconds,
    ) as client:
        await client.send_message(chat_id=chat_id, text="Оплата подтверждена. Отправляю полный комплект документов.")
        for path, caption in _delivery_files(case):
            await client.send_file(chat_id, path, caption=caption)
        await client.send_message(chat_id=chat_id, text="Готово. Не забудьте поставить подпись перед отправкой.")


def _delivery_files(case: Case) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    if case.full_doc_path and Path(case.full_doc_path).exists():
        files.append((case.full_doc_path, "Полный DOCX."))
    if case.full_pdf_path and Path(case.full_pdf_path).exists():
        files.append((case.full_pdf_path, "Полный PDF."))
    if case.instruction_path and Path(case.instruction_path).exists():
        files.append((case.instruction_path, "Инструкция по отправке в суд."))
    if not files:
        raise RuntimeError("No delivery files found")
    return files
