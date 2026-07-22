from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.keyboards.common import chat_end_menu, connect_chat_keyboard, main_menu, manager_panel
from app.models import User
from app.services.crm_background import schedule_crm_sync
from app.services.cases import latest_open_case
from app.services.chat import (
    close_session,
    connect_manager,
    get_manager_active_session,
    get_session,
    get_user_active_session,
    open_session,
    save_message,
    delete_inactivity_notifications,
)
from app.services.users import get_staff
from app.texts import manager_request_text
from app.utils import full_name, h, username_text

router = Router(name="chat")


async def _notify_staff(bot: Bot, session: AsyncSession, text: str, reply_markup=None) -> None:
    for user in await get_staff(session, "telegram"):
        if user.telegram_id and user.admin_notifications_enabled:
            await bot.send_message(user.telegram_id, text, reply_markup=reply_markup)


async def _start_chat(message: Message, bot: Bot, session: AsyncSession, current_user: User, settings) -> None:
    chat = await open_session(session, current_user)
    await message.answer(
        "Чат с менеджером открыт. Напишите вопрос следующим сообщением, менеджер увидит его здесь.",
        reply_markup=chat_end_menu(),
    )
    await _notify_staff(bot, session, manager_request_text(current_user), reply_markup=connect_chat_keyboard(chat.id))
    case = await latest_open_case(session, current_user.id)
    if case:
        schedule_crm_sync(settings, case.id, current_user.id, "manager_requested", {"note": "Пользователь запросил менеджера"})


@router.message(Command("tutor"))
async def cmd_tutor(message: Message, bot: Bot, session: AsyncSession, current_user: User, settings) -> None:
    await _start_chat(message, bot, session, current_user, settings)


@router.callback_query(F.data == "chat:start")
async def cb_start_chat(callback: CallbackQuery, bot: Bot, session: AsyncSession, current_user: User, settings) -> None:
    await _start_chat(callback.message, bot, session, current_user, settings)
    await callback.answer()


@router.callback_query(F.data.startswith("chat:inactivity:dismiss:"))
async def cb_dismiss_inactivity(callback: CallbackQuery, bot: Bot, session: AsyncSession, current_user: User, settings) -> None:
    from datetime import datetime

    chat = await get_session(session, int(callback.data.split(":")[-1]))
    if not chat or chat.user_id != current_user.id:
        await callback.answer("Предложение не найдено", show_alert=True)
        return
    if chat.manager_id:
        await callback.answer("Менеджер уже подключился к чату", show_alert=True)
        return
    current_user.inactivity_offer_dismissed_at = datetime.utcnow()
    await delete_inactivity_notifications(chat, settings, bot=bot)
    await close_session(session, chat)
    await session.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Хорошо, помощь не требуется")


@router.callback_query(F.data.startswith("chat:session:"))
async def cb_connect_chat(callback: CallbackQuery, bot: Bot, session: AsyncSession, current_user: User) -> None:
    if not current_user.is_manager:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    chat = await get_session(session, int(callback.data.split(":")[-1]))
    if not chat:
        await callback.answer("Чат не найден", show_alert=True)
        return
    chat, connected, busy = await connect_manager(session, chat, current_user)
    if busy:
        await callback.answer("У вас уже есть активный чат", show_alert=True)
        return
    if not connected:
        await callback.answer("Уже подключился другой менеджер", show_alert=True)
        return
    await session.refresh(chat, ["user"])
    await callback.message.answer(f"Вы подключились к чату с {full_name(chat.user)}.", reply_markup=manager_panel())
    if chat.user.telegram_id:
        await bot.send_message(chat.user.telegram_id, "Менеджер подключился к диалогу.", reply_markup=chat_end_menu())
    await callback.answer("Чат подключен")


@router.callback_query(F.data.startswith("chat:case:"))
async def cb_case_chat(callback: CallbackQuery, bot: Bot, session: AsyncSession, current_user: User) -> None:
    from app.models import Case

    if not current_user.is_manager:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    case = await session.get(Case, int(callback.data.split(":")[-1]))
    if not case:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await session.refresh(case, ["user"])
    chat = await open_session(session, case.user)
    chat, connected, busy = await connect_manager(session, chat, current_user)
    if busy or not connected:
        await callback.answer("Чат уже занят или у вас есть активный чат", show_alert=True)
        return
    await callback.message.answer(f"Чат по заявлению #{case.id} открыт.", reply_markup=manager_panel())
    if case.user.telegram_id:
        await bot.send_message(case.user.telegram_id, "Менеджер подключился к диалогу по вашему заявлению.", reply_markup=chat_end_menu())
    await callback.answer()


@router.message(Command("endchat"))
@router.callback_query(F.data == "chat:end")
async def end_chat(event: Message | CallbackQuery, bot: Bot, session: AsyncSession, current_user: User) -> None:
    target = event.message if isinstance(event, CallbackQuery) else event
    chat = await get_manager_active_session(session, current_user.id) if current_user.is_manager else None
    chat = chat or await get_user_active_session(session, current_user.id)
    if not chat:
        await target.answer("Активного чата сейчас нет.", reply_markup=main_menu())
        if isinstance(event, CallbackQuery):
            await event.answer()
        return
    await session.refresh(chat, ["user", "manager"])
    await close_session(session, chat)
    await target.answer("Чат завершен.", reply_markup=main_menu())
    for participant in (chat.user, chat.manager):
        if participant and participant.id != current_user.id and participant.telegram_id:
            await bot.send_message(participant.telegram_id, "Чат завершен.")
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(F.text)
async def relay_chat_message(message: Message, bot: Bot, session: AsyncSession, current_user: User, settings) -> None:
    if message.text.startswith("/"):
        return
    if current_user.is_manager:
        chat = await get_manager_active_session(session, current_user.id)
        if chat:
            await session.refresh(chat, ["user"])
            await save_message(session, chat, current_user, message.text, "manager")
            if chat.user.telegram_id:
                await bot.send_message(chat.user.telegram_id, f"<b>Менеджер:</b>\n{h(message.text)}", reply_markup=chat_end_menu())
            case = await latest_open_case(session, chat.user.id)
            if case:
                schedule_crm_sync(
                    settings,
                    case.id,
                    chat.user.id,
                    "manager_reply_sent",
                    {"note": f"\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440\u0430: {message.text[:500]}"},
                )
            return
    chat = await get_user_active_session(session, current_user.id)
    if not chat:
        return
    await session.refresh(chat, ["manager"])
    await save_message(session, chat, current_user, message.text, "user")
    if chat.manager and chat.manager.telegram_id:
        await bot.send_message(chat.manager.telegram_id, f"{full_name(current_user)} ({username_text(current_user)}):\n{h(message.text)}", reply_markup=manager_panel())
    else:
        await message.answer("Сообщение сохранено. Менеджер подключится, как только освободится.", reply_markup=chat_end_menu())
