from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserState


async def get_user_state(session: AsyncSession, platform: str, platform_user_id: str) -> dict[str, Any]:
    result = await session.execute(
        select(UserState).where(
            UserState.platform == platform,
            UserState.platform_user_id == platform_user_id,
        )
    )
    state_row = result.scalar_one_or_none()
    if state_row and state_row.data_json:
        try:
            return json.loads(state_row.data_json)
        except json.JSONDecodeError:
            return {}
    return {}


async def set_user_state(session: AsyncSession, platform: str, platform_user_id: str, state: str | None, data: dict[str, Any]) -> None:
    result = await session.execute(
        select(UserState).where(
            UserState.platform == platform,
            UserState.platform_user_id == platform_user_id,
        )
    )
    state_row = result.scalar_one_or_none()
    if not state_row:
        state_row = UserState(platform=platform, platform_user_id=platform_user_id)
        session.add(state_row)
    state_row.state = state
    state_row.data_json = json.dumps(data, ensure_ascii=False)
    await session.commit()


async def clear_user_state(session: AsyncSession, platform: str, platform_user_id: str) -> None:
    result = await session.execute(
        select(UserState).where(
            UserState.platform == platform,
            UserState.platform_user_id == platform_user_id,
        )
    )
    state_row = result.scalar_one_or_none()
    if state_row:
        state_row.state = None
        state_row.data_json = None
        await session.commit()


async def update_user_state_data(session: AsyncSession, platform: str, platform_user_id: str, updates: dict[str, Any]) -> None:
    data = await get_user_state(session, platform, platform_user_id)
    data.update(updates)
    await set_user_state(session, platform, platform_user_id, data.get("_state"), data)