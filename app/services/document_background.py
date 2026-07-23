from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app.config import Settings
from app.database import SessionLocal
from app.enums import CaseStatus
from app.models import Case, User
from app.services.crm_background import schedule_crm_sync
from app.services.documents import create_case_documents_reviewed
from app.services.legal_data import is_deadline_missed, normalize_order_data
from app.services.order_background import wait_order_extraction

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DocumentPreparationResult:
    case_id: int
    ok: bool
    reason: str = ""


_tasks: dict[int, asyncio.Task[DocumentPreparationResult]] = {}


async def _prepare(settings: Settings, case_id: int, user_id: int) -> DocumentPreparationResult:
    extraction = await wait_order_extraction(settings, case_id, user_id)
    if not extraction.ok or extraction.missing:
        return DocumentPreparationResult(case_id, False, "required_render_fields_missing")
    async with SessionLocal() as session:
        case = await session.get(Case, case_id)
        user = await session.get(User, user_id)
        if case is None or user is None or not case.received_date:
            return DocumentPreparationResult(case_id, False, "case_or_received_date_missing")
        data = normalize_order_data(json.loads(case.extracted_json or "{}"))
        stored_reason = data.get("restore_reason") or ""
        if is_deadline_missed(case.deadline_date) and not stored_reason:
            return DocumentPreparationResult(case_id, False, "restore_reason_required")
        try:
            outcome = await create_case_documents_reviewed(
                case, user, settings, session, restore_reason=stored_reason or None
            )
        except Exception as exc:
            logger.exception("Background document preparation failed case_id=%s", case_id)
            return DocumentPreparationResult(case_id, False, str(exc))
        if not outcome.ok or outcome.artifacts is None:
            return DocumentPreparationResult(case_id, False, outcome.admin_report or "document_review_failed")
        artifacts = outcome.artifacts
        case.full_doc_path = str(artifacts.full_docx_path)
        case.full_pdf_path = str(artifacts.full_pdf_path) if artifacts.full_pdf_path else None
        case.preview_pdf_path = str(artifacts.preview_pdf_path) if artifacts.preview_pdf_path else None
        case.preview_doc_path = None
        case.instruction_path = str(artifacts.instruction_docx_path)
        case.status = CaseStatus.PREVIEW_READY.value
        await session.commit()
        schedule_crm_sync(settings, case.id, user.id, "preview_generated", {"note": "Preview сформирован в фоне. Document QA: passed"})
        return DocumentPreparationResult(case.id, True)


def start_document_preparation(settings: Settings, case_id: int, user_id: int) -> asyncio.Task[DocumentPreparationResult]:
    current = _tasks.get(case_id)
    if current is not None and not current.done():
        return current
    task = asyncio.create_task(_prepare(settings, case_id, user_id), name=f"document-preparation-{case_id}")
    _tasks[case_id] = task
    if len(_tasks) > 256:
        for old_case_id, old_task in list(_tasks.items()):
            if old_case_id != case_id and old_task.done():
                _tasks.pop(old_case_id, None)
                if len(_tasks) <= 256:
                    break
    return task


async def wait_document_preparation(settings: Settings, case_id: int, user_id: int) -> DocumentPreparationResult:
    task = _tasks.get(case_id) or start_document_preparation(settings, case_id, user_id)
    try:
        return await task
    finally:
        if task.done() and _tasks.get(case_id) is task:
            _tasks.pop(case_id, None)


async def wait_started_document_preparation(case_id: int) -> DocumentPreparationResult | None:
    task = _tasks.get(case_id)
    if task is None:
        return None
    try:
        return await task
    finally:
        if task.done() and _tasks.get(case_id) is task:
            _tasks.pop(case_id, None)
