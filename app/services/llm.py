from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from app.config import Settings
from app.services.legal_data import missing_order_fields, normalize_order_data
from app.utils import parse_russian_date

logger = logging.getLogger(__name__)


ORDER_SCHEMA_HINT = {
    "court_name": "",
    "court_address": "",
    "judge": "",
    "debtor_full_name": "",
    "debtor_birth_date": "",
    "debtor_passport": "",
    "debtor_address": "",
    "creditor_name": "",
    "creditor_address": "",
    "creditor_inn": "",
    "creditor_ogrn": "",
    "case_number": "",
    "uid": "",
    "order_date": "",
    "debt_contract": "",
    "debt_period": "",
    "debt_amount": "",
    "state_duty": "",
    "total_amount": "",
}


ORDER_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {key: {"type": "string"} for key in ORDER_SCHEMA_HINT},
    "required": list(ORDER_SCHEMA_HINT),
}


ENVELOPE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "dates": {"type": "array", "items": {"type": "string"}},
        "latest_date": {"type": "string"},
        "confidence": {"type": "number"},
        "comment": {"type": "string"},
    },
    "required": ["dates", "latest_date", "confidence", "comment"],
}


def _image_data_url(path: str | Path) -> str:
    data = Path(path).read_bytes()
    suffix = Path(path).suffix.lower().lstrip(".") or "jpeg"
    mime = "image/png" if suffix == "png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _response_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    chunks: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                chunks.append(str(content.get("text") or ""))
    return "".join(chunks)


async def _responses_json(
    settings: Settings,
    *,
    instructions: str,
    text: str,
    image_path: str | Path,
    schema_name: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    body = {
        "model": settings.vision_model,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": text},
                    {"type": "input_image", "image_url": _image_data_url(image_path), "detail": "high"},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
        "max_output_tokens": 1800,
    }
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=settings.llm_timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(f"{settings.openai_base_url}/responses", json=body) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"OpenAI API error {response.status}: {raw[:800]}")
            data = json.loads(raw)
    answer = _response_text(data)
    if not answer.strip():
        raise RuntimeError("OpenAI API returned empty structured output")
    return json.loads(answer)


async def extract_order_data(settings: Settings, order_photo_path: str) -> dict[str, Any]:
    result = await _responses_json(
        settings,
        instructions=(
            "Ты аккуратно извлекаешь данные из фото судебного приказа РФ. "
            "Верни только факты, которые видны на изображении. Не исправляй и не придумывай паспорт, адрес, суммы, ИНН, ОГРН, даты и номера. "
            "Если поле не видно или не уверено, оставь пустую строку. Даты сохраняй так, как они написаны в документе."
        ),
        text="Извлеки реквизиты для заявления об отмене судебного приказа.",
        image_path=order_photo_path,
        schema_name="court_order_extraction",
        schema=ORDER_JSON_SCHEMA,
    )
    return normalize_order_data({key: str(result.get(key) or "").strip() for key in ORDER_SCHEMA_HINT})


async def extract_envelope_date(settings: Settings, envelope_photo_path: str) -> dict[str, Any]:
    result = await _responses_json(
        settings,
        instructions=(
            "На фото конверт или почтовое уведомление со штампами. Найди все видимые даты на штампах и выбери самую позднюю. "
            "Если дата не читается уверенно, latest_date оставь пустой, а confidence поставь ниже 0.5."
        ),
        text="Найди все даты на штампах. Верни latest_date в формате ДД.ММ.ГГГГ, если возможно.",
        image_path=envelope_photo_path,
        schema_name="envelope_stamp_dates",
        schema=ENVELOPE_JSON_SCHEMA,
    )
    latest = parse_russian_date(str(result.get("latest_date") or ""))
    result["latest_date_normalized"] = latest.isoformat() if latest else ""
    return result
