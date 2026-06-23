from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import BotCommand, ErrorEvent

from app.config import Settings
from app.handlers import admin, case_flow, chat, commands
from app.middlewares.user import DbUserMiddleware
from app.services.payment_web import run_payment_webhook
from app.services.reminders import run_payment_reminders


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="new", description="Новое заявление"),
            BotCommand(command="profile", description="Профиль"),
            BotCommand(command="tutor", description="Связаться с менеджером"),
            BotCommand(command="manager", description="Панель менеджера"),
            BotCommand(command="admin", description="Админ-панель"),
            BotCommand(command="endchat", description="Завершить чат"),
            BotCommand(command="cancel", description="Отменить действие"),
            BotCommand(command="help", description="Помощь"),
        ]
    )


async def on_error(event: ErrorEvent) -> None:
    if isinstance(event.exception, TelegramBadRequest) and "query is too old" in str(event.exception):
        logging.info("Ignored expired callback query")
        return
    logging.exception("Unhandled Telegram update error", exc_info=event.exception)
    try:
        if event.update.message:
            await event.update.message.answer("Произошла техническая ошибка. Мы уже записали ее в лог.")
        elif event.update.callback_query and event.update.callback_query.message:
            await event.update.callback_query.message.answer("Произошла техническая ошибка. Мы уже записали ее в лог.")
    except TelegramAPIError:
        logging.exception("Failed to send error message")


async def run_telegram_bot(settings: Settings) -> None:
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(settings=settings)
    middleware = DbUserMiddleware(settings)
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)
    dp.errors.register(on_error, ExceptionTypeFilter(Exception))
    dp.include_router(commands.router)
    dp.include_router(case_flow.router)
    dp.include_router(admin.router)
    dp.include_router(chat.router)

    tasks: list[asyncio.Task] = []
    try:
        tasks.append(asyncio.create_task(run_payment_reminders(bot)))
        if settings.yoomoney_receiver or settings.yoomoney_notification_secret or settings.payment_public_base_url:
            tasks.append(asyncio.create_task(run_payment_webhook(bot, settings)))
        if settings.drop_pending_updates:
            await bot.delete_webhook(drop_pending_updates=True)
        await set_commands(bot)
        logging.info("Telegram polling started")
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        await bot.session.close()
