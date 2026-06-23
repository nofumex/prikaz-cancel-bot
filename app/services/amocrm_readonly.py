from __future__ import annotations

from typing import Any

import aiohttp

from app.config import Settings


class AmoCrmReadOnlyClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = (settings.amocrm_base_url or "").rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "AmoCrmReadOnlyClient":
        if not self.base_url or not self.settings.amocrm_access_token:
            raise RuntimeError("amoCRM is not configured")
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.settings.amocrm_access_token}", "Accept": "application/json"}
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session:
            await self._session.close()

    async def get(self, path: str) -> Any:
        if not self._session:
            raise RuntimeError("amoCRM client is not opened")
        async with self._session.get(self.base_url + path) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"amoCRM GET error {response.status}: {text[:500]}")
            return await response.json()

    async def pipelines_summary(self) -> str:
        data = await self.get("/api/v4/leads/pipelines")
        lines = ["<b>amoCRM справочник (только чтение)</b>", ""]
        for pipeline in data.get("_embedded", {}).get("pipelines", []):
            lines.append(f"<b>{pipeline.get('name')}</b> | ID <code>{pipeline.get('id')}</code>")
            statuses = pipeline.get("_embedded", {}).get("statuses", [])
            for status in sorted(statuses, key=lambda item: item.get("sort", 0)):
                lines.append(f"  {status.get('name')} | <code>{status.get('id')}</code>")
            lines.append("")
        return "\n".join(lines).strip()
