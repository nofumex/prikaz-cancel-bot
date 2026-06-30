from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiogram import Bot
from app.config import Settings
from app.database import SessionLocal
from app.services.crm_background import schedule_crm_sync
from app.services.document_delivery import schedule_document_delivery
from app.services.payments import mark_paid_by_external_payment_id, mark_paid_by_label, mark_yookassa_canceled, verify_yoomoney_sign

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
                if not case.delivered_at:
                    schedule_document_delivery(case.id, settings, telegram_bot=bot)
        return web.Response(text="OK")

    async def yookassa(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")
        event = str(payload.get("event") or "")
        payment = payload.get("object") if isinstance(payload.get("object"), dict) else {}
        external_id = str(payment.get("id") or "")
        if not external_id:
            return web.Response(text="OK")
        async with SessionLocal() as session:
            if event == "payment.succeeded":
                case, first_time = await mark_paid_by_external_payment_id(session, external_id, payment)
                if case:
                    await session.refresh(case, ["user"])
                    schedule_crm_sync(settings, case.id, case.user.id, "payment_paid", {"payment": external_id, "note": "YooKassa payment.succeeded"})
                    if first_time and not case.delivered_at:
                        schedule_document_delivery(case.id, settings, telegram_bot=bot)
            elif event == "payment.canceled":
                case = await mark_yookassa_canceled(session, external_id, payment)
                if case:
                    await session.refresh(case, ["user"])
                    schedule_crm_sync(settings, case.id, case.user.id, "payment_canceled", {"payment": external_id, "note": "YooKassa payment.canceled"})
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_post("/payments/yoomoney", yoomoney)
    yookassa_path = settings.yookassa_webhook_path or "/payments/yookassa"
    app.router.add_post(yookassa_path, yookassa)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.payment_web_host, settings.payment_web_port)
    await site.start()
    logger.info("Payment webhook started at http://%s:%s", settings.payment_web_host, settings.payment_web_port)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
