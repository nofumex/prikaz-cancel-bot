from __future__ import annotations

import json

from sqlalchemy import func, select

from app.models import Case, CrmSyncLog

PROBLEM_EVENT_TYPES = {
    'ocr_failed',
    'document_qa_failed',
    'order_download_failed',
    'wrong_document_type',
    'payment_failed',
    'generation_failed',
    'crm_sync_failed',
}

PROBLEM_CATEGORIES = {
    'regenerations': ('Регенерации', {'paid_document_regenerated'}),
    'missing_fields': ('Нет нужных полей', {'ocr_failed', 'document_qa_failed'}),
    'document_qa': ('Проверка документа', {'document_qa_failed', 'generation_failed'}),
    'downloads': ('Загрузка файлов', {'order_download_failed', 'wrong_document_type'}),
    'payments': ('Оплата', {'payment_failed'}),
    'crm': ('CRM', {'crm_sync_failed'}),
}

EVENT_LABELS = {
    'user_started_bot': 'Подписался на бота',
    'order_photo_uploaded': 'Отправил фото приказа',
    'ocr_completed': 'OCR приказа завершен',
    'document_qa_failed': 'Проблема с OCR/документом',
    'received_date_entered': 'Ввел дату получения приказа',
    'received_date_updated': 'Изменил дату получения приказа',
    'deadline_recalculated': 'Срок подачи пересчитан',
    'manager_requested': 'Связался с менеджером',
    'manager_message_sent': 'Написал менеджеру',
    'preview_generated': 'Получил preview заявления',
    'payment_created': 'Перешел к оплате',
    'payment_paid': 'Оплатил',
    'payment_canceled': 'Платеж отменен',
    'refund_recorded': 'Возврат учтен админом',
    'documents_invalidated': 'Документы пересоздаются после изменения даты',
    'documents_delivered': 'Получил документы',
    'paid_document_correction_started': 'Пользователь сообщил: данные в заявлении неверные',
    'paid_document_field_selected': 'Выбрал поле для исправления',
    'paid_document_field_corrected': 'Исправил поле',
    'paid_document_regenerated': 'Заявление перегенерировано после оплаты',
}


def _payload_error_text(row: CrmSyncLog) -> str | None:
    if not row.request_payload:
        return None
    try:
        payload = json.loads(row.request_payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    data = payload.get('payload') or payload
    for key in ('note', 'text', 'error', 'message'):
        value = str(data.get(key) or '').strip()
        if value:
            return value
    return None


def _human_error_text(row: CrmSyncLog) -> str | None:
    if row.success is False and row.error_message:
        return row.error_message.strip()
    if row.event_type in PROBLEM_EVENT_TYPES:
        note = _payload_error_text(row)
        if note:
            return note
        if row.error_message:
            return row.error_message.strip()
        return EVENT_LABELS.get(row.event_type)
    return None


async def client_path_text(session, case_id: int) -> str:
    query = select(CrmSyncLog).where(CrmSyncLog.case_id == case_id).order_by(CrmSyncLog.created_at.asc(), CrmSyncLog.id.asc())
    rows = list((await session.execute(query)).scalars())
    events = []
    previous = None
    for row in rows:
        label = EVENT_LABELS.get(row.event_type)
        if row.event_type == 'document_qa_failed' and row.request_payload:
            try:
                payload = json.loads(row.request_payload).get('payload') or {}
                note = str(payload.get('note') or '').strip()
                if note:
                    label = 'Проблема с OCR: ' + note[:180]
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if row.event_type == 'paid_document_field_corrected' and row.request_payload:
            try:
                payload = json.loads(row.request_payload).get('payload') or {}
                note = str(payload.get('note') or '').strip()
                if note:
                    label = note
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if not label or label == previous:
            continue
        events.append(label)
        previous = label
    if not events:
        return 'Путь клиента пока не записан.'
    lines = []
    for index, label in enumerate(events):
        prefix = '┌' if index == 0 else '└' if index == len(events) - 1 else '├'
        lines.append(f'{prefix} {label}')
    return '\n'.join(lines)


async def problem_case_error_text(session, case_id: int) -> str | None:
    query = select(CrmSyncLog).where(CrmSyncLog.case_id == case_id).order_by(CrmSyncLog.created_at.desc(), CrmSyncLog.id.desc())
    rows = list((await session.execute(query)).scalars())
    for row in rows:
        error = _human_error_text(row)
        if error:
            return error
    return None


async def problem_cases_page(session, page: int, page_size: int) -> tuple[list[Case], int, dict[int, str | None]]:
    problem_filter = (CrmSyncLog.success.is_(False)) | (CrmSyncLog.event_type.in_(PROBLEM_EVENT_TYPES))
    total_stmt = (
        select(func.count(func.distinct(Case.id)))
        .join(CrmSyncLog, CrmSyncLog.case_id == Case.id)
        .where(CrmSyncLog.case_id.is_not(None), problem_filter)
    )
    total = int(await session.scalar(total_stmt) or 0)
    ids_stmt = (
        select(Case.id)
        .join(CrmSyncLog, CrmSyncLog.case_id == Case.id)
        .where(CrmSyncLog.case_id.is_not(None), problem_filter)
        .distinct()
        .order_by(Case.created_at.desc(), Case.id.desc())
        .offset(page * page_size)
        .limit(page_size)
    )
    case_ids = list((await session.execute(ids_stmt)).scalars().all())
    if not case_ids:
        return [], total, {}
    cases_by_id = {
        case.id: case
        for case in (await session.execute(select(Case).where(Case.id.in_(case_ids)))).scalars().all()
    }
    cases = [cases_by_id[case_id] for case_id in case_ids if case_id in cases_by_id]
    errors: dict[int, str | None] = {}
    for case in cases:
        errors[case.id] = await problem_case_error_text(session, case.id)
    return cases, total, errors


async def problem_category_counts(session) -> dict[str, int]:
    counts = {}
    for key, (_, event_types) in PROBLEM_CATEGORIES.items():
        query = select(func.count(func.distinct(CrmSyncLog.case_id))).where(
            CrmSyncLog.case_id.is_not(None), CrmSyncLog.event_type.in_(event_types)
        )
        if key == 'missing_fields':
            query = query.where(CrmSyncLog.request_payload.ilike('%Не удалось прочитать обязательные поля%'))
        counts[key] = int(await session.scalar(query) or 0)
    return counts


async def problem_cases_by_category(session, category: str, page: int, page_size: int):
    event_types = PROBLEM_CATEGORIES.get(category, ('', set()))[1]
    filters = [CrmSyncLog.case_id.is_not(None), CrmSyncLog.event_type.in_(event_types)]
    if category == 'missing_fields':
        filters.append(CrmSyncLog.request_payload.ilike('%Не удалось прочитать обязательные поля%'))
    total = int(await session.scalar(
        select(func.count(func.distinct(Case.id))).join(CrmSyncLog, CrmSyncLog.case_id == Case.id).where(*filters)
    ) or 0)
    ids = list((await session.execute(
        select(Case.id).join(CrmSyncLog, CrmSyncLog.case_id == Case.id).where(*filters)
        .distinct().order_by(Case.created_at.desc(), Case.id.desc()).offset(page * page_size).limit(page_size)
    )).scalars())
    cases_by_id = {row.id: row for row in (await session.execute(select(Case).where(Case.id.in_(ids)))).scalars()}
    cases = [cases_by_id[item] for item in ids if item in cases_by_id]
    errors = {case.id: await problem_case_error_text(session, case.id) for case in cases}
    return cases, total, errors


async def order_photo_paths(session, case: Case) -> list[str]:
    paths = []
    rows = list((await session.execute(
        select(CrmSyncLog).where(CrmSyncLog.case_id == case.id, CrmSyncLog.event_type == 'order_photo_uploaded')
        .order_by(CrmSyncLog.created_at.asc(), CrmSyncLog.id.asc())
    )).scalars())
    for row in rows:
        try:
            payload = json.loads(row.request_payload or '{}').get('payload') or {}
            for item in payload.get('files') or []:
                path = str(item.get('path') or '').strip()
                if path and path not in paths:
                    paths.append(path)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    if case.order_photo_path and case.order_photo_path not in paths:
        paths.append(case.order_photo_path)
    return paths
