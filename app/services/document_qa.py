from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.services.document_render_contract import (
    RENDER_CONTRACT,
    address_matches_rendered,
    clean_contract_identifier,
    date_long_text,
    dates_long_in_text,
    entity_matches_rendered,
    identifier_matches_rendered,
    money_matches_rendered,
    selected_source_value,
)
from app.services.legal_data import (
    FIELD_LABELS,
    AmountValidationResult,
    docx_text,
    is_deadline_missed,
    missing_order_fields,
    normalize_order_data,
)
from app.services.pdf_tools import pdf_page_count, pdf_text


@dataclass
class DocumentQAResult:
    missing_fields: list[str] = field(default_factory=list)
    integrity_errors: list[str] = field(default_factory=list)
    output_format_errors: list[str] = field(default_factory=list)
    artifact_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blocking_errors

    @property
    def blocking_errors(self) -> list[str]:
        return [
            *(f"missing_field={field}" for field in self.missing_fields),
            *self.integrity_errors,
            *self.output_format_errors,
            *self.artifact_errors,
        ]

    @property
    def bad_tokens(self) -> list[str]:
        """Compatibility alias for reports written before categorized QA."""
        tokens: list[str] = []
        for error in self.output_format_errors:
            match = re.search(r"\bcode=([^\s]+)", error)
            tokens.append(match.group(1) if match else error)
        if any("amount_mismatch" in error for error in self.integrity_errors):
            tokens.append("amount_mismatch")
        return sorted(set(tokens))

    @property
    def reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.missing_fields:
            reasons.append(
                "missing_fields: "
                + ", ".join(FIELD_LABELS.get(field, field) for field in self.missing_fields)
            )
        reasons.extend(self.integrity_errors)
        reasons.extend(self.output_format_errors)
        reasons.extend(self.artifact_errors)
        if self.bad_tokens:
            reasons.append("bad_tokens: " + ", ".join(self.bad_tokens))
        reasons.extend(f"warning: {warning}" for warning in self.warnings)
        return reasons


_PLACEHOLDER_PATTERNS = (
    ("MISSING", re.compile(r"\bMISSING\b", re.IGNORECASE)),
    ("None", re.compile(r"\bNone\b", re.IGNORECASE)),
    ("null", re.compile(r"\bnull\b", re.IGNORECASE)),
    ("template_braces", re.compile(r"\{\{|\}\}")),
    ("question_placeholder", re.compile(r"\?{4,}")),
    ("blank_placeholder", re.compile(r"_{16,}|____\.__\.20__")),
)
_ISO_DATE_RE = re.compile(r"(?<![\d-])\d{4}-\d{2}-\d{2}(?![\d-])")
_NUMERIC_DATE_RE = re.compile(r"(?<![\d-])\d{1,2}[./]\d{1,2}[./]\d{4}(?![\d-])")
_PII_OUTPUT_RE = re.compile(
    r"\b(?:паспорт(?:\s+серии|\s*№|\s+\d)|инн\s*\d|кпп\s*\d|огрн\s*\d|"
    r"бик\s*\d|расч[её]тн(?:ый|ого)\s+сч[её]т)\b",
    re.IGNORECASE,
)


def output_format_violations(text: str) -> list[str]:
    errors = [name for name, pattern in _PLACEHOLDER_PATTERNS if pattern.search(text)]
    if re.search(r"(?m)^\s*заявление\s+об\s+отмене", text, re.IGNORECASE) and "ВОЗРАЖЕНИЯ" not in text:
        errors.append("old_statement_title")
    if _ISO_DATE_RE.search(text):
        errors.append("iso_date")
    if _NUMERIC_DATE_RE.search(text):
        errors.append("numeric_date")
    if re.search(r"Мировому судье\s+(?:Мировой судья|Судебный участок)", text, re.IGNORECASE):
        errors.append("duplicated_court_addressee")
    if re.search(r"\bпо\s+по\b|\bза\s+период\s+с\s+с\b|\bпо\s+договор\s+№", text, re.IGNORECASE):
        errors.append("renderer_grammar")
    if re.search(r"\bзарегистрированному\b", text, re.IGNORECASE):
        errors.append("зарегистрированному")
    if re.search(r"\bурожен(?:ец|ка)?\b", text, re.IGNORECASE):
        errors.append("урожен")
    if re.search(r"\bпаспорт\b", text, re.IGNORECASE):
        errors.append("паспорт")
    if _PII_OUTPUT_RE.search(text) and "паспорт" not in errors:
        errors.append("pii_leak")
    return sorted(set(errors))


def _artifact_text(
    path: Path | None,
    *,
    artifact: str,
    kind: str,
    required: bool,
    result: DocumentQAResult,
) -> str:
    exists = bool(path and path.exists())
    result.checks[f"{artifact}_exists"] = exists
    if not exists:
        if required:
            result.artifact_errors.append(f"artifact_error artifact={artifact} error=missing")
        return ""
    try:
        if kind == "docx":
            text = docx_text(str(path))
            pages = None
        else:
            pages = pdf_page_count(path)
            if not pages:
                raise ValueError("no_pages")
            text = pdf_text(path)
        result.checks[f"{artifact}_readable"] = True
        result.checks[f"{artifact}_has_text"] = bool(text.strip())
        if pages is not None:
            result.checks[f"{artifact}_has_pages"] = pages > 0
        if not text.strip():
            result.artifact_errors.append(
                f"artifact_error artifact={artifact} error=empty_text"
                + (" (preview PDF не содержит текста после редактирования)" if artifact == "preview_pdf" else "")
            )
        return text
    except Exception as exc:
        result.checks[f"{artifact}_readable"] = False
        result.artifact_errors.append(
            f"artifact_error artifact={artifact} error=unreadable detail={type(exc).__name__}"
        )
        return ""


def _expected_representation(
    name: str,
    source_field: str,
    value: str,
    data: dict,
) -> str:
    if name == "court_addressee":
        return str(data.get("court_addressee") or value)
    if name in {"order_date", "debt_basis_date", "received_date", "deadline_date"}:
        return date_long_text(value)
    if name in {"debt_period", "debt_contract"}:
        return dates_long_in_text(value)
    return value


def _representation_present(strategy: str, name: str, expected: str, rendered_text: str) -> bool:
    if strategy == "address":
        return address_matches_rendered(expected, rendered_text)
    if strategy in {"person", "organization", "text", "date_text"}:
        return entity_matches_rendered(expected, rendered_text)
    if strategy == "debt_contract":
        identifiers = re.findall(r"\b[0-9A-Za-zА-Яа-яЁё-]*\d[0-9A-Za-zА-Яа-яЁё/-]{2,}\b", expected)
        return bool(identifiers) and all(
            entity_matches_rendered(identifier, rendered_text) for identifier in identifiers
        )
    if strategy in {"case_number", "uid", "identifier"}:
        return identifier_matches_rendered(
            expected,
            rendered_text,
            lambda value: clean_contract_identifier(name, str(value)),
        )
    if strategy == "money":
        return money_matches_rendered(expected, rendered_text)
    if strategy == "date":
        return entity_matches_rendered(expected, rendered_text)
    if strategy == "debt_basis":
        # Type is represented by a grammatical phrase and is verified through
        # the number/date/fallback fields in the same contract.
        return True
    return False


def _check_render_contract(
    *,
    data: dict,
    rendered_text: str,
    received_date: date | None,
    deadline_date: date | None,
    restore_reason: str | None,
    restore_term: bool,
) -> list[str]:
    errors: list[str] = []
    has_structured_basis = bool(data.get("debt_basis_number"))
    for spec in RENDER_CONTRACT:
        source_field, value = selected_source_value(
            spec,
            data,
            received_date=received_date,
            deadline_date=deadline_date if restore_term else None,
            restore_reason=restore_reason if restore_term else None,
        )
        if spec.name == "debt_contract" and has_structured_basis:
            continue
        if spec.name == "total_amount" and data.get("amount_render_mode") != "explicit_total":
            continue
        if not value:
            continue
        expected = _expected_representation(spec.name, source_field, value, data)
        if _representation_present(spec.strategy, spec.name, expected, rendered_text):
            continue
        canonical = re.sub(r"\s+", " ", expected).strip()
        if spec.strategy in {"address", "person"}:
            digest = hashlib.sha256(canonical.casefold().encode("utf-8")).hexdigest()[:12]
            safe_expected = f"fingerprint:{digest},tokens:{len(canonical.split())}"
        else:
            safe_expected = canonical[:120]
        errors.append(
            "integrity_error "
            f"field={source_field} artifact=full_docx strategy={spec.strategy} "
            f"expected={safe_expected!r} actual_match=missing"
        )
    return errors


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
    require_full_pdf: bool = True,
    require_instruction_docx: bool = True,
    amount_check: AmountValidationResult | None = None,
) -> DocumentQAResult:
    del card_text  # User preview text is not a rendered-document artifact.
    normalized = normalize_order_data(data)
    result = DocumentQAResult(
        missing_fields=missing_order_fields(normalized, received_date),
    )
    restore_term = is_deadline_missed(deadline_date)
    if restore_term and not restore_reason:
        result.integrity_errors.append(
            "integrity_error field=restore_reason artifact=full_docx strategy=required_when_restoring expected=present actual_match=missing"
        )
    if amount_check and not amount_check.ok:
        result.integrity_errors.extend(
            f"integrity_error field=amounts artifact=structured strategy=decimal detail={error}"
            for error in amount_check.errors
        )

    full_docx_text = _artifact_text(
        full_docx, artifact="full_docx", kind="docx", required=True, result=result
    )
    full_pdf_text = _artifact_text(
        full_pdf, artifact="full_pdf", kind="pdf", required=require_full_pdf, result=result
    )
    instruction_text = _artifact_text(
        instruction_docx,
        artifact="instruction_docx",
        kind="docx",
        required=require_instruction_docx,
        result=result,
    )
    preview_text = _artifact_text(
        preview_pdf,
        artifact="preview_pdf",
        kind="pdf",
        required=require_preview_pdf,
        result=result,
    )

    if full_docx_text:
        result.output_format_errors.extend(
            f"output_format_error artifact=full_docx code={code}"
            for code in output_format_violations(full_docx_text)
        )
        result.integrity_errors.extend(
            _check_render_contract(
                data=normalized,
                rendered_text=full_docx_text,
                received_date=received_date,
                deadline_date=deadline_date,
                restore_reason=restore_reason,
                restore_term=restore_term,
            )
        )
    if full_pdf_text:
        result.output_format_errors.extend(
            f"output_format_error artifact=full_pdf code={code}"
            for code in output_format_violations(full_pdf_text)
        )
    if instruction_text:
        result.output_format_errors.extend(
            f"output_format_error artifact=instruction_docx code={code}"
            for code in output_format_violations(instruction_text)
        )

    if preview_pdf and preview_pdf.exists() and full_pdf and full_pdf.exists():
        try:
            if preview_pdf.read_bytes() == full_pdf.read_bytes():
                result.artifact_errors.append(
                    "artifact_error artifact=preview_pdf error=redaction_not_applied"
                )
            elif preview_text and full_pdf_text and preview_text.strip() == full_pdf_text.strip():
                result.artifact_errors.append(
                    "artifact_error artifact=preview_pdf error=full_text_not_redacted"
                )
        except Exception as exc:
            result.artifact_errors.append(
                f"artifact_error artifact=preview_pdf error=redaction_check_failed detail={type(exc).__name__}"
            )

    if preview_docx is not None:
        result.warnings.append("preview_docx is legacy and intentionally excluded from strict QA")

    result.integrity_errors = sorted(set(result.integrity_errors))
    result.output_format_errors = sorted(set(result.output_format_errors))
    result.artifact_errors = sorted(set(result.artifact_errors))
    result.warnings = sorted(set(result.warnings))
    return result
