from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zipfile import ZipFile

from app.utils import parse_russian_date


BAD_DOCUMENT_TOKENS = [
    "________________",
    "уточнить",
    "не найдено",
    "____.__.20__",
    "MISSING",
    "None",
    "null",
    "????",
    "{{",
    "}}",
    "▒",
    "Бельскому Владимиру Геннадьевичу",
    "Бельскому В.Г.",
]

PREVIEW_IGNORED_TOKENS = {"▒"}

FIELD_LABELS = {
    "court_name": "Суд",
    "court_address": "Адрес суда",
    "debtor_full_name": "ФИО должника",
    "debtor_address": "Адрес должника",
    "creditor_name": "Взыскатель",
    "case_number": "Номер дела",
    "order_date": "Дата приказа",
    "debt_contract": "Договор/основание долга",
    "debt_period": "Период задолженности",
    "debt_amount": "Сумма задолженности",
    "state_duty": "Госпошлина",
    "total_amount": "Итого ко взысканию",
    "received_date": "Дата получения",
    "debtor_full_name:dative": "ФИО должника в именительном падеже",
}

REQUIRED_FIELDS = [
    "court_name",
    "court_address",
    "debtor_full_name",
    "debtor_address",
    "creditor_name",
    "case_number",
    "order_date",
    "debt_contract",
    "debt_period",
    "debt_amount",
]


def clean_text(value: object | None) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n,;")


def clean_uid(value: object | None) -> str:
    text = clean_text(value)
    text = re.sub(r"^(уид|uid)\s*[:№-]?\s*", "", text, flags=re.IGNORECASE)
    return text.strip(" ,.;")


def clean_case_number(value: object | None) -> str:
    text = clean_text(value)
    text = re.sub(r"^(дело|производство|дело/производство)\s*№?\s*", "", text, flags=re.IGNORECASE)
    return text.strip(" №")


def clean_money_text(value: object | None) -> str:
    decimal_value = money_to_decimal(value)
    if decimal_value is None:
        text = clean_text(value)
        text = text.replace("рублей", "руб.").replace("рубля", "руб.").replace("рубль", "руб.")
        text = text.replace("копейки", "коп.").replace("копеек", "коп.").replace("копейка", "коп.")
        return re.sub(r"\s+", " ", text)
    return format_money_rub_kop(decimal_value)


def money_to_decimal(value: object | None) -> Decimal | None:
    text = clean_text(value)
    if not text:
        return None
    text = text.lower().replace("\xa0", " ")
    pattern = re.search(r"(\d[\d\s]*)\s*руб\.?\s*(\d{1,2})?\s*коп\.?", text)
    if pattern:
        rubles = re.sub(r"\s+", "", pattern.group(1))
        kopeks = pattern.group(2) or "0"
        text = f"{rubles}.{kopeks}"
    else:
        text = text.replace(" ", "").replace(",", ".")
        match = re.search(r"\d+(?:\.\d{1,2})?", text)
        if not match:
            return None
        text = match.group(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def format_money_rub_kop(value: Decimal | int | float | str | None) -> str:
    if value is None:
        return ""
    decimal_value = value if isinstance(value, Decimal) else money_to_decimal(value)
    if decimal_value is None:
        return clean_text(value)
    decimal_value = decimal_value.quantize(Decimal("0.01"))
    rubles = int(decimal_value)
    kopeks = int((decimal_value - Decimal(rubles)) * 100)
    return f"{rubles:,}".replace(",", " ") + f" руб. {kopeks:02d} коп."


def normalize_debtor_full_name(value: object | None) -> str:
    text = clean_text(value)
    text = re.sub(r"\s+", " ", text)
    return text


def looks_like_dative_full_name(value: object | None) -> bool:
    text = clean_text(value)
    if not text:
        return False
    parts = [part for part in text.split() if part]
    if len(parts) < 2:
        return False
    suspicious_suffixes = ("ому", "ему", "ой", "евне", "овичу", "ичу", "ю", "у")
    suspicious = 0
    for part in parts:
        lower = part.lower().strip(".,")
        if lower.endswith(suspicious_suffixes):
            suspicious += 1
    return suspicious >= 2 or (suspicious >= 1 and len(parts) >= 3)


def suggest_nominative_full_name(value: object | None) -> str | None:
    text = clean_text(value)
    parts = [part for part in text.split() if part]
    if len(parts) < 3:
        return None

    def _fix_surname(token: str) -> str:
        lower = token.lower().strip(".,")
        if lower.endswith(("ому", "ему")):
            stem = token[:-3]
            if stem.lower().endswith(("ск", "цк", "зьк", "шк", "чк")):
                return stem + "ий"
            return stem + "ый"
        if lower.endswith(("ову", "еву")):
            return token[:-1]
        if lower.endswith(("иному", "еному")):
            return token[:-5] + "ин" if lower.endswith("иному") else token[:-5] + "ен"
        return token

    def _fix_name(token: str) -> str:
        lower = token.lower().strip(".,")
        if lower.endswith(("у", "ю")):
            return token[:-1]
        return token

    surname = _fix_surname(parts[0])
    name = _fix_name(parts[1])
    patronymic = _fix_name(parts[2])
    result = " ".join([surname, name, patronymic]).strip()
    return result if result and result != text else None


def normalize_order_data(data: dict) -> dict:
    normalized = {str(key): clean_text(value) for key, value in (data or {}).items()}
    if normalized.get("uid"):
        normalized["uid"] = clean_uid(normalized["uid"])
    if normalized.get("case_number"):
        normalized["case_number"] = clean_case_number(normalized["case_number"])
    if normalized.get("debtor_full_name"):
        normalized["debtor_full_name"] = normalize_debtor_full_name(normalized["debtor_full_name"])
    for key in ("debt_amount", "state_duty", "total_amount"):
        if normalized.get(key):
            normalized[key] = clean_money_text(normalized[key])
    if normalized.get("debt_amount") and normalized.get("state_duty") and not normalized.get("total_amount"):
        debt = money_to_decimal(normalized["debt_amount"])
        state_duty = money_to_decimal(normalized["state_duty"])
        if debt is not None and state_duty is not None:
            normalized["total_amount"] = format_money_rub_kop(debt + state_duty)
    for key in ("order_date",):
        parsed = parse_russian_date(normalized.get(key))
        if parsed:
            normalized[key] = parsed.strftime("%d.%m.%Y")
    return normalized


def russian_non_working_dates(year: int) -> set[date]:
    try:
        import holidays  # type: ignore

        return {day for day in holidays.RU(years=year)}
    except Exception:
        fixed = {
            (1, 1),
            (1, 2),
            (1, 3),
            (1, 4),
            (1, 5),
            (1, 6),
            (1, 7),
            (1, 8),
            (2, 23),
            (3, 8),
            (5, 1),
            (5, 9),
            (6, 12),
            (11, 4),
        }
        return {date(year, month, day) for month, day in fixed}


def is_non_working_day(day: date) -> bool:
    return day.weekday() >= 5 or day in russian_non_working_dates(day.year)


def legal_deadline_from_received(received: date) -> date:
    # The term starts on the next calendar day; the 10th day is received + 10.
    deadline = received + timedelta(days=10)
    while is_non_working_day(deadline):
        deadline += timedelta(days=1)
    return deadline


def is_deadline_missed(deadline: date | None, today: date | None = None) -> bool:
    if not deadline:
        return False
    today = today or date.today()
    return today > deadline


def missing_order_fields(data: dict, received_date: date | None = None) -> list[str]:
    normalized = normalize_order_data(data)
    missing = [key for key in REQUIRED_FIELDS if not normalized.get(key)]
    if not received_date:
        missing.append("received_date")
    return missing


def bad_tokens_in_text(text: str) -> list[str]:
    lower = text.lower()
    found: list[str] = []
    for token in BAD_DOCUMENT_TOKENS:
        needle = token if token != token.lower() else token.lower()
        haystack = text if token != token.lower() else lower
        if needle in haystack:
            found.append(token)
    return found


def bad_tokens_in_preview_text(text: str) -> list[str]:
    return [token for token in bad_tokens_in_text(text) if token not in PREVIEW_IGNORED_TOKENS]


def docx_text(path: str) -> str:
    with ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    text = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"</w:p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def validate_docx_clean(path: str) -> list[str]:
    return bad_tokens_in_text(docx_text(path))


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    missing: list[str]
    bad_tokens: list[str]


def validate_before_generation(data: dict, received_date: date | None) -> ValidationResult:
    missing = missing_order_fields(data, received_date)
    bad = []
    for key, value in normalize_order_data(data).items():
        found = bad_tokens_in_text(f"{key}: {value}")
        bad.extend(found)
    debtor = normalize_order_data(data).get("debtor_full_name", "")
    if looks_like_dative_full_name(debtor):
        bad.append("debtor_full_name:dative")
    return ValidationResult(ok=not missing and not bad, missing=missing, bad_tokens=sorted(set(bad)))
