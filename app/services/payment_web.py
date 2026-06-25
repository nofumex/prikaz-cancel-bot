from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiogram import Bot
from app.config import Settings
from app.database import SessionLocal
from app.services.crm_background import schedule_crm_sync
from app.services.document_delivery import schedule_document_delivery
from app.services.payments import mark_paid_by_label, verify_yoomoney_sign

logger = logging.getLogger(__name__)


async def run_payment_webhook(bot: Bot | None, settings: Settings) -> None:
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
                schedule_crm_sync(settings, case.id, case.user.id, "payment_paid", {"payment": label, "note": "Оплата подтверждена webhook"})
                schedule_document_delivery(case.id, settings, telegram_bot=bot)
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

