from __future__ import annotations

import json

from app.services.crm_background import schedule_crm_sync
from app.services.documents import create_case_documents_reviewed
from app.services.legal_data import is_deadline_missed

PAID_EDITABLE_FIELDS = {
    'court_name', 'court_address', 'debtor_full_name', 'debtor_address',
    'creditor_name', 'creditor_address', 'case_number', 'order_date', 'uid',
    'debt_contract', 'debt_period', 'debt_amount', 'state_duty',
}


def corrected_fields(case) -> set[str]:
    try:
        return set(json.loads(case.paid_corrected_fields_json or '[]'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()


def correction_allowed(case, field: str) -> bool:
    fields = corrected_fields(case)
    return field in fields or len(fields) < len(PAID_EDITABLE_FIELDS) - 1


def record_corrected_field(case, field: str) -> None:
    fields = corrected_fields(case)
    fields.add(field)
    case.paid_corrected_fields_json = json.dumps(sorted(fields), ensure_ascii=False)


def paid_regeneration_requires_new_date(case) -> bool:
    try:
        data = json.loads(case.extracted_json or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    return is_deadline_missed(case.deadline_date) and not data.get('restore_reason')


async def regenerate_paid_case(session, settings, case, user):
    data = json.loads(case.extracted_json or '{}')
    outcome = await create_case_documents_reviewed(case, user, settings, session, restore_reason=data.get('restore_reason'))
    if not outcome.ok or outcome.artifacts is None:
        raise ValueError(outcome.admin_report or 'Документ не прошел проверку после исправления.')
    artifacts = outcome.artifacts
    case.full_doc_path = str(artifacts.full_docx_path)
    case.full_pdf_path = str(artifacts.full_pdf_path) if artifacts.full_pdf_path else None
    case.preview_pdf_path = str(artifacts.preview_pdf_path) if artifacts.preview_pdf_path else None
    case.instruction_path = str(artifacts.instruction_docx_path)
    case.paid_regeneration_count = (case.paid_regeneration_count or 0) + 1
    await session.commit()
    schedule_crm_sync(settings, case.id, user.id, 'paid_document_regenerated', {'note': 'Заявление перегенерировано после оплаты'})
    return artifacts
