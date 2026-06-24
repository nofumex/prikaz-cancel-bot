from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiogram import Bot
from aiogram.types import FSInputFile

from app.config import Settings
from app.database import SessionLocal
from app.enums import CaseStatus
from app.services.payments import mark_paid_by_label, verify_yoomoney_sign

logger = logging.getLogger(__name__)


async def _deliver(bot: Bot, case) -> None:
    await bot.send_message(case.user.telegram_id, "Оплата подтверждена. Отправляю полный комплект документов.")
    if case.full_doc_path:
        await bot.send_document(case.user.telegram_id, FSInputFile(case.full_doc_path), caption="Полный вариант заявления.")
    if case.full_pdf_path:
        await bot.send_document(case.user.telegram_id, FSInputFile(case.full_pdf_path), caption="Полный PDF.")
    if case.instruction_path:
        await bot.send_document(case.user.telegram_id, FSInputFile(case.instruction_path), caption="Инструкция по отправке в суд.")


async def run_payment_webhook(bot: Bot, settings: Settings) -> None:
    async def yoomoney(request: web.Request) -> web.Response:
        form = {key: value for key, value in (await request.post()).items()}
        if not verify_yoomoney_sign(form, settings.yoomoney_notification_secret):
            return web.Response(status=403, text="bad sign")
        label = str(form.get("label") or "")
        if not label:
            return web.Response(status=400, text="missing label")
        async with SessionLocal() as session:
            case = await mark_paid_by_label(session, label, form)
            if case:
                await session.refresh(case, ["user"])
                if case.user.platform == "telegram" and case.user.telegram_id:
                    await _deliver(bot, case)
                    case.status = CaseStatus.DELIVERED.value
                    await session.commit()
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_post("/payments/yoomoney", yoomoney)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.payment_web_host, settings.payment_web_port)
    await site.start()
    logger.info("Payment webhook started at http://%s:%s/payments/yoomoney", settings.payment_web_host, settings.payment_web_port)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
