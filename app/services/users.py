from __future__ import annotations

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import User


async def get_or_create_telegram_user(session: AsyncSession, tg_user: TelegramUser, settings: Settings) -> User:
    platform_user_id = str(tg_user.id)
    result = await session.execute(select(User).where(User.platform == "telegram", User.platform_user_id == platform_user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(platform="telegram", platform_user_id=platform_user_id, telegram_id=tg_user.id)
        session.add(user)
    user.username = tg_user.username
    user.first_name = tg_user.first_name
    user.last_name = tg_user.last_name
    user.is_admin = tg_user.id in settings.admin_ids
    user.is_manager = user.is_admin or tg_user.id in settings.manager_ids
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
    user.first_name = first_name
    user.last_name = last_name
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
