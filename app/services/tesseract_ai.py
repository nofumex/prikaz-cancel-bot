from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.image_preprocessing import build_order_ocr_variants
from app.services.legal_data import clean_text, make_short_name, normalize_order_data


_TESSERACT_LANE = asyncio.Lock()

ORDER_FIELD_KEYS = (
    "court_name", "court_address", "judge", "debtor_name_raw", "debtor_name_context",
    "debtor_full_name", "debtor_name_source_fragment", "debtor_birth_date", "debtor_passport",
    "debtor_address", "creditor_name", "creditor_address", "creditor_inn", "creditor_ogrn",
    "case_number", "uid", "order_date", "debt_contract", "debt_period", "debt_amount",
    "state_duty", "total_amount",
)

EVIDENCE_FIELDS = (
    "court_name", "court_address", "judge", "debtor_full_name", "debtor_address",
    "creditor_name", "creditor_address", "case_number", "uid", "order_date",
    "debt_contract", "debt_period", "debt_amount", "state_duty", "total_amount",
)


def _string_object(keys: tuple[str, ...] | list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {key: {"type": "string"} for key in keys},
        "required": list(keys),
    }


NAME_OCCURRENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string"},
        "grammatical_case": {"type": "string", "enum": ["nominative", "other", "unclear"]},
        "source_fragment": {"type": "string"},
    },
    "required": ["text", "grammatical_case", "source_fragment"],
}


TESSERACT_RECONCILIATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "safe_to_generate": {"type": "boolean"},
        "fields": _string_object(list(ORDER_FIELD_KEYS)),
        "debtor_name_occurrences": {"type": "array", "items": NAME_OCCURRENCE_SCHEMA},
        "debtor_full_name_source": {"type": "string", "enum": ["extracted", "generated"]},
        "source_fragments": _string_object(list(EVIDENCE_FIELDS)),
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "safe_to_generate", "fields", "debtor_name_occurrences", "debtor_full_name_source",
        "source_fragments", "issues",
    ],
}


RECONCILE_INSTRUCTIONS = """
Ты финальный интерпретатор российского судебного приказа после обязательной первичной AI-проверки.
Формулировки, порядок блоков и расположение реквизитов могут быть любыми: определяй роль значения по смыслу всего
документа. Первичные поля являются только кандидатами. Tesseract содержит четыре независимых прочтения страницы.

ФИО ДОЛЖНИКА: буквы фамилии, имени и отчества разрешено брать ТОЛЬКО из блока TESSERACT OCR. Не перечитывай ФИО
по изображению и не используй первичный AI-кандидат ФИО: он намеренно не передан. Собери в debtor_name_occurrences все
реально встречающиеся в Tesseract формы ФИО должника. occurrence.text содержит только ФИО без слов «должник» и
«взыскать с», source_fragment — строку Tesseract вокруг него. Сопоставляй повторения посимвольно и не заменяй редкую
фамилию более распространённой.

Если Tesseract содержит напечатанную именительную форму, fields.debtor_full_name должен дословно совпасть с ней и
debtor_full_name_source=extracted. Только если именительной формы во всех четырёх прочтениях нет, поставь generated и
восстанови именительный падеж из повторяющихся косвенных форм, меняя только падежные окончания и не меняя основу
фамилии. debtor_name_raw сохрани в самой ясной исходной напечатанной форме. debtor_name_source_fragment возьми из
Tesseract. Изображения используй для остальных реквизитов, но не для изменения букв ФИО.

Для каждого значимого реквизита верни дословный source_fragment. Не придумывай отсутствующие буквы, цифры, даты,
суммы или реквизиты. Различай сумму долга, госпошлину и итог. Пробел в денежном числе разделяет тысячи.

safe_to_generate=true только если это вынесенный судом судебный приказ и читаемо подтверждены: суд, должник,
взыскатель, дата приказа, номер дела или УИД, долг, госпошлина или итог. Заявление взыскателя о выдаче судебного
приказа не является судебным приказом. Если обязательные данные нельзя подтвердить, safe_to_generate=false.
Никаких confidence, вероятностей, словарной частотности и выбора человеком.
""".strip()


@dataclass(slots=True)
class TesseractOcrResult:
    text: str
    raw_texts: list[str]
    variant_paths: list[Path]
    latency_ms: int


@dataclass(slots=True)
class TesseractAiExtraction:
    data: dict[str, str]
    safe_to_generate: bool
    issues: list[str]
    source_fragments: dict[str, str]
    debtor_name_occurrences: list[dict[str, str]]
    debtor_full_name_source: str
    ocr: TesseractOcrResult
    llm_result: Any


def _line_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def compact_tesseract_texts(raw_texts: list[str]) -> str:
    if not raw_texts:
        return ""
    sections: list[str] = []
    seen: set[str] = set()
    for index, text in enumerate(raw_texts):
        lines: list[str] = []
        for raw_line in (text or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            key = _line_key(line)
            if not key or key in seen:
                continue
            seen.add(key)
            lines.append(line)
        if lines:
            sections.append(f"[TESSERACT VARIANT {index + 1}]\n" + "\n".join(lines))
    return "\n\n".join(sections)[:28000]


def _tesseract(path: Path) -> str:
    if not shutil.which("tesseract") or not path.exists():
        return ""
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "rus", "--psm", "6"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


async def extract_fast_tesseract_text(
    order_photo_path: str | Path,
    *,
    case_id: int | None = None,
) -> TesseractOcrResult:
    variants = build_order_ocr_variants(order_photo_path, case_id=case_id)
    started = time.perf_counter()
    async with _TESSERACT_LANE:
        raw_texts = []
        for path in variants:
            raw_texts.append(await asyncio.to_thread(_tesseract, path))
    return TesseractOcrResult(
        text=compact_tesseract_texts(list(raw_texts)),
        raw_texts=list(raw_texts),
        variant_paths=variants,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _name_occurrences(payload: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in payload.get("debtor_name_occurrences") or []:
        if not isinstance(item, dict) or not clean_text(item.get("text")):
            continue
        result.append({
            "text": clean_text(item.get("text")),
            "grammatical_case": clean_text(item.get("grammatical_case")) or "unclear",
            "source_fragment": clean_text(item.get("source_fragment")),
        })
    return result


def _contract_ok(payload: dict[str, Any]) -> bool:
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    fragments = payload.get("source_fragments") if isinstance(payload.get("source_fragments"), dict) else {}
    occurrences = _name_occurrences(payload)
    full_name = clean_text(fields.get("debtor_full_name"))
    if not full_name or not occurrences:
        return False
    if payload.get("debtor_full_name_source") == "extracted":
        nominatives = {
            item["text"] for item in occurrences if item.get("grammatical_case") == "nominative"
        }
        if full_name not in nominatives:
            return False
    mandatory_groups = (
        ("court_name",), ("debtor_full_name",), ("creditor_name",), ("order_date",),
        ("debt_amount",), ("case_number", "uid"), ("state_duty", "total_amount"),
    )
    return all(
        any(clean_text(fields.get(field)) and clean_text(fragments.get(field)) for field in group)
        for group in mandatory_groups
    )


def normalize_tesseract_ai_data(fields: dict[str, Any]) -> dict[str, str]:
    selected = {key: clean_text(fields.get(key)) for key in ORDER_FIELD_KEYS}
    full_name = selected.get("debtor_full_name", "")
    raw_name = selected.get("debtor_name_raw") or full_name
    selected["_debtor_name_tesseract_locked"] = "1"
    normalized = normalize_order_data(selected)
    normalized["debtor_full_name"] = full_name
    normalized["debtor_name_raw"] = raw_name
    normalized["debtor_short_name"] = make_short_name(full_name)
    normalized["_debtor_name_tesseract_locked"] = "1"
    normalized.pop("debtor_full_name_confidence", None)
    normalized.pop("debtor_name_confidence", None)
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", normalized.get("order_date", "")):
        normalized["order_date"] = ""
    return {str(key): clean_text(value) for key, value in normalized.items()}


async def extract_order_data_from_tesseract_ai(
    settings: Settings,
    session: AsyncSession | None,
    *,
    case_id: int | None,
    user_id: int | None,
    order_photo_path: str | Path,
    primary_candidates: dict[str, Any],
    ocr: TesseractOcrResult | None = None,
) -> TesseractAiExtraction:
    from app.services.llm import _responses_json, record_openai_usage

    if ocr is None:
        ocr = await extract_fast_tesseract_text(order_photo_path, case_id=case_id)
    candidates = dict(primary_candidates)
    for key in (
        "debtor_full_name", "debtor_name_raw", "debtor_name_context",
        "debtor_name_source_fragment", "debtor_full_name_confidence", "debtor_name_confidence",
    ):
        candidates.pop(key, None)
    result = await _responses_json(
        settings,
        instructions=RECONCILE_INSTRUCTIONS,
        text=(
            "PRIMARY AI CANDIDATES EXCEPT DEBTOR NAME (not truth):\n"
            + json.dumps(candidates, ensure_ascii=False, indent=2)
            + "\n\nTESSERACT OCR — ONLY SOURCE FOR DEBTOR NAME LETTERS:\n"
            + ocr.text
        ),
        image_paths=ocr.variant_paths,
        schema_name="tesseract_order_reconciliation",
        schema=TESSERACT_RECONCILIATION_SCHEMA,
        model=settings.tesseract_ai_model or settings.text_model,
    )
    await record_openai_usage(
        settings,
        session,
        case_id=case_id,
        user_id=user_id,
        operation="tesseract_ai_reconcile",
        model=result.model,
        result=result,
        success=True,
    )
    payload = result.data
    occurrences = _name_occurrences(payload)
    fields = normalize_tesseract_ai_data(payload.get("fields") or {})
    fragments = {
        key: clean_text((payload.get("source_fragments") or {}).get(key)) for key in EVIDENCE_FIELDS
    }
    safe = bool(payload.get("safe_to_generate")) and _contract_ok(payload)
    return TesseractAiExtraction(
        data=fields,
        safe_to_generate=safe,
        issues=[clean_text(item) for item in (payload.get("issues") or []) if clean_text(item)],
        source_fragments=fragments,
        debtor_name_occurrences=occurrences,
        debtor_full_name_source=clean_text(payload.get("debtor_full_name_source")),
        ocr=ocr,
        llm_result=result,
    )


def compact_statement_audit_text(data: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {clean_text(data.get(key))}" for key in ORDER_FIELD_KEYS if clean_text(data.get(key)))
