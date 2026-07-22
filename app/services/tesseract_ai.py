from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageOps

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.image_preprocessing import assess_order_image, prepare_order_ocr_image
from app.services.legal_data import (
    clean_case_number,
    clean_money_text,
    clean_text,
    clean_uid,
    format_money_rub_kop,
    make_short_name,
    money_to_decimal,
    normalize_address_text,
    parse_russian_date,
)
from app.utils import ensure_dir

_TESSERACT_LANE = asyncio.Lock()
PIPELINE_VERSION = "tesseract-text-v3"
CROP_PREPROCESSING_VERSION = "field-crop-v2"

SIMPLE_FIELD_KEYS = (
    "court_name", "court_address", "judge", "debtor_name_printed",
    "debtor_name_nominative", "debtor_address", "creditor_name",
    "creditor_address", "case_number", "uid", "order_date",
    "debt_contract", "debt_period", "debt_amount", "state_duty",
    "total_amount",
)

SIMPLE_ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_court_order": {"type": "boolean"},
        **{name: {"type": "string"} for name in SIMPLE_FIELD_KEYS},
    },
    "required": ["is_court_order", *SIMPLE_FIELD_KEYS],
}

SIMPLE_ORDER_INSTRUCTIONS = """
You receive the complete Tesseract OCR text of a Russian court order. Extract it
into the fixed JSON object for an application cancelling that court order.
Never invent values. debtor_name_printed preserves the printed grammatical
case; debtor_name_nominative contains the same full name in nominative case.
judge is surname and initials only. uid is allowed only immediately after an
explicit UID label; taxpayer IDs, debtor identifiers, case numbers and
proceeding numbers are never UID. If no explicit UID exists, return an empty
uid. Return one postal address per party without bank or tax details. Normalize
dates, the debt period, and Russian ruble/kopeck amounts. If total_amount is not
printed but debt_amount and state_duty are unambiguous, calculate their sum.
Return empty strings for genuinely absent values and JSON only.
""".strip()

ORDER_FIELD_KEYS = (
    "court_name", "court_address", "judge", "debtor_name_raw", "debtor_name_context",
    "debtor_full_name", "debtor_name_source_fragment", "debtor_birth_date", "debtor_passport",
    "debtor_address", "creditor_name", "creditor_address", "creditor_legal_address",
    "creditor_correspondence_address", "creditor_inn", "creditor_ogrn",
    "case_number", "uid", "order_date", "debt_contract", "debt_period", "debt_amount",
    "interest", "penalty", "state_duty", "total_amount", "proceeding_type",
)
EVIDENCE_FIELDS = tuple(key for key in ORDER_FIELD_KEYS if key not in {
    "debtor_name_context", "debtor_name_source_fragment", "debtor_birth_date", "debtor_passport",
    "creditor_inn", "creditor_ogrn",
})
LLM_FIELD_KEYS = (
    "court_name", "court_address", "judge", "debtor_full_name", "debtor_address",
    "creditor_name", "creditor_legal_address", "creditor_correspondence_address", "case_number", "uid", "order_date",
    "debt_contract", "debt_period", "debt_amount", "state_duty", "total_amount",
    "proceeding_type",
)
LLM_STATUSES = ("candidate", "ambiguous", "missing")
ENTITY_ROLES = ("document", "court", "judge", "debtor", "creditor", "representative", "other")
FIELD_OWNER_ROLES = {
    "court_name": {"court"}, "court_address": {"court"}, "judge": {"judge"},
    "debtor_full_name": {"debtor"}, "debtor_address": {"debtor"},
    "creditor_name": {"creditor"}, "creditor_address": {"creditor"},
    "creditor_legal_address": {"creditor"}, "creditor_correspondence_address": {"creditor"},
    "case_number": {"document"}, "uid": {"document"}, "order_date": {"document"},
    "debt_contract": {"document"}, "debt_period": {"document"}, "debt_amount": {"document"},
    "interest": {"document"}, "penalty": {"document"}, "state_duty": {"document"},
    "total_amount": {"document"}, "proceeding_type": {"document"},
}
FIELD_SEMANTIC_ROLES = {
    "court_name": "court_name", "court_address": "court_address", "judge": "judge_name",
    "debtor_full_name": "debtor_name", "debtor_address": "debtor_address",
    "creditor_name": "creditor_name", "creditor_address": "creditor_address",
    "creditor_legal_address": "creditor_legal_address",
    "creditor_correspondence_address": "creditor_correspondence_address",
    "case_number": "court_order_case_number", "uid": "court_order_uid",
    "order_date": "court_order_issue_date", "debt_contract": "debt_basis",
    "debt_period": "debt_period", "debt_amount": "principal_debt", "interest": "interest",
    "penalty": "penalty", "state_duty": "state_duty", "total_amount": "total_recovery",
    "proceeding_type": "proceeding_type",
}
FIELD_ALLOWED_SEMANTIC_ROLES = {
    name: ({role} if isinstance(role, str) else set(role))
    for name, role in FIELD_SEMANTIC_ROLES.items()
}
FIELD_ALLOWED_SEMANTIC_ROLES["creditor_address"] = {
    "creditor_legal_address", "creditor_correspondence_address",
}
FIELD_ALLOWED_SEMANTIC_ROLES["creditor_legal_address"] = {"creditor_legal_address"}
FIELD_ALLOWED_SEMANTIC_ROLES["creditor_correspondence_address"] = {"creditor_correspondence_address"}

CRITICAL_FIELDS = {
    "court_name", "court_address", "debtor_full_name", "debtor_address", "creditor_name", "creditor_address", "creditor_legal_address", "creditor_correspondence_address", "case_number", "uid",
    "order_date", "debt_amount", "state_duty", "total_amount",
}

REQUIRED_GENERATION_FIELDS = {
    "court_name", "court_address", "judge", "debtor_full_name", "debtor_address",
    "creditor_name", "creditor_legal_address", "creditor_correspondence_address",
    "case_number", "uid", "order_date", "debt_contract", "debt_period",
    "debt_amount", "state_duty", "total_amount",
}

def _string_array() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


ENTITY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "entity_id": {"type": "string"}, "role": {"type": "string", "enum": list(ENTITY_ROLES)},
        "source_word_ids": _string_array(),
    },
    "required": ["entity_id", "role", "source_word_ids"],
}
TEXT_FIELD_ASSIGNMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "printed_value": {"type": "string"},
        "semantic_role": {
            "type": "string",
            "enum": ["", *sorted(set().union(*FIELD_ALLOWED_SEMANTIC_ROLES.values()))],
        },
        "derived_value": {"type": "string"},
        "alternatives": _string_array(),
        "status": {"type": "string", "enum": list(LLM_STATUSES)},
    },
    "required": ["printed_value", "semantic_role", "derived_value", "alternatives", "status"],
}
TEXT_NAME_OCCURRENCE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "printed_value": {"type": "string"},
        "grammatical_case": {"type": "string", "enum": ["nominative", "other", "unclear"]},
    },
    "required": ["printed_value", "grammatical_case"],
}
TESSERACT_RECONCILIATION_SCHEMA: dict[str, Any] = {
    "$defs": {"field_assignment": TEXT_FIELD_ASSIGNMENT_SCHEMA},
    "type": "object", "additionalProperties": False,
    "properties": {
        "is_court_order": {"type": "boolean"},
        "field_assignments": {
            "type": "object", "additionalProperties": False,
            "properties": {key: {"$ref": "#/$defs/field_assignment"} for key in LLM_FIELD_KEYS},
            "required": list(LLM_FIELD_KEYS),
        },
        "debtor_name_occurrences": {"type": "array", "items": TEXT_NAME_OCCURRENCE_SCHEMA},
    },
    "required": ["is_court_order", "field_assignments", "debtor_name_occurrences"],
}

RECONCILE_INSTRUCTIONS = """
Извлеки значения российского судебного приказа только из текста двух OCR-прогонов.
Не возвращай word_id, координаты, confidence, owner entities или relation IDs. Для каждого
фиксированного поля верни printed_value как точную непрерывную цитату из OCR без исправлений,
добавлений и нормализации. Программа сама найдёт цитату в OCR и построит span, bbox и confidence.

Для case_number цитируй полный номер после «Дело №», для uid — полный идентификатор после «УИД»
без самих меток. Для каждой суммы цитируй все напечатанные цифры и единицы, включая рубли и
копейки; для debt_period — обе даты. Адрес цитируй как один почтовый адрес без слов «адрес», «зарегистрированному», без метки
владельца, ИНН, КПП, ОГРН, БИК, банка и счетов. Judge цитируй только ФИО/инициалы. Если точной однозначной цитаты нет, status ambiguous или missing, не достраивай текст.

Для creditor_legal_address semantic_role creditor_legal_address; для
creditor_correspondence_address semantic_role creditor_correspondence_address. При двух адресах без явной метки нужного типа — ambiguous.
Для debtor_full_name printed_value сохраняет напечатанный падеж; только проверенный именительный
можно вернуть в derived_value. Для proceeding_type derived_value: gpk/apk/kas/unclear.
Во всех остальных полях derived_value пустой: нормализацию выполняет программа.
LLM status только candidate/ambiguous/missing, никогда confirmed.
""".strip()

@dataclass(frozen=True, slots=True)
class OcrWord:
    word_id: str
    line_id: str
    page: int
    text: str
    bbox: tuple[int, int, int, int]
    confidence: float


@dataclass(frozen=True, slots=True)
class OcrLine:
    line_id: str
    page: int
    text: str
    bbox: tuple[int, int, int, int]
    confidence: float
    word_ids: tuple[str, ...]


@dataclass(slots=True)
class TesseractOcrResult:
    text: str
    raw_texts: list[str]
    variant_paths: list[Path]
    latency_ms: int
    words: list[OcrWord] = field(default_factory=list)
    lines: list[OcrLine] = field(default_factory=list)
    image_hash: str = ""


@dataclass(slots=True)
class TesseractAiExtraction:
    data: dict[str, Any]
    safe_to_generate: bool
    issues: list[str]
    source_fragments: dict[str, str]
    debtor_name_occurrences: list[dict[str, Any]]
    debtor_full_name_source: str
    selected_name_occurrence: str
    ocr: TesseractOcrResult
    llm_result: Any


def _canonical(value: Any) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip().casefold()


def _bbox_union(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    if not boxes:
        return (0, 0, 0, 0)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def compact_tesseract_texts(raw_texts: list[str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for text in raw_texts:
        for raw_line in (text or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            key = line.casefold()
            if line and key not in seen:
                seen.add(key)
                lines.append(line)
    return "\n".join(lines)[:28000]


def _parse_tsv(tsv: str, *, run_prefix: str = "") -> tuple[list[OcrWord], list[OcrLine]]:
    words: list[OcrWord] = []
    groups: dict[tuple[int, int, int, int], list[OcrWord]] = {}
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text or row.get("level") != "5":
            continue
        try:
            page = max(1, int(row.get("page_num") or 1))
            key = (page, int(row.get("block_num") or 0), int(row.get("par_num") or 0), int(row.get("line_num") or 0))
            left, top = int(row.get("left") or 0), int(row.get("top") or 0)
            width, height = int(row.get("width") or 0), int(row.get("height") or 0)
            confidence = max(0.0, float(row.get("conf") or 0))
        except (TypeError, ValueError):
            continue
        line_id = run_prefix + ("p%d_b%d_p%d_l%d" % key)
        word = OcrWord(f"{run_prefix}p{page}_w{len(words) + 1}", line_id, page, text, (left, top, left + width, top + height), confidence)
        words.append(word)
        groups.setdefault(key, []).append(word)
    lines: list[OcrLine] = []
    for key, line_words in groups.items():
        confidences = [word.confidence for word in line_words if word.confidence >= 0]
        lines.append(OcrLine(
            line_words[0].line_id, key[0], " ".join(word.text for word in line_words),
            _bbox_union([word.bbox for word in line_words]),
            sum(confidences) / len(confidences) if confidences else 0.0,
            tuple(word.word_id for word in line_words),
        ))
    return words, lines


def _tesseract_tsv(path: Path, psm: int = 3) -> str:
    if not shutil.which("tesseract") or not path.exists():
        return ""
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "rus+eng", "--psm", str(psm), "tsv"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


async def extract_fast_tesseract_text(order_photo_path: str | Path, *, case_id: int | None = None) -> TesseractOcrResult:
    quality = assess_order_image(order_photo_path)
    if not quality.ok:
        raise ValueError(quality.reason)
    prepared = prepare_order_ocr_image(order_photo_path, case_id=case_id)
    image_hash = hashlib.sha256(Path(order_photo_path).read_bytes()).hexdigest()
    started = time.perf_counter()
    async with _TESSERACT_LANE:
        tsv_primary = await asyncio.to_thread(_tesseract_tsv, prepared, 3)
        tsv_secondary = await asyncio.to_thread(_tesseract_tsv, prepared, 6)
    primary_words, primary_lines = _parse_tsv(tsv_primary, run_prefix="r1_")
    secondary_words, secondary_lines = _parse_tsv(tsv_secondary, run_prefix="r2_")
    words = [*primary_words, *secondary_words]
    lines = [*primary_lines, *secondary_lines]
    primary_text = "\n".join(line.text for line in primary_lines)
    secondary_text = "\n".join(line.text for line in secondary_lines)
    text = compact_tesseract_texts([primary_text, secondary_text])
    return TesseractOcrResult(
        text=text, raw_texts=[primary_text, secondary_text], variant_paths=[prepared],
        latency_ms=int((time.perf_counter() - started) * 1000), words=words, lines=lines, image_hash=image_hash,
    )


def _crop_psm(field_name: str) -> int:
    if field_name in {"court_address", "debtor_address", "creditor_address", "creditor_legal_address", "creditor_correspondence_address", "debt_amount", "interest", "penalty", "state_duty", "total_amount"}:
        return 6
    if field_name in {"case_number", "uid", "debtor_full_name", "judge", "order_date"}:
        return 7
    return 11


def _crop_tesseract(path: Path, psm: int) -> str:
    if not shutil.which("tesseract") or not path.exists():
        return ""
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "rus+eng", "--psm", str(psm)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _crop_candidate(field_name: str, crop_text: str, expected: str) -> str:
    text = clean_text(crop_text)
    if not text:
        return ""
    if field_name == "uid":
        candidates = re.findall(r"(?<!\w)[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_/-]{9,}(?!\w)", text)
        return next((item for item in candidates if _format_ok("uid", item)), "")
    if field_name == "case_number":
        candidates = re.findall(r"(?<!\w)[0-9А-Яа-яA-Za-z]+(?:[-/][0-9А-Яа-яA-Za-z]+)+(?!\w)", text)
        return next((item for item in candidates if _format_ok("case_number", item)), "")
    if field_name == "judge":
        match = re.search(r"\b[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\s*\.\s*[А-ЯЁ]\s*\.", text)
        return clean_text(match.group(0)) if match else ""
    if field_name in {"debt_amount", "state_duty", "total_amount"}:
        text = re.sub(r"\b(?:kor|kop)\b", "коп", text, flags=re.IGNORECASE)
        candidates = re.findall(
            r"\d[\d\s]*(?:[,.]\d{2})?\s*руб\w*\.?\s*(?:\d{1,2}\s*коп\w*\.?)?",
            text, flags=re.IGNORECASE,
        )
        for candidate in reversed(candidates):
            candidate = clean_text(candidate)
            if _format_ok(field_name, candidate) and not _money_unit_incomplete(field_name, candidate):
                return candidate
        return ""
    if expected and _match_key(expected) in _match_key(text):
        return expected
    return ""


def _crop_consensus_key(value: str) -> str:
    text = clean_text(value).casefold().replace("–", "-").replace("—", "-").replace("‑", "-")
    return re.sub(r"\s*([/,_-])\s*|\s+", lambda match: match.group(1) or "", text)


def _select_crop_consensus(attempts: list[dict[str, Any]]) -> tuple[str, list[int]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        candidate = clean_text(attempt.get("candidate"))
        if candidate:
            groups.setdefault(_crop_consensus_key(candidate), []).append(attempt)
    agreed = [items for items in groups.values() if len({int(item["psm"]) for item in items}) >= 2]
    if not agreed:
        return "", []
    best = max(agreed, key=lambda items: len({int(item["psm"]) for item in items}))
    return clean_text(best[0]["candidate"]), sorted({int(item["psm"]) for item in best})

def _crop_equivalent(field_name: str, candidate: str, expected: str) -> bool:
    if not candidate or not expected:
        return False
    if field_name in {"debt_amount", "state_duty", "total_amount"}:
        return (
            not _money_unit_incomplete(field_name, candidate)
            and money_to_decimal(candidate) is not None
            and money_to_decimal(candidate) == money_to_decimal(expected)
        )
    return _match_key(candidate) == _match_key(expected)


async def verify_disputed_fields(
    records: dict[str, dict[str, Any]], ocr: TesseractOcrResult, *, case_id: int | None = None,
) -> dict[str, dict[str, str]]:
    """Run targeted OCR; callers must rerun the simple validator with returned evidence."""
    verified: dict[str, dict[str, str]] = {}
    if not ocr.variant_paths or not ocr.variant_paths[0].exists():
        return verified
    eligible_reasons = {
        "low_ocr_confidence", "source_mismatch", "source_text_not_found", "format_invalid",
        "ocr_single_run", "ocr_disagreement", "llm_ambiguous", "money_unit_incomplete",
    }
    for name, record in records.items():
        if name not in REQUIRED_GENERATION_FIELDS or record.get("status") != "disputed":
            continue
        reasons = {item for item in clean_text(record.get("verification_reason")).split(",") if item}
        if not reasons or not reasons <= eligible_reasons:
            continue
        bbox = record.get("bbox") or []
        if len(bbox) != 4 or not any(bbox):
            continue
        with Image.open(ocr.variant_paths[0]) as image:
            margin = max(12, int(min(image.size) * 0.015))
            left, top, right, bottom = [int(value) for value in bbox]
            horizontal_margin = margin
            vertical_margin = margin
            if name == "judge":
                line_height = max(1, bottom - top)
                horizontal_margin = max(8, line_height * 2)
                vertical_margin = max(3, line_height // 3)
            crop = image.crop((
                max(0, left - horizontal_margin), max(0, top - vertical_margin),
                min(image.width, right + horizontal_margin), min(image.height, bottom + vertical_margin),
            ))
            if crop.width < 1000:
                scale = min(4, max(2, 1000 // max(1, crop.width)))
                crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
            crop = ImageEnhance.Contrast(ImageOps.grayscale(crop)).enhance(1.25)
        expected = clean_text(record.get("extracted_value"))
        modes = list(dict.fromkeys([_crop_psm(name), 7, 6, 11]))
        attempts: list[dict[str, Any]] = []
        for psm in modes:
            key_material = f"{ocr.image_hash}:{bbox}:{PIPELINE_VERSION}:{CROP_PREPROCESSING_VERSION}:{name}:rus+eng:psm{psm}"
            cache_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
            cache_path = ensure_dir("storage/ocr_cache/crops") / f"{cache_key}.json"
            crop_text = ""
            try:
                crop_text = clean_text(json.loads(cache_path.read_text(encoding="utf-8")).get("text"))
            except (OSError, ValueError, TypeError):
                pass
            if not crop_text:
                crop_path = ensure_dir(Path("storage/debug") / f"case_{case_id or 'unknown'}") / f"verify_{name}_{cache_key[:10]}.png"
                crop.save(crop_path)
                async with _TESSERACT_LANE:
                    crop_text = await asyncio.to_thread(_crop_tesseract, crop_path, psm)
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"text": crop_text, "psm": psm}, ensure_ascii=False), encoding="utf-8")
                temporary.replace(cache_path)
            candidate = _crop_candidate(name, crop_text, expected)
            attempts.append({"psm": psm, "raw_text": crop_text, "candidate": candidate})
        consensus_value, consensus_psms = _select_crop_consensus(attempts)
        accepted_value = consensus_value
        if name == "judge" and consensus_value and not _crop_equivalent(name, consensus_value, expected):
            accepted_value = ""
        verified[name] = {
            "value": accepted_value,
            "reason": "targeted_crop_consensus" if accepted_value else "targeted_crop_no_consensus",
            "attempts": attempts,
            "consensus_value": consensus_value,
            "consensus_psms": consensus_psms,
        }
    return verified

def _word_map(ocr: TesseractOcrResult) -> dict[str, OcrWord]:
    return {word.word_id: word for word in ocr.words}


def _words_for(ids: Any, words: dict[str, OcrWord]) -> list[OcrWord]:
    if not isinstance(ids, list):
        return []
    unique_ids = list(dict.fromkeys(item for item in ids if isinstance(item, str)))
    return [words[item] for item in unique_ids if item in words]


def _source_value(selected: list[OcrWord]) -> str:
    return " ".join(word.text for word in selected)


def _safe_address(value: str) -> str:
    value = re.sub(r"^\s*адрес\s*:\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*\n\s*", ", ", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"([,.;])(?:\s*\1)+", r"\1", value)
    return value.strip(" ,")


def _relation_supports(owner: dict[str, Any], relation: list[OcrWord]) -> bool:
    if not relation:
        return False
    role = clean_text(owner.get("role"))
    if role == "document":
        return True
    relation_ids = {word.word_id for word in relation}
    owner_ids = set(owner.get("source_word_ids") or [])
    if relation_ids & owner_ids:
        return True
    text = _canonical(_source_value(relation))
    owner_name = _canonical(owner.get("name"))
    if owner_name and owner_name in text:
        return True
    keywords = {
        "court": ("суд", "судебн", "судебный участок"),
        "judge": ("судья", "мировой судья"),
        "debtor": ("должник", "должника", "место жительств", "зарегистрирован"),
        "creditor": ("взыскател", "место нахожд"),
        "representative": ("представител",),
    }
    expected = keywords.get(role, ())
    if not any(token in text for token in expected):
        return False
    incompatible = set().union(*(set(values) for key, values in keywords.items() if key != role))
    return not any(token in text for token in incompatible)

def _relation_contradicts_owner(owner: dict[str, Any], relation: list[OcrWord]) -> bool:
    text = _canonical(_source_value(relation))
    role = clean_text(owner.get("role"))
    patterns = {
        "court": (r"\bсуд\b", r"\bсудебн\w*"),
        "judge": (r"\bсудь\w*",),
        "debtor": (r"\bдолжник\w*",),
        "creditor": (r"\bвзыскател\w*",),
        "representative": (r"\bпредставител\w*",),
    }
    return any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for other, role_patterns in patterns.items() if other != role
        for pattern in role_patterns
    )

def _line_parts(line_id: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"p(\d+)_b(\d+)_p(\d+)_l(\d+)", line_id)
    return tuple(int(value) for value in match.groups()) if match else None


def _axis_position(box: tuple[int, int, int, int] | list[int]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _between(entity_box: list[int], owner_box: list[int], value_box: tuple[int, int, int, int]) -> bool:
    ex, ey = _axis_position(entity_box)
    ox, oy = _axis_position(owner_box)
    vx, vy = _axis_position(value_box)
    if abs(oy - vy) <= max(owner_box[3] - owner_box[1], value_box[3] - value_box[1]):
        return min(ox, vx) < ex < max(ox, vx) and abs(ey - vy) <= max(
            owner_box[3] - owner_box[1], value_box[3] - value_box[1]
        )
    return min(oy, vy) < ey < max(oy, vy)


def _spatial_owner_evidence(
    field_name: str,
    owner: dict[str, Any],
    selected: list[OcrWord],
    entities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "supported": False,
        "same_page": False,
        "same_block": False,
        "adjacent_block": False,
        "owner_distance_line_heights": None,
        "second_owner_distance_line_heights": None,
        "nearest_owner_entity_id": "",
        "intervening_entity_ids": [],
    }
    if not selected or not owner.get("bbox"):
        return evidence
    pages = {word.page for word in selected}
    evidence["same_page"] = len(pages) == 1 and owner.get("page") in pages
    if not evidence["same_page"]:
        return evidence

    source_box = _bbox_union([word.bbox for word in selected])
    intervening = [
        clean_text(entity.get("entity_id")) for entity in entities.values()
        if entity.get("entity_id") != owner.get("entity_id")
        and entity.get("role") != "document"
        and entity.get("page") == selected[0].page
        and entity.get("bbox")
        and _between(entity["bbox"], owner["bbox"], source_box)
    ]
    evidence["intervening_entity_ids"] = [item for item in intervening if item]
    source_lines = {_line_parts(word.line_id) for word in selected}
    source_lines.discard(None)
    owner_lines = {_line_parts(line_id) for line_id in owner.get("source_line_ids") or []}
    owner_lines.discard(None)
    source_blocks = {(item[0], item[1]) for item in source_lines}
    owner_blocks = {(item[0], item[1]) for item in owner_lines}
    evidence["same_block"] = bool(source_blocks & owner_blocks)

    line_heights = [max(1, word.bbox[3] - word.bbox[1]) for word in selected]
    line_heights.append(max(1, owner["bbox"][3] - owner["bbox"][1]))
    line_height = max(1.0, sum(line_heights) / len(line_heights))

    def distance(entity: dict[str, Any]) -> float:
        ex, ey = _axis_position(entity["bbox"])
        sx, sy = _axis_position(source_box)
        vertical_gap = max(0.0, max(entity["bbox"][1], source_box[1]) - min(entity["bbox"][3], source_box[3]))
        horizontal_gap = abs(ex - sx) if abs(ey - sy) <= line_height else 0.0
        return (vertical_gap + 0.25 * horizontal_gap) / line_height

    owner_distance = distance(owner)
    evidence["owner_distance_line_heights"] = round(owner_distance, 3)
    owner_x, owner_y = _axis_position(owner["bbox"])
    value_x, value_y = _axis_position(source_box)
    follows_owner = value_y > owner_y or (abs(value_y - owner_y) <= line_height and value_x > owner_x)
    evidence["value_follows_owner"] = follows_owner
    if not follows_owner:
        return evidence
    adjacent = any(
        sp == op and abs(sb - ob) == 1
        for sp, sb in source_blocks for op, ob in owner_blocks
    )
    evidence["adjacent_block"] = adjacent and owner_distance <= 4.0
    if not (evidence["same_block"] or evidence["adjacent_block"]):
        return evidence

    compatible_roles = FIELD_OWNER_ROLES[field_name]
    candidates = [
        entity for entity in entities.values()
        if entity.get("role") in compatible_roles
        and entity.get("page") == selected[0].page
        and entity.get("bbox")
    ]
    ranked = sorted((distance(entity), entity) for entity in candidates)
    if not ranked:
        return evidence
    evidence["nearest_owner_entity_id"] = clean_text(ranked[0][1].get("entity_id"))
    if ranked[0][1].get("entity_id") != owner.get("entity_id"):
        return evidence
    if len(ranked) > 1:
        second_distance = ranked[1][0]
        evidence["second_owner_distance_line_heights"] = round(second_distance, 3)
        if second_distance <= owner_distance + 1.0 or second_distance <= owner_distance * 1.25:
            return evidence

    if evidence["intervening_entity_ids"]:
        return evidence
    evidence["supported"] = True
    return evidence

def _semantic_relation_supports(field_name: str, relation: list[OcrWord]) -> bool:
    text = _canonical(_source_value(relation))
    if not text:
        return False
    required = {
        "case_number": ("дел", "производств"),
        "uid": ("уид",),
        "order_date": ("приказ", "вынес", "постанов"),
        "debt_contract": ("договор", "займ", "кредит"),
        "debt_period": ("период",),
        "debt_amount": ("задолж", "основн", "долг"),
        "interest": ("процент",),
        "penalty": ("неустой", "пен"),
        "state_duty": ("госпош", "пошлин"),
        "total_amount": ("итого", "всего", "общ"),
        "proceeding_type": ("гпк", "апк", "кас", "производ"),
    }
    forbidden = {
        "case_number": ("уид",),
        "uid": ("дело", "договор"),
        "order_date": ("договор", "получ", "заявлен"),
        "debt_amount": ("госпош", "пошлин", "итого", "всего"),
        "state_duty": ("основной долг", "задолженность", "итого", "всего"),
        "total_amount": ("основной долг", "госпошлина"),
    }
    if any(token in text for token in forbidden.get(field_name, ())):
        return False
    expected = required.get(field_name)
    if expected is None:
        return True
    # Exact semantic_role is schema-locked. Relation text must be present and
    # must not carry a conflicting label; explicit positive labels strengthen
    # evidence but their absence alone is not a contradiction.
    return any(token in text for token in expected)

def _document_semantic_supports(
    field_name: str,
    relation: list[OcrWord],
    selected: list[OcrWord],
    all_words: list[OcrWord],
) -> tuple[bool, str]:
    if _semantic_relation_supports(field_name, relation):
        return True, "explicit_semantic_label"
    if not selected:
        return False, "invalid"
    positions = {word.word_id: index for index, word in enumerate(all_words)}
    selected_positions = [positions[word.word_id] for word in selected if word.word_id in positions]
    if not selected_positions:
        return False, "invalid"
    first, last = min(selected_positions), max(selected_positions)
    context = [
        word for word in all_words[max(0, first - 8):last + 1]
        if word.page == selected[0].page
    ]
    if _semantic_relation_supports(field_name, context):
        return True, "ocr_context_semantic_label"
    return False, "invalid"

_ADDRESS_REQUISITE_RE = re.compile(
    r"(?:\b(?:инн|кпп|огрн|бик|корреспондентск(?:ий|ого)|расч[её]тн(?:ый|ого)|лицев(?:ой|ого)|банк)\b|\b[рк]/с\b)",
    re.IGNORECASE,
)


def _address_span_ok(
    field_name: str, semantic_role: str, value: str, relation_text: str, document_text: str,
) -> bool:
    if re.search(r"\b(?:адрес\w*|зарегистрирован\w*)\b", value, flags=re.IGNORECASE):
        return False
    if field_name not in {"creditor_address", "creditor_legal_address", "creditor_correspondence_address"}:
        return True
    if _ADDRESS_REQUISITE_RE.search(value):
        return False
    if re.search(r"\b(?:РЕШИЛ|паспорт\w*|должник\w*|руб\w*|коп\w*)\b", value, re.IGNORECASE):
        return False
    if re.search(r"\b(?:адрес|для\s+корреспонденц\w*)\b", value, flags=re.IGNORECASE):
        return False
    if re.search(r"\b(?:город|улица|дом|корпус|строение|квартира)\.?\s*$", value, flags=re.IGNORECASE):
        return False
    correspondence = bool(re.search(r"(?:для\s+корреспонденц\w*|почтовый\s+адрес)", relation_text, flags=re.IGNORECASE))
    legal = bool(re.search(r"(?:юридическ\w*\s+адрес|место\s+нахожд)\w*", relation_text, flags=re.IGNORECASE))
    if field_name == "creditor_correspondence_address" or semantic_role == "creditor_correspondence_address":
        return correspondence
    if (field_name == "creditor_correspondence_address" or semantic_role != "creditor_legal_address") or correspondence:
        return False
    if legal:
        return True
    document_has_correspondence = bool(re.search(
        r"(?:для\s+корреспонденц\w*|почтовый\s+адрес)", document_text, flags=re.IGNORECASE,
    ))
    address_count = max(
        len(re.findall(r"(?<!\d)\d{6}(?!\d)", document_text)),
        len(re.findall(r"\bадрес\w*\b", document_text, flags=re.IGNORECASE)),
    )
    return not document_has_correspondence and address_count <= 1


def _declared_nominative_plausible(value: str) -> bool:
    tokens = [token.strip(" ,.;:()[]").casefold() for token in value.split() if token.strip(" ,.;:()[]")]
    if not tokens:
        return False
    if re.search(r"(?:скому|цкому|ому|ему)$", tokens[0]):
        return False
    return sum(token.endswith(("у", "ю")) for token in tokens) < 2


def _money_unit_incomplete(field_name: str, value: str) -> bool:
    if field_name not in {"debt_amount", "interest", "penalty", "state_duty"} or "коп" in value.casefold():
        return False
    if field_name == "state_duty" and re.search(r"руб", value, re.IGNORECASE):
        return True
    if re.search(r"руб\w*\.?\s*\d{1,2}\s*$", value, flags=re.IGNORECASE):
        return True
    return field_name == "state_duty" and not re.search(r"руб", value, flags=re.IGNORECASE) and bool(re.search(r"\d", value))


def _incomplete_money_value(field_name: str, value: str) -> Decimal | None:
    match = re.search(r"(\d[\d\s]*)\s*руб\w*\.?\s*(\d{1,2})\s*$", value, flags=re.IGNORECASE)
    if match:
        rubles = re.sub(r"\s+", "", match.group(1))
        try:
            return Decimal(f"{rubles}.{match.group(2).zfill(2)}")
        except Exception:
            return None
    if field_name == "state_duty":
        numbers = re.findall(r"\d[\d\s]*", value)
        if numbers:
            try:
                return Decimal(re.sub(r"\s+", "", numbers[-1]))
            except Exception:
                return None
    return None

def _normalize_debt_period(value: str) -> str:
    matches = re.findall(r"(?<!\d)(\d{1,2}[./]\d{1,2}[./]\d{2,4})(?!\d)", value)
    if len(matches) != 2:
        return ""
    parsed = [parse_russian_date(item) for item in matches]
    if any(item is None for item in parsed):
        return ""
    return f"с {parsed[0].strftime('%d.%m.%Y')} по {parsed[1].strftime('%d.%m.%Y')}"

def _format_ok(field_name: str, value: str) -> bool:
    if not value:
        return False
    if field_name == "case_number":
        return bool(re.search(r"\d", value) and re.search(r"[/\\-]", value))
    if field_name == "uid":
        return bool(
            len(re.sub(r"\W", "", value)) >= 10
            and re.search(r"\d", value)
            and re.fullmatch(r"[0-9A-Za-zА-Яа-яЁё_/-]+", value)
        )
    if field_name == "order_date":
        return parse_russian_date(value) is not None and bool(re.search(r"\d{4}", value))
    if field_name == "debt_period":
        return bool(_normalize_debt_period(value))
    if field_name in {"debt_amount", "interest", "penalty", "state_duty", "total_amount"}:
        return money_to_decimal(value) is not None
    if field_name == "judge":
        return bool(re.fullmatch(r"[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\s*\.\s*[А-ЯЁ]\s*\.", value))
    if field_name == "debtor_full_name":
        return len(value.split()) >= 2 and not re.search(r"\d|\b(?:адрес|должник|взыскатель)\b", value, re.IGNORECASE)
    return True


def _normalized_value(field_name: str, extracted: str, proposed: str) -> str:
    if field_name == "case_number":
        return clean_case_number(extracted)
    if field_name == "uid":
        return clean_uid(extracted)
    if field_name == "order_date":
        parsed = parse_russian_date(extracted)
        return parsed.strftime("%d.%m.%Y") if parsed else ""
    if field_name == "debt_period":
        return _normalize_debt_period(extracted)
    if field_name in {"debt_amount", "interest", "penalty", "state_duty", "total_amount"}:
        return clean_money_text(extracted)
    if field_name in {"court_address", "debtor_address", "creditor_address", "creditor_legal_address", "creditor_correspondence_address"}:
        return _safe_address(extracted)
    if field_name == "proceeding_type":
        if proposed in {"gpk", "apk", "kas", "unclear"}:
            return proposed
        source = _canonical(extracted)
        return next((code for token, code in (("гпк", "gpk"), ("апк", "apk"), ("кас", "kas")) if token in source), "unclear")
    return extracted


def _document_value(record: dict[str, Any]) -> str:
    if record.get("status") not in {"confirmed", "verified", "user_confirmed"}:
        return ""
    if record.get("status") == "user_confirmed" and clean_text(record.get("user_value")):
        return clean_text(record["user_value"])
    if record.get("status") == "verified" and clean_text(record.get("verified_value")):
        return clean_text(record["verified_value"])
    field_name = record["field_name"]
    if field_name == "debtor_full_name" and clean_text(record.get("derived_value")) and not record.get("has_nominative_source"):
        return clean_text(record["derived_value"])
    return clean_text(record.get("normalized_value") or record.get("extracted_value"))


def _creditor_labeled_spans(field_name: str, ocr: TesseractOcrResult) -> list[list[OcrWord]]:
    if field_name not in {"creditor_legal_address", "creditor_correspondence_address"}:
        return []
    groups: dict[tuple[str, int, str], list[OcrWord]] = {}
    for word in ocr.words:
        block_match = re.match(r"(?:r\d+_)?p\d+_b(\d+)_p", word.line_id)
        block_id = block_match.group(1) if block_match else word.line_id
        groups.setdefault((_ocr_run_id(word), word.page, block_id), []).append(word)
    spans: list[list[OcrWord]] = []
    for run_words in groups.values():
        tokens = [re.sub(r"[^а-яёa-z0-9]+", "", word.text.casefold()) for word in run_words]
        legal_markers = [i for i in range(len(tokens) - 1) if tokens[i].startswith("юридическ") and tokens[i + 1].startswith("адрес")]
        correspondence = next((i for i in range(len(tokens) - 1) if tokens[i] == "для" and tokens[i + 1].startswith("корреспонденц")), None)
        legal = next((i for i in reversed(legal_markers) if correspondence is None or i < correspondence), None)
        if field_name == "creditor_legal_address":
            if legal is None:
                continue
            start, end = legal + 2, correspondence if correspondence is not None and correspondence > legal else len(run_words)
            for index in range(start, end):
                if tokens[index].startswith(("реквизит", "инн", "кпп", "огрн", "бик")):
                    end = index
                    break
        else:
            if correspondence is None:
                continue
            start, end = correspondence + 2, len(run_words)
            for index in range(start, len(tokens)):
                if tokens[index].startswith(("реквизит", "инн", "кпп", "огрн", "бик")) or tokens[index] in {"рс", "кс"}:
                    end = index
                    break
        hard_boundaries = ("решил", "паспорт", "должник", "взыскател", "руб", "коп", "судебн")
        end = min(end, start + 36)
        for index in range(start, end):
            if any(tokens[index].startswith(boundary) for boundary in hard_boundaries):
                end = index
                break
        span = run_words[start:end]
        while span and not re.search(r"[а-яёa-z0-9]", span[0].text, re.IGNORECASE):
            span = span[1:]
        while span and not re.search(r"[а-яёa-z0-9]", span[-1].text, re.IGNORECASE):
            span = span[:-1]
        if span:
            spans.append(span)
    return spans


def _best_labeled_span(field_name: str, ocr: TesseractOcrResult) -> tuple[list[OcrWord], dict[str, Any]]:
    spans = _creditor_labeled_spans(field_name, ocr)
    if field_name == "creditor_legal_address":
        resolutive = [span for span in spans if "в пользу взыскателя" in _canonical(_span_context(span, ocr, radius=28))]
        if resolutive:
            spans = resolutive
    if not spans:
        return [], {"kind": "program_labeled_span", "matched_runs": [], "match_count": 0, "two_run_agreement": False}
    by_value: dict[str, list[list[OcrWord]]] = {}
    for span in spans:
        address_key = re.sub(r"[^а-яёa-z0-9]+", "", _source_value(span).casefold())
        by_value.setdefault(address_key, []).append(span)
    best_group = max(
        by_value.values(),
        key=lambda group: (len({_ocr_run_id(span[0]) for span in group}), max(sum(w.confidence for w in span) / len(span) for span in group)),
    )
    best_group.sort(key=lambda span: sum(word.confidence for word in span) / len(span), reverse=True)
    runs = sorted({_ocr_run_id(span[0]) for span in best_group})
    return best_group[0], {
        "kind": "program_labeled_span", "matched_runs": runs,
        "match_count": len(spans), "two_run_agreement": len(runs) >= 2,
    }

def _match_key(value: Any) -> str:
    text = clean_text(value).casefold().replace("ё", "е")
    text = text.replace("–", "-").replace("—", "-").replace("‑", "-")
    return re.sub(r"\s+", "", text)


def _address_text_key(value: str) -> str:
    text = clean_text(value).casefold().replace("ё", "е")
    return re.sub(r"[\s.,]+", "", text)

def _ocr_run_id(word: OcrWord) -> str:
    match = re.match(r"(r\d+_)", word.word_id)
    return match.group(1) if match else "legacy"


def _find_text_spans(
    value: str, ocr: TesseractOcrResult, *, max_words: int = 32, key_func: Any = _match_key,
) -> list[list[OcrWord]]:
    target = key_func(value)
    if not target:
        return []
    groups: dict[tuple[str, int], list[OcrWord]] = {}
    for word in ocr.words:
        groups.setdefault((_ocr_run_id(word), word.page), []).append(word)
    matches: list[list[OcrWord]] = []
    target_words = max(1, len(clean_text(value).split()))
    min_length = max(1, target_words - 2)
    max_length = min(max_words, target_words + 4)
    for run_words in groups.values():
        for start in range(len(run_words)):
            for length in range(min_length, min(max_length, len(run_words) - start) + 1):
                span = run_words[start:start + length]
                if key_func(_source_value(span)) == target:
                    matches.append(span)
    return matches


def _best_text_span(
    value: str, ocr: TesseractOcrResult, *, key_func: Any = _match_key,
) -> tuple[list[OcrWord], dict[str, Any]]:
    matches = _find_text_spans(value, ocr, key_func=key_func)
    if not matches:
        return [], {"kind": "ocr_text_match", "matched_runs": [], "match_count": 0}
    matches.sort(key=lambda span: sum(word.confidence for word in span) / len(span), reverse=True)
    runs = sorted({_ocr_run_id(span[0]) for span in matches})
    return matches[0], {
        "kind": "ocr_text_match",
        "matched_runs": runs,
        "match_count": len(matches),
        "two_run_agreement": len(runs) >= 2,
    }


def _refine_program_span(field_name: str, selected: list[OcrWord]) -> tuple[list[OcrWord], str]:
    if not selected:
        return selected, ""
    if field_name == "debtor_address" and re.search(r"^зарегистрирован\w*$", selected[0].text, re.IGNORECASE):
        return selected[1:], "removed_debtor_relation_label"
    if field_name == "judge":
        separators = [index for index, word in enumerate(selected) if word.text in {"—", "–", "-"}]
        if separators and separators[-1] + 1 < len(selected):
            return selected[separators[-1] + 1:], "selected_name_after_judge_label"
    return selected, ""

def _span_context(selected: list[OcrWord], ocr: TesseractOcrResult, radius: int = 24) -> str:
    if not selected:
        return ""
    run, page = _ocr_run_id(selected[0]), selected[0].page
    run_words = [word for word in ocr.words if _ocr_run_id(word) == run and word.page == page]
    positions = {word.word_id: index for index, word in enumerate(run_words)}
    indices = [positions[word.word_id] for word in selected if word.word_id in positions]
    if not indices:
        return ""
    return _source_value(run_words[max(0, min(indices) - radius):max(indices) + radius + 1])


def validate_text_assignments(
    payload: dict[str, Any], ocr: TesseractOcrResult,
    *, verified_values: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    verified_values = verified_values or {}
    records: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    used: dict[str, str] = {}
    occurrences = _name_occurrences(payload, ocr)
    nominatives = {
        _canonical(item.get("text")) for item in occurrences
        if item.get("grammatical_case") == "nominative"
    }
    for assignment in _iter_field_assignments(payload):
        name = clean_text(assignment.get("field_name"))
        printed = clean_text(assignment.get("printed_value"))
        address_field = name in {"creditor_legal_address", "creditor_correspondence_address"}
        program_labeled = False
        if assignment.get("status") == "missing" and not printed and not address_field:
            continue
        if address_field and printed:
            selected, match_provenance = _best_text_span(printed, ocr, key_func=_address_text_key)
        else:
            selected, match_provenance = _best_text_span(printed, ocr)
        if address_field and not selected:
            selected, match_provenance = _best_labeled_span(name, ocr)
            program_labeled = bool(selected)
        selected, refinement = _refine_program_span(name, selected)
        if refinement:
            match_provenance["refinement"] = refinement
            match_provenance["llm_printed_value"] = printed
        extracted = _source_value(selected)
        crop_evidence = verified_values.get(name) or {}
        verified_value = clean_text(crop_evidence.get("value"))
        consensus_psms = {
            int(psm) for psm in (crop_evidence.get("consensus_psms") or [])
            if str(psm).isdigit()
        }
        crop_valid = bool(
            verified_value
            and len(consensus_psms) >= 2
            and _format_ok(name, verified_value)
            and not _money_unit_incomplete(name, verified_value)
        )
        reasons: list[str] = []
        if assignment.get("status") != "candidate" and not program_labeled and not crop_valid:
            reasons.append("llm_ambiguous")
        if (
            name in REQUIRED_GENERATION_FIELDS
            and selected
            and not match_provenance.get("two_run_agreement")
            and not crop_valid
        ):
            reasons.append(
                "ocr_disagreement" if match_provenance.get("match_count", 0) > 1
                else "ocr_single_run"
            )
        if not selected:
            reasons.append("source_text_not_found")
        if not program_labeled and assignment.get("semantic_role") not in FIELD_ALLOWED_SEMANTIC_ROLES[name]:
            reasons.append("semantic_role_mismatch")
        if not _format_ok(name, verified_value or extracted):
            reasons.append("format_invalid")
        context = _span_context(selected, ocr)
        subtype_context = (
            "юридический адрес"
            if name == "creditor_legal_address" and re.search(r"юридическ\w*\s+адрес", context, re.IGNORECASE)
            else "для корреспонденции"
            if name == "creditor_correspondence_address" and re.search(r"для\s+корреспонденц", context, re.IGNORECASE)
            else context
        )
        if not _address_span_ok(
            name, (
                FIELD_SEMANTIC_ROLES[name] if program_labeled
                else clean_text(assignment.get("semantic_role"))
            ), extracted, subtype_context, ocr.text,
        ):
            reasons.append("address_span_invalid")
        if _money_unit_incomplete(name, verified_value or extracted) and not crop_valid:
            reasons.append("money_unit_incomplete")
        confidence = sum(word.confidence for word in selected) / len(selected) if selected else 0.0
        threshold = 80 if name in {"case_number", "uid"} else 55
        if confidence < threshold and not crop_valid:
            reasons.append("low_ocr_confidence")
        for word in selected:
            other = used.get(word.word_id)
            if other and other != name:
                amount_roles = {"debt_amount", "state_duty", "total_amount"}
                if {other, name} <= {"case_number", "uid"} or (other in amount_roles and name in amount_roles):
                    reasons.append(f"exclusive_conflict:{other}")
                    if other in records:
                        records[other].update(status="disputed", document_value="", verification_reason="exclusive_conflict")
            used[word.word_id] = name
        derived = clean_text(assignment.get("derived_value")) if name in {"debtor_full_name", "proceeding_type"} else ""
        normalized = _normalized_value(name, verified_value or extracted, derived)
        has_nominative = name != "debtor_full_name" or (
            _canonical(extracted) in nominatives and (not derived or _canonical(derived) == _canonical(extracted))
        )
        record = {
            "field_name": name, "raw_ocr_value": extracted, "extracted_value": extracted,
            "normalized_value": normalized, "derived_value": derived,
            "verified_value": verified_value, "user_value": "", "document_value": "",
            "value_provenance": (
                {"kind": "targeted_crop_ocr", "printed_value": extracted, "crop_value": verified_value}
                if crop_valid else {"kind": "printed"}
            ), "match_provenance": match_provenance,
            "crop_provenance": crop_evidence if crop_evidence else {},
            "source_word_ids": [word.word_id for word in selected],
            "source_line_ids": list(dict.fromkeys(word.line_id for word in selected)),
            "page": selected[0].page if selected else 0,
            "bbox": list(_bbox_union([word.bbox for word in selected])),
            "confidence": round(confidence, 2),
            "alternatives": [clean_text(item) for item in (assignment.get("alternatives") or [])[:2]],
            "owner_entity_id": "", "semantic_role": (
                FIELD_SEMANTIC_ROLES[name] if program_labeled else clean_text(assignment.get("semantic_role"))
            ),
            "relation_evidence_word_ids": [], "relation_evidence_text": context,
            "relation_validation": "program_text_match", "geometry_evidence": {},
            "status": "disputed" if reasons else "confirmed",
            "verification_reason": ",".join(dict.fromkeys(reasons)) or clean_text(crop_evidence.get("reason")),
            "has_nominative_source": has_nominative,
        }
        record["document_value"] = _document_value(record)
        if name == "debtor_full_name" and record["document_value"] and record["document_value"] != extracted:
            record["value_provenance"] = {"kind": "derived_from_printed", "printed_value": extracted}
            if crop_valid:
                record["value_provenance"]["crop_value"] = verified_value
        elif name == "debt_period" and record["document_value"]:
            record["value_provenance"] = {"kind": "normalized_from_printed", "printed_value": extracted}
        records[name] = record
        issues.extend(f"{name}:{reason}" for reason in dict.fromkeys(reasons))
    return _apply_post_validation(records, issues)

def _iter_field_assignments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    assignments = payload.get("field_assignments") or {}
    if isinstance(assignments, dict):
        result = []
        for field_name in LLM_FIELD_KEYS:
            value = assignments.get(field_name)
            if isinstance(value, dict):
                result.append({**value, "field_name": field_name})
        return result
    # Backward compatibility for stored development fixtures only. The live schema is fixed-key.
    return [item for item in assignments if isinstance(item, dict)] if isinstance(assignments, list) else []

def validate_assignments(
    payload: dict[str, Any], ocr: TesseractOcrResult,
    *, verified_values: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    assignments = payload.get("field_assignments") or {}
    if isinstance(assignments, dict) and any(
        isinstance(item, dict) and "printed_value" in item for item in assignments.values()
    ):
        return validate_text_assignments(payload, ocr, verified_values=verified_values)
    words = _word_map(ocr)
    verified_values = verified_values or {}
    issues: list[str] = []
    raw_entities = [entity for entity in (payload.get("entities") or []) if isinstance(entity, dict)]
    document_roots = [entity for entity in raw_entities if entity.get("role") == "document"]
    if len(document_roots) != 1:
        issues.append("document_root_count_invalid")
    entities: dict[str, dict[str, Any]] = {}
    for entity in raw_entities:
        entity_id = clean_text(entity.get("entity_id"))
        if (
            not entity_id
            or entity.get("status", "candidate") != "candidate"
            or (entity.get("role") == "document" and len(document_roots) != 1)
        ):
            continue
        selected = _words_for(entity.get("source_word_ids"), words)
        if (
            isinstance(payload.get("field_assignments"), dict)
            and entity.get("role") == "document"
            and (not selected or len(selected) > 12)
        ):
            issues.append("document_root_span_invalid")
            continue
        if entity.get("role") != "document" and not selected:
            continue
        resolved = dict(entity)
        resolved["name"] = _source_value(selected)
        resolved["source_line_ids"] = list(dict.fromkeys(word.line_id for word in selected))
        resolved["page"] = selected[0].page if selected else 0
        resolved["bbox"] = list(_bbox_union([word.bbox for word in selected]))
        resolved["confidence"] = round(sum(word.confidence for word in selected) / len(selected), 2) if selected else 0.0
        entities[entity_id] = resolved
    occurrences = _name_occurrences(payload, ocr)
    records: dict[str, dict[str, Any]] = {}
    used: dict[str, str] = {}
    nominatives = {
        _canonical(item.get("text")) for item in occurrences
        if item.get("grammatical_case") == "nominative"
    }
    for assignment in _iter_field_assignments(payload):
        if not isinstance(assignment, dict):
            continue
        name = clean_text(assignment.get("field_name"))
        if name not in FIELD_OWNER_ROLES or name in records:
            continue
        selected = _words_for(assignment.get("source_word_ids"), words)
        relation = _words_for(assignment.get("relation_evidence_word_ids"), words)
        if assignment.get("status") == "missing" and not selected:
            continue
        owner = entities.get(clean_text(assignment.get("owner_entity_id")))
        extracted = _source_value(selected)
        crop_evidence = verified_values.get(name) or {}
        verified_value = clean_text(crop_evidence.get("value"))
        reasons: list[str] = []
        if assignment.get("status") != "candidate":
            reasons.append("llm_ambiguous")
        source_matches = bool(selected)
        crop_matches = bool(verified_value and _canonical(verified_value) == _canonical(extracted))
        if not source_matches and not crop_matches:
            reasons.append("source_mismatch")
        if owner is None or owner.get("role") not in FIELD_OWNER_ROLES[name]:
            reasons.append("owner_mismatch")
        if assignment.get("semantic_role") not in FIELD_ALLOWED_SEMANTIC_ROLES[name]:
            reasons.append("semantic_role_mismatch")
        relation_ids = {word.word_id for word in relation}
        selected_ids = {word.word_id for word in selected}
        relation_points_only_to_value = (
            name in {"court_address", "debtor_address", "creditor_address"}
            and bool(relation_ids)
            and relation_ids <= selected_ids
        )
        explicit_owner_support = (
            not relation_points_only_to_value
            and _relation_supports(owner or {}, relation)
            and not _relation_contradicts_owner(owner or {}, relation)
        )
        geometry_evidence = _spatial_owner_evidence(name, owner, selected, entities) if owner else {
            "supported": False, "same_page": False, "intervening_entity_ids": []
        }
        structural_conflict = (
            owner is not None
            and owner.get("role") != "document"
            and (
                not geometry_evidence.get("same_page")
                or bool(geometry_evidence.get("intervening_entity_ids"))
            )
        )
        owner_supported = (
            (explicit_owner_support and not structural_conflict)
            or bool(geometry_evidence.get("supported"))
        )
        relation_method = "explicit_owner_label" if explicit_owner_support else (
            "ocr_structure_geometry" if geometry_evidence.get("supported") else "invalid"
        )
        if owner and owner.get("role") == "document":
            semantic_supported, semantic_method = _document_semantic_supports(
                name, relation, selected, list(words.values()),
            )
            if semantic_supported:
                relation_method = semantic_method
        else:
            semantic_supported = owner_supported
        if not owner_supported or not semantic_supported:
            reasons.append("relation_evidence_invalid")
        if not _format_ok(name, verified_value or extracted):
            reasons.append("format_invalid")
        if not _address_span_ok(
            name, clean_text(assignment.get("semantic_role")), extracted,
            _source_value(relation), _source_value(list(words.values())),
        ):
            reasons.append("address_span_invalid")
        if _money_unit_incomplete(name, extracted):
            reasons.append("money_unit_incomplete")
        confidence = sum(word.confidence for word in selected) / len(selected) if selected else 0.0
        confidence_threshold = 80 if name in {"case_number", "uid"} else 55
        if confidence < confidence_threshold and not crop_matches:
            reasons.append("low_ocr_confidence")
        for word in selected:
            other = used.get(word.word_id)
            if other and other != name:
                exclusive_pairs = ({"case_number", "uid"}, {"order_date", "debt_contract"})
                amount_roles = {"debt_amount", "interest", "penalty", "state_duty", "total_amount"}
                incompatible = (
                    FIELD_OWNER_ROLES.get(other) != FIELD_OWNER_ROLES.get(name)
                    or any({other, name} <= pair for pair in exclusive_pairs)
                    or (other in amount_roles and name in amount_roles and other != name)
                )
                if incompatible:
                    reasons.append(f"exclusive_conflict:{other}")
                    if other in records:
                        records[other]["status"] = "disputed"
                        records[other]["verification_reason"] = "exclusive_conflict"
            used[word.word_id] = name
        raw = _source_value(selected)
        derived = clean_text(assignment.get("derived_value")) if name in {"debtor_full_name", "proceeding_type"} else ""
        normalized = _normalized_value(name, verified_value or extracted, derived)
        line_ids = list(dict.fromkeys(word.line_id for word in selected))
        record = {
            "field_name": name, "raw_ocr_value": raw, "extracted_value": extracted,
            "normalized_value": normalized, "derived_value": derived,
            "verified_value": verified_value, "user_value": "", "document_value": "",
            "value_provenance": {"kind": "printed"},
            "source_word_ids": [word.word_id for word in selected], "source_line_ids": line_ids,
            "page": selected[0].page if selected else 0, "bbox": list(_bbox_union([word.bbox for word in selected])),
            "confidence": round(confidence, 2), "alternatives": [clean_text(item) for item in (assignment.get("alternatives") or [])[:2]],
            "owner_entity_id": clean_text(assignment.get("owner_entity_id")),
            "semantic_role": clean_text(assignment.get("semantic_role")),
            "relation_evidence_word_ids": [word.word_id for word in relation],
            "relation_evidence_text": _source_value(relation),
            "relation_validation": relation_method,
            "geometry_evidence": geometry_evidence,
            "status": "disputed" if reasons else "confirmed",
            "verification_reason": ",".join(reasons) or clean_text(crop_evidence.get("reason")),
            "has_nominative_source": (
                name != "debtor_full_name"
                or (_canonical(extracted) in nominatives and (not derived or _canonical(derived) == _canonical(extracted)))
            ),
        }
        record["document_value"] = _document_value(record)
        if name == "debtor_full_name" and record["document_value"] and record["document_value"] != extracted:
            record["value_provenance"] = {
                "kind": "derived_from_printed",
                "printed_value": extracted,
            }
        elif name == "debt_period" and record["document_value"]:
            record["value_provenance"] = {
                "kind": "normalized_from_printed",
                "printed_value": extracted,
            }
        records[name] = record
        issues.extend(f"{name}:{reason}" for reason in reasons)
    return _apply_post_validation(records, issues)


def _apply_post_validation(
    records: dict[str, dict[str, Any]], issues: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    total_for_completion = records.get("total_amount")
    amount_names = [name for name in ("debt_amount", "interest", "penalty", "state_duty") if name in records]
    arithmetic_completion_reasons = {"money_unit_incomplete", "ocr_single_run", "ocr_disagreement"}
    incomplete_names = [
        name for name in amount_names
        if (
            "money_unit_incomplete" in set(clean_text(records[name].get("verification_reason")).split(","))
            and set(clean_text(records[name].get("verification_reason")).split(",")) <= arithmetic_completion_reasons
        )
    ]
    if total_for_completion and incomplete_names:
        total_value = money_to_decimal(total_for_completion.get("document_value") or total_for_completion.get("extracted_value"))
        amount_values = {
            name: (
                _incomplete_money_value(name, records[name].get("extracted_value", ""))
                if name in incomplete_names
                else money_to_decimal(records[name].get("document_value") or records[name].get("extracted_value"))
            )
            for name in amount_names
        }
        if total_value is not None and all(value is not None for value in amount_values.values()) and sum(amount_values.values()) == total_value:
            for name in incomplete_names:
                record = records[name]
                completed = format_money_rub_kop(amount_values[name])
                record.update(
                    status="confirmed",
                    normalized_value=completed,
                    derived_value=completed,
                    document_value=completed,
                    verification_reason="arithmetic_unit_completion",
                    value_provenance={
                        "kind": "calculated_unit_completion",
                        "printed_value": record.get("raw_ocr_value", ""),
                        "formula": "debt_amount + interest + penalty + state_duty = total_amount",
                    },
                )
                issues = [
                    issue for issue in issues
                    if issue not in {f"{name}:{reason}" for reason in arithmetic_completion_reasons}
                ]
    case_record, uid_record = records.get("case_number"), records.get("uid")
    if (
        case_record and uid_record
        and case_record.get("status") == "confirmed" and uid_record.get("status") == "confirmed"
        and _canonical(case_record.get("document_value")) == _canonical(uid_record.get("document_value"))
    ):
        for record in (case_record, uid_record):
            record.update(status="disputed", document_value="", verification_reason="case_uid_conflict")
        issues.extend(("case_number:case_uid_conflict", "uid:case_uid_conflict"))
    # Printed total must reconcile; failure disputes the involved critical amounts.
    amount_names = [name for name in ("debt_amount", "interest", "penalty", "state_duty") if name in records]
    total_record = records.get("total_amount")
    if total_record and total_record["status"] == "confirmed" and amount_names:
        parts = [money_to_decimal(records[name].get("document_value")) for name in amount_names]
        total = money_to_decimal(total_record.get("document_value"))
        if total is not None and all(value is not None for value in parts) and sum(parts) != total:
            total_record.update(status="disputed", document_value="", verification_reason="amount_arithmetic_mismatch")
            issues.append("total_amount:amount_arithmetic_mismatch")
    return records, issues


def _name_occurrences(payload: dict[str, Any], ocr: TesseractOcrResult | None = None) -> list[dict[str, Any]]:
    words = _word_map(ocr) if ocr is not None else {}
    result: list[dict[str, Any]] = []
    for item in payload.get("debtor_name_occurrences") or []:
        if not isinstance(item, dict):
            continue
        selected = _words_for(item.get("source_word_ids"), words) if words else []
        if not selected and ocr is not None and clean_text(item.get("printed_value")):
            selected, _ = _best_text_span(clean_text(item.get("printed_value")), ocr)
        text = _source_value(selected) if selected else clean_text(item.get("printed_value") or item.get("text"))
        if not text:
            continue
        occurrence = dict(item)
        occurrence["text"] = text
        if occurrence.get("grammatical_case") == "nominative" and not _declared_nominative_plausible(text):
            occurrence["llm_grammatical_case"] = "nominative"
            occurrence["grammatical_case"] = "other"
        occurrence["source_line_ids"] = list(dict.fromkeys(word.line_id for word in selected))
        occurrence["page"] = selected[0].page if selected else 0
        occurrence["bbox"] = list(_bbox_union([word.bbox for word in selected]))
        result.append(occurrence)
    return result


def lock_selected_tesseract_name(payload: dict[str, Any]) -> dict[str, Any]:
    """Compatibility helper: exact nominative occurrence always wins."""
    fields = dict(payload.get("fields") or {})
    selected = clean_text(payload.get("selected_name_occurrence"))
    nominatives = {clean_text(item.get("text")) for item in _name_occurrences(payload) if item.get("grammatical_case") == "nominative"}
    if selected in nominatives:
        fields["debtor_full_name"] = selected
    return fields


def _contract_ok(payload: dict[str, Any]) -> bool:
    if "field_assignments" in payload:
        assignments = payload.get("field_assignments") or {}
        text_contract = isinstance(assignments, dict) and any(
            isinstance(item, dict) and "printed_value" in item for item in assignments.values()
        )
        return bool(payload.get("is_court_order") and assignments and (text_contract or payload.get("entities")))
    fields = lock_selected_tesseract_name(payload)
    occurrences = _name_occurrences(payload)
    selected = clean_text(payload.get("selected_name_occurrence"))
    nominatives = {clean_text(item.get("text")) for item in occurrences if item.get("grammatical_case") == "nominative"}
    if payload.get("debtor_full_name_source") == "extracted":
        return bool(selected and selected in nominatives and fields.get("debtor_full_name"))
    return bool(payload.get("debtor_full_name_source") == "generated" and occurrences and not nominatives)


def normalize_tesseract_ai_data(fields: dict[str, Any]) -> dict[str, str]:
    selected = {key: clean_text(fields.get(key)) for key in ORDER_FIELD_KEYS}
    full_name = selected.get("debtor_full_name", "")
    selected["debtor_name_raw"] = selected.get("debtor_name_raw") or full_name
    selected["debtor_short_name"] = make_short_name(full_name)
    selected["_debtor_name_tesseract_locked"] = "1"
    selected["_document_values_locked"] = "1"
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", selected.get("order_date", "")):
        selected["order_date"] = ""
    return selected


def _llm_input(ocr: TesseractOcrResult) -> str:
    return json.dumps(
        {"ocr_runs": [text.splitlines() for text in ocr.raw_texts]},
        ensure_ascii=False, separators=(",", ":"),
    )


def _simple_extraction_data(payload: dict[str, Any], ocr: TesseractOcrResult) -> tuple[dict[str, Any], list[str]]:
    values = {name: clean_text(payload.get(name)) for name in SIMPLE_FIELD_KEYS}
    values["debtor_full_name"] = values.pop("debtor_name_nominative") or values["debtor_name_printed"]
    printed_name = values.pop("debtor_name_printed")
    values["debtor_name_raw"] = values["debtor_full_name"]
    values["debtor_short_name"] = make_short_name(values["debtor_full_name"])

    parsed_date = parse_russian_date(values["order_date"])
    if parsed_date:
        values["order_date"] = parsed_date.strftime("%d.%m.%Y")
    values["debt_period"] = _normalize_debt_period(values["debt_period"])
    for name in ("debt_amount", "state_duty", "total_amount"):
        amount = money_to_decimal(values[name])
        if amount is not None:
            values[name] = format_money_rub_kop(amount)

    uid = values["uid"]
    if uid and not re.fullmatch(r"\w+(?:[-/]\w+)+", uid, flags=re.UNICODE):
        values["uid"] = ""

    issues: list[str] = []
    if len(ocr.text.strip()) < 300:
        issues.append("ocr_insufficient")
    if not payload.get("is_court_order"):
        issues.append("not_court_order")
    required = (
        "court_name", "court_address", "judge", "debtor_full_name",
        "debtor_address", "creditor_name", "creditor_address", "case_number",
        "order_date", "debt_contract", "debt_period", "debt_amount",
        "state_duty", "total_amount",
    )
    issues.extend(f"missing:{name}" for name in required if not values.get(name))
    if not parsed_date:
        issues.append("format:order_date")
    amounts = {name: money_to_decimal(values[name]) for name in ("debt_amount", "state_duty", "total_amount")}
    if all(amount is not None for amount in amounts.values()):
        if amounts["debt_amount"] + amounts["state_duty"] != amounts["total_amount"]:
            issues.append("arithmetic")

    safe = not issues
    values.update({
        "_pipeline_version": PIPELINE_VERSION,
        "_pipeline_status": "ready" if safe else "technical_fail",
        "_simple_validation_errors": list(dict.fromkeys(issues)),
        "_document_kind": "court_order" if payload.get("is_court_order") else "other",
        "_document_values_locked": "1",
        "_debtor_name_tesseract_locked": "1",
        "_debtor_name_printed": printed_name,
        "_ocr_checkpoint": {"image_hash": ocr.image_hash, "pipeline_version": PIPELINE_VERSION},
    })
    return values, list(dict.fromkeys(issues))

async def extract_order_data_from_tesseract_ai(
    settings: Settings, session: AsyncSession | None, *, case_id: int | None, user_id: int | None,
    order_photo_path: str | Path, primary_candidates: dict[str, Any] | None = None,
    ocr: TesseractOcrResult | None = None,
) -> TesseractAiExtraction:
    from app.services.llm import _responses_json, record_openai_usage

    if ocr is None:
        ocr = await extract_fast_tesseract_text(order_photo_path, case_id=case_id)
    if not ocr.words:
        raise RuntimeError("Tesseract did not return readable words")
    result = await _responses_json(
        settings, instructions=SIMPLE_ORDER_INSTRUCTIONS, text=ocr.text,
        schema_name="simple_court_order_extraction", schema=SIMPLE_ORDER_SCHEMA,
        model=settings.tesseract_ai_model or settings.text_model,
        max_output_tokens=1800,
    )
    await record_openai_usage(
        settings, session, case_id=case_id, user_id=user_id, operation="tesseract_text_extraction",
        model=result.model, result=result, success=True,
    )
    flat, issues = _simple_extraction_data(result.data, ocr)
    safe = not issues
    return TesseractAiExtraction(
        data=flat, safe_to_generate=safe, issues=issues, source_fragments={},
        debtor_name_occurrences=[], debtor_full_name_source="generated",
        selected_name_occurrence="", ocr=ocr, llm_result=result,
    )


def pending_confirmation_fields(data: dict[str, Any]) -> list[dict[str, Any]]:
    if clean_text(data.get("_pipeline_version")) == PIPELINE_VERSION:
        return []
    provenance = data.get("_field_provenance") if isinstance(data.get("_field_provenance"), dict) else {}
    result = []
    for name, record in provenance.items():
        if isinstance(record, dict) and record.get("status") == "disputed" and name in CRITICAL_FIELDS:
            result.append(record)
    return sorted(result, key=lambda item: (item.get("page") or 0, (item.get("bbox") or [0, 0])[1], item.get("field_name") or ""))


def build_confirmation_crop(order_photo_path: str | Path, record: dict[str, Any], *, case_id: int | None = None) -> Path | None:
    bbox = record.get("bbox") or []
    if len(bbox) != 4 or not any(bbox):
        return None
    prepared = prepare_order_ocr_image(order_photo_path, case_id=case_id)
    if not prepared.exists():
        return None
    field_name = clean_text(record.get("field_name")) or "field"
    target = ensure_dir(Path("storage/debug") / f"case_{case_id or 'unknown'}") / f"confirm_{field_name}.jpg"
    with Image.open(prepared) as image:
        margin = max(12, int(min(image.size) * 0.015))
        left, top, right, bottom = [int(value) for value in bbox]
        crop = image.crop((max(0, left - margin), max(0, top - margin), min(image.width, right + margin), min(image.height, bottom + margin)))
        if crop.width < 1000:
            scale = min(3, max(2, 1000 // max(1, crop.width)))
            crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
        crop.convert("RGB").save(target, quality=92)
    return target

def apply_user_field_confirmation(data: dict[str, Any], field_name: str, user_value: str) -> dict[str, Any]:
    """Resolve one field without rerunning OCR/LLM or mutating its source."""
    updated = dict(data)
    provenance = {name: dict(value) for name, value in (data.get("_field_provenance") or {}).items()}
    record = provenance.get(field_name)
    if record is None:
        raise KeyError(field_name)
    record["user_value"] = clean_text(user_value)
    record["status"] = "user_confirmed"
    record["verification_reason"] = "explicit_user_confirmation"
    record["document_value"] = _document_value(record)
    provenance[field_name] = record
    updated["_field_provenance"] = provenance
    updated[field_name] = record["document_value"]
    # The shared confirmation service performs the full reducer and generation
    # validation pass before it may change the pipeline status to ready.
    updated["_pipeline_status"] = "awaiting_user_confirmation"
    return updated


def compact_statement_audit_text(data: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {clean_text(data.get(key))}" for key in ORDER_FIELD_KEYS if clean_text(data.get(key)))
