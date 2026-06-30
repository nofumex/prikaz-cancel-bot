from __future__ import annotations

import json
import logging
from typing import Any
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserState

logger = logging.getLogger(__name__)

_MAX_STATE_TTL_SECONDS = 3600


class MaxStateManager:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, dict[str, Any], float]] = {}

    def _cache_key(self, platform: str, platform_user_id: str) -> str:
        return f"{platform}:{platform_user_id}"

    async def get_state(self, session: AsyncSession, platform: str, platform_user_id: str) -> str | None:
        key = self._cache_key(platform, platform_user_id)
        cached = self._cache.get(key)
        if cached:
            state, data, ts = cached
            if time.monotonic() - ts < _MAX_STATE_TTL_SECONDS:
                return state
            del self._cache[key]
        result = await session.execute(
            select(UserState).where(UserState.platform == platform, UserState.platform_user_id == str(platform_user_id))
        )
        row = result.scalar_one_or_none()
        if row:
            self._cache[key] = (row.state or "", json.loads(row.data_json or "{}"), time.monotonic())
            return row.state
        return None

    async def get_data(self, session: AsyncSession, platform: str, platform_user_id: str) -> dict[str, Any]:
        key = self._cache_key(platform, platform_user_id)
        cached = self._cache.get(key)
        if cached:
            return cached[1]
        await self.get_state(session, platform, platform_user_id)
        return self._cache.get(key, ("", {}, 0))[1]

    async def set_state(self, session: AsyncSession, platform: str, platform_user_id: str, state: str | None, data: dict[str, Any] | None = None) -> None:
        key = self._cache_key(platform, platform_user_id)
        if data is None:
            data = {}
        self._cache[key] = (state or "", data, time.monotonic())
        result = await session.execute(
            select(UserState).where(UserState.platform == platform, UserState.platform_user_id == str(platform_user_id))
        )
        row = result.scalar_one_or_none()
        if row:
            row.state = state
            row.data_json = json.dumps(data, ensure_ascii=False)
        else:
            row = UserState(platform=platform, platform_user_id=str(platform_user_id), state=state, data_json=json.dumps(data, ensure_ascii=False))
            session.add(row)
        await session.commit()

    async def clear(self, session: AsyncSession, platform: str, platform_user_id: str) -> None:
        key = self._cache_key(platform, platform_user_id)
        self._cache.pop(key, None)
        await self.set_state(session, platform, platform_user_id, None, {})


max_state_manager = MaxStateManager()
