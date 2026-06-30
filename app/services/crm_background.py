from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.config import Settings
from app.database import SessionLocal
from app.models import Case, CrmSyncLog, User
from app.services.amocrm import get_amocrm_service

logger = logging.getLogger(__name__)


def _safe_error(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    return text.replace("Authorization", "Auth")[:2000]


def schedule_crm_sync(
    settings: Settings,
    case_id: int | None,
    user_id: int | None,
    event_type: str,
    payload: dict | None = None,
) -> None:
    if not settings.amocrm_enabled:
        return
    if not settings.crm_sync_background:
        asyncio.create_task(run_crm_sync_job(settings, case_id, user_id, event_type, payload))
        return
    logger.info("CRM sync scheduled event=%s case_id=%s", event_type, case_id)
    task = asyncio.create_task(run_crm_sync_job(settings, case_id, user_id, event_type, payload))
    task.add_done_callback(_consume_task_exception)


def _consume_task_exception(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("CRM background task crashed")


async def run_crm_sync_job(
    settings: Settings,
    case_id: int | None,
    user_id: int | None,
    event_type: str,
    payload: dict | None = None,
) -> None:
    start = time.monotonic()
    logger.info("CRM sync start event=%s case_id=%s", event_type, case_id)
    attempts = max(1, settings.crm_sync_max_attempts)
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.wait_for(
                _run_once(settings, case_id, user_id, event_type, payload or {}),
                timeout=max(1, settings.crm_sync_timeout_seconds),
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.info("CRM sync done event=%s case_id=%s duration_ms=%s", event_type, case_id, duration_ms)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = _safe_error(exc)
            if attempt < attempts:
                await asyncio.sleep(max(0, settings.crm_sync_retry_base_seconds) * attempt)
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.error("CRM sync failed event=%s case_id=%s duration_ms=%s error=%s", event_type, case_id, duration_ms, last_error)
    async with SessionLocal() as session:
        session.add(
            CrmSyncLog(
                case_id=case_id,
                user_id=user_id,
                event_type=event_type,
                amo_entity_type="lead",
                success=False,
                error_message=last_error or "unknown CRM sync error",
                request_payload=None,
                response_payload=None,
            )
        )
        if case_id:
            case = await session.get(Case, case_id)
            if case:
                case.amocrm_sync_error = last_error
                case.amocrm_synced = False
        await session.commit()


async def _run_once(settings: Settings, case_id: int | None, user_id: int | None, event_type: str, payload: dict[str, Any]) -> None:
    async with SessionLocal() as session:
        case = await session.get(Case, case_id) if case_id else None
        user = await session.get(User, user_id) if user_id else None
        if not user and case:
            await session.refresh(case, ["user"])
            user = case.user
        if not case or not user:
            raise RuntimeError(f"CRM sync target not found case_id={case_id} user_id={user_id}")
        crm = get_amocrm_service(settings)
        await crm.sync_case_event(session, case, user, event_type, payload)
