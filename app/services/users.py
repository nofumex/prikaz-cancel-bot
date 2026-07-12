from __future__ import annotations

from aiogram.types import User as TelegramUser
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Case, User


async def _clear_reminder_delivery_block(session: AsyncSession, user: User) -> None:
    if not user.id or not user.reminder_delivery_blocked_at:
        return
    user.reminder_delivery_blocked_at = None
    user.reminder_delivery_error = None
    await session.execute(
        update(Case).where(Case.user_id == user.id).values(
            reminder_delivery_blocked_at=None,
            reminder_delivery_error=None,
        )
    )


async def get_or_create_telegram_user(session: AsyncSession, tg_user: TelegramUser, settings: Settings) -> User:
    platform_user_id = str(tg_user.id)
    result = await session.execute(select(User).where(User.platform == "telegram", User.platform_user_id == platform_user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(platform="telegram", platform_user_id=platform_user_id, telegram_id=tg_user.id)
        session.add(user)
    user.username = tg_user.username
    user.telegram_username = tg_user.username
    user.first_name = tg_user.first_name
    user.last_name = tg_user.last_name
    user.is_admin = tg_user.id in settings.admin_ids
    user.is_manager = user.is_admin or tg_user.id in settings.manager_ids
    await _clear_reminder_delivery_block(session, user)
    await session.commit()
    await session.refresh(user)
    return user


async def get_or_create_platform_user(
    session: AsyncSession,
    platform: str,
    platform_user_id: str,
    settings: Settings,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    result = await session.execute(select(User).where(User.platform == platform, User.platform_user_id == str(platform_user_id)))
    user = result.scalar_one_or_none()
    if not user:
        user = User(platform=platform, platform_user_id=str(platform_user_id))
        session.add(user)
    user.username = username
    user.telegram_username = username
    user.first_name = first_name
    user.last_name = last_name
    if platform == "max":
        try:
            max_id = int(platform_user_id)
        except (TypeError, ValueError):
            max_id = None
        user.is_admin = bool(max_id is not None and max_id in settings.max_admin_ids)
        user.is_manager = user.is_admin
    await _clear_reminder_delivery_block(session, user)
    await session.commit()
    await session.refresh(user)
    return user


async def get_staff(session: AsyncSession, platform: str = "telegram") -> list[User]:
    result = await session.execute(
        select(User)
        .where(User.platform == platform, (User.is_admin.is_(True)) | (User.is_manager.is_(True)))
        .order_by(User.is_admin.desc(), User.created_at.asc())
    )
    return list(result.scalars().all())
