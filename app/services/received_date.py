from __future__ import annotations

from datetime import date

from app.enums import CaseStatus
from app.services.cases import set_received_date
from app.services.crm_background import schedule_crm_sync
from app.utils import parse_russian_date

DATE_PROMPT = (
    'Введите дату получения судебного приказа в формате ДД.ММ.ГГГГ, например: 10.07.2026\n\n'
    'Можно написать через точку, пробел, слэш, запятую или дефис: '
    '10.07.2026, 10 07 2026, 10/07/26.'
)
DATE_PARSE_ERROR = 'Не удалось понять дату. Введите дату в формате ДД.ММ.ГГГГ, например 10.07.2026'


def validate_received_date(case, raw: str | None, *, today: date | None = None) -> tuple[date | None, str | None]:
    received = parse_russian_date(raw)
    if not received:
        return None, DATE_PARSE_ERROR
    extracted = getattr(case, 'extracted_json', None)
    order_date = None
    if extracted:
        import json

        try:
            order_date = parse_russian_date((json.loads(extracted) or {}).get('order_date'))
        except (TypeError, ValueError):
            order_date = None
    if order_date and received < order_date:
        return None, 'Дата получения не может быть раньше даты судебного приказа. Проверьте дату и введите еще раз.'
    if received > (today or date.today()):
        return None, 'Дата получения не может быть в будущем. Проверьте дату и введите еще раз.'
    return received, None


async def save_received_date(session, settings, case, user, received: date) -> str:
    previous = case.received_date
    changed = previous is not None and previous != received
    had_documents = bool(case.preview_pdf_path or case.preview_doc_path or case.full_doc_path or case.full_pdf_path)
    if changed and had_documents:
        case.preview_pdf_path = None
        case.preview_doc_path = None
        case.full_doc_path = None
        case.full_pdf_path = None
        case.instruction_path = None
        case.payment_url = None
        case.status = CaseStatus.PROCESSING.value
    await set_received_date(session, case, received)
    event = 'received_date_updated' if previous else 'received_date_entered'
    payload = {
        'received_date': received.strftime('%d.%m.%Y'),
        'deadline': case.deadline_date.strftime('%d.%m.%Y') if case.deadline_date else '',
    }
    schedule_crm_sync(settings, case.id, user.id, event, payload)
    schedule_crm_sync(settings, case.id, user.id, 'deadline_recalculated', payload)
    if changed and had_documents:
        schedule_crm_sync(settings, case.id, user.id, 'documents_invalidated', payload)
    return event
