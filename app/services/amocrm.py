from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Case, CrmSyncLog, User
from app.services.legal_data import normalize_order_data
from app.utils import safe_json_loads

logger = logging.getLogger(__name__)

PIPELINE_STATUSES = [
    "Подписался на бота",
    "Отправил фотографию приказа",
    "Сформирован предпросмотр",
    "Ожидает оплату",
    "Оплатил",
    "Нужна проверка",
    "Связался с менеджером",
    "Отказ / не оплатил",
]

EVENT_STATUS_MAP = {
    "start": "Подписался на бота",
    "order_photo": "Отправил фотографию приказа",
    "preview_ready": "Сформирован предпросмотр",
    "payment_created": "Ожидает оплату",
    "paid": "Оплатил",
    "qa_failed": "Нужна проверка",
    "manager_contact": "Связался с менеджером",
    "payment_declined": "Отказ / не оплатил",
}


class AmoCrmService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pipeline_cache: dict[str, Any] | None = None
        self._statuses_cache: dict[str, int] | None = None

    def is_enabled(self) -> bool:
        return bool(
            self.settings.amocrm_enabled
            and self.settings.amocrm_base_url
            and self.settings.amocrm_access_token
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict | None = None,
        retries: int = 3,
    ) -> tuple[dict | list | None, str | None]:
        if not self.is_enabled():
            return None, "amoCRM disabled"
        url = f"{self.settings.amocrm_base_url}/api/v4{path}"
        headers = {
            "Authorization": f"Bearer {self.settings.amocrm_access_token}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=30)
        last_error: str | None = None
        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.request(method, url, json=json_body, params=params) as response:
                        text = await response.text()
                        if response.status >= 400:
                            last_error = f"HTTP {response.status}: {text[:500]}"
                            if response.status in {429, 500, 502, 503, 504}:
                                await asyncio.sleep(2 ** attempt)
                                continue
                            return None, last_error
                        if not text.strip():
                            return {}, None
                        return json.loads(text), None
            except Exception as exc:
                last_error = str(exc)
                logger.warning("amoCRM request failed (%s %s): %s", method, path, exc)
                await asyncio.sleep(2 ** attempt)
        return None, last_error

    async def _log_sync(
        self,
        session: AsyncSession | None,
        *,
        case: Case | None,
        user: User | None,
        event_type: str,
        amo_entity_type: str | None,
        amo_entity_id: int | None,
        request_payload: Any,
        response_payload: Any,
        success: bool,
        error_message: str | None,
    ) -> None:
        if session is None:
            return
        row = CrmSyncLog(
            case_id=case.id if case else None,
            user_id=user.id if user else None,
            event_type=event_type,
            amo_entity_type=amo_entity_type,
            amo_entity_id=amo_entity_id,
            request_payload=json.dumps(request_payload, ensure_ascii=False) if request_payload is not None else None,
            response_payload=json.dumps(response_payload, ensure_ascii=False) if response_payload is not None else None,
            success=success,
            error_message=error_message,
        )
        session.add(row)
        if case:
            case.amocrm_last_sync_at = datetime.utcnow()
            case.amocrm_synced = success
            case.amocrm_sync_error = error_message
        await session.commit()

    async def get_pipeline_by_name(self, name: str) -> dict | None:
        data, error = await self._request("GET", "/leads/pipelines")
        if error or not data:
            logger.error("Failed to get pipelines: %s", error)
            return None
        embedded = data.get("_embedded", {}) if isinstance(data, dict) else {}
        for pipeline in embedded.get("pipelines", []):
            if pipeline.get("name") == name:
                return pipeline
        return None

    async def create_pipeline(self, name: str) -> dict | None:
        body = [{"name": name, "sort": 1, "is_main": False, "is_unsorted_on": False, "is_archive": False}]
        data, error = await self._request("POST", "/leads/pipelines", json_body=body)
        if error:
            logger.error("Failed to create pipeline: %s", error)
            return None
        embedded = data.get("_embedded", {}) if isinstance(data, dict) else {}
        pipelines = embedded.get("pipelines", [])
        return pipelines[0] if pipelines else None

    async def ensure_pipeline(self) -> dict | None:
        if self._pipeline_cache:
            return self._pipeline_cache
        name = self.settings.amocrm_pipeline_name
        pipeline = await self.get_pipeline_by_name(name)
        if pipeline:
            self._pipeline_cache = pipeline
            return pipeline
        if not self.settings.amocrm_auto_create_pipeline:
            logger.error("Pipeline '%s' not found and auto-create disabled", name)
            return None
        pipeline = await self.create_pipeline(name)
        self._pipeline_cache = pipeline
        return pipeline

    async def ensure_statuses(self, pipeline_id: int) -> dict[str, int]:
        if self._statuses_cache:
            return self._statuses_cache
        data, error = await self._request("GET", f"/leads/pipelines/{pipeline_id}")
        if error or not data:
            logger.error("Failed to get pipeline statuses: %s", error)
            return {}
        embedded = data.get("_embedded", {}) if isinstance(data, dict) else {}
        existing = {s["name"]: s["id"] for s in embedded.get("statuses", [])}
        result = {name: existing[name] for name in PIPELINE_STATUSES if name in existing}
        missing = [name for name in PIPELINE_STATUSES if name not in result]
        if missing and self.settings.amocrm_auto_create_statuses:
            to_create = []
            for index, name in enumerate(PIPELINE_STATUSES):
                if name in result:
                    continue
                to_create.append(
                    {
                        "name": name,
                        "sort": (index + 1) * 10,
                        "pipeline_id": pipeline_id,
                    }
                )
            if to_create:
                created, create_error = await self._request("POST", f"/leads/pipelines/{pipeline_id}/statuses", json_body=to_create)
                if not create_error and created:
                    embedded_created = created.get("_embedded", {}) if isinstance(created, dict) else {}
                    for status in embedded_created.get("statuses", []):
                        result[status["name"]] = status["id"]
        self._statuses_cache = result
        return result

    async def find_contact_by_telegram_id(self, telegram_id: int) -> dict | None:
        query = f"telegram_{telegram_id}"
        data, error = await self._request("GET", "/contacts", params={"query": str(telegram_id), "limit": 50})
        if error or not data:
            return None
        embedded = data.get("_embedded", {}) if isinstance(data, dict) else {}
        for contact in embedded.get("contacts", []):
            custom = contact.get("custom_fields_values") or []
            for field in custom:
                for value in field.get("values", []):
                    if str(value.get("value")) == str(telegram_id):
                        return contact
        contacts = embedded.get("contacts", [])
        return contacts[0] if contacts else None

    async def create_or_update_contact(self, user: User) -> int | None:
        name_parts = [user.first_name or "", user.last_name or ""]
        name = " ".join(p for p in name_parts if p).strip() or (user.username and f"@{user.username}") or f"Telegram {user.telegram_id}"
        body = {
            "name": name,
            "custom_fields_values": [
                {
                    "field_code": "PHONE",
                    "values": [{"value": user.phone, "enum_code": "WORK"}],
                }
            ]
            if user.phone
            else [],
        }
        existing_id = user.amocrm_contact_id
        if not existing_id and user.telegram_id:
            found = await self.find_contact_by_telegram_id(user.telegram_id)
            if found:
                existing_id = found.get("id")
        if existing_id:
            data, error = await self._request("PATCH", "/contacts", json_body=[{**body, "id": existing_id}])
        else:
            data, error = await self._request("POST", "/contacts", json_body=[body])
        if error:
            logger.error("Failed to create/update contact: %s", error)
            return None
        embedded = data.get("_embedded", {}) if isinstance(data, dict) else {}
        contacts = embedded.get("contacts", [])
        contact_id = contacts[0].get("id") if contacts else existing_id
        return int(contact_id) if contact_id else None

    def _lead_name(self, case: Case, user: User) -> str:
        if user.username:
            return f"Судебный приказ #{case.id} — @{user.username}"
        return f"Судебный приказ #{case.id} — Telegram ID {user.telegram_id or user.platform_user_id}"

    async def create_lead(self, case: Case, user: User, status_name: str) -> int | None:
        pipeline = await self.ensure_pipeline()
        if not pipeline:
            return None
        pipeline_id = pipeline["id"]
        statuses = await self.ensure_statuses(pipeline_id)
        status_id = statuses.get(status_name) or statuses.get("Подписался на бота")
        contact_id = await self.create_or_update_contact(user)
        body = [
            {
                "name": self._lead_name(case, user),
                "pipeline_id": pipeline_id,
                "status_id": status_id,
                "_embedded": {"contacts": [{"id": contact_id}]} if contact_id else {},
            }
        ]
        data, error = await self._request("POST", "/leads", json_body=body)
        if error:
            logger.error("Failed to create lead: %s", error)
            return None
        embedded = data.get("_embedded", {}) if isinstance(data, dict) else {}
        leads = embedded.get("leads", [])
        return int(leads[0]["id"]) if leads else None

    async def update_lead_status(self, case: Case, status_name: str) -> bool:
        if not case.amocrm_lead_id:
            return False
        pipeline = await self.ensure_pipeline()
        if not pipeline:
            return False
        statuses = await self.ensure_statuses(pipeline["id"])
        status_id = statuses.get(status_name)
        if not status_id:
            logger.error("Status '%s' not found in pipeline", status_name)
            return False
        data, error = await self._request(
            "PATCH",
            "/leads",
            json_body=[{"id": case.amocrm_lead_id, "status_id": status_id, "pipeline_id": pipeline["id"]}],
        )
        if error:
            logger.error("Failed to update lead status: %s", error)
            return False
        case.amocrm_status_id = status_id
        case.amocrm_pipeline_id = pipeline["id"]
        return True

    async def add_lead_note(self, case: Case, text: str) -> bool:
        if not case.amocrm_lead_id:
            return False
        body = [
            {
                "entity_id": case.amocrm_lead_id,
                "note_type": "common",
                "params": {"text": text[:65000]},
            }
        ]
        data, error = await self._request("POST", f"/leads/{case.amocrm_lead_id}/notes", json_body=body)
        if error:
            logger.error("Failed to add lead note: %s", error)
            return False
        return True

    async def attach_file_to_lead(self, case: Case, file_path: str | Path, caption: str = "") -> bool:
        path = Path(file_path)
        if not case.amocrm_lead_id or not path.exists():
            return False
        note = f"{caption}: {path.resolve()}" if caption else str(path.resolve())
        return await self.add_lead_note(case, f"Файл: {note}")

    async def sync_case_event(
        self,
        session: AsyncSession | None,
        case: Case,
        user: User,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        if not self.is_enabled():
            if self.settings.amocrm_debug:
                logger.info("amoCRM disabled, skip event %s for case %s", event_type, case.id)
            return
        payload = payload or {}
        status_name = EVENT_STATUS_MAP.get(event_type)
        try:
            if not case.amocrm_lead_id:
                contact_id = await self.create_or_update_contact(user)
                if contact_id:
                    user.amocrm_contact_id = contact_id
                    case.amocrm_contact_id = contact_id
                lead_id = await self.create_lead(case, user, status_name or "Подписался на бота")
                if lead_id:
                    case.amocrm_lead_id = lead_id
                    pipeline = await self.ensure_pipeline()
                    if pipeline:
                        case.amocrm_pipeline_id = pipeline["id"]
                    if session:
                        await session.commit()
            elif status_name:
                await self.update_lead_status(case, status_name)

            note_parts = [f"Событие: {event_type}"]
            if payload.get("text"):
                note_parts.append(str(payload["text"]))
            if payload.get("note"):
                note_parts.append(str(payload["note"]))
            if note_parts and case.amocrm_lead_id:
                await self.add_lead_note(case, "\n".join(note_parts))

            if self.settings.amocrm_attach_files and payload.get("files"):
                for item in payload["files"]:
                    await self.attach_file_to_lead(case, item.get("path", ""), item.get("caption", ""))

            if session:
                await self._log_sync(
                    session,
                    case=case,
                    user=user,
                    event_type=event_type,
                    amo_entity_type="lead",
                    amo_entity_id=case.amocrm_lead_id,
                    request_payload={"event": event_type, "payload": payload},
                    response_payload={"lead_id": case.amocrm_lead_id},
                    success=True,
                    error_message=None,
                )
        except Exception as exc:
            logger.exception("amoCRM sync failed for case %s event %s", case.id, event_type)
            if session:
                await self._log_sync(
                    session,
                    case=case,
                    user=user,
                    event_type=event_type,
                    amo_entity_type="lead",
                    amo_entity_id=case.amocrm_lead_id,
                    request_payload={"event": event_type, "payload": payload},
                    response_payload=None,
                    success=False,
                    error_message=str(exc),
                )

    async def build_ocr_note(self, case: Case) -> str:
        data = normalize_order_data(safe_json_loads(case.extracted_json, {}))
        lines = [
            "OCR завершен. Карточка данных:",
            f"Суд: {data.get('court_name', '')}",
            f"Должник: {data.get('debtor_full_name', '')}",
            f"Взыскатель: {data.get('creditor_name', '')}",
            f"Номер дела: {data.get('case_number', '')}",
            f"УИД: {data.get('uid', '')}",
            f"Дата приказа: {data.get('order_date', '')}",
            f"Дата получения: {case.received_date.strftime('%d.%m.%Y') if case.received_date else ''}",
            f"Срок до: {case.deadline_date.strftime('%d.%m.%Y') if case.deadline_date else ''}",
        ]
        return "\n".join(lines)


_service: AmoCrmService | None = None


def get_amocrm_service(settings: Settings) -> AmoCrmService:
    global _service
    if _service is None or _service.settings is not settings:
        _service = AmoCrmService(settings)
    return _service
