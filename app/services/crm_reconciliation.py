from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal
from app.models import Case, ChatMessage, ChatSession, CrmSyncLog, User
from app.services.amocrm import crm_event_dedupe_key
from app.services.cases import ensure_user_has_case, latest_case
from app.services.chat_crm_sync import CHAT_MESSAGE_CRM_EVENT, build_chat_message_payload
from app.services.crm_background import run_crm_sync_job

logger = logging.getLogger(__name__)


async def reconcile_missing_crm_data(settings: Settings) -> dict[str, int]:
    """Repair missing user deals and retry durable failed manager-chat events."""
    result = {"users_checked": 0, "deals_repaired": 0, "messages_retried": 0}
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
