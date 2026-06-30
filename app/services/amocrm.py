from __future__ import annotations

import asyncio
import json
import logging
import time
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
    "Отправил приказ",
    "Указал дату",
    "Оплатил",
    "Получил напоминание (не оплатил)",
]

EVENT_STATUS_MAP = {
    "user_started_bot": "Подписался на бота",
    "order_photo_uploaded": "Отправил приказ",
    "received_date_entered": "Указал дату",
    "envelope_photo_uploaded": "Указал дату",
    "ocr_completed": "Указал дату",
    "case_data_confirmed": "Указал дату",
    "preview_generated": "Указал дату",
    "payment_created": "Указал дату",
    "document_qa_failed": "Указал дату",
    "manager_requested": "Указал дату",
    "payment_paid": "Оплатил",
    "documents_delivered": "Оплатил",
    "reminder_sent": "Получил напоминание (не оплатил)",
    "payment_abandoned": "Получил напоминание (не оплатил)",
}


def _safe_json(data: Any, limit: int = 4000) -> str | None:
    if data is None:
        return None
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    # Defensive redaction
    text = text.replace("Authorization", "Auth")
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


class _RateLimiter:
    def __init__(self, rps: int) -> None:
        self._interval = 1.0 / max(1, rps)
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_for = self._interval - (now - self._last_call)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_call = time.monotonic()


class AmoCrmService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pipeline_cache: dict[str, Any] | None = None
        self._statuses_cache: dict[str, int] | None = None
        self._limiter = _RateLimiter(settings.amocrm_rps_limit)
        self._timeout = aiohttp.ClientTimeout(total=30)

    def is_enabled(self) -> bool:
        return bool(self.settings.amocrm_enabled and self.settings.amocrm_base_url and self.settings.amocrm_access_token)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        retries: int = 3,
    ) -> tuple[dict | list | None, str | None]:
        if not self.is_enabled():
            return None, "amoCRM disabled"
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.settings.amocrm_base_url}/api/v4{path}"
        headers = {"Authorization": f"Bearer {self.settings.amocrm_access_token}"}
        if files is None:
            headers["Content-Type"] = "application/json"

        last_error: str | None = None
        for attempt in range(retries):
            await self._limiter.wait()
            try:
                async with aiohttp.ClientSession(timeout=self._timeout, headers=headers) as session:
                    async with session.request(method, url, json=json_body, params=params, data=files) as response:
                        raw_text = await response.text()
                        if response.status >= 400:
                            last_error = f"HTTP {response.status}: {raw_text[:600]}"
                            if response.status in {429, 500, 502, 503, 504}:
                                await asyncio.sleep(1.5**attempt)
                                continue
                            return None, last_error
                        if not raw_text.strip():
                            return {}, None
                        try:
                            return json.loads(raw_text), None
                        except json.JSONDecodeError:
                            return {"raw": raw_text}, None
            except Exception as exc:
                last_error = str(exc)
                logger.warning("amoCRM request failed method=%s path=%s err=%s", method, path, exc)
                await asyncio.sleep(1.5**attempt)
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
            request_payload=_safe_json(request_payload),
            response_payload=_safe_json(response_payload),
            success=success,
            error_message=(error_message[:2000] if error_message else None),
        )
        session.add(row)
        if case:
            case.amocrm_last_sync_at = datetime.utcnow()
            case.amocrm_synced = success
            case.amocrm_sync_error = error_message
        await session.commit()

    async def get_pipeline_by_name(self, name: str) -> dict | None:
        data, error = await self.request("GET", "/leads/pipelines")
        if error or not isinstance(data, dict):
            logger.error("Failed to get pipelines: %s", error)
            return None
        for pipeline in data.get("_embedded", {}).get("pipelines", []):
            if pipeline.get("name") == name:
                return pipeline
        return None

    async def create_pipeline(self, name: str) -> dict | None:
        payload = [{"name": name, "sort": 1, "is_main": False, "is_unsorted_on": False, "is_archive": False}]
        data, error = await self.request("POST", "/leads/pipelines", json_body=payload)
        if error or not isinstance(data, dict):
            logger.error("Failed to create pipeline: %s", error)
            return None
        return (data.get("_embedded", {}).get("pipelines") or [None])[0]

    async def ensure_pipeline(self) -> dict | None:
        if self._pipeline_cache:
            return self._pipeline_cache

        # Prefer explicit pipeline_id when configured.
        if self.settings.amocrm_pipeline_id:
            data, error = await self.request("GET", f"/leads/pipelines/{self.settings.amocrm_pipeline_id}")
            if not error and isinstance(data, dict):
                self._pipeline_cache = data
                return data

        pipeline = await self.get_pipeline_by_name(self.settings.amocrm_pipeline_name)
        if pipeline:
            self._pipeline_cache = pipeline
            return pipeline
        if not self.settings.amocrm_auto_create_pipeline:
            logger.error("Pipeline '%s' not found and auto-create disabled", self.settings.amocrm_pipeline_name)
            return None
        created = await self.create_pipeline(self.settings.amocrm_pipeline_name)
        self._pipeline_cache = created
        return created

    async def get_statuses(self, pipeline_id: int) -> dict[str, int]:
        data, error = await self.request("GET", f"/leads/pipelines/{pipeline_id}")
        if error or not isinstance(data, dict):
            logger.error("Failed to get statuses for pipeline %s: %s", pipeline_id, error)
            return {}
        statuses = data.get("_embedded", {}).get("statuses", [])
        return {str(item["name"]): int(item["id"]) for item in statuses if item.get("name") and item.get("id")}

    async def ensure_statuses(self, pipeline_id: int) -> dict[str, int]:
        if self._statuses_cache:
            return self._statuses_cache
        existing = await self.get_statuses(pipeline_id)
        missing = [name for name in PIPELINE_STATUSES if name not in existing]
        created_count = 0
        if missing and self.settings.amocrm_auto_create_statuses:
            payload = []
            for idx, name in enumerate(PIPELINE_STATUSES, start=1):
                if name in existing:
                    continue
                payload.append({"name": name, "sort": idx * 10, "pipeline_id": pipeline_id})
            if payload:
                response, error = await self.request("POST", f"/leads/pipelines/{pipeline_id}/statuses", json_body=payload)
                if error:
                    logger.error("Failed creating statuses: %s", error)
                elif isinstance(response, dict):
                    for item in response.get("_embedded", {}).get("statuses", []):
                        if item.get("name") and item.get("id"):
                            existing[str(item["name"])] = int(item["id"])
                            created_count += 1
        self._statuses_cache = existing
        if self.settings.amocrm_debug:
            logger.info("amoCRM statuses ensured pipeline=%s created=%s", pipeline_id, created_count)
        return existing

    async def get_status_id(self, status_name: str) -> int | None:
        pipeline = await self.ensure_pipeline()
        if not pipeline:
            return None
        statuses = await self.ensure_statuses(int(pipeline["id"]))
        return statuses.get(status_name)

    async def find_contact_by_platform_id(self, platform: str, platform_user_id: str) -> dict | None:
        data, error = await self.request("GET", "/contacts", params={"query": str(platform_user_id), "limit": 50})
        if error or not isinstance(data, dict):
            return None
        contacts = data.get("_embedded", {}).get("contacts", [])
        platform_id = str(platform_user_id)
        for contact in contacts:
            custom_values = contact.get("custom_fields_values") or []
            for field in custom_values:
                for value in field.get("values", []):
                    if str(value.get("value")) == platform_id:
                        return contact
        # Fallback if no custom field exists in account.
        return contacts[0] if contacts else None

    async def create_or_update_contact(self, user: User) -> int | None:
        name = (
            " ".join(part for part in [user.first_name or "", user.last_name or ""] if part).strip()
            or (user.telegram_username and f"@{user.telegram_username}")
            or (user.username and f"@{user.username}")
            or f"{user.platform.upper()} {user.platform_user_id}"
        )
        source = "Telegram бот" if user.platform == "telegram" else "MAX бот"
        note_text = (
            f"Источник: {source} — отмена судебного приказа\n"
            f"Platform: {user.platform}\n"
            f"Platform user ID: {user.platform_user_id}\n"
            f"Telegram ID: {user.telegram_id or ''}\n"
            f"Username: @{(user.telegram_username or user.username or '').lstrip('@')}"
        )
        contact_payload: dict[str, Any] = {"name": name}
        if user.phone:
            contact_payload["custom_fields_values"] = [{"field_code": "PHONE", "values": [{"value": user.phone, "enum_code": "WORK"}]}]

        contact_id = user.amocrm_contact_id
        if not contact_id:
            found = await self.find_contact_by_platform_id(user.platform, user.platform_user_id)
            if found:
                contact_id = int(found["id"])

        if contact_id:
            _, error = await self.request("PATCH", "/contacts", json_body=[{**contact_payload, "id": contact_id}])
            if error:
                logger.error("Failed to update contact: %s", error)
                return None
        else:
            created, error = await self.request("POST", "/contacts", json_body=[contact_payload])
            if error or not isinstance(created, dict):
                logger.error("Failed to create contact: %s", error)
                return None
            created_list = created.get("_embedded", {}).get("contacts", [])
            if not created_list:
                return None
            contact_id = int(created_list[0]["id"])

        if contact_id:
            await self.request(
                "POST",
                f"/contacts/{contact_id}/notes",
                json_body=[{"entity_id": contact_id, "note_type": "common", "params": {"text": note_text[:65000]}}],
            )
        return int(contact_id) if contact_id else None

    def _lead_name(self, case: Case, user: User) -> str:
        username = user.telegram_username or user.username
        if username:
            return f"Судебный приказ — {user.platform} @{username.lstrip('@')}"
        return f"Судебный приказ — {user.platform} ID {user.platform_user_id}"



    async def find_lead_by_name(self, name: str, pipeline_id: int) -> dict | None:
        data, error = await self.request("GET", "/leads", params={"query": name, "limit": 50})
        if error or not isinstance(data, dict):
            return None
        for lead in data.get("_embedded", {}).get("leads", []):
            if lead.get("name") == name and int(lead.get("pipeline_id") or 0) == int(pipeline_id):
                return lead
        return None

    async def find_lead_by_platform_user(self, user: User, pipeline_id: int) -> dict | None:
        queries = [f"{user.platform} ID {user.platform_user_id}"]
        if user.username or user.telegram_username:
            queries.append((user.username or user.telegram_username or "").lstrip("@"))
        for query in queries:
            data, error = await self.request("GET", "/leads", params={"query": query, "limit": 50})
            if error or not isinstance(data, dict):
                continue
            for lead in data.get("_embedded", {}).get("leads", []):
                if int(lead.get("pipeline_id") or 0) == int(pipeline_id):
                    return lead
        return None

    async def create_lead(self, case: Case, user: User, status_name: str) -> int | None:
        pipeline = await self.ensure_pipeline()
        if not pipeline:
            return None
        pipeline_id = int(pipeline["id"])
        statuses = await self.ensure_statuses(pipeline_id)
        status_id = statuses.get(status_name) or statuses.get("Подписался на бота")
        lead_name = self._lead_name(case, user)
        existing = await self.find_lead_by_name(lead_name, pipeline_id) or await self.find_lead_by_platform_user(user, pipeline_id)
        if existing:
            lead_id = int(existing["id"])
            case.amocrm_lead_id = lead_id
            case.amo_lead_id = lead_id
            case.amocrm_pipeline_id = pipeline_id
            await self.request("PATCH", "/leads", json_body=[{"id": lead_id, "name": lead_name}])
            await self.update_lead_status(case, status_name)
            return lead_id
        contact_id = await self.create_or_update_contact(user)
        payload = [
            {
                "name": lead_name,
                "pipeline_id": pipeline_id,
                "status_id": status_id,
                "_embedded": {"contacts": [{"id": contact_id}]} if contact_id else {},
            }
        ]
        data, error = await self.request("POST", "/leads", json_body=payload)
        if error or not isinstance(data, dict):
            logger.error("Failed to create lead: %s", error)
            return None
        leads = data.get("_embedded", {}).get("leads", [])
        if not leads:
            return None
        lead_id = int(leads[0]["id"])
        case.amocrm_pipeline_id = pipeline_id
        case.amocrm_status_id = status_id
        case.amocrm_status_name = status_name
        return lead_id

    async def update_lead_status(self, case: Case, status_name: str) -> bool:
        lead_id = case.amocrm_lead_id or case.amo_lead_id
        if not lead_id:
            return False
        if case.amocrm_status_name == "Оплатил" and status_name == "Получил напоминание (не оплатил)":
            return True
        pipeline = await self.ensure_pipeline()
        if not pipeline:
            return False
        pipeline_id = int(pipeline["id"])
        statuses = await self.ensure_statuses(pipeline_id)
        status_id = statuses.get(status_name)
        if not status_id:
            logger.error("Status '%s' not found in ensured statuses", status_name)
            return False
        _, error = await self.request(
            "PATCH",
            "/leads",
            json_body=[{"id": int(lead_id), "pipeline_id": pipeline_id, "status_id": status_id}],
        )
        if error:
            logger.error("Failed to update lead status: %s", error)
            return False
        case.amocrm_pipeline_id = pipeline_id
        case.amocrm_status_id = status_id
        case.amocrm_status_name = status_name
        return True

    async def add_lead_note(self, case: Case, text: str) -> bool:
        lead_id = case.amocrm_lead_id or case.amo_lead_id
        if not lead_id:
            return False
        _, error = await self.request(
            "POST",
            f"/leads/{int(lead_id)}/notes",
            json_body=[{"entity_id": int(lead_id), "note_type": "common", "params": {"text": text[:65000]}}],
        )
        return error is None

    async def attach_file_to_lead(self, case: Case, file_path: str | Path, caption: str) -> bool:
        # amoCRM file API varies by account permissions and token scope.
        # Keep production-safe fallback: note with local path + explicit marker.
        path = Path(file_path)
        lead_id = case.amocrm_lead_id or case.amo_lead_id
        if not lead_id or not path.exists():
            return False
        note = f"{caption}: {path.resolve()}\nФайл не загружен в amoCRM, fallback path"
        return await self.add_lead_note(case, note)

    async def sync_case_event(
        self,
        session: AsyncSession | None,
        case: Case,
        user: User,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        payload = payload or {}
        if not self.is_enabled():
            if session:
                await self._log_sync(
                    session,
                    case=case,
                    user=user,
                    event_type=event_type,
                    amo_entity_type="lead",
                    amo_entity_id=case.amocrm_lead_id or case.amo_lead_id,
                    request_payload={"event_type": event_type, "payload": payload, "disabled": True},
                    response_payload=None,
                    success=False,
                    error_message="amoCRM disabled",
                )
            return

        status_name = EVENT_STATUS_MAP.get(event_type)
        response_payload: dict[str, Any] = {}
        try:
            if not (case.amocrm_lead_id or case.amo_lead_id):
                contact_id = await self.create_or_update_contact(user)
                if contact_id:
                    user.amocrm_contact_id = contact_id
                    case.amocrm_contact_id = contact_id
                lead_id = await self.create_lead(case, user, status_name or "Подписался на бота")
                if lead_id:
                    case.amocrm_lead_id = lead_id
                    case.amo_lead_id = lead_id
            elif status_name:
                await self.update_lead_status(case, status_name)

            if status_name:
                response_payload["status_name"] = status_name
                response_payload["status_id"] = case.amocrm_status_id

            note_parts = [f"Событие: {event_type}"]
            if payload.get("note"):
                note_parts.append(str(payload["note"]))
            if payload.get("text"):
                note_parts.append(str(payload["text"]))
            if payload.get("received_date"):
                note_parts.append(f"Дата получения: {payload['received_date']}")
            if payload.get("deadline"):
                note_parts.append(f"Срок до: {payload['deadline']}")
            if payload.get("payment"):
                note_parts.append(f"Платеж: {payload['payment']}")
            await self.add_lead_note(case, "\n".join(note_parts))

            if self.settings.amocrm_attach_files and payload.get("files"):
                for item in payload["files"]:
                    await self.attach_file_to_lead(case, item.get("path", ""), item.get("caption", "Файл"))

            if session:
                await self._log_sync(
                    session,
                    case=case,
                    user=user,
                    event_type=event_type,
                    amo_entity_type="lead",
                    amo_entity_id=case.amocrm_lead_id or case.amo_lead_id,
                    request_payload={"event_type": event_type, "payload": payload},
                    response_payload=response_payload,
                    success=True,
                    error_message=None,
                )
        except Exception as exc:
            logger.exception("amoCRM sync failed case=%s event=%s", case.id, event_type)
            if session:
                await self._log_sync(
                    session,
                    case=case,
                    user=user,
                    event_type=event_type,
                    amo_entity_type="lead",
                    amo_entity_id=case.amocrm_lead_id or case.amo_lead_id,
                    request_payload={"event_type": event_type, "payload": payload},
                    response_payload=response_payload,
                    success=False,
                    error_message=str(exc),
                )

    async def sync_case_current_state(self, session: AsyncSession, case: Case, user: User) -> None:
        status_event = {
            "draft": "user_started_bot",
            "waiting_order_photo": "user_started_bot",
            "waiting_order_rephoto": "order_photo_uploaded",
            "waiting_envelope": "order_photo_uploaded",
            "waiting_received_date": "order_photo_uploaded",
            "processing": "ocr_completed",
            "needs_review": "document_qa_failed",
            "preview_ready": "preview_generated",
            "payment_pending": "payment_created",
            "paid": "payment_paid",
            "delivered": "documents_delivered",
        }.get(case.status, "user_started_bot")
        await self.sync_case_event(session, case, user, status_event)

    async def ensure_pipeline_and_statuses(self) -> dict[str, Any]:
        report: dict[str, Any] = {"ok": False, "pipeline": None, "statuses": {}, "created": 0, "errors": []}
        pipeline = await self.ensure_pipeline()
        if not pipeline:
            report["errors"].append("Pipeline not found")
            return report
        report["pipeline"] = {"id": pipeline.get("id"), "name": pipeline.get("name")}
        before = await self.get_statuses(int(pipeline["id"]))
        current = await self.ensure_statuses(int(pipeline["id"]))
        report["statuses"] = current
        report["created"] = max(0, len(current) - len(before))
        missing = [name for name in PIPELINE_STATUSES if name not in current]
        if missing:
            report["errors"].append("Missing statuses: " + ", ".join(missing))
        report["ok"] = not report["errors"]
        return report

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
            f"Сумма долга: {data.get('debt_amount', '')}",
            f"Госпошлина: {data.get('state_duty', '')}",
            f"Итого: {data.get('total_amount', '')}",
        ]
        return "\n".join(lines)


_service: AmoCrmService | None = None


def get_amocrm_service(settings: Settings) -> AmoCrmService:
    global _service
    if _service is None or _service.settings is not settings:
        _service = AmoCrmService(settings)
    return _service
