from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.services.document_templates.styles import FONT_NAME, StyleProfile, page_margins_cm
from app.services.legal_data import (
    AmountValidationResult,
    docx_text,
    format_money_rub_kop,
)
from app.services.pdf_tools import pdf_page_count, pdf_text

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None

MAX_WORD_GAP_PT = 28.0
SHORT_PAGE2_LINE_LIMIT = 3


@dataclass
class VisualQAResult:
    ok: bool
    page_count: int | None = None
    font_name: str = FONT_NAME
    body_font_size: float | None = None
    margins: dict[str, float] = field(default_factory=page_margins_cm)
    amounts: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    weird_space_lines: list[str] = field(default_factory=list)


def _check_word_gaps(pdf_path: Path) -> tuple[list[str], list[str]]:
    if fitz is None:
        return [], ["PyMuPDF недоступен для проверки пробелов"]
    errors: list[str] = []
    weird_lines: list[str] = []
    document = fitz.open(str(pdf_path))
    try:
        for page in document:
            words = page.get_text("words")
            if not words:
                continue
            by_line: dict[tuple[int, int], list[tuple]] = {}
            for word in words:
                x0, y0, x1, y1, text, block_no, line_no, _ = word
                if not str(text).strip():
                    continue
                by_line.setdefault((block_no, line_no), []).append((x0, x1, text, y0))
            for line_words in by_line.values():
                line_words.sort(key=lambda item: item[0])
                for index in range(len(line_words) - 1):
                    gap = line_words[index + 1][0] - line_words[index][1]
                    if gap > MAX_WORD_GAP_PT:
                        snippet = " ".join(item[2] for item in line_words)
                        weird_lines.append(snippet[:120])
                        errors.append("weird_justified_spaces")
                        break
    finally:
        document.close()
    return sorted(set(errors)), weird_lines[:5]


def _check_page_breaks(pdf_path: Path, *, restore_term: bool) -> list[str]:
    if fitz is None or restore_term:
        return []
    errors: list[str] = []
    document = fitz.open(str(pdf_path))
    try:
        if document.page_count < 2:
            return []
        page1_text = document[0].get_text("text")
        page2_text = document[1].get_text("text")
        if "ПРОШУ:" in page1_text and "1." not in page1_text.split("ПРОШУ:")[-1][:80]:
            errors.append("proshu_orphaned_at_page_bottom")
        if page1_text.strip().endswith("ПРОШУ:"):
            errors.append("proshu_orphaned_at_page_bottom")
        if "1. Отменить" in page1_text and "1. Отменить" not in page2_text:
            if page1_text.count("1. Отменить") and page2_text.count("1. Отменить"):
                pass
            elif "1. Отменить" in page1_text and "Отменить судебный приказ" not in page2_text:
                if page1_text.find("1. Отменить") > page1_text.rfind("\n", 0, page1_text.find("1. Отменить")):
                    tail = page1_text[page1_text.find("1. Отменить") :]
                    if len(tail) < 40:
                        errors.append("proshu_item1_split_across_pages")
    finally:
        document.close()
    return errors


def _short_nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _page2_has_only_signature_or_short_tail(text: str) -> bool:
    lines = _short_nonempty_lines(text)
    if not lines:
        return False
    signature_markers = ("_____________", "/")
    has_signature = any(any(marker in line for marker in signature_markers) for line in lines)
    if has_signature and len(lines) <= SHORT_PAGE2_LINE_LIMIT:
        return True
    return len(lines) <= SHORT_PAGE2_LINE_LIMIT and all(len(line) <= 80 for line in lines)


def _check_signature_orphan(pdf_path: Path, *, restore_term: bool) -> list[str]:
    if fitz is None or restore_term:
        return []
    document = fitz.open(str(pdf_path))
    try:
        if document.page_count < 2:
            return []
        page2_text = document[1].get_text("text")
        if _page2_has_only_signature_or_short_tail(page2_text):
            return ["signature_orphaned_on_page2"]
    finally:
        document.close()
    return []


def run_visual_qa(
    *,
    full_docx: Path | None,
    full_pdf: Path | None,
    preview_pdf: Path | None,
    data: dict,
    restore_term: bool,
    amount_check: AmountValidationResult,
    profile: StyleProfile | None = None,
) -> VisualQAResult:
    result = VisualQAResult(ok=True)
    profile = profile or StyleProfile.normal()
    result.body_font_size = profile.body_font_size
    result.margins = page_margins_cm(profile)
    if amount_check.debt_amount is not None:
        result.amounts["debt_amount"] = format_money_rub_kop(amount_check.debt_amount)
    if amount_check.state_duty is not None:
        result.amounts["state_duty"] = format_money_rub_kop(amount_check.state_duty)
    if amount_check.computed_total is not None:
        result.amounts["total_amount"] = format_money_rub_kop(amount_check.computed_total)
    elif amount_check.total_amount is not None:
        result.amounts["total_amount"] = format_money_rub_kop(amount_check.total_amount)

    if not full_pdf or not full_pdf.exists():
        result.errors.append("full_pdf_missing")
        result.ok = False
        return result

    if fitz is None:
        result.errors.append("pymupdf_unavailable")
        result.ok = False
        return result

    try:
        fitz.open(str(full_pdf)).close()
    except Exception as exc:
        result.errors.append(f"full_pdf_unreadable: {exc}")
        result.ok = False
        return result

    result.page_count = pdf_page_count(full_pdf)
    if not restore_term and result.page_count and result.page_count > 1:
        result.warnings.append(f"page_count={result.page_count} (допустимо после compact mode)")

    full_text = pdf_text(full_pdf)
    bad: list[str] = []
    if "amount_mismatch" in (amount_check.errors or []):
        bad.append("amount_mismatch")
    if bad:
        result.errors.extend(sorted(set(bad)))
    gap_errors, weird = _check_word_gaps(full_pdf)
    result.weird_space_lines = weird
    result.errors.extend(error for error in gap_errors if error not in result.errors)

    if not restore_term and result.page_count and result.page_count > 1:
        result.errors.extend(_check_signature_orphan(full_pdf, restore_term=restore_term))
        result.errors.extend(_check_page_breaks(full_pdf, restore_term=restore_term))
    elif not restore_term and result.page_count == 1:
        result.errors.extend(_check_page_breaks(full_pdf, restore_term=restore_term))

    if preview_pdf and preview_pdf.exists() and full_pdf.exists():
        if preview_pdf.read_bytes() == full_pdf.read_bytes():
            result.errors.append("preview_equals_full_pdf")
        try:
            preview_text = pdf_text(preview_pdf)
            if preview_text.strip() == full_text.strip():
                result.errors.append("preview_contains_full_text")
        except Exception:
            result.errors.append("preview_pdf_unreadable")

    docx_full_text = docx_text(str(full_docx)) if full_docx and full_docx.exists() else ""
    if "ВОЗРАЖЕНИЯ" not in full_text and "ВОЗРАЖЕНИЯ" not in docx_full_text:
        result.errors.append("missing_title_vozrazheniya")

    result.errors = sorted(set(result.errors))
    result.ok = not result.errors
    return result
