from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.services.legal_data import (
    BAD_DOCUMENT_TOKENS,
    FIELD_LABELS,
    AmountValidationResult,
    VALIDATION_SKIP_KEYS,
    bad_tokens_in_preview_text,
    bad_tokens_in_text,
    is_deadline_missed,
    looks_like_dative_full_name,
    missing_order_fields,
    normalize_order_data,
    validate_docx_clean,
)
from app.services.pdf_tools import pdf_text


@dataclass
class DocumentQAResult:
    ok: bool
    missing_fields: list[str] = field(default_factory=list)
    bad_tokens: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def run_document_qa(
    *,
    data: dict,
    received_date: date | None,
    deadline_date: date | None,
    full_docx: Path | None,
    full_pdf: Path | None,
    preview_pdf: Path | None,
    instruction_docx: Path | None,
    preview_docx: Path | None = None,
    card_text: str = "",
    restore_reason: str | None = None,
    require_preview_pdf: bool = True,
    amount_check: AmountValidationResult | None = None,
) -> DocumentQAResult:
    normalized = normalize_order_data(data)
    missing = missing_order_fields(normalized, received_date)
    bad: list[str] = []
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    checks["full_docx_exists"] = full_docx is not None and full_docx.exists()
    checks["full_pdf_exists"] = full_pdf is not None and full_pdf.exists()
    checks["preview_pdf_exists"] = preview_pdf is not None and preview_pdf.exists()
    checks["instruction_exists"] = instruction_docx is not None and instruction_docx.exists()

    if not checks["full_docx_exists"]:
        reasons.append("полный DOCX не создан")
    if not checks["full_pdf_exists"]:
        reasons.append("полный PDF не создан")
    if require_preview_pdf and not checks["preview_pdf_exists"]:
        reasons.append("preview PDF не создан")
    if not checks["instruction_exists"]:
        reasons.append("инструкция DOCX не создана")

    if missing:
        reasons.append("не заполнены обязательные поля: " + ", ".join(FIELD_LABELS.get(f, f) for f in missing))

    debtor = normalized.get("debtor_full_name", "")
    raw_debtor = str(data.get("debtor_full_name") or data.get("debtor_name_raw") or "")
    if looks_like_dative_full_name(raw_debtor) or looks_like_dative_full_name(debtor):
        bad.append("debtor_full_name:dative")
        reasons.append("подозрительное ФИО должника")

    if is_deadline_missed(deadline_date) and not restore_reason:
        reasons.append("срок пропущен, но не указана причина восстановления")

    if amount_check and not amount_check.ok:
        bad.append("amount_mismatch")
        reasons.append("amount_mismatch: суммы долга, госпошлины и итога не согласованы")

    if full_docx and full_docx.exists():
        bad.extend(validate_docx_clean(str(full_docx)))
    if full_pdf and full_pdf.exists():
        try:
            bad.extend(bad_tokens_in_text(pdf_text(full_pdf)))
        except Exception as exc:
            reasons.append(f"не удалось прочитать полный PDF: {exc}")
    if preview_pdf and preview_pdf.exists():
        try:
            preview_bad = bad_tokens_in_preview_text(pdf_text(preview_pdf))
            bad.extend(preview_bad)
            if preview_bad:
                reasons.append("preview PDF содержит запрещённые токены")
        except Exception as exc:
            reasons.append(f"не удалось прочитать preview PDF: {exc}")
    if preview_docx and preview_docx.exists():
        bad.extend(token for token in validate_docx_clean(str(preview_docx)) if token != "▒")

    for key, value in normalized.items():
        if key in VALIDATION_SKIP_KEYS:
            continue
        bad.extend(bad_tokens_in_text(f"{key}: {value}"))
    bad.extend(bad_tokens_in_text(card_text))

    for token in BAD_DOCUMENT_TOKENS:
        if token in bad:
            reasons.append(f"стоп-лист: {token}")

    ok = not missing and not bad and all(checks.values()) and not (
        require_preview_pdf and not checks.get("preview_pdf_exists")
    )
    return DocumentQAResult(
        ok=ok,
        missing_fields=missing,
        bad_tokens=sorted(set(bad)),
        checks=checks,
        reasons=reasons,
    )
