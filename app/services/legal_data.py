from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zipfile import ZipFile

from app.services.name_normalizer import (
    NameNormalizationResult,
    is_probably_not_nominative,
    make_short_name,
    normalize_person_name_from_ocr,
)
from app.utils import parse_russian_date, parse_structured_date


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
    "Дата подачи: поставить от руки",
    "Подпись: поставить от руки",
    "Считаю требования взыскателя спорными",
    "№5",
    "в городе Москва",
    "по договор №",
    "коп..",
    "руб..",
    "зарегистрированному",
    "урожен",
    "паспорт",
]

PREVIEW_IGNORED_TOKENS = {"▒"}

FIELD_LABELS = {
    "court_name": "Суд",
    "court_address": "Адрес суда",
    "debtor_full_name": "ФИО должника",
    "debtor_address": "Адрес должника",
    "creditor_name": "Взыскатель",
    "creditor_address": "Адрес взыскателя",
    "creditor_legal_address": "Юридический адрес взыскателя",
    "creditor_correspondence_address": "Адрес взыскателя для корреспонденции",
    "case_number": "Номер дела",
    "uid": "УИД",
    "order_date": "Дата приказа",
    "debt_contract": "Договор/основание долга",
    "debt_period": "Период задолженности",
    "debt_amount": "Сумма задолженности",
    "state_duty": "Госпошлина",
    "total_amount": "Итого ко взысканию",
    "received_date": "Дата получения",
    "case_number_or_uid": "Номер дела или УИД",
    "state_duty_or_total_amount": "Госпошлина или итоговая сумма",
    "render_court_addressee": "Адресат суда",
    "render_court_address": "Адрес суда",
    "render_judge_name": "Имя судьи",
    "render_debtor_full_name": "ФИО должника",
    "render_debtor_short_name": "Имя должника для подписи",
    "render_debtor_address": "Адрес должника",
    "render_creditor_name": "Взыскатель",
    "render_creditor_address": "Адрес взыскателя",
    "render_case_identifier": "Номер дела или производства",
    "render_order_date_long": "Дата судебного приказа",
    "render_court_instrumental": "Наименование суда",
    "render_order_facts_sentence": "Сведения о судебном приказе",
}

REQUIRED_FIELDS = [
    "court_name",
    "debtor_full_name",
    "creditor_name",
    "order_date",
    "debt_amount",
]


def clean_text(value: object | None) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"№\s*(\d+)", r"№ \1", text)
    # Normalize Latin OCR homoglyphs only inside common legal abbreviations.
    text = re.sub(r"\b[ОO]{3}\b", "ООО", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[ПP][КK][ОOЮ]\b", "ПКО", text, flags=re.IGNORECASE)
    text = text.strip(" \t\r\n,;")
    if text.casefold() in {"missing", "none", "null", "n/a", "unknown"}:
        return ""
    text = re.sub(r"\bMISSING\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,;")
    return text


def normalize_address_text(value: object | None) -> str:
    text = clean_text(value)
    text = re.sub(r"^(\d{6}),\s*\1,\s*", r"\1, ", text)
    text = re.sub(r"\bв\s+городе\s+Москва\b", "г. Москва", text, flags=re.IGNORECASE)
    text = re.sub(r"\bв\s+городе\s+", "г. ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bгород\s+Москва\b", "г. Москва", text, flags=re.IGNORECASE)
    text = re.sub(r"\bгород\s+", "г. ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bпо\s+улице\s+", "ул. ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bулица\s+", "ул. ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bул\.\s*([^,]+?)\s+д\.\s*", r"ул. \1, д. ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bул\.\s*([^,]+),\s*(?=\d)", r"ул. \1, д. ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bдом\s+№?\s*", "д. ", text, flags=re.IGNORECASE)
    text = re.sub(r",\s*д\.\s*", ", д. ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,.;")


ADDRESS_MARKER_RE = re.compile(
    r"(?:"
    r"\b\u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d(?:\u0430|\u043d\u043e\u043c\u0443|\u043d\u043e\u0439)?\s*(?:\u043f\u043e\s+\u0430\u0434\u0440\u0435\u0441\u0443)?"
    r"|\b\u043f\u0440\u043e\u0436\u0438\u0432\u0430\u0435\u0442\s+\u043f\u043e\s+\u0430\u0434\u0440\u0435\u0441\u0443"
    r"|\b\u043f\u0440\u043e\u0436\u0438\u0432\u0430\u044e\u0449(?:\u0438\u0439|\u0435\u043c\u0443)\s+\u043f\u043e\s+\u0430\u0434\u0440\u0435\u0441\u0443"
    r"|\b\u043c\u0435\u0441\u0442\u043e\s+\u0436\u0438\u0442\u0435\u043b\u044c\u0441\u0442\u0432\u0430"
    r"|\b\u0430\u0434\u0440\u0435\u0441\s+\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u0438"
    r")\s*[:,-]?\s*",
    flags=re.IGNORECASE,
)

DEBTOR_ADDRESS_STOP_RE = re.compile(
    r"\b(?:\u043f\u0430\u0441\u043f\u043e\u0440\u0442|\u0432\u044b\u0434\u0430\u043d|\u0443\u0444\u043c\u0441|\u043e\u0443\u0444\u043c\u0441|\u043c\u0432\u0434|\u043a\u043e\u0434\s+\u043f\u043e\u0434\u0440\u0430\u0437\u0434\u0435\u043b\u0435\u043d\u0438\u044f|\u0434\u0430\u0442\u0430\s+\u0440\u043e\u0436\u0434\u0435\u043d\u0438\u044f)\b",
    flags=re.IGNORECASE,
)

DEBTOR_ADDRESS_GARBAGE_RE = re.compile(
    r"\b(?:\u0443\u0440\u043e\u0436\u0435\u043d|\u043f\u0430\u0441\u043f\u043e\u0440\u0442|\u0432\u044b\u0434\u0430\u043d|\u0443\u0444\u043c\u0441|\u043e\u0443\u0444\u043c\u0441|\u043c\u0432\u0434|\u043a\u043e\u0434\s+\u043f\u043e\u0434\u0440\u0430\u0437\u0434\u0435\u043b\u0435\u043d\u0438\u044f|\u0434\u0430\u0442\u0430\s+\u0440\u043e\u0436\u0434\u0435\u043d\u0438\u044f)\b",
    flags=re.IGNORECASE,
)


def clean_debtor_address(value: object | None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    marker = None
    for match in ADDRESS_MARKER_RE.finditer(text):
        marker = match
    if marker:
        text = text[marker.end() :]
    stop = DEBTOR_ADDRESS_STOP_RE.search(text)
    if stop:
        text = text[: stop.start()]
    text = re.sub(r"^\s*(?:\u0430\u0434\u0440\u0435\u0441\s*)?[:,-]\s*", "", text, flags=re.IGNORECASE)
    normalized = normalize_address_text(text)
    if DEBTOR_ADDRESS_GARBAGE_RE.search(normalized) or ADDRESS_MARKER_RE.search(normalized):
        return ""
    return normalized


def keep_house_number_together(value: str) -> str:
    return re.sub(r"\bд\.\s+(?=\d)", "д.\xa0", value)
def clean_uid(value: object | None) -> str:
    text = clean_text(value)
    text = re.sub(r"^(уид|uid)\s*[:№-]?\s*", "", text, flags=re.IGNORECASE)
    return text.strip(" ,.;")


def clean_case_number(value: object | None) -> str:
    text = clean_text(value)
    text = re.sub(r"^(дело|производство|дело/производство)\s*№?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s*-\s*", "-", text)
    return text.strip(" №")


_UID_PATTERN = re.compile(r"\b\d{2}[A-ZА-ЯЁ]{2}\d{4}-\d{2}-\d{4}-\d{6}-\d{2}\b", re.IGNORECASE)
_LABELED_CASE_PATTERN = re.compile(r"(?:дело|производство)\s*№?\s*([^\s,;]+)", re.IGNORECASE)


def normalize_case_identifiers(case_number: object | None, uid: object | None) -> tuple[str, str]:
    """Separate a court case number from the long electronic UID."""
    case_text = clean_text(case_number)
    uid_text = clean_text(uid)
    combined = f"{case_text} {uid_text}".strip()
    uid_match = _UID_PATTERN.search(combined)
    labeled_case = _LABELED_CASE_PATTERN.search(combined)

    normalized_uid = clean_uid(uid_match.group(0)).replace("М", "M").replace("м", "M").replace("С", "S").replace("с", "S") if uid_match else ""
    normalized_case = clean_case_number(labeled_case.group(1)) if labeled_case else ""

    if not normalized_case:
        candidate = _UID_PATTERN.sub("", case_text).strip(" ,;-")
        candidate = clean_case_number(candidate)
        if candidate and not _UID_PATTERN.fullmatch(candidate):
            normalized_case = candidate
    if not normalized_uid and uid_text and _UID_PATTERN.fullmatch(clean_uid(uid_text)):
        normalized_uid = clean_uid(uid_text).replace("М", "M").replace("м", "M").replace("С", "S").replace("с", "S")
    elif not normalized_uid and uid_text and re.fullmatch(r"\d{18,}", clean_uid(uid_text)):
        normalized_uid = clean_uid(uid_text)
    elif (
        not normalized_uid and uid_text
        and re.search(r"[^\d]", clean_uid(uid_text))
        and re.search(r"\d", clean_uid(uid_text))
        and canonical_identifier(uid_text) != canonical_identifier(normalized_case)
    ):
        # Preserve explicitly assigned non-standard court UIDs; never rewrite
        # them into a case number merely because they miss the common pattern.
        normalized_uid = clean_uid(uid_text)
    if normalized_case and normalized_uid and canonical_identifier(normalized_case) == canonical_identifier(normalized_uid):
        normalized_uid = ""
    return normalized_case, normalized_uid


def canonical_identifier(value: object | None) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "", clean_text(value).lower())


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
    text = text.replace("рублей", "руб.").replace("рубля", "руб.").replace("рубль", "руб.")
    text = text.replace("копейки", "коп.").replace("копеек", "коп.").replace("копейка", "коп.")

    comma_rub = re.search(r"(\d[\d\s]*),(\d{1,2})\s*руб\.?", text)
    if comma_rub:
        rubles = re.sub(r"[\s.]", "", comma_rub.group(1))
        kopeks = comma_rub.group(2)
        try:
            return Decimal(f"{rubles}.{kopeks}")
        except InvalidOperation:
            return None

    # Another widespread form puts both the currency word and kopeks inside
    # the parenthetical wording: 2000 (две тысячи рублей 00 копеек).
    inside_parentheses = re.search(
        r"(\d[\d\s.]*)\s*\([^)]{0,260}?руб\.?\s*(\d{1,2})\s*коп\.?[^)]{0,40}\)",
        text,
    )
    if inside_parentheses:
        rubles = re.sub(r"[\s.]", "", inside_parentheses.group(1))
        kopeks = inside_parentheses.group(2)
        try:
            return Decimal(f"{rubles}.{kopeks}")
        except InvalidOperation:
            return None

    # Court orders frequently repeat the numeric amount in words between the
    # ruble digits and the word "рубль":
    #   120821 (сто двадцать тысяч ...) рубль 10 копеек
    # Preserve the kopeks instead of falling back to the first bare number.
    pattern = re.search(
        r"(\d[\d\s.]*)\s*(?:\([^)]{0,300}\)\s*)?руб\.?\s*(\d{1,2})?\s*коп\.?",
        text,
    )
    if pattern:
        rubles_raw = pattern.group(1)
        kopeks = pattern.group(2) or "0"
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", rubles_raw.replace(" ", "")):
            rubles = rubles_raw.replace(" ", "").replace(".", "")
        else:
            rubles = re.sub(r"[\s.]", "", rubles_raw)
        try:
            return Decimal(f"{rubles}.{kopeks}")
        except InvalidOperation:
            return None

    # Whole-ruble amounts are legally common and may omit kopeks entirely:
    # "2000 руб.", "44 600 рублей". Treat them as exactly .00.
    rubles_only = re.search(r"(\d[\d\s.]*)\s*руб\.?(?!\s*\d{1,2}\s*коп)", text)
    if rubles_only:
        rubles = re.sub(r"[\s.]", "", rubles_only.group(1))
        try:
            return Decimal(f"{rubles}.00")
        except InvalidOperation:
            return None

    # A money-looking string that could not be parsed must not silently lose
    # kopeks through the generic bare-number fallback.
    if "руб" in text or "коп" in text:
        return None

    compact = text.replace(" ", "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d{1,2})?", compact)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def parse_money(value: object | None) -> Decimal | None:
    return money_to_decimal(value)


def money_from_source_fragment(value: object | None) -> Decimal | None:
    """Read the numeric amount from an image-grounded OCR quote.

    Role-specific fragments can contain a contract number before the amount,
    so decimal-comma values near "в размере" take precedence over the first
    bare number in the string.
    """
    text = clean_text(value).lower().replace("\xa0", " ")
    if not text:
        return None
    explicit_rubles = re.findall(
        r"(\d[\d ]*)\s*(?:руб(?:л(?:ей|я|ь)?)?\.?|р\.)\s*(\d{1,2})\s*(?:коп(?:еек|ейки|ейка)?\.?)",
        text,
        flags=re.IGNORECASE,
    )
    if explicit_rubles:
        rubles, kopeks = explicit_rubles[-1]
        try:
            return Decimal(re.sub(r"\s+", "", rubles)) + Decimal(kopeks) / 100
        except InvalidOperation:
            pass
    decimal_rubles = re.findall(
        r"(\d[\d ]*[,.]\d{2})\s*(?:руб(?:л(?:ей|я|ь)?)?\.?|р\.)",
        text,
        flags=re.IGNORECASE,
    )
    if decimal_rubles:
        try:
            return Decimal(decimal_rubles[-1].replace(" ", "").replace(",", "."))
        except InvalidOperation:
            pass
    contextual = re.findall(
        r"(?:в\s+размере|в\s+сумме|сумм[аеуы])\s*[:\-]?\s*(\d[\d\s]*[,.]\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    decimal_values = contextual or re.findall(r"\d[\d\s]*[,.]\d{2}", text)
    if decimal_values:
        raw = decimal_values[-1].replace(" ", "").replace(",", ".")
        try:
            return Decimal(raw)
        except InvalidOperation:
            pass
    return money_to_decimal(text)


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


def normalize_debtor_full_name(value: object | None, context: str | None = None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    result = normalize_person_name_from_ocr(text, context)
    return result.normalized or text


def normalize_debtor_name_fields(data: dict) -> tuple[dict, NameNormalizationResult | None]:
    if clean_text(data.get("_debtor_name_tesseract_locked")) == "1":
        updated = dict(data)
        full_name = clean_text(updated.get("debtor_full_name"))
        updated["debtor_full_name"] = full_name
        updated["debtor_name_raw"] = clean_text(updated.get("debtor_name_raw")) or full_name
        updated["debtor_short_name"] = make_short_name(full_name)
        updated.pop("debtor_full_name_confidence", None)
        updated.pop("debtor_name_confidence", None)
        return updated, None
    raw = clean_text(data.get("debtor_name_raw") or data.get("debtor_full_name"))
    context = clean_text(data.get("debtor_name_context") or data.get("debtor_name_source_fragment"))
    if not raw:
        return data, None
    result = normalize_person_name_from_ocr(raw, context or None)
    updated = dict(data)
    updated["debtor_name_raw"] = raw
    if context:
        updated["debtor_name_context"] = context
    llm_name = clean_text(data.get("debtor_full_name"))
    llm_confidence = 0.0
    try:
        llm_confidence = float(data.get("debtor_full_name_confidence") or 0)
    except (TypeError, ValueError):
        llm_confidence = 0.0
    if llm_name:
        updated["debtor_full_name"] = llm_name
        result = NameNormalizationResult(
            raw=raw or llm_name,
            normalized=llm_name,
            short_name=make_short_name(llm_name),
            confidence=llm_confidence,
            warnings=["structured_llm_value"],
        )
    elif result.confidence >= 0.85:
        updated["debtor_full_name"] = result.normalized
    elif llm_name:
        updated["debtor_full_name"] = llm_name
    updated["debtor_short_name"] = make_short_name(updated.get("debtor_full_name") or result.normalized)
    updated["debtor_name_normalized_from"] = raw if result.normalized != raw else ""
    updated["debtor_name_confidence"] = str(max(result.confidence, llm_confidence))
    return updated, result


def looks_like_dative_full_name(value: object | None) -> bool:
    return is_probably_not_nominative(clean_text(value))


def suggest_nominative_full_name(value: object | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    result = normalize_person_name_from_ocr(text)
    if result.normalized and result.normalized != text:
        return result.normalized
    if result.confidence >= 0.75 and not is_probably_not_nominative(result.normalized):
        return result.normalized
    return None


def normalize_court_forms(court_name: str) -> dict[str, str]:
    court = clean_text(court_name)
    # OCR sometimes appends the postal address and web site from the same
    # header line. Those are separate facts and must not enter the addressee.
    court = re.split(r",\s*\d{6}\b", court, maxsplit=1)[0].strip(" ,")
    court = re.split(r"\s+(?:www\.?|https?://)", court, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,")
    lower = court.lower()
    base = court
    if lower.startswith("мировому судье "):
        base = court[len("мировому судье ") :].strip()
    elif lower.startswith("мировой судья "):
        base = court[len("мировой судья ") :].strip()
    elif lower.startswith("мировой суд ") and "судебн" in lower:
        base = court[len("мировой суд ") :].strip()
    elif lower.startswith("судебный участок"):
        base = re.sub(r"^судебный участок", "судебного участка", court, flags=re.IGNORECASE)
    if base.lower().startswith("судебный участок"):
        base = re.sub(r"^судебный участок", "судебного участка", base, flags=re.IGNORECASE)
    return {
        "court_name": base,
        "court_addressee": f"Мировому судье {base}" if not base.lower().startswith("мировому") else court,
        "court_instrumental": f"мировым судьей {base}",
    }


def structured_court_facts(court_name: object | None, judge: object | None = None) -> dict[str, str]:
    """Split OCR court prose into atomic facts used by templates."""
    forms = normalize_court_forms(clean_text(court_name)) if clean_text(court_name) else {}
    base = forms.get("court_name", "")
    number_match = re.search(r"(?:№|номер)\s*(\d+[\w-]*)", base, flags=re.IGNORECASE)
    unit_number = number_match.group(1) if number_match else ""
    court_type = "magistrate" if "судебн" in base.lower() and "участ" in base.lower() else "court"
    region_match = re.search(
        r"((?:[А-ЯЁA-Z][^,]{1,80}?\s+)?(?:области|область|края|край|республики|республика))\s*$",
        base,
        flags=re.IGNORECASE,
    )
    region = clean_text(region_match.group(1)) if region_match else ""
    territory = base
    territory = re.sub(r"^судебного участка\s*№?\s*\d+[\w-]*\s*", "", territory, flags=re.IGNORECASE)
    if region and territory.lower().endswith(region.lower()):
        territory = territory[: -len(region)].strip(" ,")
    return {
        "court_type": court_type,
        "court_unit_number": unit_number,
        "court_territory": clean_text(territory),
        "court_region": region,
        "judge_name": clean_text(judge),
    }


def structured_debt_basis_facts(value: object | None) -> dict[str, str]:
    """Extract agreement facts without storing a ready-made sentence."""
    text = clean_text(value)
    labeled_number = re.search(r"№\s*([A-Za-zА-Яа-яЁё]{0,8}\s*\d{3,}(?:[-/]\d+)*)", text)
    number_match = labeled_number or re.search(r"(\d{5,}(?:[-/]\d+)*)", text)
    date_match = re.search(r"\b(\d{1,2}[./]\d{1,2}[./]\d{2,4})\b", text)
    lower = text.lower()
    if "карт" in lower:
        basis_type = "credit_card_agreement"
    elif "кредит" in lower:
        basis_type = "credit_agreement"
    elif "займ" in lower:
        basis_type = "loan_agreement"
    elif text:
        basis_type = "agreement"
    else:
        basis_type = ""
    return {
        "debt_basis_type": basis_type,
        "debt_basis_number": re.sub(r"\s+", "", number_match.group(1)) if number_match else "",
        "debt_basis_date": date_match.group(1).replace("/", ".") if date_match else "",
    }


def normalize_order_data(data: dict) -> dict:
    # document_value is selected by the evidence reducer; legacy normalization
    # must not rewrite it or stringify its nested provenance.
    if clean_text((data or {}).get("_document_values_locked")) == "1":
        normalized = {
            str(key): (value if isinstance(value, (dict, list)) else clean_text(value))
            for key, value in (data or {}).items()
        }
        full_name = clean_text(normalized.get("debtor_full_name"))
        if full_name:
            normalized["debtor_name_raw"] = clean_text(normalized.get("debtor_name_raw")) or full_name
            normalized["debtor_short_name"] = make_short_name(full_name)
        court_name = clean_text(normalized.get("court_name"))
        if court_name and not normalized.get("court_addressee"):
            normalized["court_addressee"] = normalize_court_forms(court_name)["court_addressee"]
        normalized["judge_name"] = clean_text(normalized.get("judge_name") or normalized.get("judge"))
        return normalized
    normalized = {str(key): clean_text(value) for key, value in (data or {}).items()}
    case_number, uid = normalize_case_identifiers(normalized.get("case_number"), normalized.get("uid"))
    normalized["case_number"] = case_number
    normalized["uid"] = uid
    normalized, _ = normalize_debtor_name_fields(normalized)
    for key in ("court_address", "creditor_address"):
        if normalized.get(key):
            normalized[key] = normalize_address_text(normalized[key])
    court_address = normalized.get("court_address", "")
    court_name = normalized.get("court_name", "")
    if (
        court_address
        and court_address.lower() in court_name.lower()
        and not re.search(r"\b(?:ул\.|улица|д\.|дом|проспект|пер\.|шоссе)\b|\d{6}", court_address, re.IGNORECASE)
    ):
        normalized["court_address"] = ""
    if normalized.get("debtor_address"):
        normalized["debtor_address"] = clean_debtor_address(normalized["debtor_address"])
    if normalized.get("court_name"):
        court_forms = normalize_court_forms(normalized["court_name"])
        normalized["court_name"] = court_forms["court_name"]
        normalized["court_addressee"] = court_forms["court_addressee"]
        normalized["court_instrumental"] = court_forms["court_instrumental"]
        normalized.update(structured_court_facts(
            normalized["court_name"],
            normalized.get("judge_name") or normalized.get("judge"),
        ))
    if normalized.get("debt_contract"):
        derived_basis = structured_debt_basis_facts(normalized["debt_contract"])
        for key, value in derived_basis.items():
            if value and not normalized.get(key):
                normalized[key] = value
    for key in ("debt_amount", "state_duty", "total_amount"):
        if normalized.get(key):
            normalized[key] = clean_money_text(normalized[key])
    debt = money_to_decimal(normalized.get("debt_amount"))
    state_duty = money_to_decimal(normalized.get("state_duty"))
    total = money_to_decimal(normalized.get("total_amount"))
    if debt is not None and state_duty is not None and not normalized.get("total_amount"):
        normalized["total_amount"] = format_money_rub_kop(debt + state_duty)
        total = money_to_decimal(normalized.get("total_amount"))
    if debt is not None and total is not None and not normalized.get("state_duty"):
        inferred_state = (total - debt).quantize(Decimal("0.01"))
        if inferred_state > 0:
            normalized["state_duty"] = format_money_rub_kop(inferred_state)
    for key in ("order_date",):
        parsed = parse_structured_date(normalized.get(key))
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
    render = normalized.get("render")
    if isinstance(render, dict):
        required_render = (
            "court_addressee", "court_address", "judge_name", "debtor_full_name",
            "debtor_short_name", "debtor_address", "creditor_name", "creditor_address",
            "case_identifier", "order_date_long", "court_instrumental",
            "order_facts_sentence",
        )
        missing = [f"render_{key}" for key in required_render if not clean_text(render.get(key))]
        if not received_date:
            missing.append("received_date")
        return missing
    missing = [key for key in REQUIRED_FIELDS if not normalized.get(key)]
    if not (normalized.get("case_number") or normalized.get("uid")):
        missing.append("case_number_or_uid")
    if not (normalized.get("state_duty") or normalized.get("total_amount")):
        missing.append("state_duty_or_total_amount")
    if not received_date:
        missing.append("received_date")
    return missing


def has_old_statement_title(text: str) -> bool:
    lower = text.lower()
    if "возражения" in lower:
        return False
    return bool(re.search(r"(?m)^\s*заявление\s+об\s+отмене", lower))


def bad_tokens_in_text(text: str) -> list[str]:
    lower = text.lower()
    found: list[str] = []
    if has_old_statement_title(text):
        found.append("old_statement_title")
    for token in BAD_DOCUMENT_TOKENS:
        if token.lower() in lower:
            found.append(token)
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        found.append("iso_date")
    if re.search(r"Копия судебного приказа от\s+\d{1,2}\.\d{1,2}\.\d{4}", text, re.IGNORECASE):
        found.append("numeric_attachment_date")
    if re.search(r"Мировому судье\s+(?:Мировой судья|Судебный участок)", text, re.IGNORECASE):
        found.append("duplicated_court_addressee")
    return found


OUTPUT_DOCUMENT_ONLY_BAD_TOKENS = {
    "iso_date",
    "numeric_attachment_date",
    "duplicated_court_addressee",
}


def bad_tokens_in_structured_text(text: str) -> list[str]:
    """Structured data permits source prose; only objective sentinels block."""
    found: list[str] = []
    for name, pattern in (
        ("MISSING", r"\bMISSING\b"),
        ("None", r"\bNone\b"),
        ("null", r"\bnull\b"),
        ("template_braces", r"\{\{|\}\}"),
        ("question_placeholder", r"\?{4,}"),
    ):
        if re.search(pattern, text, re.IGNORECASE):
            found.append(name)
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


VALIDATION_SKIP_KEYS = {
    "debtor_name_raw",
    "debtor_name_context",
    "debtor_name_source_fragment",
    "debtor_name_normalized_from",
    "debtor_name_confidence",
    "debtor_short_name",
    "court_addressee",
    "court_instrumental",
    "restore_reason",
    "court_type",
    "court_unit_number",
    "court_territory",
    "court_region",
    "judge_name",
    "debt_basis_type",
    "debt_basis_number",
    "debt_basis_date",
}


@dataclass
class AmountValidationResult:
    ok: bool
    debt_amount: Decimal | None = None
    state_duty: Decimal | None = None
    total_amount: Decimal | None = None
    computed_total: Decimal | None = None
    errors: list[str] = field(default_factory=list)


def validate_amounts(data: dict) -> AmountValidationResult:
    normalized = normalize_order_data(data)
    debt = money_to_decimal(normalized.get("debt_amount"))
    interest = money_to_decimal(normalized.get("interest"))
    penalty = money_to_decimal(normalized.get("penalty"))
    state_duty = money_to_decimal(normalized.get("state_duty"))
    total = money_to_decimal(normalized.get("total_amount"))
    errors: list[str] = []
    if debt is None and normalized.get("debt_amount"):
        errors.append("debt_amount: не удалось распознать сумму долга")
    if state_duty is None and normalized.get("state_duty"):
        errors.append("state_duty: не удалось распознать госпошлину")
    if total is None and normalized.get("total_amount"):
        errors.append("total_amount: не удалось распознать итоговую сумму")
    computed_total = None
    if debt is not None and state_duty is not None:
        computed_total = (
            debt + (interest or Decimal("0")) + (penalty or Decimal("0")) + state_duty
        ).quantize(Decimal("0.01"))
        if total is not None and abs(total - computed_total) > Decimal("0.01"):
            errors.append("amount_mismatch")
    return AmountValidationResult(
        ok=not errors,
        debt_amount=debt,
        state_duty=state_duty,
        total_amount=total,
        computed_total=computed_total,
        errors=errors,
    )


def validate_before_generation(data: dict, received_date: date | None) -> ValidationResult:
    missing = missing_order_fields(data, received_date)
    if clean_text((data or {}).get("_document_values_locked")) == "1":
        provenance = data.get("_field_provenance") if isinstance(data.get("_field_provenance"), dict) else {}
        blocking_fields = {
            "court_name", "court_address", "judge", "debtor_full_name", "debtor_address",
            "creditor_name", "creditor_legal_address", "creditor_correspondence_address",
            "case_number", "uid", "order_date", "debt_contract",
            "debt_amount", "state_duty", "total_amount",
        }
        disputed = [
            FIELD_LABELS.get(name, name) for name, record in provenance.items()
            if name in blocking_fields and isinstance(record, dict) and record.get("status") == "disputed"
        ]
        if disputed:
            missing = list(dict.fromkeys([*missing, *disputed, "Подтверждение спорных полей"]))
    bad = []
    for key, value in normalize_order_data(data).items():
        if key in VALIDATION_SKIP_KEYS or key.startswith("_"):
            continue
        found = bad_tokens_in_structured_text(f"{key}: {value}")
        if clean_text((data or {}).get("_document_values_locked")) == "1" and key in {"court_address", "debtor_address", "creditor_address"}:
            found = [token for token in found if token != "в городе Москва"]
        bad.extend(found)
    return ValidationResult(ok=not missing and not bad, missing=missing, bad_tokens=sorted(set(bad)))
