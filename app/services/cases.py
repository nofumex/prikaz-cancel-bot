from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import CaseStatus, PaymentStatus
from app.models import Case, Payment, User
from app.services.legal_data import legal_deadline_from_received


ACTIVE_FINAL_STATUSES = {CaseStatus.DELIVERED.value, CaseStatus.CANCELED.value, CaseStatus.SUPERSEDED.value}


async def create_case(session: AsyncSession, user: User, *, chat_id: str | None = None) -> Case:
    case = Case(
        user_id=user.id,
        platform=user.platform,
        platform_user_id=user.platform_user_id,
        platform_chat_id=chat_id or user.platform_user_id,
        status=CaseStatus.WAITING_ORDER_PHOTO.value,
    )
    session.add(case)
    await session.commit()
    await session.refresh(case)
    return case


async def supersede_open_cases(session: AsyncSession, user: User) -> None:
    result = await session.execute(
        select(Case).where(Case.user_id == user.id, Case.status.not_in(list(ACTIVE_FINAL_STATUSES)))
    )
    for case in result.scalars().all():
        case.status = CaseStatus.SUPERSEDED.value
    await session.flush()


async def get_or_create_active_case(
    session: AsyncSession,
    user: User,
    *,
    chat_id: str | None = None,
    force_new: bool = False,
) -> Case:
    case = await latest_open_case(session, user.id)
    if case and case.status == CaseStatus.WAITING_ORDER_PHOTO.value and not case.order_photo_path:
        if chat_id and case.platform_chat_id != chat_id:
            case.platform_chat_id = chat_id
            await session.commit()
        return case
    if case and not force_new:
        return case
    if case:
        await supersede_open_cases(session, user)
        await session.commit()
    return await create_case(session, user, chat_id=chat_id)


async def latest_case(session: AsyncSession, user_id: int) -> Case | None:
    result = await session.execute(select(Case).where(Case.user_id == user_id).order_by(Case.created_at.desc()).limit(1))
    return result.scalar_one_or_none()


async def latest_open_case(session: AsyncSession, user_id: int) -> Case | None:
    result = await session.execute(
        select(Case)
        .where(
            Case.user_id == user_id,
            Case.status.not_in(list(ACTIVE_FINAL_STATUSES)),
        )
        .order_by(Case.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_cases(session: AsyncSession, limit: int = 10, status: str | None = None) -> list[Case]:
    stmt = select(Case).order_by(Case.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Case.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def save_photo_path(session: AsyncSession, case: Case, kind: str, path: Path) -> None:
    if kind == "order":
        case.order_photo_path = str(path)
        case.status = CaseStatus.WAITING_ENVELOPE.value
    elif kind == "envelope":
        case.envelope_photo_path = str(path)
        case.status = CaseStatus.PROCESSING.value
    await session.commit()


async def set_received_date(session: AsyncSession, case: Case, received_date) -> None:
    case.received_date = received_date
    case.deadline_date = legal_deadline_from_received(received_date)
    case.status = CaseStatus.PROCESSING.value
    await session.commit()


def new_payment_label(case_id: int) -> str:
    return f"prikaz-{case_id}-{secrets.token_hex(4)}"


async def due_unpaid_cases(session: AsyncSession) -> list[Case]:
    now = datetime.utcnow()
    reminder_gap_cutoff = now - timedelta(hours=23)
    due_24h = now - timedelta(hours=24)
    due_48h = now - timedelta(hours=48)
    due_72h = now - timedelta(hours=72)
    reminder_gap_ok = or_(Case.last_reminder_at.is_(None), Case.last_reminder_at <= reminder_gap_cutoff)
    latest_active_unpaid_ids = (
        select(func.max(Case.id))
        .join(Payment, Payment.case_id == Case.id)
        .where(
            Case.status == CaseStatus.PAYMENT_PENDING.value,
            Payment.status == PaymentStatus.PENDING.value,
        )
        .group_by(Case.platform, Case.platform_user_id)
    )
    result = await session.execute(
        select(Case)
        .join(Payment, Payment.case_id == Case.id)
        .where(
            Case.id.in_(latest_active_unpaid_ids),
            Case.status == CaseStatus.PAYMENT_PENDING.value,
            Payment.status == PaymentStatus.PENDING.value,
            Case.reminders_sent < 3,
            or_(
                and_(Case.reminders_sent == 0, Payment.created_at <= due_24h),
                and_(Case.reminders_sent == 1, Payment.created_at <= due_48h, reminder_gap_ok),
                and_(Case.reminders_sent == 2, Payment.created_at <= due_72h, reminder_gap_ok),
            ),
        )
        .order_by(Case.created_at.asc())
        .limit(50)
    )
    return list(result.scalars().unique().all())
