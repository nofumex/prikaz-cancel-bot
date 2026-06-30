from __future__ import annotations

import json
from typing import Any

import aiohttp

from app.config import Settings
from app.models import Case, Payment


class YooKassaError(RuntimeError):
    pass


_SECRET_KEYS = {"authorization", "secret", "secret_key", "password", "api_key", "token"}


def sanitize_yookassa_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).lower() in _SECRET_KEYS:
                cleaned[key] = "***"
            else:
                cleaned[key] = sanitize_yookassa_payload(value)
        return cleaned
    if isinstance(payload, list):
        return [sanitize_yookassa_payload(item) for item in payload]
    return payload


def raw_json(payload: Any) -> str:
    return json.dumps(sanitize_yookassa_payload(payload or {}), ensure_ascii=False)


class YooKassaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = "https://api.yookassa.ru/v3"
        self.timeout = aiohttp.ClientTimeout(total=30)

    def is_configured(self) -> bool:
        return bool(self.settings.yookassa_enabled and self.settings.yookassa_shop_id and self.settings.yookassa_secret_key)

    def _auth(self) -> aiohttp.BasicAuth:
        if not self.settings.yookassa_shop_id or not self.settings.yookassa_secret_key:
            raise YooKassaError("YooKassa credentials are not configured")
        return aiohttp.BasicAuth(self.settings.yookassa_shop_id, self.settings.yookassa_secret_key)

    async def request(self, method: str, path: str, *, json_body: dict | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.is_configured():
            raise YooKassaError("YooKassa is disabled or not configured")
        url = path if path.startswith("http") else self.base_url + path
        async with aiohttp.ClientSession(timeout=self.timeout, auth=self._auth()) as session:
            async with session.request(method, url, json=json_body, headers=headers) as response:
                raw = await response.text()
                try:
                    payload = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    payload = {"raw": raw}
                if response.status >= 400:
                    raise YooKassaError(f"YooKassa API error {response.status}: {raw[:600]}")
                return payload

    async def create_payment(self, case: Case, payment: Payment) -> dict[str, Any]:
        return_url = self.settings.yookassa_return_url or (self.settings.payment_public_base_url and f"{self.settings.payment_public_base_url}/payments/success")
        if not return_url:
            raise YooKassaError("YOOKASSA_RETURN_URL or PAYMENT_PUBLIC_BASE_URL is required")
        amount = f"{payment.amount:.2f}"
        body = {
            "amount": {"value": amount, "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": f"Заявление об отмене судебного приказа #{case.id}",
            "metadata": {
                "case_id": str(case.id),
                "payment_label": payment.label,
                "platform": case.platform,
                "platform_user_id": case.platform_user_id or "",
            },
        }
        if self.settings.yookassa_test_mode:
            body["test"] = True
        return await self.request("POST", "/payments", json_body=body, headers={"Idempotence-Key": payment.label})

    async def get_payment(self, external_payment_id: str) -> dict[str, Any]:
        if not external_payment_id:
            raise YooKassaError("external_payment_id is empty")
        return await self.request("GET", f"/payments/{external_payment_id}")
