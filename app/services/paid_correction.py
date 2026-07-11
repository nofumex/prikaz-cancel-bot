from __future__ import annotations

import json

from app.services.crm_background import schedule_crm_sync
from app.services.documents import create_case_documents_reviewed


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
    await session.commit()
    schedule_crm_sync(settings, case.id, user.id, 'paid_document_regenerated', {'note': 'Заявление перегенерировано после оплаты'})
    return artifacts
