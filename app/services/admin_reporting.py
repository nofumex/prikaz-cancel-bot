from __future__ import annotations

import json

from sqlalchemy import select

from app.models import CrmSyncLog

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
    'documents_invalidated': 'Документы пересоздаются после изменения даты',
    'documents_delivered': 'Получил документы',
}


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
