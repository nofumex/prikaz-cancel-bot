from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from app.adapters.max.keyboards import MaxKeyboard, to_attachments

logger = logging.getLogger(__name__)


class MaxBotClient:
    def __init__(self, token: str, base_url: str = "https://botapi.max.ru") -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "MaxBotClient":
        self._session = aiohttp.ClientSession(headers={"Authorization": self.token})
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("MAX client session is not started")
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with self.session.request(method, self.base_url + path, **kwargs) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"MAX API error {response.status}: {payload}")
            return payload

    async def get_updates(self, marker: int | None = None, timeout: int = 30, limit: int = 100) -> dict[str, Any]:
        params = {"timeout": timeout, "limit": limit, "types": "message_created,message_callback,bot_started"}
        if marker is not None:
            params["marker"] = marker
        return await self.request("GET", "/updates", params=params, timeout=timeout + 10)

    async def send_message(self, chat_id: str | int, text: str, keyboard: MaxKeyboard | None = None, attachments: list[dict] | None = None) -> None:
        body: dict[str, Any] = {"text": text, "format": "html"}
        all_attachments = (attachments or []) + (to_attachments(keyboard) or [])
        if all_attachments:
            body["attachments"] = all_attachments
        await self.request("POST", "/messages", params={"chat_id": chat_id}, json=body)

    async def answer_callback(self, callback_id: str | None) -> None:
        if not callback_id:
            return
        try:
            await self.request("POST", f"/messages/callback/{callback_id}", json={"callback_id": callback_id})
        except Exception:
            logger.debug("MAX callback answer failed", exc_info=True)

    async def safe_send_message(self, *args, **kwargs) -> bool:
        try:
            await self.send_message(*args, **kwargs)
            return True
        except Exception:
            logger.exception("MAX send failed")
            await asyncio.sleep(0.2)
            return False
