from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from app.adapters.max.bot import handle_update
from app.adapters.max.client import MaxBotClient
from app.adapters.max.mapper import parse_update, sanitize_raw_update
from app.config import Settings

logger = logging.getLogger(__name__)


async def run_max_webhook(settings: Settings) -> None:
    async with MaxBotClient(
        settings.max_bot_token,
        settings.max_api_base_url,
        upload_retry_attempts=settings.max_upload_retry_attempts,
        upload_retry_base_seconds=settings.max_upload_retry_base_seconds,
    ) as client:
        app = web.Application()

        async def webhook(request: web.Request) -> web.Response:
            if settings.max_webhook_secret:
                secret = request.headers.get("X-Max-Bot-Api-Secret")
                if secret != settings.max_webhook_secret:
                    return web.Response(status=403, text="Forbidden")
            payload = await request.json()
            update_type = payload.get("update_type")
            logger.info("MAX webhook received type=%s", update_type)
            if settings.max_debug_raw_updates:
                logger.info("MAX webhook raw sanitized=%s", sanitize_raw_update(payload))
            asyncio.create_task(_process_update(client, payload, settings))
            return web.Response(text="OK")

        app.router.add_post("/max/webhook", webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, settings.max_webhook_host, settings.max_webhook_port)
        await site.start()
        logger.info("MAX webhook started at http://%s:%s/max/webhook", settings.max_webhook_host, settings.max_webhook_port)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()


async def _process_update(client: MaxBotClient, payload: dict, settings: Settings) -> None:
    try:
        event = parse_update(payload)
        if event:
            await handle_update(client, event, settings)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("MAX webhook update processing failed")
