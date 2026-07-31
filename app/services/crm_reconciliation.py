from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal
from app.models import Case, ChatMessage, ChatSession, CrmSyncLog, User
from app.services.amocrm import crm_event_dedupe_key
from app.services.cases import ensure_user_has_case, latest_case
from app.services.chat_crm_sync import CHAT_MESSAGE_CRM_EVENT, build_chat_message_payload
from app.services.crm_background import run_crm_sync_job

logger = logging.getLogger(__name__)


def _status_for_case(case: Case) -> str:
    if case.consultation_reminder_sent_at or case.post_payment_followup_sent_at:
        return "Получил предложение о консультации"
    if case.status in {"paid", "delivered"}:
        return "Оплатил"
    if case.reminders_sent or case.deadline_reminder_sent_at:
        return "Получил напоминание"
    if case.status in {"waiting_order_rephoto", "waiting_envelope", "waiting_received_date"}:
        return "Отправил приказ"
    if case.status in {"processing", "needs_review", "preview_ready", "payment_pending"}:
        return "Указал дату"
    return "Подписался на бота"


def _existing_case_files(case: Case) -> list[dict[str, str]]:
    candidates = (
        (case.order_photo_path, "Фото судебного приказа"),
        (case.envelope_photo_path, "Фото конверта"),
        (case.full_doc_path, "Готовое заявление DOCX"),
        (case.full_pdf_path, "Готовое заявление PDF"),
        (case.preview_pdf_path, "Предпросмотр заявления"),
        (case.instruction_path, "Инструкция"),
    )
    return [
        {"path": str(path), "caption": caption}
        for path, caption in candidates
        if path and Path(path).is_file()
    ]


async def _history_jobs_for_user(user_id: int) -> list[tuple[dict, str]]:
    jobs: list[tuple[dict, str]] = []
    async with SessionLocal() as session:
        cases = list(
            (
                await session.execute(
                    select(Case).where(Case.user_id == user_id).order_by(Case.created_at, Case.id)
                )
            ).scalars()
        )
        for case in cases:
            def add_case_event(suffix: str, note: str, files: list[dict[str, str]] | None = None) -> None:
                source_key = f"case:{case.id}:{suffix}"
                payload: dict = {"source_event_key": source_key, "note": note}
                if files:
                    payload["files"] = files
                jobs.append((payload, source_key))

            add_case_event(
                "started",
                f"Восстановленное действие: пользователь начал заявку #{case.id}",
            )
            if case.order_photo_path:
                add_case_event(
                    "order_uploaded",
                    f"Восстановленное действие: отправлен судебный приказ по заявке #{case.id}",
                    [{"path": case.order_photo_path, "caption": "Фото судебного приказа"}]
                    if Path(case.order_photo_path).is_file() else None,
                )
            if case.envelope_photo_path:
                add_case_event(
                    "envelope_uploaded",
                    f"Восстановленное действие: отправлено фото конверта по заявке #{case.id}",
                    [{"path": case.envelope_photo_path, "caption": "Фото конверта"}]
                    if Path(case.envelope_photo_path).is_file() else None,
                )
            if case.received_date:
                add_case_event(
                    "received_date",
                    (
                        f"Восстановленное действие: указана дата получения {case.received_date.strftime('%d.%m.%Y')}\n"
                        f"Срок подачи: {case.deadline_date.strftime('%d.%m.%Y') if case.deadline_date else 'не рассчитан'}"
                    ),
                )
            document_files = [
                item
                for item in _existing_case_files(case)
                if item["caption"] not in {"Фото судебного приказа", "Фото конверта"}
            ]
            if document_files:
                add_case_event(
                    "documents_generated",
                    f"Восстановленное действие: сформированы документы по заявке #{case.id}",
                    document_files,
                )
            if case.payment_label:
                add_case_event(
                    "payment_created",
                    f"Восстановленное действие: создан платёж {case.payment_label} по заявке #{case.id}",
                )
            if case.paid_at or case.status in {"paid", "delivered"}:
                add_case_event(
                    "paid",
                    (
                        f"Восстановленное действие: заявление #{case.id} оплачено"
                        + (f" {case.paid_at.strftime('%d.%m.%Y %H:%M')}" if case.paid_at else "")
                    ),
                )
            if case.delivered_at or case.status == "delivered":
                add_case_event(
                    "delivered",
                    (
                        f"Восстановленное действие: документы по заявке #{case.id} отправлены пользователю"
                        + (f" {case.delivered_at.strftime('%d.%m.%Y %H:%M')}" if case.delivered_at else "")
                    ),
                )
            if case.reminders_sent:
                add_case_event(
                    "reminders",
                    f"Восстановленное действие: пользователю отправлено напоминаний — {case.reminders_sent}",
                )

        logs = list(
            (
                await session.execute(
                    select(CrmSyncLog)
                    .where(
                        CrmSyncLog.user_id == user_id,
                        CrmSyncLog.request_payload.is_not(None),
                        (CrmSyncLog.amo_entity_id.is_(None)) | (CrmSyncLog.success.is_(False)),
                        CrmSyncLog.event_type.not_in(
                            ["crm_reconciliation", "crm_stage_reconciliation", "history_replay", "chat_message", "user_message_received"]
                        ),
                    )
                    .order_by(CrmSyncLog.created_at, CrmSyncLog.id)
                )
            ).scalars()
        )
        seen: set[str] = set()
        for log in logs:
            raw_key = log.dedupe_key or f"{log.event_type}:{log.case_id}:{log.request_payload}"
            source_key = "crm_log:" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            if source_key in seen:
                continue
            seen.add(source_key)
            try:
                request = json.loads(log.request_payload or "{}")
            except json.JSONDecodeError:
                request = {}
            original = request.get("payload") if isinstance(request, dict) else {}
            original = original if isinstance(original, dict) else {}
            payload = {
                "source_event_key": source_key,
                "note": (
                    f"Восстановленное действие: {log.event_type}\n"
                    f"{str(original.get('note') or original.get('text') or '').strip()}"
                ).strip(),
            }
            files = [
                item
                for item in (original.get("files") or [])
                if isinstance(item, dict) and item.get("path") and Path(str(item["path"])).is_file()
            ]
            if files:
                payload["files"] = files
            jobs.append((payload, source_key))
    return jobs


async def reconcile_missing_crm_data(settings: Settings) -> dict[str, int]:
    """Repair missing user deals and retry durable failed manager-chat events."""
    result = {
        "users_checked": 0,
        "deals_repaired": 0,
        "history_events_replayed": 0,
        "stages_reconciled": 0,
        "messages_retried": 0,
    }
    if not settings.amocrm_enabled:
        return result

    async with SessionLocal() as session:
        user_ids = list(
            (
                await session.execute(
                    select(User.id)
                    .where(User.is_admin.is_(False), User.is_manager.is_(False))
                    .order_by(User.id)
                )
            ).scalars()
        )

    reconciliation_targets: list[tuple[int, int]] = []
    for user_id in user_ids:
        result["users_checked"] += 1
        async with SessionLocal() as session:
            user = await session.get(User, user_id)
            if user is None:
                continue
            case = await latest_case(session, user.id)
            if case is None:
                case, _ = await ensure_user_has_case(session, user)
            case_id = case.id
            missing_lead = not (case.amocrm_lead_id or case.amo_lead_id)
        if missing_lead:
            await run_crm_sync_job(
                settings,
                case_id,
                user_id,
                "crm_reconciliation",
                {"note": "Автовосстановление отсутствующей сделки CRM"},
            )
            result["deals_repaired"] += 1
        await run_crm_sync_job(
            settings,
            case_id,
            user_id,
            "crm_stage_reconciliation",
            {
                "status_name_override": _status_for_case(case),
                "force_status": True,
                "note": f"Этап восстановлен по фактическому статусу заявки #{case.id}: {case.status}",
            },
        )
        result["stages_reconciled"] += 1
        reconciliation_targets.append((user_id, case_id))

    # Files and timeline notes are slower than stage changes. Process them only
    # after every user has a deal and the correct visible pipeline stage.
    for user_id, case_id in reconciliation_targets:
        history_jobs = await _history_jobs_for_user(user_id)
        for payload, _ in history_jobs:
            await run_crm_sync_job(
                settings,
                case_id,
                user_id,
                "history_replay",
                payload,
            )
            result["history_events_replayed"] += 1

    pending_messages: list[tuple[int, int, dict, str]] = []
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ChatMessage, ChatSession, User)
                .join(ChatSession, ChatSession.id == ChatMessage.session_id)
                .join(User, User.id == ChatSession.user_id)
                .order_by(ChatMessage.id)
            )
        ).all()
        for message, chat, customer in rows:
            case = await session.get(Case, chat.case_id) if chat.case_id else await latest_case(session, customer.id)
            if case is None:
                case, _ = await ensure_user_has_case(session, customer)
            if chat.case_id is None:
                chat.case_id = case.id
            payload = build_chat_message_payload(
                platform=customer.platform,
                customer=customer,
                text=message.text,
                sender_role=message.sender_role,
                chat_session_id=chat.id,
                external_message_id=f"chat:{message.id}",
                message_datetime=message.created_at,
                attachment_path=message.attachment_path,
                attachment_name=message.attachment_name,
            )
            if payload is None:
                continue
            key = crm_event_dedupe_key(case.id, CHAT_MESSAGE_CRM_EVENT, payload)
            pending_messages.append((case.id, customer.id, payload, key))
        await session.commit()
        message_keys = [item[3] for item in pending_messages]
        successful_keys = set(
            (
                await session.execute(
                    select(CrmSyncLog.dedupe_key).where(
                        CrmSyncLog.success.is_(True),
                        CrmSyncLog.dedupe_key.in_(message_keys),
                    )
                )
            ).scalars()
        ) if message_keys else set()

    for case_id, user_id, payload, key in pending_messages:
        if key in successful_keys:
            continue
        await run_crm_sync_job(
            settings,
            case_id,
            user_id,
            CHAT_MESSAGE_CRM_EVENT,
            payload,
        )
        result["messages_retried"] += 1
    return result


async def run_crm_reconciliation(settings: Settings) -> None:
    if not settings.amocrm_enabled:
        return
    while True:
        try:
            result = await reconcile_missing_crm_data(settings)
            logger.info("CRM reconciliation complete: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("CRM reconciliation cycle failed")
        await asyncio.sleep(60)
