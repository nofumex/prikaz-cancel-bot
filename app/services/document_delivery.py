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
        if case.delivered_at:
            return
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
        schedule_crm_sync(
            settings,
            case.id,
            case.user.id,
            "documents_delivered",
            {
                "note": "Полные документы: DOCX и инструкция выданы",
                "files": [
                    {"path": case.full_doc_path or "", "caption": "Полный DOCX"},
                    {"path": case.full_pdf_path or "", "caption": "Полный PDF"},
                ],
            },
        )


def delivery_instruction_text(case: Case) -> str:
    deadline = case.deadline_date.strftime("%d.%m.%Y") if case.deadline_date else None
    lines = [
        "Оплата подтверждена. Полный вариант заявления DOCX во вложении.",
        "",
        "Инструкция по подаче:",
        "1. Откройте DOCX и проверьте свои данные.",
        "2. Распечатайте заявление.",
        "3. Поставьте дату и подпись от руки.",
        "4. Подайте заявление мировому судье, который вынес приказ, или отправьте заказным письмом.",
    ]
    if deadline:
        lines.append(f"Срок подачи: до {deadline}.")
    return "\n".join(lines)


async def _deliver_to_telegram(case: Case, bot: Bot) -> None:
    if not case.user.telegram_id:
        raise RuntimeError("Telegram user id is empty")
    path = _full_docx_file(case)
    await bot.send_document(case.user.telegram_id, FSInputFile(path), caption=delivery_instruction_text(case))


async def _deliver_to_max(case: Case, settings: Settings) -> None:
    chat_id = case.platform_chat_id or case.platform_user_id or case.user.platform_user_id
    async with MaxBotClient(
        settings.max_bot_token,
        settings.max_api_base_url,
        upload_retry_attempts=settings.max_upload_retry_attempts,
        upload_retry_base_seconds=settings.max_upload_retry_base_seconds,
    ) as client:
        await client.send_file(chat_id, _full_docx_file(case), caption=delivery_instruction_text(case))


def _full_docx_file(case: Case) -> str:
    if case.full_doc_path and Path(case.full_doc_path).exists():
        return case.full_doc_path
    raise RuntimeError("Full DOCX file not found")
