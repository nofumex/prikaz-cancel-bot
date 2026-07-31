from __future__ import annotations

import re
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.keyboards.common import chat_end_menu, connect_chat_keyboard, main_menu, manager_panel
from app.models import Case, User
from app.services.cases import ensure_user_has_case, latest_case
from app.services.chat_crm_sync import schedule_chat_message_crm_sync
from app.services.crm_background import schedule_crm_sync
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
latest_open_case = latest_case  # Compatibility name; unlike the old query it also returns completed cases.


async def _notify_staff(bot: Bot, session: AsyncSession, text: str, reply_markup=None) -> None:
    for user in await get_staff(session, "telegram"):
        if user.telegram_id and user.admin_notifications_enabled:
            await bot.send_message(user.telegram_id, text, reply_markup=reply_markup)


async def _start_chat(message: Message, bot: Bot, session: AsyncSession, current_user: User, settings) -> None:
    case, _ = await ensure_user_has_case(session, current_user, chat_id=str(message.chat.id))
    chat = await open_session(session, current_user, case_id=case.id)
    await message.answer(
        "Чат с менеджером открыт. Напишите вопрос следующим сообщением, менеджер увидит его здесь.",
        reply_markup=chat_end_menu(),
    )
    await _notify_staff(bot, session, manager_request_text(current_user), reply_markup=connect_chat_keyboard(chat.id))
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
    chat = await open_session(session, case.user, case_id=case.id)
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


def _telegram_message_text(message: Message) -> str:
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if getattr(message, "text", None):
        return text
    if getattr(message, "photo", None):
        label = "фото"
    elif getattr(message, "document", None):
        label = message.document.file_name or "документ"
    elif getattr(message, "video", None):
        label = "видео"
    elif getattr(message, "voice", None):
        label = "голосовое сообщение"
    elif getattr(message, "audio", None):
        label = "аудио"
    elif getattr(message, "sticker", None):
        label = "стикер"
    else:
        label = "вложение/сообщение"
    return f"[вложение: {label}]" + (f" {text}" if text else "")


def _telegram_attachment_info(message: Message) -> tuple[str, str, str] | None:
    if getattr(message, "photo", None):
        return message.photo[-1].file_id, "photo.jpg", "photo"
    if getattr(message, "document", None):
        return message.document.file_id, message.document.file_name or "document", "document"
    if getattr(message, "video", None):
        return message.video.file_id, message.video.file_name or "video.mp4", "video"
    if getattr(message, "voice", None):
        return message.voice.file_id, "voice.ogg", "voice"
    if getattr(message, "audio", None):
        return message.audio.file_id, message.audio.file_name or "audio.mp3", "audio"
    if getattr(message, "sticker", None):
        return message.sticker.file_id, "sticker.webp", "sticker"
    return None


async def _store_telegram_attachment(bot: Bot, message: Message) -> tuple[str | None, str | None, str | None]:
    info = _telegram_attachment_info(message)
    if info is None:
        return None, None, None
    file_id, original_name, attachment_type = info
    safe_name = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", Path(original_name).name).strip("._") or "attachment"
    chat_id = getattr(getattr(message, "chat", None), "id", "unknown")
    message_id = getattr(message, "message_id", "unknown")
    path = Path("storage/chat/telegram") / f"{chat_id}_{message_id}_{safe_name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    await bot.download(file_id, destination=path)
    return str(path), original_name, attachment_type


async def _chat_case(session: AsyncSession, chat, customer: User, *, chat_id: str | None = None) -> Case:
    case_id = getattr(chat, "case_id", None)
    case = await session.get(Case, case_id) if case_id else await latest_open_case(session, customer.id)
    if case is None:
        case, _ = await ensure_user_has_case(session, customer, chat_id=chat_id)
    if getattr(chat, "case_id", None) is None:
        chat.case_id = case.id
        if hasattr(session, "commit"):
            await session.commit()
    return case


@router.message()
async def relay_chat_message(message: Message, bot: Bot, session: AsyncSession, current_user: User, settings) -> None:
    if message.text and message.text.startswith("/"):
        return
    saved = _telegram_message_text(message)
    message_chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    external_message_id = f"{message_chat_id}:{message_id}" if message_chat_id is not None and message_id is not None else None
    if current_user.is_manager:
        chat = await get_manager_active_session(session, current_user.id)
        if chat:
            await session.refresh(chat, ["user"])
            attachment_path, attachment_name, attachment_type = await _store_telegram_attachment(bot, message)
            stored_message = await save_message(
                session,
                chat,
                current_user,
                saved,
                "manager",
                attachment_path=attachment_path,
                attachment_name=attachment_name,
                attachment_type=attachment_type,
            )
            if chat.user.telegram_id:
                if message.text:
                    await bot.send_message(chat.user.telegram_id, f"<b>Менеджер:</b>\n{h(message.text)}", reply_markup=chat_end_menu())
                else:
                    await bot.copy_message(chat.user.telegram_id, message.chat.id, message.message_id, reply_markup=chat_end_menu())
            case = await _chat_case(session, chat, chat.user)
            schedule_chat_message_crm_sync(
                settings,
                platform="telegram",
                customer=chat.user,
                case_id=case.id,
                text=saved,
                sender_role="manager",
                chat_session_id=chat.id,
                external_message_id=f"chat:{stored_message.id}" if isinstance(getattr(stored_message, "id", None), int) else external_message_id,
                message_datetime=getattr(message, "date", None),
                attachment_path=attachment_path,
                attachment_name=attachment_name,
            )
            return
    chat = await get_user_active_session(session, current_user.id)
    if not chat:
        return
    await session.refresh(chat, ["manager"])
    attachment_path, attachment_name, attachment_type = await _store_telegram_attachment(bot, message)
    stored_message = await save_message(
        session,
        chat,
        current_user,
        saved,
        "user",
        attachment_path=attachment_path,
        attachment_name=attachment_name,
        attachment_type=attachment_type,
    )
    if chat.manager and chat.manager.telegram_id:
        if message.text:
            await bot.send_message(chat.manager.telegram_id, f"{full_name(current_user)} ({username_text(current_user)}):\n{h(message.text)}", reply_markup=manager_panel())
        else:
            await bot.copy_message(chat.manager.telegram_id, message.chat.id, message.message_id, reply_markup=manager_panel())
    else:
        await message.answer("Сообщение сохранено. Менеджер подключится, как только освободится.", reply_markup=chat_end_menu())
    case = await _chat_case(session, chat, current_user, chat_id=str(message_chat_id) if message_chat_id is not None else None)
    schedule_chat_message_crm_sync(
        settings,
        platform="telegram",
        customer=current_user,
        case_id=case.id,
        text=saved,
        sender_role="user",
        chat_session_id=chat.id,
        external_message_id=f"chat:{stored_message.id}" if isinstance(getattr(stored_message, "id", None), int) else external_message_id,
        message_datetime=getattr(message, "date", None),
        attachment_path=attachment_path,
        attachment_name=attachment_name,
    )
