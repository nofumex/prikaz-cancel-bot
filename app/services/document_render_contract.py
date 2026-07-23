from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Callable

from app.services.legal_data import clean_case_number, clean_uid, clean_text, money_to_decimal
from app.utils import parse_structured_date


MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def date_long_text(raw: str | date) -> str:
    parsed = raw if isinstance(raw, date) else parse_structured_date(raw)
    if not parsed:
        return str(raw)
    return f"{parsed.day} {MONTHS_GENITIVE[parsed.month - 1]} {parsed.year} года"


def dates_long_in_text(raw: str) -> str:
    return re.sub(
        r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4})\b",
        lambda match: date_long_text(match.group(0)),
        raw,
    )


@dataclass(frozen=True, slots=True)
class RenderFieldSpec:
    name: str
    source_fields: tuple[str, ...]
    required: bool
    strategy: str
    intentionally_not_rendered: str = ""


RENDER_CONTRACT: tuple[RenderFieldSpec, ...] = (
    RenderFieldSpec("court_addressee", ("court_addressee", "court_name"), True, "text"),
    RenderFieldSpec("court_address", ("court_address",), False, "address"),
    RenderFieldSpec("judge_name", ("judge_name", "judge"), False, "person"),
    RenderFieldSpec("debtor_full_name", ("debtor_full_name",), True, "person"),
    RenderFieldSpec("debtor_address", ("debtor_address",), False, "address"),
    RenderFieldSpec("creditor_name", ("creditor_name",), True, "organization"),
    RenderFieldSpec(
        "creditor_render_address",
        ("creditor_legal_address", "creditor_correspondence_address", "creditor_address"),
        False,
        "address",
    ),
    RenderFieldSpec("case_number", ("case_number",), False, "case_number"),
    RenderFieldSpec("uid", ("uid",), False, "uid"),
    RenderFieldSpec("order_date", ("order_date",), True, "date"),
    RenderFieldSpec("debt_basis_type", ("debt_basis_type",), False, "debt_basis"),
    RenderFieldSpec("debt_basis_number", ("debt_basis_number",), False, "identifier"),
    RenderFieldSpec("debt_basis_date", ("debt_basis_date",), False, "date"),
    RenderFieldSpec("debt_contract", ("debt_contract",), False, "debt_contract"),
    RenderFieldSpec("debt_period", ("debt_period",), False, "date_text"),
    RenderFieldSpec("debt_amount", ("debt_amount",), True, "money"),
    RenderFieldSpec("interest", ("interest",), False, "money"),
    RenderFieldSpec("penalty", ("penalty",), False, "money"),
    RenderFieldSpec("state_duty", ("state_duty",), False, "money"),
    RenderFieldSpec(
        "total_amount",
        ("total_amount",),
        False,
        "money",
        intentionally_not_rendered="Rendered only when amount_render_mode=explicit_total",
    ),
    RenderFieldSpec("received_date", ("received_date",), True, "date"),
    RenderFieldSpec("deadline_date", ("deadline_date",), False, "date"),
    RenderFieldSpec("restore_reason", ("restore_reason",), False, "text"),
)

INTENTIONALLY_NOT_RENDERED_FIELDS: dict[str, str] = {
    "debtor_name_raw": "source provenance only",
    "debtor_name_printed": "source provenance only",
    "debtor_name_nominative": "compatibility source field; debtor_full_name is rendered",
    "debtor_name_context": "source provenance only",
    "debtor_name_source_fragment": "source provenance only",
    "debtor_birth_date": "sensitive source fact not needed in objections",
    "debtor_passport": "sensitive source fact forbidden in generated statement",
    "creditor_inn": "bank/tax requisites are not needed in the statement header",
    "creditor_ogrn": "bank/tax requisites are not needed in the statement header",
    "proceeding_type": "controls extraction classification, not statement wording",
}


def select_creditor_address_for_render(data: dict) -> tuple[str, str]:
    """The header uses the legal address; correspondence and legacy are fallbacks."""
    for field in ("creditor_legal_address", "creditor_correspondence_address", "creditor_address"):
        value = clean_text(data.get(field))
        if value:
            value = re.split(r"\s*;\s*", value, maxsplit=1)[0]
            value = re.split(
                r"\b(?:реквизиты|инн|кпп|огрн|бик)\b",
                value,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" ,;")
            return field, value
    return "", ""


_ADDRESS_PREFIX_RE = re.compile(
    r"^(?:зарегистрирован(?:ному|ной|а)?(?:\s+по\s+адресу)?|"
    r"юридический\s+адрес|адрес\s+для\s+корреспонденции|"
    r"расположенн(?:ому|ой)?\s+по\s+адресу)\s*[:,-]?\s*",
    re.IGNORECASE,
)
_ADDRESS_TAIL_RE = re.compile(
    r"\b(?:реквизиты|паспорт|инн|кпп|огрн|бик|корр?\.?\s*сч[её]т|"
    r"расч[её]тн(?:ый|ого)\s+сч[её]т)\b",
    re.IGNORECASE,
)


def _base_canonical(value: object | None) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value)).casefold().replace("ё", "е")
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    text = text.replace("«", '"').replace("»", '"').replace("„", '"')
    return text


def canonicalize_address_for_qa(value: object | None) -> str:
    text = _base_canonical(value)
    text = _ADDRESS_PREFIX_RE.sub("", text)
    tail = _ADDRESS_TAIL_RE.search(text)
    if tail:
        text = text[: tail.start()]
    replacements = (
        (r"\bв\s+городе\b|\bгород\b|\bг\.", "г"),
        (r"\bулица\b|\bул\.", "ул"),
        (r"\bдом\s*№?|\bд\.", "д"),
        (r"\bкорпус\b|\bкорп\.", "корп"),
        (r"\bстроение\b|\bстр\.", "стр"),
        (r"\bквартира\b|\bкв\.", "кв"),
        (r"\bкомната\b|\bкомн\.", "комн"),
        (r"\bофис\b|\bоф\.", "оф"),
        (r"\bобласть\b|\bобл\.", "обл"),
        (r"\bреспублика\b|\bресп\.", "респ"),
        (r"\bавтономный\s+округ\b|\bао\b", "ао"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"№\s*", "", text)
    text = re.sub(r"[^0-9a-zа-я-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonicalize_entity_for_qa(value: object | None) -> str:
    text = _base_canonical(value)
    text = re.sub(r"\s*\.\s*", ".", text)
    text = re.sub(r"№\s*", "", text)
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def address_matches_rendered(expected: str, rendered_text: str) -> bool:
    canonical_expected = canonicalize_address_for_qa(expected)
    canonical_rendered = canonicalize_address_for_qa(rendered_text)
    return bool(canonical_expected and canonical_expected in canonical_rendered)


def entity_matches_rendered(expected: str, rendered_text: str) -> bool:
    canonical_expected = canonicalize_entity_for_qa(expected)
    canonical_rendered = canonicalize_entity_for_qa(rendered_text)
    return bool(canonical_expected and canonical_expected in canonical_rendered)


def identifier_matches_rendered(expected: str, rendered_text: str, cleaner: Callable[[object], str]) -> bool:
    canonical_expected = canonicalize_entity_for_qa(cleaner(expected))
    return bool(canonical_expected and canonical_expected in canonicalize_entity_for_qa(rendered_text))


def money_matches_rendered(expected: str, rendered_text: str) -> bool:
    amount = money_to_decimal(expected)
    if amount is None:
        return False
    rendered_amounts = {
        money_to_decimal(match.group(0))
        for match in re.finditer(r"\d[\d \u00a0]*\s*руб\.?\s*\d{1,2}\s*коп\.?", rendered_text, re.IGNORECASE)
    }
    return amount in rendered_amounts


def selected_source_value(spec: RenderFieldSpec, data: dict, *, received_date: date | None = None,
                          deadline_date: date | None = None, restore_reason: str | None = None) -> tuple[str, str]:
    if spec.name == "creditor_render_address":
        return select_creditor_address_for_render(data)
    context_values = {
        "received_date": received_date,
        "deadline_date": deadline_date,
        "restore_reason": restore_reason,
    }
    if spec.name in context_values:
        value = context_values[spec.name]
        return spec.name, clean_text(value)
    for field in spec.source_fields:
        value = clean_text(data.get(field))
        if value:
            return field, value
    return spec.source_fields[0], ""


def clean_contract_identifier(name: str, value: str) -> str:
    if name == "case_number":
        return clean_case_number(value)
    if name == "uid":
        return clean_uid(value)
    return value
