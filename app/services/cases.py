from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import CaseStatus
from app.models import Case, User
from app.services.legal_data import legal_deadline_from_received


async def create_case(session: AsyncSession, user: User) -> Case:
    case = Case(user_id=user.id, platform=user.platform, status=CaseStatus.WAITING_ORDER_PHOTO.value)
    session.add(case)
    await session.commit()
    await session.refresh(case)
    return case


async def latest_case(session: AsyncSession, user_id: int) -> Case | None:
    result = await session.execute(select(Case).where(Case.user_id == user_id).order_by(Case.created_at.desc()).limit(1))
    return result.scalar_one_or_none()


async def latest_open_case(session: AsyncSession, user_id: int) -> Case | None:
    result = await session.execute(
        select(Case)
        .where(
            Case.user_id == user_id,
            Case.status.not_in([CaseStatus.DELIVERED.value, CaseStatus.CANCELED.value]),
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
    cutoff = datetime.utcnow() - timedelta(hours=23)
    result = await session.execute(
        select(Case)
        .where(
            Case.status == CaseStatus.PAYMENT_PENDING.value,
            Case.reminders_sent < 3,
            (Case.last_reminder_at.is_(None)) | (Case.last_reminder_at <= cutoff),
        )
        .order_by(Case.created_at.asc())
        .limit(50)
    )
    return list(result.scalars().all())
