from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape
from pathlib import Path
from typing import Any

from app.services.legal_data import FIELD_LABELS, ValidationResult, validate_before_generation
from app.services.tesseract_ai import (
    CRITICAL_FIELDS,
    REQUIRED_GENERATION_FIELDS,
    _canonical,
    _document_value,
    _format_ok,
    apply_user_field_confirmation,
    build_confirmation_crop,
    pending_confirmation_fields,
)


@dataclass(frozen=True, slots=True)
class ConfirmationStep:
    field_name: str
    label: str
    recognized_value: str
    alternatives: tuple[str, ...]
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    data: dict[str, Any]
    validation: ValidationResult
    missing_fields: tuple[str, ...]
    ready: bool
    next_step: ConfirmationStep | None


def next_confirmation(data: dict[str, Any]) -> ConfirmationStep | None:
    pending = pending_confirmation_fields(data)
    if not pending:
        return None
    record = pending[0]
    field_name = str(record.get("field_name") or "")
    value = str(record.get("extracted_value") or record.get("raw_ocr_value") or "")
    alternatives = tuple(str(item).strip() for item in (record.get("alternatives") or [])[:2] if str(item).strip())
    return ConfirmationStep(field_name, FIELD_LABELS.get(field_name, field_name), value, alternatives, record)


def confirmation_text(step: ConfirmationStep) -> str:
    text = f"Проверьте {escape(step.label.lower())}\n\nМы распознали: <b>{escape(step.recognized_value)}</b>"
    if step.alternatives:
        text += "\n\nВозможные варианты: " + "; ".join(escape(item) for item in step.alternatives)
    return text


def confirmation_crop(order_photo_path: str | Path, step: ConfirmationStep, *, case_id: int | None = None) -> Path | None:
    return build_confirmation_crop(order_photo_path, step.record, case_id=case_id)


def reduce_and_validate(data: dict[str, Any], received_date: date | None) -> ConfirmationResult:
    """Single reducer/validation gate shared by Telegram, MAX and generation."""
    updated = dict(data)
    provenance = {
        name: dict(record) for name, record in (data.get("_field_provenance") or {}).items()
        if isinstance(record, dict)
    }
    updated["_field_provenance"] = provenance

    for name, record in provenance.items():
        candidate = _document_value(record)
        if (
            name in CRITICAL_FIELDS
            and record.get("status") in {"confirmed", "user_confirmed"}
            and not _format_ok(name, candidate)
        ):
            record["status"] = "disputed"
            record["verification_reason"] = "document_value_format_invalid"

    case_value = _document_value(provenance.get("case_number", {}))
    uid_value = _document_value(provenance.get("uid", {}))
    if case_value and uid_value and _canonical(case_value) == _canonical(uid_value):
        for name in ("case_number", "uid"):
            record = provenance.get(name)
            if record:
                record["status"] = "disputed"
                record["verification_reason"] = "case_number_uid_conflict"

    for name, record in provenance.items():
        record["document_value"] = _document_value(record)
        value = record["document_value"]
        if value:
            updated[name] = value
        else:
            updated.pop(name, None)
    if updated.get("debtor_full_name"):
        # Compatibility alias is still sourced from the immutable provenance.
        debtor = provenance.get("debtor_full_name") or {}
        updated["debtor_name_raw"] = debtor.get("raw_ocr_value") or updated["debtor_full_name"]

    unresolved = [
        name for name in REQUIRED_GENERATION_FIELDS
        if (
            provenance.get(name, {}).get("status") not in {"confirmed", "verified", "user_confirmed"}
            or not _format_ok(name, provenance.get(name, {}).get("document_value") or "")
        )
    ]
    unresolved = list(dict.fromkeys(unresolved))
    updated["_pipeline_status"] = "awaiting_user_confirmation"
    validation = validate_before_generation(updated, received_date)
    missing = [FIELD_LABELS.get(item, item) for item in validation.missing if item != "received_date"]
    if updated.get("_document_kind") != "court_order":
        missing.append("Документ не является судебным приказом")
    missing.extend(FIELD_LABELS.get(name, name) for name in unresolved)
    missing = list(dict.fromkeys(missing))
    ready = validation.ok and not missing and not unresolved and updated.get("_document_kind") == "court_order"
    updated["_pipeline_status"] = "ready" if ready else "awaiting_user_confirmation"
    # Re-run with the final status so callers cannot observe a provisional-ready result.
    final_validation = validate_before_generation(updated, received_date)
    ignored = {"received_date", "Подтверждение спорных полей"}
    final_missing = list(dict.fromkeys([
        *missing,
        *(FIELD_LABELS.get(item, item) for item in final_validation.missing if item not in ignored),
    ]))
    return ConfirmationResult(updated, final_validation, tuple(final_missing), ready, next_confirmation(updated))


def apply_confirmation_answer(
    data: dict[str, Any], field_name: str, value: str, received_date: date | None,
) -> ConfirmationResult:
    updated = apply_user_field_confirmation(data, field_name, value)
    return reduce_and_validate(updated, received_date)
