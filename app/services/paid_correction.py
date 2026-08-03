from __future__ import annotations

import json
import re

from app.services.crm_background import schedule_crm_sync
from app.services.documents import create_case_documents_reviewed
from app.services.document_templates.statement_templates import (
    normalize_court_addressee,
    normalize_court_instrumental,
)
from app.services.legal_data import (
    clean_money_text,
    clean_text,
    is_deadline_missed,
    normalize_order_data,
    structured_court_facts,
    structured_debt_basis_facts,
    validate_amounts,
)
from app.services.name_normalizer import make_short_name
from app.utils import parse_structured_date


PAID_EDITABLE_FIELDS = {
    "court_name", "court_address", "judge_name", "debtor_full_name", "debtor_address",
    "creditor_name", "creditor_address", "creditor_legal_address",
    "creditor_correspondence_address", "case_number", "order_date", "uid",
    "debt_contract", "debt_period", "debt_amount", "interest", "penalty",
    "state_duty", "total_amount", "received_date",
}

DERIVED_FIELDS = {
    "render", "court_addressee", "court_instrumental", "debtor_short_name",
    "creditor_name_genitive", "court_type", "court_unit_number", "court_territory",
    "court_region", "debt_basis_type", "debt_basis_number", "debt_basis_date",
}

_MONEY_IN_FACTS = r"\d[\d \u00a0]*(?:[.,]\d{1,2})?\s*(?:руб(?:\.|лей|ля)?\s*\d{0,2}\s*коп\.?)"


def _json_object(raw: object) -> dict:
    try:
        value = json.loads(raw or "{}") if not isinstance(raw, dict) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def corrected_fields(case) -> set[str]:
    try:
        value = json.loads(getattr(case, "paid_corrected_fields_json", None) or "[]")
        return {str(item) for item in value} if isinstance(value, list) else set()
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()


def manual_corrections(case) -> dict[str, str]:
    stored = _json_object(getattr(case, "paid_corrections_json", None))
    result = {
        key: clean_text(value)
        for key, value in stored.items()
        if key in PAID_EDITABLE_FIELDS
    }
    # Legacy cases only stored corrected field names. Recover their values from
    # extracted_json without mutating either legacy column.
    source = _json_object(getattr(case, "extracted_json", None))
    for field in corrected_fields(case):
        if field not in result and field in PAID_EDITABLE_FIELDS and field != "received_date" and field in source:
            result[field] = clean_text(source[field])
    return result


def correction_allowed(case, field: str) -> bool:
    del case
    return field in PAID_EDITABLE_FIELDS


def record_corrected_field(case, field: str) -> None:
    fields = corrected_fields(case)
    fields.add(field)
    case.paid_corrected_fields_json = json.dumps(sorted(fields), ensure_ascii=False)


def _strip_derived(data: dict) -> dict:
    return {
        key: value
        for key, value in data.items()
        if key not in DERIVED_FIELDS and not str(key).startswith("render_")
    }


def _promote_legacy_render_sources(data: dict) -> dict:
    """Recover source facts from old render-only payloads before discarding render."""
    promoted = dict(data)
    nested = promoted.get("render") if isinstance(promoted.get("render"), dict) else {}

    def rendered(name: str) -> str:
        return clean_text(nested.get(name) or promoted.get(f"render_{name}"))

    mapping = {
        "court_name": "court_addressee",
        "court_address": "court_address",
        "judge_name": "judge_name",
        "debtor_full_name": "debtor_full_name",
        "debtor_address": "debtor_address",
        "creditor_name": "creditor_name",
        "creditor_address": "creditor_address",
    }
    for target, render_name in mapping.items():
        if not clean_text(promoted.get(target)) and rendered(render_name):
            promoted[target] = rendered(render_name)

    if not clean_text(promoted.get("order_date")):
        parsed = parse_structured_date(rendered("order_date_long"))
        if parsed:
            promoted["order_date"] = parsed.strftime("%d.%m.%Y")

    identifier = rendered("case_identifier")
    if identifier:
        if not clean_text(promoted.get("uid")):
            uid_match = re.search(r"УИД\s*[:№]?\s*([^,;]+)", identifier, re.IGNORECASE)
            if uid_match:
                promoted["uid"] = uid_match.group(1).strip()
        if not clean_text(promoted.get("case_number")):
            case_match = re.search(r"№\s*([^,;]+)", identifier)
            if case_match:
                promoted["case_number"] = case_match.group(1).strip()

    facts = rendered("order_facts_sentence")
    if facts:
        amount_patterns = {
            "interest": rf"процент\w*\s+в размере\s+({_MONEY_IN_FACTS})",
            "penalty": rf"(?:пен\w*|неустойк\w*)\s+в размере\s+({_MONEY_IN_FACTS})",
            "state_duty": rf"(?:госпошлин\w*|государственн\w+\s+пошлин\w*)\s+в размере\s+({_MONEY_IN_FACTS})",
            "total_amount": rf"(?:общая сумма взыскания составляет|итого[^\d]*)\s*({_MONEY_IN_FACTS})",
        }
        debt_match = re.search(
            rf"задолженност\w*.*?\s+в размере\s+({_MONEY_IN_FACTS})",
            facts,
            re.IGNORECASE,
        )
        if debt_match and not clean_text(promoted.get("debt_amount")):
            promoted["debt_amount"] = clean_money_text(debt_match.group(1))
        for field, pattern in amount_patterns.items():
            if clean_text(promoted.get(field)):
                continue
            match = re.search(pattern, facts, re.IGNORECASE)
            if match:
                promoted[field] = clean_money_text(match.group(1))

        period_match = re.search(
            r"за период\s+(.+?)(?=\s+в размере)", facts, re.IGNORECASE
        )
        if period_match and not clean_text(promoted.get("debt_period")):
            promoted["debt_period"] = period_match.group(1).strip(" ,.;")
        contract_match = re.search(
            r"задолженност\w*\s+по\s+(.+?)(?=\s+за период|\s+в размере)",
            facts,
            re.IGNORECASE,
        )
        if contract_match and not clean_text(promoted.get("debt_contract")):
            promoted["debt_contract"] = contract_match.group(1).strip(" ,.;")
    return promoted


def _rebuild_derived(data: dict) -> dict:
    """Deterministically rebuild non-editable values from final source facts."""
    rebuilt = _strip_derived(data)
    full_name = clean_text(rebuilt.get("debtor_full_name"))
    if full_name:
        rebuilt["debtor_short_name"] = make_short_name(full_name)

    court_name = clean_text(rebuilt.get("court_name"))
    if court_name:
        rebuilt["court_addressee"] = normalize_court_addressee(court_name)
        rebuilt["court_instrumental"] = normalize_court_instrumental(court_name)
        rebuilt.update(structured_court_facts(
            court_name, rebuilt.get("judge_name") or rebuilt.get("judge")
        ))

    contract = clean_text(rebuilt.get("debt_contract"))
    if contract:
        rebuilt.update(structured_debt_basis_facts(contract))
    return rebuilt


def prepare_paid_case_data(case) -> dict:
    """Overlay all manual edits and remove every stale render representation."""
    overrides = manual_corrections(case)
    legacy = _promote_legacy_render_sources(_json_object(getattr(case, "extracted_json", None)))
    merged = _strip_derived(legacy)
    merged.update({key: value for key, value in overrides.items() if key != "received_date"})
    normalized = normalize_order_data(merged)
    # Explicit input has higher priority than formatting, inference and OCR.
    normalized.update({key: value for key, value in overrides.items() if key != "received_date"})
    if "total_amount" in overrides:
        normalized["amount_render_mode"] = "explicit_total"
    rebuilt = _rebuild_derived(normalized)
    provenance = rebuilt.get("_field_provenance")
    provenance = dict(provenance) if isinstance(provenance, dict) else {}
    for key, value in overrides.items():
        if key == "received_date":
            continue
        old_record = provenance.get(key)
        record = dict(old_record) if isinstance(old_record, dict) else {}
        record.update({
            "value": value,
            "status": "confirmed",
            "verification_reason": "manual_user_correction",
        })
        provenance[key] = record
    if provenance:
        rebuilt["_field_provenance"] = provenance
    # The renderer normalizes once more. Locking the already-normalized final
    # facts keeps explicit user text authoritative during that second pass.
    rebuilt["_document_values_locked"] = "1"
    return rebuilt


def apply_paid_correction(case, field: str, value: str) -> dict:
    if field not in PAID_EDITABLE_FIELDS or field == "received_date":
        raise ValueError("Поле нельзя редактировать напрямую.")
    overrides = manual_corrections(case)
    overrides[field] = clean_text(value)
    if field == "creditor_address":
        # The generic legacy address is the address the user expects to see in
        # the statement. Keep the higher-priority legal source in sync.
        overrides["creditor_legal_address"] = clean_text(value)
    case.paid_corrections_json = json.dumps(overrides, ensure_ascii=False, sort_keys=True)
    record_corrected_field(case, field)
    data = prepare_paid_case_data(case)
    case.extracted_json = json.dumps(data, ensure_ascii=False)
    return data


def record_paid_received_date(case) -> None:
    overrides = manual_corrections(case)
    overrides["received_date"] = case.received_date.strftime("%d.%m.%Y")
    case.paid_corrections_json = json.dumps(overrides, ensure_ascii=False, sort_keys=True)
    record_corrected_field(case, "received_date")
    case.extracted_json = json.dumps(prepare_paid_case_data(case), ensure_ascii=False)


def paid_regeneration_requires_new_date(case) -> bool:
    data = prepare_paid_case_data(case)
    return is_deadline_missed(case.deadline_date) and not data.get("restore_reason")


async def regenerate_paid_case(session, settings, case, user):
    data = prepare_paid_case_data(case)
    case.extracted_json = json.dumps(data, ensure_ascii=False)
    amount_check = validate_amounts(data)
    if not amount_check.ok:
        if "amount_mismatch" in amount_check.errors:
            raise ValueError(
                "Суммы не совпадают: долг (включая проценты и пени) + госпошлина должны "
                "равняться итоговой сумме. Исправьте суммы; введённые значения сохранены."
            )
        raise ValueError("Проверьте формат сумм: " + "; ".join(amount_check.errors))
    await session.commit()

    outcome = await create_case_documents_reviewed(
        case, user, settings, session, restore_reason=data.get("restore_reason")
    )
    if not outcome.ok or outcome.artifacts is None:
        raise ValueError(outcome.admin_report or "Документ не прошёл проверку после исправления.")
    artifacts = outcome.artifacts
    case.full_doc_path = str(artifacts.full_docx_path)
    case.full_pdf_path = str(artifacts.full_pdf_path) if artifacts.full_pdf_path else None
    case.preview_pdf_path = str(artifacts.preview_pdf_path) if artifacts.preview_pdf_path else None
    case.instruction_path = str(artifacts.instruction_docx_path)
    case.paid_regeneration_count = (case.paid_regeneration_count or 0) + 1
    await session.commit()
    schedule_crm_sync(
        settings, case.id, user.id, "paid_document_regenerated",
        {"note": "Заявление перегенерировано после оплаты"},
    )
    return artifacts
