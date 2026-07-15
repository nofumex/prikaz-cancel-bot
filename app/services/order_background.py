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
from app.services.legal_data import missing_order_fields, normalize_debtor_name_fields, normalize_order_data
from app.services.llm import extract_order_data

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OrderExtractionResult:
    case_id: int
    missing: list[str]
    ok: bool


_tasks: dict[int, asyncio.Task[OrderExtractionResult]] = {}


async def _extract_and_store(settings: Settings, case_id: int, user_id: int) -> OrderExtractionResult:
    async with SessionLocal() as session:
        case = await session.get(Case, case_id)
        user = await session.get(User, user_id)
        if case is None or user is None or not case.order_photo_path:
            return OrderExtractionResult(case_id, ["order_photo"], False)
        try:
            extracted = await extract_order_data(
                settings, session, case_id=case.id, user_id=user.id,
                order_photo_path=case.order_photo_path,
            )
        except Exception:
            logger.exception("Background order extraction failed case_id=%s", case_id)
            extracted = {}
        extracted = normalize_order_data(extracted)
        extracted, name_result = normalize_debtor_name_fields(extracted)
        if name_result and name_result.confidence >= 0.85 and name_result.normalized:
            extracted["debtor_full_name"] = name_result.normalized
        missing = [field for field in missing_order_fields(extracted, case.received_date) if field != "received_date"]
        case.extracted_json = json.dumps(extracted, ensure_ascii=False)
        case.missing_fields = json.dumps(missing, ensure_ascii=False)
        if missing:
            case.order_rephoto_attempts = (case.order_rephoto_attempts or 0) + 1
            case.status = CaseStatus.WAITING_ORDER_REPHOTO.value
        else:
            case.order_rephoto_attempts = 0
            case.status = CaseStatus.PROCESSING.value
        await session.commit()
        schedule_crm_sync(settings, case.id, user.id, "ocr_completed", {"note": "OCR приказа завершен в фоне"})
        return OrderExtractionResult(case.id, missing, not missing)


def start_order_extraction(settings: Settings, case_id: int, user_id: int) -> asyncio.Task[OrderExtractionResult]:
    current = _tasks.get(case_id)
    if current is not None and not current.done():
        return current
    task = asyncio.create_task(_extract_and_store(settings, case_id, user_id), name=f"order-extraction-{case_id}")
    _tasks[case_id] = task

    # Keep completed tasks until the next user step so a DB object loaded just
    # before the commit cannot accidentally start a second OCR run. Bound the
    # registry for users who upload a photo and never continue.
    if len(_tasks) > 256:
        for old_case_id, old_task in list(_tasks.items()):
            if old_case_id != case_id and old_task.done():
                _tasks.pop(old_case_id, None)
                if len(_tasks) <= 256:
                    break
    return task


async def wait_order_extraction(settings: Settings, case_id: int, user_id: int) -> OrderExtractionResult:
    task = _tasks.get(case_id) or start_order_extraction(settings, case_id, user_id)
    try:
        return await task
    finally:
        if task.done() and _tasks.get(case_id) is task:
            _tasks.pop(case_id, None)
