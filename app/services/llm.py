from __future__ import annotations

import asyncio
import base64
import hashlib
from difflib import SequenceMatcher
import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import OpenAIUsage
from app.services.amount_recovery import recover_amounts_from_mismatch
from app.services.classic_ocr import classic_amount_facts, classic_court_name, extract_classic_ocr_text
from app.services.image_preprocessing import build_amount_ocr_variants, build_order_ocr_variants
from app.services.legal_data import clean_money_text, format_money_rub_kop, missing_order_fields, money_from_source_fragment, normalize_debtor_name_fields, normalize_order_data, validate_amounts
from app.services.order_integrity import (
    CRITICAL_ORDER_FIELDS,
    ORDER_EVIDENCE_SCHEMA,
    build_order_evidence_schema,
    conflicting_fields,
    evidence_payload_fields,
    merge_verified_order_data,
)
from app.services.tesseract_ai import (
    extract_fast_tesseract_text,
    extract_order_data_from_tesseract_ai,
)
from app.utils import ensure_dir, parse_russian_date

logger = logging.getLogger(__name__)

ORDER_EXTRACTION_CACHE_VERSION = "v7-tesseract-text"
_order_cache_locks: dict[str, asyncio.Lock] = {}


def _order_cache_path(order_photo_path: str) -> Path:
    digest = hashlib.sha256(Path(order_photo_path).read_bytes()).hexdigest()
    return ensure_dir("storage/ocr_cache") / f"order_{ORDER_EXTRACTION_CACHE_VERSION}_{digest}.json"


def _read_order_cache(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        return normalize_order_data(data) if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write_order_cache(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": ORDER_EXTRACTION_CACHE_VERSION, "data": data}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


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


@dataclass(slots=True)
class PrimaryOrderExtraction:
    data: dict[str, Any]
    document_kind: str
    is_court_order: bool
    confidence: float
    reason: str = ""


ORDER_SCHEMA_HINT = {
    "court_name": "",
    "court_address": "",
    "judge": "",
    "debtor_name_raw": "",
    "debtor_name_context": "",
    "debtor_full_name": "",
    "debtor_name_source_fragment": "",
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


ORDER_JSON_SCHEMA_PROPERTIES = {
    **{key: {"type": "string"} for key in ORDER_SCHEMA_HINT},
    "debtor_full_name_confidence": {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
    },
    "document_kind": {
        "type": "string",
        "enum": ["court_order", "other", "unclear"],
    },
    "is_court_order": {"type": "boolean"},
    "document_type_confidence": {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
    },
    "document_type_reason": {"type": "string"},
}

ORDER_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": ORDER_JSON_SCHEMA_PROPERTIES,
    "required": list(ORDER_JSON_SCHEMA_PROPERTIES.keys()),
}


AMOUNTS_JSON_SCHEMA_PROPERTIES = {
    "debt_amount": {"type": "string"},
    "debt_amount_fragment": {"type": "string"},
    "state_duty": {"type": "string"},
    "state_duty_fragment": {"type": "string"},
    "total_amount": {"type": "string"},
    "total_amount_fragment": {"type": "string"},
    "comment": {"type": "string"},
}

AMOUNTS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": AMOUNTS_JSON_SCHEMA_PROPERTIES,
    "required": list(AMOUNTS_JSON_SCHEMA_PROPERTIES.keys()),
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

DOCUMENT_REVIEW_CLEAN_FIELDS = {
    "debtor_full_name": "",
    "debtor_address": "",
    "court_name": "",
    "court_address": "",
    "creditor_name": "",
    "creditor_address": "",
    "case_number": "",
    "uid": "",
    "debt_contract": "",
    "debt_period": "",
    "order_date": "",
    "debt_amount": "",
    "state_duty": "",
    "total_amount": "",
}

DOCUMENT_REVIEW_ISSUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "code": {"type": "string"},
        "field": {"type": "string"},
        "severity": {"type": "string", "enum": ["ok", "warning", "blocker"]},
        "message": {"type": "string"},
        "suggested_fix": {"type": "string"},
        "source_fragment": {"type": "string"},
        "source_verified": {"type": "boolean"},
    },
    "required": [
        "code", "field", "severity", "message", "suggested_fix",
        "source_fragment", "source_verified",
    ],
}

DOCUMENT_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "severity": {"type": "string", "enum": ["ok", "warning", "blocker"]},
        "needs_regeneration": {"type": "boolean"},
        "issues": {"type": "array", "items": DOCUMENT_REVIEW_ISSUE_SCHEMA},
        "clean_fields": {
            "type": "object",
            "additionalProperties": False,
            "properties": {key: {"type": "string"} for key in DOCUMENT_REVIEW_CLEAN_FIELDS},
            "required": list(DOCUMENT_REVIEW_CLEAN_FIELDS.keys()),
        },
    },
    "required": ["ok", "severity", "needs_regeneration", "issues", "clean_fields"],
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
    non_cached_input_tokens = max(0, input_tokens - cached_input_tokens)
    reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)
    image_tokens = input_details.get("image_tokens")
    if image_tokens is not None:
        try:
            image_tokens = int(image_tokens)
        except (TypeError, ValueError):
            image_tokens = None
    return {
        "input_tokens": input_tokens,
        "non_cached_input_tokens": non_cached_input_tokens,
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
    metadata: dict[str, Any] | None = None,
) -> None:
    if session is None:
        return
    usage = result.usage if result else {}
    parsed = _parse_usage({"usage": usage})
    prices = _pricing_for_model(settings, model)
    non_cached = parsed.get("non_cached_input_tokens", parsed["input_tokens"])
    input_cost = _money_cost(non_cached, prices["input"])
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
        raw_usage_json=json.dumps({"usage": usage, "metadata": metadata or {}}, ensure_ascii=False),
        raw_response_model=result.model if result else model,
        success=success,
        error_message=error_message,
        latency_ms=result.latency_ms if result else None,
    )
    session.add(usage_row)
    await session.commit()


def _save_debug_attempt(
    case_id: int | None,
    operation: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    model: str | None = None,
) -> None:
    if case_id is None:
        return
    debug_dir = ensure_dir(Path("storage/debug") / f"case_{case_id}")
    attempts_dir = ensure_dir(debug_dir / "attempts")
    safe_operation = re.sub(r"[^a-zA-Z0-9_-]+", "_", operation).strip("_") or "attempt"
    safe_request = re.sub(r"[^a-zA-Z0-9_-]+", "_", request_id or "no_request_id")[-80:]
    attempt_path = attempts_dir / f"{time.time_ns()}_{safe_operation}_{safe_request}.json"
    body = {
        "operation": operation,
        "request_id": request_id,
        "model": model,
        "payload": payload,
    }
    attempt_path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_order_ocr_raw(
    case_id: int | None,
    raw_data: dict[str, Any],
    *,
    request_id: str | None = None,
    model: str | None = None,
) -> None:
    if case_id is None:
        return
    debug_dir = ensure_dir(Path("storage/debug") / f"case_{case_id}")
    # Keep the compatibility snapshot for admin tooling, but never use it as
    # the audit history: every attempt is also written immutably below.
    (debug_dir / "order_ocr_raw.json").write_text(
        json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _save_debug_attempt(
        case_id,
        "order_ocr",
        raw_data,
        request_id=request_id,
        model=model,
    )


async def _responses_json(
    settings: Settings,
    *,
    instructions: str,
    text: str,
    image_path: str | Path | None = None,
    image_paths: list[str | Path] | None = None,
    schema_name: str,
    schema: dict[str, Any],
    model: str | None = None,
    max_output_tokens: int = 1800,
) -> LLMResult:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    paths: list[Path] = []
    if image_paths:
        paths = [Path(p) for p in image_paths]
    elif image_path:
        paths = [Path(image_path)]

    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    for path in paths:
        content.append({"type": "input_image", "image_url": _image_data_url(path), "detail": "high"})

    start = time.perf_counter()
    body = {
        "model": model or settings.vision_model,
        "instructions": instructions,
        "input": [{"role": "user", "content": content}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
        "max_output_tokens": max_output_tokens,
    }
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=settings.llm_timeout_seconds)
    last_error: Exception | None = None
    for attempt in range(2):
        try:
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
                model=str(data.get("model") or model or settings.vision_model),
                request_id=str(data.get("id") or ""),
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.15)
    raise RuntimeError(f"OpenAI structured request failed after retry: {last_error}")


async def _extract_order_data_primary(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str,
) -> PrimaryOrderExtraction:
    result: LLMResult | None = None
    classification_instructions = (
        "First classify the image. A Russian court order is a document issued by a court or magistrate "
        "that identifies the parties and orders recovery. An FSSP, Gosuslugi, or bank card showing an "
        "enforcement proceeding, a payment button, or a list of debts is not a court order. For those "
        "images return document_kind=other, is_court_order=false, and do not invent order fields. "
        "Set document_type_confidence to your confidence in the classification itself: high for clearly court_order "
        "or clearly other, and low only when document_kind=unclear. "
    )
    try:
        result = await _responses_json(
            settings,
            instructions=classification_instructions + (
                "Ты аккуратно извлекаешь данные из фото судебного приказа РФ. "
                "Верни только факты, которые видны на изображении. Не придумывай паспорт, адрес, суммы, ИНН, ОГРН, даты и номера. "
                "Если поле не видно или не уверено, оставь пустую строку. Даты сохраняй так, как они написаны в документе. "
                "Найди ФИО должника в судебном приказе. Если ФИО стоит в родительном/дательном/винительном падеже, "
                "восстанови именительный падеж. Например: 'Бельского Владимира Геннадьевича' → 'Бельский Владимир Геннадьевич', "
                "'Бельскому Владимиру Геннадьевичу' → 'Бельский Владимир Геннадьевич'. "
                "В поле debtor_full_name верни только именительный падеж. "
                "В debtor_name_raw сохрани ФИО как в приказе. В debtor_name_context — фразу вокруг ФИО. "
                "В debtor_name_source_fragment — точный фрагмент текста приказа."
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
    raw_data = {key: str(result.data.get(key) or "").strip() for key in ORDER_SCHEMA_HINT}
    if result.data.get("debtor_full_name_confidence") is not None:
        raw_data["debtor_full_name_confidence"] = str(result.data.get("debtor_full_name_confidence") or 0)
    document_kind = str(result.data.get("document_kind") or "unclear").strip().lower()
    is_court_order = bool(result.data.get("is_court_order"))
    try:
        confidence = max(0.0, min(1.0, float(result.data.get("document_type_confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(result.data.get("document_type_reason") or "").strip()
    _save_order_ocr_raw(
        case_id,
        {
            **raw_data,
            "document_kind": document_kind,
            "is_court_order": is_court_order,
            "document_type_confidence": confidence,
            "document_type_reason": reason,
        },
        request_id=result.request_id,
        model=result.model,
    )
    normalized = normalize_order_data(raw_data)
    normalized, name_result = normalize_debtor_name_fields(normalized)
    if (
        not getattr(settings, "tesseract_ai_enabled", False)
        and name_result
        and name_result.confidence < 0.85
        and looks_like_dative(normalized.get("debtor_full_name", ""))
    ):
        try:
            normalized = await normalize_debtor_name_llm(settings, session, case_id=case_id, user_id=user_id, data=normalized)
        except Exception:
            logger.warning("LLM name normalization failed", exc_info=True)
    return PrimaryOrderExtraction(
        data=normalized,
        document_kind=document_kind,
        is_court_order=is_court_order,
        confidence=confidence,
        reason=reason,
    )


async def _extract_order_evidence(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str,
    classic_ocr_text: str = "",
) -> tuple[dict[str, Any], LLMResult]:
    """Independently re-read legally significant fields from the image.

    This request intentionally does not receive the primary OCR result, which
    prevents the verifier from merely agreeing with an earlier transcription.
    """
    model = settings.order_verifier_model or settings.ai_review_model or settings.text_model
    verifier_images = build_order_ocr_variants(order_photo_path, case_id=case_id)
    result: LLMResult | None = None
    try:
        result = await _responses_json(
            settings,
            instructions=(
                "Ты независимый верификатор судебного приказа РФ. Прочитай изображение сам, "
                "не додумывай и не нормализуй неразборчивые символы по смыслу. Для каждого поля "
                "верни значение и точный фрагмент исходного документа, где оно напечатано. "
                "Различай сумму задолженности, госпошлину и общую сумму. Сверяй цифровую сумму "
                "с суммой прописью. В court_name не включай слова 'мировой суд' или 'мировой судья' "
                "как часть названия: возвращай судебный участок и территорию. Для debtor_address "
                "проверяй каждую букву по изображению. Если поле отсутствует, верни пустую строку. "
                "В debtor_full_name верни ФИО в именительном падеже, а source_fragment оставь точной цитатой с изображения. "
                "Не оценивай правдоподобие: значение должно следовать только из изображения."
            ),
            text=(
                "Независимо извлеки критические поля судебного приказа вместе с точными цитатами.\n\n"
                "НЕЗАВИСИМЫЙ TESSERACT OCR (может содержать ошибки, используй для посимвольной сверки с изображением):\n"
                + classic_ocr_text[:18000]
            ),
            image_paths=verifier_images,
            schema_name="court_order_independent_evidence",
            schema=ORDER_EVIDENCE_SCHEMA,
            model=model,
        )
    except Exception as exc:
        await record_openai_usage(
            settings,
            session,
            case_id=case_id,
            user_id=user_id,
            operation="order_integrity_verifier",
            model=model,
            success=False,
            error_message=str(exc),
        )
        raise
    await record_openai_usage(
        settings,
        session,
        case_id=case_id,
        user_id=user_id,
        operation="order_integrity_verifier",
        model=result.model,
        result=result,
        success=True,
    )
    _save_debug_attempt(
        case_id,
        "order_integrity_verifier",
        result.data,
        request_id=result.request_id,
        model=result.model,
    )
    return result.data, result


async def _adjudicate_order_conflicts(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str,
    primary: dict[str, Any],
    verifier_payload: dict[str, Any],
    conflicts: list[str],
    classic_ocr_text: str = "",
) -> dict[str, Any]:
    model = settings.order_adjudicator_model or settings.ai_review_model or settings.text_model
    verifier = evidence_payload_fields(verifier_payload)
    comparison = {
        field: {
            "primary": str(primary.get(field) or ""),
            "independent_verifier": verifier.get(field).value if verifier.get(field) else "",
            "verifier_fragment": verifier.get(field).source_fragment if verifier.get(field) else "",
        }
        for field in conflicts
    }
    result: LLMResult | None = None
    try:
        result = await _responses_json(
            settings,
            instructions=(
                "Ты финальный арбитр расхождений OCR судебного приказа. Решай только по исходному "
                "изображению, а не по правдоподобию кандидатов. Для конфликтных полей внимательно "
                "увеличь соответствующую область, выбери посимвольно верное значение и приведи "
                "точную цитату. Особое внимание: одна буква в ФИО/адресе, копейки, сумма долга против "
                "итога, номер дела, номер договора и корректное название судебного участка. "
                "debtor_full_name верни в именительном падеже; исходную падежную форму сохрани в source_fragment. "
                "Неконфликтные поля можно повторить или оставить пустыми."
            ),
            text=(
                "Конфликтные поля: " + ", ".join(conflicts) + "\n"
                "Кандидаты (это подсказки, не источник истины):\n"
                + json.dumps(comparison, ensure_ascii=False, indent=2)
                + "\n\nНЕЗАВИСИМЫЙ TESSERACT OCR:\n"
                + classic_ocr_text[:16000]
            ),
            image_path=order_photo_path,
            schema_name="court_order_conflict_adjudication",
            schema=build_order_evidence_schema(conflicts),
            model=model,
        )
    except Exception as exc:
        await record_openai_usage(
            settings,
            session,
            case_id=case_id,
            user_id=user_id,
            operation="order_integrity_adjudicator",
            model=model,
            success=False,
            error_message=str(exc),
            metadata={"conflicts": conflicts},
        )
        raise
    await record_openai_usage(
        settings,
        session,
        case_id=case_id,
        user_id=user_id,
        operation="order_integrity_adjudicator",
        model=result.model,
        result=result,
        success=True,
        metadata={"conflicts": conflicts},
    )
    _save_debug_attempt(
        case_id,
        "order_integrity_adjudicator",
        result.data,
        request_id=result.request_id,
        model=result.model,
    )
    return result.data


async def extract_order_data(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str,
) -> dict[str, Any]:
    cache_path = _order_cache_path(order_photo_path)
    cached = _read_order_cache(cache_path)
    if cached is not None:
        return cached
    lock = _order_cache_locks.setdefault(cache_path.name, asyncio.Lock())
    try:
        async with lock:
            cached = _read_order_cache(cache_path)
            if cached is not None:
                return cached
            data = await _extract_order_data_uncached(
                settings, session, case_id=case_id, user_id=user_id, order_photo_path=order_photo_path
            )
            normalized = normalize_order_data(data)
            _write_order_cache(cache_path, normalized)
            return normalized
    finally:
        if _order_cache_locks.get(cache_path.name) is lock and not lock.locked():
            _order_cache_locks.pop(cache_path.name, None)


async def _extract_order_data_tesseract_fast(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str,
) -> dict[str, Any]:
    """One full-page Tesseract TSV call followed by one text-only LLM call."""
    ocr = await extract_fast_tesseract_text(order_photo_path, case_id=case_id)
    extraction = await extract_order_data_from_tesseract_ai(
        settings,
        session,
        case_id=case_id,
        user_id=user_id,
        order_photo_path=order_photo_path,
        primary_candidates={},
        ocr=ocr,
    )
    _save_debug_attempt(
        case_id,
        "tesseract_text_extraction",
        {
            "safe_to_generate": extraction.safe_to_generate,
            "issues": extraction.issues,
            "debtor_name_occurrences": extraction.debtor_name_occurrences,
            "tesseract_ms": extraction.ocr.latency_ms,
            "text_llm_ms": extraction.llm_result.latency_ms,
            "final_data": extraction.data,
        },
        request_id=extraction.llm_result.request_id,
        model=extraction.llm_result.model,
    )
    return extraction.data


async def _extract_order_data_uncached(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str,
) -> dict[str, Any]:
    mode = getattr(settings, "order_recognition_mode", "legacy")
    if mode == "tesseract_text":
        return await _extract_order_data_tesseract_fast(
            settings,
            session,
            case_id=case_id,
            user_id=user_id,
            order_photo_path=order_photo_path,
        )

    if not settings.order_integrity_enabled:
        primary = await _extract_order_data_primary(
            settings,
            session,
            case_id=case_id,
            user_id=user_id,
            order_photo_path=order_photo_path,
        )
        return primary.data

    classic_task = asyncio.create_task(extract_classic_ocr_text(order_photo_path, case_id=case_id))
    try:
        primary = await _extract_order_data_primary(
            settings,
            session,
            case_id=case_id,
            user_id=user_id,
            order_photo_path=order_photo_path,
        )
    except BaseException:
        if not classic_task.done():
            classic_task.cancel()
        await asyncio.gather(classic_task, return_exceptions=True)
        raise

    if primary.document_kind == "other" and not primary.is_court_order and primary.confidence >= 0.75:
        if not classic_task.done():
            classic_task.cancel()
        await asyncio.gather(classic_task, return_exceptions=True)
        logger.info(
            "Order integrity skipped for non-order case_id=%s confidence=%.2f reason=%s",
            case_id,
            primary.confidence,
            primary.reason[:300],
        )
        return {
            **primary.data,
            "_document_kind": "other",
            "_document_type_confidence": str(primary.confidence),
            "_document_type_reason": primary.reason,
        }

    verifier_task = asyncio.create_task(
        _extract_order_evidence(
            settings,
            None,
            case_id=case_id,
            user_id=user_id,
            order_photo_path=order_photo_path,
            classic_ocr_text="",
        )
    )
    # Money is read by a role-specific extractor in parallel, so the
    # arithmetic invariant does not add another full round-trip on mismatch.
    amounts_task = asyncio.create_task(
        _extract_order_amounts_result(
            settings,
            case_id=case_id,
            user_id=user_id,
            order_photo_path=order_photo_path,
        )
    )
    verifier_result, amounts_result, classic_result = await asyncio.gather(
        verifier_task, amounts_task, classic_task, return_exceptions=True
    )
    if isinstance(verifier_result, BaseException):
        await record_openai_usage(
            settings,
            session,
            case_id=case_id,
            user_id=user_id,
            operation="order_integrity_verifier",
            model=settings.order_verifier_model or settings.ai_review_model or settings.text_model,
            success=False,
            error_message=str(verifier_result),
        )
        raise RuntimeError(f"independent order verifier failed after internal API retries: {verifier_result}")

    verifier_payload, verifier_llm_result = verifier_result
    await record_openai_usage(
        settings,
        session,
        case_id=case_id,
        user_id=user_id,
        operation="order_integrity_verifier",
        model=verifier_llm_result.model,
        result=verifier_llm_result,
        success=True,
    )

    verifier_fields = evidence_payload_fields(verifier_payload)
    conflicts = conflicting_fields(primary.data, verifier_fields)
    adjudicator_result: dict[str, Any] | None = None
    if conflicts:
        try:
            adjudicator_result = await _adjudicate_order_conflicts(
                settings,
                session,
                case_id=case_id,
                user_id=user_id,
                order_photo_path=order_photo_path,
                primary=primary.data,
                verifier_payload=verifier_payload,
                conflicts=conflicts,
                classic_ocr_text=classic_result if isinstance(classic_result, str) else "",
            )
        except Exception:
            logger.exception("Order integrity adjudication failed for case %s", case_id)

    decision = merge_verified_order_data(
        primary.data,
        verifier_payload,
        adjudicator_result,
    )
    if isinstance(classic_result, str):
        classic_court = classic_court_name(classic_result)
        current_court = str(decision.data.get("court_name") or "")
        current_numbers = re.findall(r"\d+", current_court)
        classic_numbers = re.findall(r"\d+", classic_court)
        similarity = SequenceMatcher(None, current_court.lower(), classic_court.lower()).ratio() if current_court and classic_court else 0.0
        if classic_court and current_numbers == classic_numbers and similarity >= 0.78:
            decision.data["court_name"] = classic_court
            decision.data = normalize_order_data(decision.data)
    if isinstance(amounts_result, BaseException):
        await record_openai_usage(
            settings, session, case_id=case_id, user_id=user_id,
            operation="amounts_ocr_retry", model=settings.vision_model,
            success=False, error_message=str(amounts_result),
        )
    else:
        amounts_payload, amounts_llm_result = amounts_result
        await record_openai_usage(
            settings, session, case_id=case_id, user_id=user_id,
            operation="amounts_ocr_retry", model=amounts_llm_result.model,
            result=amounts_llm_result, success=True,
        )
        amount_recovery = recover_amounts_from_mismatch(decision.data, amounts_payload)
        if amount_recovery.applied:
            decision.data = amount_recovery.order_data
            decision.applied_fields.update(
                {
                    "debt_amount": str(decision.data.get("debt_amount") or ""),
                    "state_duty": str(decision.data.get("state_duty") or ""),
                    "total_amount": str(decision.data.get("total_amount") or ""),
                }
            )
    final_amount_check = validate_amounts(decision.data)
    if not final_amount_check.ok and isinstance(classic_result, str):
        classic_amounts = classic_amount_facts(classic_result)
        classic_recovery = recover_amounts_from_mismatch(decision.data, classic_amounts)
        if classic_recovery.applied:
            decision.data = classic_recovery.order_data
    _save_debug_attempt(
        case_id,
        "order_integrity_decision",
        {
            "conflicts": decision.conflicts,
            "unresolved_fields": decision.unresolved_fields,
            "applied_fields": decision.applied_fields,
            "evidence": decision.evidence,
            "final_data": decision.data,
        },
    )
    if decision.unresolved_fields:
        raise RuntimeError(
            "Order integrity could not resolve fields: " + ", ".join(decision.unresolved_fields)
        )
    return decision.data


async def extract_order_amounts(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str,
) -> dict[str, Any]:
    try:
        amounts, result = await _extract_order_amounts_result(
            settings, case_id=case_id, user_id=user_id, order_photo_path=order_photo_path
        )
    except Exception as exc:
        await record_openai_usage(
            settings, session, case_id=case_id, user_id=user_id,
            operation="amounts_ocr_retry", model=settings.vision_model,
            success=False, error_message=str(exc),
        )
        raise
    await record_openai_usage(
        settings, session, case_id=case_id, user_id=user_id,
        operation="amounts_ocr_retry", model=result.model, result=result, success=True,
    )
    return amounts


async def _extract_order_amounts_result(
    settings: Settings,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str,
) -> tuple[dict[str, Any], LLMResult]:
    image_variants = build_amount_ocr_variants(order_photo_path, case_id=case_id)
    instructions = (
        "Прочитай на фото судебного приказа только денежные суммы.\n\n"
        "Найди ровно три значения:\n"
        "1. сумма задолженности / задолженность / сумма долга;\n"
        "2. расходы по оплате государственной пошлины / госпошлина;\n"
        "3. всего к взысканию / итого / общая сумма.\n\n"
        'Верни каждую сумму строго в формате:\n"78 472 руб. 87 коп."\n\n'
        "Обязательно верни точный фрагмент текста, из которого взята сумма.\n"
        "Не вычисляй суммы сам на этом шаге.\n"
        "Не заменяй копейки на 00, если на изображении видны копейки.\n"
        'Особое внимание удели копейкам после "руб." и перед "коп.".\n'
        "Не путай сумму долга с общей суммой."
    )
    result = await _responses_json(
        settings,
        instructions=instructions,
        text="Прочитай только три денежные суммы с фрагментами текста.",
        image_paths=image_variants,
        schema_name="court_order_amounts",
        schema=AMOUNTS_JSON_SCHEMA,
    )
    amounts = {key: result.data.get(key, "") for key in AMOUNTS_JSON_SCHEMA_PROPERTIES}
    for key in ("debt_amount", "state_duty", "total_amount"):
        fragment_value = money_from_source_fragment(amounts.get(f"{key}_fragment"))
        if fragment_value is not None:
            amounts[key] = format_money_rub_kop(fragment_value)
        elif amounts.get(key):
            amounts[key] = clean_money_text(amounts[key])
    if case_id is not None:
        debug_dir = ensure_dir(Path("storage/debug") / f"case_{case_id}")
        (debug_dir / "amounts_ocr_retry.json").write_text(
            json.dumps(amounts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _save_debug_attempt(
            case_id,
            "amounts_ocr_retry",
            amounts,
            request_id=result.request_id,
            model=result.model,
        )
    return amounts, result


def looks_like_dative(value: str) -> bool:
    from app.services.name_normalizer import is_probably_not_nominative

    return is_probably_not_nominative(value)


NAME_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "debtor_full_name": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["debtor_full_name", "confidence"],
}

def assert_openai_strict_schema(schema: dict) -> None:
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())


async def normalize_debtor_name_llm(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    data: dict[str, Any],
) -> dict[str, Any]:
    if not settings.openai_api_key:
        return data
    raw = data.get("debtor_name_raw") or data.get("debtor_full_name") or ""
    context = data.get("debtor_name_context") or data.get("debtor_name_source_fragment") or ""
    start = time.perf_counter()
    body = {
        "model": settings.text_model,
        "instructions": (
            "Восстанови ФИО должника в именительном падеже по русским правилам. "
            "Верни только нормализованное ФИО и confidence 0..1."
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"ФИО: {raw}\nКонтекст: {context}",
                    }
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "debtor_name_normalization",
                "schema": NAME_JSON_SCHEMA,
                "strict": True,
            }
        },
        "max_output_tokens": 300,
    }
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=settings.llm_timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as http:
        async with http.post(f"{settings.openai_base_url}/responses", json=body) as response:
            raw_response = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"OpenAI API error {response.status}: {raw_response[:800]}")
            payload = json.loads(raw_response)
    answer = json.loads(_response_text(payload))
    llm_result = LLMResult(
        data=answer,
        usage=_parse_usage(payload),
        model=str(payload.get("model") or settings.text_model),
        request_id=str(payload.get("id") or ""),
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    await record_openai_usage(
        settings,
        session,
        case_id=case_id,
        user_id=user_id,
        operation="name_normalization",
        model=llm_result.model,
        result=llm_result,
        success=True,
    )
    updated = dict(data)
    if answer.get("debtor_full_name"):
        updated["debtor_full_name"] = str(answer["debtor_full_name"]).strip()
        updated["debtor_full_name_confidence"] = str(answer.get("confidence") or 0)
        updated, _ = normalize_debtor_name_fields(updated)
    return updated


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


async def review_generated_document(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    document_text: str,
    source_data: dict[str, Any],
    visual_summary: dict[str, Any] | None = None,
    regeneration_happened: bool = False,
    source_image_path: str | Path | None = None,
) -> dict[str, Any]:
    """Semantic review of the final client-visible statement text."""
    if not settings.openai_api_key:
        return {
            "ok": True,
            "severity": "ok",
            "needs_regeneration": False,
            "issues": [],
            "clean_fields": dict(DOCUMENT_REVIEW_CLEAN_FIELDS),
            "skipped": "OPENAI_API_KEY is not configured",
        }

    instructions = (
        "You are a legal QA reviewer for a final Russian court-order cancellation statement. "
        "Review the final client-visible text against the attached SOURCE COURT ORDER IMAGE. Return strict JSON only. "
        "Only issues present in FINAL STATEMENT TEXT may block delivery. SOURCE OCR/CASE FIELDS is reference-only and must not be treated as client-visible text. "
        "Check the header for OCR garbage, passport data, birthplace used as debtor address, multiple creditor addresses, "
        "non-nominative debtor full name, bad court-address wrapping, missing space after the numero sign, unsupported legal claims, "
        "empty required fields, signature/page-two layout problems, weird spaces, wrong short name in signature, and obvious OCR garbage. "
        "Prefer safe clean_fields corrections over blockers for universal field-cleaning cases: nominative debtor full name, debtor address without passport/birthplace/registration garbage, one normalized creditor address, clean court header, and correct signature source name. "
        "Do not flag feminine nominative Russian names such as \"\u041a\u0430\u0440\u0438\u043c\u043e\u0432\u0430 \u0415\u043b\u0435\u043d\u0430 \u0412\u0438\u043a\u0442\u043e\u0440\u043e\u0432\u043d\u0430\". "
        "Flag debtor block tokens including \"\u0443\u0440\u043e\u0436\u0435\u043d\", \"\u043f\u0430\u0441\u043f\u043e\u0440\u0442\", \"\u0432\u044b\u0434\u0430\u043d\", \"\u0423\u0424\u041c\u0421\", \"\u041e\u0423\u0424\u041c\u0421\", \"\u041c\u0412\u0414\", "
        "and registration markers left in raw grammatical form. In clean_fields, provide only safe text-field fixes. "
        "Check every factual field against the image itself, including single-letter address/name errors, court wording, case number, contract, dates, rubles and kopeks. "
        "Never suggest changing amounts, order date, case number, or UID unless the exact value is directly visible in the source image. "
        "The received date is user-provided and is not expected to appear in the order image. "
        "If a fix is unsafe or uncertain, keep suggested_fix empty and mark the issue as blocker."
        " Set source_verified=true only when source_fragment is an exact quote visibly supporting the fix in the attached image."
    )
    payload_text = (
        "FINAL STATEMENT TEXT:\n"
        f"{document_text[:18000]}\n\n"
        "SOURCE OCR/CASE FIELDS AND CURRENT NORMALIZED FIELDS:\n"
        f"{json.dumps(source_data, ensure_ascii=False, indent=2)[:8000]}\n\n"
        "DETERMINISTIC/VISUAL QA SUMMARY:\n"
        f"{json.dumps(visual_summary or {}, ensure_ascii=False, indent=2)[:4000]}"
    )
    result: LLMResult | None = None
    attempts = [
        (getattr(settings, "ai_review_model", settings.text_model), "primary"),
        (getattr(settings, "ai_review_model", settings.text_model), "primary_retry"),
    ]
    fallback_model = getattr(settings, "ai_review_fallback_model", None)
    if fallback_model:
        attempts.append((fallback_model, "fallback"))
    last_error: Exception | None = None
    for model, attempt_name in attempts:
        try:
            result = await _responses_json(
                settings,
                instructions=instructions,
                text=payload_text,
                image_path=source_image_path,
                schema_name="document_ai_review",
                schema=DOCUMENT_REVIEW_SCHEMA,
                model=model,
            )
            break
        except Exception as exc:
            last_error = exc
            await record_openai_usage(
                settings,
                session,
                case_id=case_id,
                user_id=user_id,
                operation="document_ai_review",
                model=model,
                success=False,
                error_message=str(exc),
                metadata={"regeneration_happened": regeneration_happened, "attempt": attempt_name},
            )
    if result is None:
        raise RuntimeError(f"document_ai_review failed after retry/fallback: {last_error}")

    data = dict(result.data)
    clean_fields = data.get("clean_fields") if isinstance(data.get("clean_fields"), dict) else {}
    data["clean_fields"] = {key: str(clean_fields.get(key) or "").strip() for key in DOCUMENT_REVIEW_CLEAN_FIELDS}
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    severity = str(data.get("severity") or "ok")
    await record_openai_usage(
        settings,
        session,
        case_id=case_id,
        user_id=user_id,
        operation="document_ai_review",
        model=result.model,
        result=result,
        success=True,
        metadata={
            "issues_count": len(issues),
            "severity": severity,
            "regeneration_happened": regeneration_happened,
        },
    )
    return data
