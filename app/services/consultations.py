from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Case, ConsultationBroadcast, ConsultationBroadcastDelivery, User
from app.services.cases import ensure_user_has_case
from app.services.crm_background import schedule_crm_sync
from app.utils import full_name, h

logger = logging.getLogger(__name__)

CONSULTATION_OFFER_TEXT = (
    "<b>Мы можем помочь вам с долгами по судебному приказу!</b>\n\n"
    "Нажмите кнопку ниже и мы проконсультируем вас!"
)
CONSULTATION_ACCEPTED_TEXT = "✅ Ваша заявка принята. Скоро с вами свяжутся."
CONSULTATION_PHONE_TEXT = (
    "<b>Пожалуйста, поделитесь номером телефона — так мы сможем связаться с вами для консультации.</b>\n\n"
    'Для этого нажмите кнопку "Поделиться контактом" снизу'
)
STAFF_TELEGRAM_IDS = {6143011344, 7727079839}
TEST_BROADCAST_USER_IDS = {"8608404966", "7727079839", "185607445"}
TEST_NOTIFICATION_TELEGRAM_IDS = {8608404966, 7727079839}


def consultation_notification_ids(settings, *, test_mode: bool) -> set[int]:
    if test_mode:
        return TEST_NOTIFICATION_TELEGRAM_IDS
    return set(settings.admin_ids) | STAFF_TELEGRAM_IDS


async def consultation_case(session: AsyncSession, user: User, chat_id: str | None = None) -> Case:
    case, _ = await ensure_user_has_case(session, user, chat_id=chat_id)
    return case


def consultation_notification(user: User) -> str:
    username = "—" if user.platform == "max" else (f"@{h((user.telegram_username or user.username).lstrip('@'))}" if (user.telegram_username or user.username) else "—")
    return (
        "<b>Новая заявка на консультацию</b>\n\n"
        f"Платформа: {h(user.platform)}\n"
        f"ID: <code>{h(user.platform_user_id)}</code>\n"
        f"Username: {username}\n"
        f"Имя: {h(full_name(user) or '—')}\n"
        f"Телефон: <code>{h(user.phone or '—')}</code>"
    )


async def submit_consultation(
    session: AsyncSession,
    settings,
    user: User,
    *,
    chat_id: str | None,
    notify: Callable[[str], Awaitable[None]],
) -> Case:
    """Create/reuse the user's CRM lead and move it to Consultation."""
    case = await consultation_case(session, user, chat_id)
    schedule_crm_sync(
        settings,
        case.id,
        user.id,
        "consultation_requested",
        {"note": "Пользователь оставил заявку на консультацию", "force_status": True},
    )
    try:
        await notify(consultation_notification(user))
    except Exception:
        logger.exception("Could not notify staff about consultation user_id=%s", user.id)
    return case


async def consultation_recipients(session: AsyncSession, *, test_ids: set[str] | None = None) -> list[User]:
    stmt = select(User)
    if test_ids is None:
        stmt = stmt.where(User.is_admin.is_(False), User.is_manager.is_(False))
    else:
        stmt = stmt.where(User.platform_user_id.in_(test_ids))
    return list((await session.execute(stmt.order_by(User.id))).scalars())


async def start_consultation_broadcast(session: AsyncSession, *, test_mode: bool) -> ConsultationBroadcast:
    broadcast = ConsultationBroadcast(is_test=test_mode)
    session.add(broadcast)
    await session.commit()
    await session.refresh(broadcast)
    return broadcast


async def failed_consultation_recipients(session: AsyncSession, *, test_mode: bool) -> tuple[ConsultationBroadcast | None, list[User]]:
    broadcast = await session.scalar(
        select(ConsultationBroadcast)
        .where(ConsultationBroadcast.is_test.is_(test_mode))
        .order_by(ConsultationBroadcast.id.desc())
        .limit(1)
    )
    if broadcast is None:
        return None, []
    users = list((await session.execute(
        select(User)
        .join(ConsultationBroadcastDelivery, ConsultationBroadcastDelivery.user_id == User.id)
        .where(ConsultationBroadcastDelivery.broadcast_id == broadcast.id, ConsultationBroadcastDelivery.status == "failed")
        .order_by(User.id)
    )).scalars())
    return broadcast, users


async def save_consultation_delivery(session: AsyncSession, broadcast: ConsultationBroadcast, user: User, *, sent: bool, error: str | None = None) -> None:
    delivery = await session.scalar(
        select(ConsultationBroadcastDelivery).where(
            ConsultationBroadcastDelivery.broadcast_id == broadcast.id,
            ConsultationBroadcastDelivery.user_id == user.id,
        )
    )
    if delivery is None:
        delivery = ConsultationBroadcastDelivery(broadcast_id=broadcast.id, user_id=user.id)
        session.add(delivery)
    delivery.status = "sent" if sent else "failed"
    delivery.error_message = error[:2000] if error else None
    delivery.attempts += 1
    delivery.last_attempt_at = datetime.utcnow()
    await session.commit()
