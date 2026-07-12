from __future__ import annotations

import math
import json
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import func, select

from app.adapters.max import keyboards
from app.adapters.max.client import MaxBotClient
from app.adapters.max.mapper import IncomingEvent
from app.adapters.max.state import max_state_manager
from app.config import Settings
from app.enums import CaseStatus
from app.models import Case, CrmSyncLog, OpenAIUsage, User
from app.services.amocrm import get_amocrm_service
from app.services.app_settings import payments_enabled, toggle_payments
from app.services.app_settings import reminder_settings, update_reminder_setting
from app.services.reminder_center import reminder_counts, reminder_dashboard_text, send_manual_reminders
from app.services.admin_reporting import PROBLEM_CATEGORIES, client_path_text, order_photo_paths, problem_case_error_text, problem_cases_by_category, problem_category_counts, problem_cases_page
from app.services.amount_recovery import format_amount_mismatch_admin_report
from app.services.document_delivery import schedule_document_delivery
from app.services.legal_data import FIELD_LABELS, normalize_order_data
from app.services.payments import mark_paid_by_label, net_payment_totals, record_manual_refund
from app.texts import case_summary
from app.utils import full_name, h, username_text

PAGE_SIZE = 5
GenerateDocuments = Callable[[Case], Awaitable[None]]


async def _send(client: MaxBotClient, event: IncomingEvent, text: str, keyboard=None) -> None:
    await client.send_message(chat_id=event.chat_id, text=text, keyboard=keyboard)


def _money(value: float | None) -> str:
    return chr(36) + f'{(value or 0.0):.4f}'


def _case_button_label(case: Case, error: str | None = None) -> str:
    date = case.created_at.strftime('%d.%m') if case.created_at else ''
    label = f'#{case.id} • {date} • {full_name(case.user)}'
    if error:
        trimmed = ' '.join(error.split())
        label = f'{label} • Ошибка: {trimmed[:60]}'
    return label


async def _deny(client: MaxBotClient, event: IncomingEvent, user: User, manager: bool = False) -> bool:
    if user.is_manager if manager else user.is_admin:
        return False
    if manager:
        text = 'Эта команда доступна только менеджеру или администратору.'
    else:
        text = 'Эта команда доступна только администратору.'
    await _send(client, event, text)
    return True


async def _show_cases(client, event, session, user: User, payments_only: bool, page: int) -> None:
    condition = Case.status == CaseStatus.PAYMENT_PENDING.value if payments_only else None
    count = select(func.count(Case.id))
    if condition is not None:
        count = count.where(condition)
    total = int(await session.scalar(count) or 0)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    query = select(Case)
    if condition is not None:
        query = query.where(condition)
    query = query.order_by(Case.created_at.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE)
    cases = list((await session.execute(query)).scalars())
    if not cases:
        text = 'Неоплаченных предпросмотров пока нет.' if payments_only else 'Заявок пока нет.'
        keyboard = keyboards.admin_panel(payments_enabled()) if user.is_admin else keyboards.manager_panel()
        await _send(client, event, text, keyboard)
        return
    items = []
    for case in cases:
        await session.refresh(case, ['user'])
        date = case.created_at.strftime('%d.%m') if case.created_at else ''
        items.append((case.id, f'#{case.id} • {date} • {full_name(case.user)}'))
    prefix = 'admin:payments' if payments_only else 'admin:cases'
    if payments_only:
        text = '<b>⏳ Ожидают оплату</b>\n\nВыберите заявку:'
    else:
        text = f'<b>📋 Заявки</b>\n\nПоказано по {PAGE_SIZE} на странице. Выберите заявку:'
    await _send(client, event, text, keyboards.admin_cases_page(items, page, pages, prefix))


async def _show_problem_cases(client, event, session, user: User, page: int) -> None:
    counts = await problem_category_counts(session)
    rows = [[keyboards.btn(f'{label} ({counts.get(key, 0)})', f'admin:problem_group_{key}:0')] for key, (label, _) in PROBLEM_CATEGORIES.items()]
    rows.append([keyboards.btn('↩️ Админка', 'admin:panel')])
    await _send(client, event, '<b>⚠️ Проблемные заявки</b>\n\nВыберите тип проблемы:', rows)


async def _show_problem_group(client, event, session, category: str, page: int) -> None:
    cases, total, errors = await problem_cases_by_category(session, category, page, PAGE_SIZE)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    items = []
    for case in cases:
        await session.refresh(case, ['user'])
        items.append((case.id, _case_button_label(case, errors.get(case.id))))
    if not items:
        await _send(client, event, 'В этой категории заявок нет.')
        return
    await _send(client, event, f'<b>{PROBLEM_CATEGORIES[category][0]}</b>\n\nВыберите заявку:', keyboards.admin_cases_page(items, page, pages, f'admin:problem_group_{category}'))


async def _show_case(client, event, session, data: str) -> None:
    _, _, case_id, prefix, page = data.split(':')
    case = await session.get(Case, int(case_id))
    if not case:
        await _send(client, event, 'Заявка не найдена.')
        return
    await session.refresh(case, ['user'])
    query = select(OpenAIUsage).where(OpenAIUsage.case_id == case.id)
    usages = list((await session.execute(query)).scalars())
    usage_text = ''
    if usages:
        rows = ['', '<b>OpenAI по заявке:</b>']
        for row in usages:
            rows.append(f'{row.operation}: input {row.input_tokens}, output {row.output_tokens}, cost {_money(row.total_cost_usd)}')
        rows.append(f'Итого: {_money(sum(row.total_cost_usd or 0.0 for row in usages))}')
        usage_text = '\n' + '\n'.join(rows)
    dash = '—'
    text = (
        case_summary(case) + '\n\n'
        + f'<b>Клиент:</b> {h(full_name(case.user))}\n'
        + f'<b>Username:</b> {h(username_text(case.user))}\n'
        + f'<b>ID:</b> <code>{h(case.user.platform_user_id)}</code>\n\n'
        + '<b>CRM:</b>\n'
        + f'Контакт: {case.amocrm_contact_id or dash}\n'
        + f'Сделка: {case.amocrm_lead_id or case.amo_lead_id or dash}\n'
        + f'Этап: {h(case.amocrm_status_name or dash)} (ID {case.amocrm_status_id or dash})'
        + ('\nОшибка: ' + h(case.amocrm_sync_error) if case.amocrm_sync_error else '')
        + usage_text
    )
    problem_error = await problem_case_error_text(session, case.id)
    if problem_error:
        text += f'\n\n<b>Ошибка:</b> {h(problem_error)}'
    paid = case.status in {CaseStatus.PAID.value, CaseStatus.DELIVERED.value}
    path_text = await client_path_text(session, case.id)
    text += '\n\n<b>Путь клиента:</b>\n' + path_text
    keyboard = keyboards.admin_case_actions(case.id, paid, f'admin:{prefix}:{page}')
    file_rows = [
        [keyboards.btn('📎 Фото приказа', f'admin:file:{case.id}:order'), keyboards.btn('📄 Preview', f'admin:file:{case.id}:preview')],
        [keyboards.btn('📝 DOCX заявления', f'admin:file:{case.id}:docx'), keyboards.btn('📕 PDF заявления', f'admin:file:{case.id}:pdf')],
        [keyboards.btn('🧾 Все файлы заявки', f'admin:file:{case.id}:all')],
    ]
    keyboard = file_rows + keyboard
    await _send(client, event, text, keyboard)


async def _show_stats(client, event, session) -> None:
    users = int(await session.scalar(select(func.count(User.id))) or 0)
    cases = int(await session.scalar(select(func.count(Case.id))) or 0)
    pending = int(await session.scalar(select(func.count(Case.id)).where(Case.status == CaseStatus.PAYMENT_PENDING.value)) or 0)
    paid_statuses = [CaseStatus.PAID.value, CaseStatus.DELIVERED.value]
    paid = int(await session.scalar(select(func.count(Case.id)).where(Case.status.in_(paid_statuses))) or 0)
    regenerations = int(await session.scalar(
        select(func.count(CrmSyncLog.id)).where(CrmSyncLog.event_type == 'paid_document_regenerated')
    ) or 0)
    usage = (await session.execute(select(
        func.coalesce(func.sum(OpenAIUsage.total_cost_usd), 0.0),
        func.coalesce(func.sum(OpenAIUsage.total_tokens), 0),
        func.coalesce(func.sum(OpenAIUsage.input_tokens), 0),
        func.coalesce(func.sum(OpenAIUsage.cached_input_tokens), 0),
        func.coalesce(func.sum(OpenAIUsage.output_tokens), 0),
        func.coalesce(func.sum(OpenAIUsage.reasoning_tokens), 0),
    ))).one()
    synced = int(await session.scalar(select(func.count(Case.id)).where(Case.amocrm_synced.is_(True))) or 0)
    errors = int(await session.scalar(select(func.count(CrmSyncLog.id)).where(CrmSyncLog.success.is_(False))) or 0)
    telegram_users = int(await session.scalar(select(func.count(User.id)).where(User.platform == 'telegram')) or 0)
    max_users = int(await session.scalar(select(func.count(User.id)).where(User.platform == 'max')) or 0)
    platform_total = telegram_users + max_users
    telegram_percent = round(telegram_users * 100 / platform_total) if platform_total else 0
    max_percent = round(max_users * 100 / platform_total) if platform_total else 0
    payment_count, payment_sum, yookassa_count, yookassa_sum = await net_payment_totals(session)
    text = (
        '<b>Статистика</b>\n\n'
        + f'Пользователей: {users}\nЗаявлений всего: {cases}\nОжидают оплату: {pending}\nОплачено/выдано: {paid}\nРегенераций заявлений: {regenerations}\n\n'
        + f'<b>CRM</b>\nСинхронизировано сделок: {synced}\nОшибки синхронизации: {errors}\n\n'
        + f'<b>Пользователи по платформам</b>\nTelegram: {telegram_users} ({telegram_percent}%)\nMAX: {max_users} ({max_percent}%)\nВсего: {platform_total} (100%)\n\n'
        + f'<b>Оплаты</b>\nYooKassa: {yookassa_count} оплат, {yookassa_sum:,} ₽\nВсего успешных: {payment_count} оплат, {payment_sum:,} ₽\n\n'.replace(',', ' ')
        + f'<b>OpenAI API</b>\nВсего потрачено: {_money(float(usage[0] or 0))}\nВсего токенов: {int(usage[1] or 0)}\n'
        + f'Input: {int(usage[2] or 0)}\nCached input: {int(usage[3] or 0)}\nOutput: {int(usage[4] or 0)}\nReasoning: {int(usage[5] or 0)}'
    )
    await _send(client, event, text, keyboards.admin_panel(payments_enabled()))


async def handle_admin_update(
    client: MaxBotClient,
    event: IncomingEvent,
    settings: Settings,
    session,
    user: User,
    *,
    generate_documents: GenerateDocuments | None = None,
) -> bool:
    admin_state = None
    if user.is_admin and event.text and not event.text.startswith('/') and session is not None and hasattr(session, 'execute'):
        admin_state = await max_state_manager.get_state(session, 'max', event.platform_user_id)
    if user.is_admin and admin_state == 'max_admin_edit_amount' and event.text:
        state_data = await max_state_manager.get_data(session, 'max', event.platform_user_id)
        case = await session.get(Case, int(state_data['case_id']))
        field = state_data['field']
        value = event.text.strip()
        if not case or not value:
            await _send(client, event, 'Значение не должно быть пустым.')
            return True
        extracted = normalize_order_data(json.loads(case.extracted_json or '{}'))
        extracted[field] = value
        case.extracted_json = json.dumps(normalize_order_data(extracted), ensure_ascii=False)
        await session.commit()
        await max_state_manager.clear(session, 'max', event.platform_user_id)
        await _send(client, event, f'✅ Поле {FIELD_LABELS.get(field, field)} обновлено: {value}')
        return True
    if user.is_admin and admin_state == 'max_broadcast_setting' and event.text:
        state_data = await max_state_manager.get_data(session, 'max', event.platform_user_id)
        value = event.text.strip()
        if state_data['value_type'] == 'hours':
            if not value.isdigit() or not 1 <= int(value) <= 720:
                await _send(client, event, 'Введите целое число часов от 1 до 720.')
                return True
            value = int(value)
        elif not value:
            await _send(client, event, 'Текст не должен быть пустым.')
            return True
        update_reminder_setting(state_data['key'], value)
        await max_state_manager.clear(session, 'max', event.platform_user_id)
        await _send(client, event, '✅ Настройка сохранена.', keyboards.broadcast_settings_menu())
        return True
    data = event.callback_data
    command = (event.text or '').strip().lower()
    admin_action = command in {'/admin', '/refund'} or bool(data and (data.startswith('admin:') or data.startswith('broadcast:')))
    manager_action = command == '/manager' or data == 'manager:cases'
    if not admin_action and not manager_action:
        return False
    if manager_action:
        if await _deny(client, event, user, manager=True):
            return True
        if command == '/manager':
            await _send(client, event, '<b>Панель менеджера</b>', keyboards.manager_panel())
        else:
            await _show_cases(client, event, session, user, False, 0)
        return True
    if await _deny(client, event, user):
        return True
    if command == '/refund':
        parts = (event.text or '').strip().split(maxsplit=1)
        if len(parts) < 2:
            await _send(client, event, 'Использование: /refund CASE_ID')
            return True
        try:
            case_id = int(parts[1].strip())
        except ValueError:
            await _send(client, event, 'Использование: /refund CASE_ID')
            return True
        case = await session.get(Case, case_id)
        if not case:
            await _send(client, event, 'Заявка не найдена.')
            return True
        payment, applied = await record_manual_refund(session, case, user)
        if not payment:
            await _send(client, event, 'По заявке нет успешной оплаты')
            return True
        if not applied:
            await _send(client, event, 'Возврат по этой заявке уже учтен')
            return True
        amount_text = f'{payment.amount:,}'.replace(',', ' ')
        await _send(client, event, f'Возврат по заявке #{case.id} учтен.\nПлатеж: <code>{h(payment.label)}</code>\nСумма: {amount_text} ₽')
        return True
    if command == '/admin' or data == 'admin:panel':
        await _send(client, event, '<b>⚙️ Админ-панель</b>', keyboards.admin_panel(payments_enabled()))
    elif data == 'admin:noop':
        pass
    elif data and data.startswith('admin:cases'):
        page = int(data.split(':')[-1]) if data.startswith('admin:cases:') else 0
        await _show_cases(client, event, session, user, False, page)
    elif data and data.startswith('admin:payments'):
        page = int(data.split(':')[-1]) if data.startswith('admin:payments:') else 0
        await _show_cases(client, event, session, user, True, page)
    elif data and data.startswith('admin:problem_cases'):
        page = int(data.split(':')[-1]) if data.startswith('admin:problem_cases:') else 0
        await _show_problem_cases(client, event, session, user, page)
    elif data and data.startswith('admin:problem_group_'):
        prefix, raw_page = data.rsplit(':', 1)
        category = prefix.removeprefix('admin:problem_group_')
        await _show_problem_group(client, event, session, category, int(raw_page))
    elif data and data.startswith('admin:case:'):
        await _show_case(client, event, session, data)
    elif data == 'admin:stats':
        await _show_stats(client, event, session)
    elif data == 'admin:toggle_payments':
        enabled = toggle_payments()
        status = 'включен' if enabled else 'выключен для тестов'
        await _send(client, event, f'Режим оплаты {status}.', keyboards.admin_panel(enabled))
    else:
        return await _handle_admin_action(client, event, settings, session, user, data, generate_documents)
    return True


async def _handle_admin_action(client, event, settings, session, user, data, generate_documents) -> bool:
    if data == 'admin:broadcasts':
        await _send(client, event, reminder_dashboard_text(await reminder_counts(session)), keyboards.broadcast_menu())
        return True
    if data and data.startswith('broadcast:ask:'):
        kind = data.split(':')[-1]
        count = (await reminder_counts(session))[kind]['pending']
        labels = {'try': 'напоминание попробовать', 'pay': 'напоминание оплатить', 'consultation': 'предложение консультации'}
        await _send(client, event, f'Отправить «{labels[kind]}» пользователям: <b>{count}</b>?', keyboards.broadcast_confirm(kind))
        return True
    if data and data.startswith('broadcast:send:'):
        kind = data.split(':')[-1]
        await _send(client, event, '⏳ Рассылка началась...')
        sent, failed = await send_manual_reminders(session, settings, None, kind)
        await _send(client, event, f'✅ Рассылка завершена. Отправлено: {sent}. Ошибок: {failed}.', keyboards.broadcast_menu())
        return True
    if data == 'broadcast:settings':
        cfg = reminder_settings()
        try_hours = cfg['reminder_try_hours']
        pay_hours = cfg['reminder_pay_hours']
        consultation_hours = cfg['reminder_consultation_hours']
        text = (
            '<b>⚙️ Настройки напоминаний</b>\n\n'
            f'Попробовать: через {try_hours} ч.\n'
            f'Оплатить: через {pay_hours} ч.\n'
            f'Консультация: через {consultation_hours} ч.'
        )
        await _send(client, event, text, keyboards.broadcast_settings_menu())
        return True
    if data and data.startswith('broadcast:edit:'):
        _, _, value_type, kind = data.split(':')
        key = f'reminder_{kind}_{value_type}' if value_type == 'text' else f'reminder_{kind}_hours'
        current = reminder_settings()[key]
        await max_state_manager.set_state(session, 'max', event.platform_user_id, 'max_broadcast_setting', {'key': key, 'value_type': value_type})
        prompt = 'Введите новый текст напоминания:' if value_type == 'text' else 'Введите задержку в часах (1–720):'
        await _send(client, event, f'{prompt}\n\nСейчас: <code>{h(current)}</code>')
        return True
    if data and data.startswith('admin:user:'):
        case = await session.get(Case, int(data.split(':')[-1]))
        if not case:
            await _send(client, event, 'Заявка не найдена.')
            return True
        await session.refresh(case, ['user'])
        target = case.user
        dash = '—'
        text = (
            f'<b>Профиль клиента</b>\nИмя: {full_name(target)}\nПлатформа: {h(target.platform)}\n'
            f'Username: {username_text(target)}\nID: <code>{h(target.platform_user_id)}</code>\n'
            f'Телефон: {h(target.phone or dash)}\nEmail: {h(target.email or dash)}'
        )
        await _send(client, event, text)
        return True
    if data and data.startswith('admin:retry_amounts:'):
        case = await session.get(Case, int(data.split(':')[-1]))
        if not case or not case.order_photo_path:
            await _send(client, event, 'Нет фото приказа.')
            return True
        await session.refresh(case, ['user'])
        from app.handlers.case_flow import _resolve_amount_mismatch

        extracted = normalize_order_data(json.loads(case.extracted_json or '{}'))
        updated, check, recovery, retry_amounts = await _resolve_amount_mismatch(
            settings, session, case, case.user, extracted, force_retry=True
        )
        if check.ok:
            await _send(client, event, f'✅ Суммы согласованы для заявки #{case.id}.')
        else:
            report = format_amount_mismatch_admin_report(case.id, updated, retry_amounts, check, recovery)
            await _send(client, event, report)
        return True
    if data and data.startswith('admin:edit_amount:'):
        _, _, case_id, field = data.split(':')
        case = await session.get(Case, int(case_id))
        if not case:
            await _send(client, event, 'Заявка не найдена.')
            return True
        await max_state_manager.set_state(session, 'max', event.platform_user_id, 'max_admin_edit_amount', {'case_id': case.id, 'field': field})
        await _send(client, event, f'Введите новое значение для поля <b>{FIELD_LABELS.get(field, field)}</b>.')
        return True
    if data and data.startswith('admin:apply_suggested:'):
        case_id = int(data.split(':')[-1])
        case = await session.get(Case, case_id)
        debug_path = Path('storage/debug') / f'case_{case_id}' / 'amount_recovery.json'
        if not case or not debug_path.exists():
            await _send(client, event, 'Нет предложенной суммы.')
            return True
        payload = json.loads(debug_path.read_text(encoding='utf-8'))
        qa = payload.get('qa_report') or {}
        value = qa.get('new_debt_amount') or qa.get('debt_candidate')
        if not value:
            await _send(client, event, 'Нет предложенной суммы.')
            return True
        extracted = normalize_order_data(json.loads(case.extracted_json or '{}'))
        extracted['debt_amount'] = value
        case.extracted_json = json.dumps(normalize_order_data(extracted), ensure_ascii=False)
        await session.commit()
        await _send(client, event, f'✅ Применена сумма долга: {value}')
        return True
    if data and data.startswith('admin:file:'):
        _, _, case_id, kind = data.split(':')
        case = await session.get(Case, int(case_id))
        if not case:
            await _send(client, event, 'Заявка не найдена.')
            return True
        order_paths = await order_photo_paths(session, case)
        paths = {
            'order': [(f'Фото приказа {index}', path) for index, path in enumerate(order_paths, 1)],
            'preview': [('Preview заявления', case.preview_pdf_path or case.preview_doc_path)],
            'docx': [('DOCX заявления', case.full_doc_path)],
            'pdf': [('PDF заявления', case.full_pdf_path)],
        }
        selected = sum(paths.values(), []) if kind == 'all' else paths.get(kind, [])
        sent = 0
        for caption, raw_path in selected:
            if raw_path and Path(raw_path).exists():
                await client.send_document(event.chat_id, raw_path, caption=caption)
                sent += 1
        if sent == 0:
            if kind == 'order':
                reason = 'Приказ еще не загружен.'
            elif not case.extracted_json:
                reason = 'OCR еще не завершен.'
            elif kind == 'preview':
                reason = 'Preview еще не сгенерирован.'
            else:
                reason = 'Полный документ еще не сгенерирован. Используйте кнопку «Сгенерировать документы».'
            await _send(client, event, reason)
        elif kind == 'all':
            await _send(client, event, f'Отправлено файлов: {sent}.')
        return True
    if data == 'admin:check_crm':
        report = await get_amocrm_service(settings).ensure_pipeline_and_statuses()
        if not report.get('pipeline'):
            await _send(client, event, 'amoCRM недоступна или воронка не найдена.')
            return True
        pipeline = report['pipeline']
        name_key = 'name'
        id_key = 'id'
        lines = ['amoCRM проверена', '', f'Воронка: {pipeline.get(name_key)}', f'Pipeline ID: {pipeline.get(id_key)}', '', 'Этапы:']
        names = ['Подписался на бота', 'Отправил приказ', 'Указал дату', 'Оплатил', 'Получил напоминание (не оплатил)']
        for name in names:
            status_id = report.get('statuses', {}).get(name)
            mark = '✅' if status_id else '❌'
            suffix = f' — id {status_id}' if status_id else ''
            lines.append(f'{mark} {name}{suffix}')
        await _send(client, event, '\n'.join(lines), keyboards.admin_panel(payments_enabled()))
        return True
    if data == 'admin:crm_stats':
        synced = int(await session.scalar(select(func.count(Case.id)).where(Case.amocrm_synced.is_(True))) or 0)
        errors = int(await session.scalar(select(func.count(CrmSyncLog.id)).where(CrmSyncLog.success.is_(False))) or 0)
        pending = int(await session.scalar(select(func.count(Case.id)).where(Case.amocrm_synced.is_(False))) or 0)
        text = f'CRM:\nСделок синхронизировано: {synced}\nОшибки синхронизации: {errors}\nОжидают синхронизации: {pending}'
        await _send(client, event, text, keyboards.admin_panel(payments_enabled()))
        return True
    if data == 'admin:managers':
        query = select(User).where(User.is_manager.is_(True)).order_by(User.created_at.desc())
        managers = list((await session.execute(query)).scalars())
        text = 'Менеджеров пока нет. Добавьте MAX_ADMIN_IDS в .env.'
        if managers:
            rows = [f'{h(full_name(item))} | {h(username_text(item))} | <code>{h(item.platform_user_id)}</code>' for item in managers]
            text = '<b>Менеджеры</b>\n\n' + '\n'.join(rows)
        await _send(client, event, text, keyboards.admin_panel(payments_enabled()))
        return True
    if data and data.startswith('admin:crm_sync:'):
        case = await session.get(Case, int(data.split(':')[-1]))
        if not case:
            await _send(client, event, 'Заявка не найдена.')
        else:
            await session.refresh(case, ['user'])
            crm = get_amocrm_service(settings)
            await crm.sync_case_current_state(session, case, case.user)
            await _send(client, event, f'CRM-синхронизация для заявки #{case.id} выполнена.')
        return True
    if data and data.startswith('admin:mark_paid:'):
        case = await session.get(Case, int(data.split(':')[-1]))
        if not case or not case.payment_label:
            await _send(client, event, 'Заявка без платежа.')
        else:
            payload = {'manual_admin_id': user.id}
            paid_case = await mark_paid_by_label(session, case.payment_label, payload)
            await _send(client, event, f'Оплата по заявлению #{paid_case.id} отмечена. Документы отправляются клиенту.')
            schedule_document_delivery(paid_case.id, settings)
        return True
    generate_prefixes = ('admin:generate:', 'admin:rerun_qa:')
    if data and data.startswith(generate_prefixes):
        case = await session.get(Case, int(data.split(':')[-1]))
        if not case:
            await _send(client, event, 'Заявка не найдена.')
        elif generate_documents is None:
            await _send(client, event, 'Генерация недоступна в этом режиме.')
        else:
            await session.refresh(case, ['user'])
            await _send(client, event, f'🔄 Повторная генерация для заявки #{case.id}...')
            await generate_documents(case)
        return True
    await _send(client, event, 'Эта функция админки MAX пока не поддерживается.', keyboards.admin_panel(payments_enabled()))
    return True
