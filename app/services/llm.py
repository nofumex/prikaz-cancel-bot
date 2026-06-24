from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import OpenAIUsage
from app.services.legal_data import missing_order_fields, normalize_order_data
from app.utils import parse_russian_date

logger = logging.getLogger(__name__)


DEFAULT_MODEL_PRICING = {
    "gpt-5.4-mini": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    }
}


@dataclass
class LLMResult:
    data: dict[str, Any]
    usage: dict[str, Any]
    model: str
    request_id: str | None
    latency_ms: int


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


def _parse_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    cached_input_tokens = int(input_details.get("cached_tokens") or 0)
    reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)
    image_tokens = input_details.get("image_tokens")
    if image_tokens is not None:
        try:
            image_tokens = int(image_tokens)
        except (TypeError, ValueError):
            image_tokens = None
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "image_tokens": image_tokens,
        "total_tokens": total_tokens,
    }


def _pricing_for_model(settings: Settings, model: str | None) -> dict[str, float]:
    prices = DEFAULT_MODEL_PRICING.get(model or "", {}).copy()
    if settings.openai_model_pricing_json:
        try:
            data = json.loads(settings.openai_model_pricing_json)
            if isinstance(data, dict):
                model_prices = data.get(model or "", {})
                if isinstance(model_prices, dict):
                    prices.update({k: float(v) for k, v in model_prices.items() if k in {"input", "cached_input", "output"}})
        except Exception:
            logger.warning("Failed to parse OPENAI_MODEL_PRICING_JSON", exc_info=True)
    prices.setdefault("input", settings.openai_input_price_per_1m)
    prices.setdefault("cached_input", settings.openai_cached_input_price_per_1m)
    prices.setdefault("output", settings.openai_output_price_per_1m)
    return prices


def _money_cost(tokens: int, price_per_1m: float) -> float:
    return float(Decimal(tokens) * Decimal(str(price_per_1m)) / Decimal("1000000"))


async def record_openai_usage(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    operation: str,
    model: str | None,
    result: LLMResult | None = None,
    success: bool,
    error_message: str | None = None,
) -> None:
    if session is None:
        return
    usage = result.usage if result else {}
    parsed = _parse_usage({"usage": usage})
    prices = _pricing_for_model(settings, model)
    input_cost = _money_cost(parsed["input_tokens"], prices["input"])
    cached_cost = _money_cost(parsed["cached_input_tokens"], prices["cached_input"])
    output_cost = _money_cost(parsed["output_tokens"], prices["output"])
    usage_row = OpenAIUsage(
        case_id=case_id,
        user_id=user_id,
        provider="openai",
        endpoint="responses",
        operation=operation,
        model=model,
        input_tokens=parsed["input_tokens"],
        cached_input_tokens=parsed["cached_input_tokens"],
        output_tokens=parsed["output_tokens"],
        reasoning_tokens=parsed["reasoning_tokens"],
        image_tokens=parsed["image_tokens"],
        total_tokens=parsed["total_tokens"],
        input_cost_usd=input_cost,
        cached_input_cost_usd=cached_cost,
        output_cost_usd=output_cost,
        total_cost_usd=input_cost + cached_cost + output_cost,
        request_id=(result.request_id if result and result.request_id else None),
        raw_usage_json=json.dumps(usage, ensure_ascii=False),
        raw_response_model=result.model if result else model,
        success=success,
        error_message=error_message,
        latency_ms=result.latency_ms if result else None,
    )
    session.add(usage_row)
    await session.commit()


async def _responses_json(
    settings: Settings,
    *,
    instructions: str,
    text: str,
    image_path: str | Path,
    schema_name: str,
    schema: dict[str, Any],
) -> LLMResult:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    start = time.perf_counter()
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
    return LLMResult(
        data=json.loads(answer),
        usage=_parse_usage(data),
        model=str(data.get("model") or settings.vision_model),
        request_id=str(data.get("id") or ""),
        latency_ms=int((time.perf_counter() - start) * 1000),
    )


async def extract_order_data(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str,
) -> dict[str, Any]:
    result: LLMResult | None = None
    try:
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
    except Exception as exc:
        await record_openai_usage(settings, session, case_id=case_id, user_id=user_id, operation="order_ocr", model=settings.vision_model, success=False, error_message=str(exc))
        raise
    await record_openai_usage(settings, session, case_id=case_id, user_id=user_id, operation="order_ocr", model=result.model, result=result, success=True)
    return normalize_order_data({key: str(result.data.get(key) or "").strip() for key in ORDER_SCHEMA_HINT})


async def extract_envelope_date(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    envelope_photo_path: str,
) -> dict[str, Any]:
    result: LLMResult | None = None
    try:
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
    except Exception as exc:
        await record_openai_usage(settings, session, case_id=case_id, user_id=user_id, operation="envelope_ocr", model=settings.vision_model, success=False, error_message=str(exc))
        raise
    await record_openai_usage(settings, session, case_id=case_id, user_id=user_id, operation="envelope_ocr", model=result.model, result=result, success=True)
    latest = parse_russian_date(str(result.data.get("latest_date") or ""))
    data = dict(result.data)
    data["latest_date_normalized"] = latest.isoformat() if latest else ""
    return data
