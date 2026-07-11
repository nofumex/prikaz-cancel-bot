from __future__ import annotations

from pathlib import Path
import logging

from app.adapters.max import keyboards
from app.adapters.max.client import MaxBotClient
from app.adapters.max.mapper import IncomingEvent
from app.models import Case, User
from app.services.cases import latest_open_case
from app.services.chat import (
    close_session,
    connect_manager,
    get_manager_active_session,
    get_session,
    get_user_active_session,
    open_session,
    save_message,
)
from app.services.crm_background import schedule_crm_sync
from app.services.users import get_staff
from app.utils import full_name, h, username_text

logger = logging.getLogger(__name__)


async def _send(client, *, user_id=None, chat_id=None, text='', keyboard=None):
    await client.send_message(user_id=user_id, chat_id=chat_id, text=text, keyboard=keyboard)


async def _forward_attachment(client: MaxBotClient, event: IncomingEvent, target_user_id: str, settings) -> bool:
    if not (event.photo_url or event.document_url or event.photo_token or event.document_token or event.attachment_id):
        return False
    suffix = Path(event.document_name or 'attachment.jpg').suffix or '.jpg'
    path = Path(settings.max_download_dir) / 'chat' / f'{event.message_id or event.platform_user_id}{suffix}'
    path.parent.mkdir(parents=True, exist_ok=True)
    url = event.photo_url or event.document_url
    data = None
    if url:
        await client.download_external_url(url, path)
    else:
        token = event.photo_token or event.document_token
        if token:
            data = await client.download_by_token(token)
        if data is None and event.attachment_id:
            data = await client.download_by_id(event.attachment_id)
        if data is None:
            return False
        path.write_bytes(data)
    await client.send_document_to_user(target_user_id, str(path), caption=event.text or None)
    return True


async def _notify_staff(client, session, settings, text, keyboard=None):
    notified = set()
    for staff in await get_staff(session, 'max'):
        if staff.admin_notifications_enabled:
            try:
                await _send(client, user_id=staff.platform_user_id, text=text, keyboard=keyboard)
                notified.add(str(staff.platform_user_id))
            except Exception:
                logger.exception('Failed to notify MAX staff user_id=%s', staff.platform_user_id)
    for admin_id in settings.max_admin_ids:
        if str(admin_id) in notified:
            continue
        try:
            await _send(client, user_id=admin_id, text=text, keyboard=keyboard)
        except Exception:
            logger.exception('Failed to notify configured MAX admin user_id=%s', admin_id)


async def _start_chat(client, event, session, settings, user):
    chat = await open_session(session, user)
    await _send(
        client,
        chat_id=event.chat_id,
        text='Чат с менеджером открыт. Напишите вопрос следующим сообщением.',
        keyboard=keyboards.chat_end_menu(),
    )
    text = f'Новый чат MAX: {full_name(user)} ({username_text(user)})'
    await _notify_staff(client, session, settings, text, keyboards.connect_chat_keyboard(chat.id))
    case = await latest_open_case(session, user.id)
    if case:
        schedule_crm_sync(settings, case.id, user.id, 'manager_requested', {'note': 'MAX: пользователь открыл live chat'})


async def _end_chat(client, event, session, user):
    chat = await get_manager_active_session(session, user.id) if user.is_manager else None
    chat = chat or await get_user_active_session(session, user.id)
    if not chat:
        await _send(client, chat_id=event.chat_id, text='Активного чата сейчас нет.', keyboard=keyboards.main_menu())
        return
    await session.refresh(chat, ['user', 'manager'])
    await close_session(session, chat)
    await _send(client, chat_id=event.chat_id, text='Чат завершен.', keyboard=keyboards.main_menu())
    for participant in (chat.user, chat.manager):
        if participant and participant.id != user.id:
            await _send(client, user_id=participant.platform_user_id, text='Чат завершен.', keyboard=keyboards.main_menu())


async def handle_chat_update(client, event, settings, session, user: User) -> bool:
    data = event.callback_data
    command = (event.text or '').strip().lower()
    if data == 'chat:start' or command == '/tutor':
        await _start_chat(client, event, session, settings, user)
        return True
    if data == 'chat:end' or command == '/endchat':
        await _end_chat(client, event, session, user)
        return True
    if data and data.startswith('chat:session:'):
        if not user.is_manager:
            await _send(client, chat_id=event.chat_id, text='Недостаточно прав.')
            return True
        chat = await get_session(session, int(data.split(':')[-1]))
        if not chat:
            await _send(client, chat_id=event.chat_id, text='Чат не найден.')
            return True
        chat, connected, busy = await connect_manager(session, chat, user)
        if busy or not connected:
            await _send(client, chat_id=event.chat_id, text='Чат уже занят или у вас есть активный чат.')
            return True
        await session.refresh(chat, ['user'])
        await _send(client, chat_id=event.chat_id, text=f'Вы подключились к чату с {full_name(chat.user)}.', keyboard=keyboards.manager_panel())
        await _send(client, user_id=chat.user.platform_user_id, text='Менеджер подключился к диалогу.', keyboard=keyboards.chat_end_menu())
        return True
    if data and data.startswith('chat:case:'):
        if not user.is_manager:
            await _send(client, chat_id=event.chat_id, text='Недостаточно прав.')
            return True
        case = await session.get(Case, int(data.split(':')[-1]))
        if not case:
            await _send(client, chat_id=event.chat_id, text='Заявка не найдена.')
            return True
        await session.refresh(case, ['user'])
        chat = await open_session(session, case.user)
        chat, connected, busy = await connect_manager(session, chat, user)
        if busy or not connected:
            await _send(client, chat_id=event.chat_id, text='Чат уже занят или у вас есть активный чат.')
            return True
        await _send(client, chat_id=event.chat_id, text=f'Чат по заявлению #{case.id} открыт.', keyboard=keyboards.manager_panel())
        await _send(client, user_id=case.user.platform_user_id, text='Менеджер подключился к диалогу по вашему заявлению.', keyboard=keyboards.chat_end_menu())
        return True
    if data == 'manager:active_chat':
        chat = await get_manager_active_session(session, user.id)
        text = 'Активного чата сейчас нет.' if not chat else f'Активный чат #{chat.id}. Отправьте сообщение.'
        await _send(client, chat_id=event.chat_id, text=text, keyboard=keyboards.manager_panel())
        return True
    if event.callback_data or command.startswith('/'):
        return False
    if session is None or not hasattr(session, 'execute'):
        return False
    return await _relay_message(client, event, settings, session, user)


async def _relay_message(client, event, settings, session, user: User) -> bool:
    file_label = 'файл'
    if user.is_manager:
        chat = await get_manager_active_session(session, user.id)
        if chat:
            await session.refresh(chat, ['user'])
            saved = event.text or f'[вложение: {event.document_name or event.attachment_type or file_label}]'
            await save_message(session, chat, user, saved, 'manager')
            prefix = '<b>Менеджер:</b>\n'
            if event.text:
                await _send(client, user_id=chat.user.platform_user_id, text=prefix + h(event.text), keyboard=keyboards.chat_end_menu())
            if event.has_raw_attachment:
                forwarded = await _forward_attachment(client, event, chat.user.platform_user_id, settings)
                if not forwarded:
                    await _send(client, chat_id=event.chat_id, text='Не удалось переслать вложение. Отправьте его еще раз.')
            return True
    chat = await get_user_active_session(session, user.id)
    if not chat:
        return False
    await session.refresh(chat, ['manager'])
    saved = event.text or f'[вложение: {event.document_name or event.attachment_type or file_label}]'
    await save_message(session, chat, user, saved, 'user')
    if chat.manager:
        header = f'{full_name(user)} ({username_text(user)}):\n'
        if event.text:
            await _send(client, user_id=chat.manager.platform_user_id, text=header + h(event.text), keyboard=keyboards.manager_panel())
        if event.has_raw_attachment:
            forwarded = await _forward_attachment(client, event, chat.manager.platform_user_id, settings)
            if not forwarded:
                await _send(client, chat_id=event.chat_id, text='Не удалось переслать вложение. Отправьте его еще раз.')
    else:
        if event.has_raw_attachment:
            targets = {str(item) for item in settings.max_admin_ids}
            targets.update(str(item.platform_user_id) for item in await get_staff(session, 'max'))
            for target in targets:
                try:
                    await _forward_attachment(client, event, target, settings)
                except Exception:
                    logger.exception('Failed to forward pending MAX chat attachment to user_id=%s', target)
        await _send(client, chat_id=event.chat_id, text='Сообщение сохранено. Менеджер подключится, как только освободится.', keyboard=keyboards.chat_end_menu())
    case = await latest_open_case(session, user.id)
    if case:
        schedule_crm_sync(settings, case.id, user.id, 'manager_message_sent', {'note': saved[:500]})
    return True
