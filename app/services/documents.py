from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Case, User
from app.services.document_templates import DocumentArtifacts, create_case_documents as _create_case_documents
from app.services.legal_data import (
    FIELD_LABELS,
    clean_case_number,
    clean_uid,
    docx_text,
    format_money_rub_kop,
    is_deadline_missed,
    missing_order_fields,
    normalize_order_data,
    validate_before_generation,
)
from app.services.llm import review_generated_document
from app.utils import h

logger = logging.getLogger(__name__)

MANUAL_REVIEW_USER_TEXT = (
    "Не удалось сформировать заявление по этому фото. "
    "Пожалуйста, сфотографируйте судебный приказ целиком ещё раз при хорошем освещении."
)
SAFE_AI_REVIEW_FIELDS = {
    "debtor_full_name",
    "debtor_address",
    "court_name",
    "court_address",
    "creditor_name",
    "creditor_address",
    "debt_contract",
    "debt_period",
}
VALID_DOCUMENT_AI_REVIEW_MODES = {"off", "shadow", "autofix", "blocking"}
SOURCE_ONLY_HINTS = {
    "passport",
    "паспорт",
    "дата рождения",
    "birth",
    "место рождения",
    "birthplace",
    "регистрац",
    "registration",
    "прописк",
    "уфмс",
    "оуфмс",
    "мвд",
    "выдан",
    "урожен",
}
FINAL_TEXT_SENSITIVE_MARKERS = {
    "паспорт",
    "дата рождения",
    "место рождения",
    "зарегистрирован",
    "регистрац",
    "прописан",
    "уфмс",
    "оуфмс",
    "мвд",
    "выдан",
    "урожен",
}


@dataclass
class DocumentReviewOutcome:
    ok: bool
    artifacts: DocumentArtifacts | None = None
    review: dict[str, Any] = field(default_factory=dict)
    applied_fixes: dict[str, str] = field(default_factory=dict)
    regeneration_count: int = 0
    admin_report: str = ""


def document_ai_review_mode(settings: Settings) -> str:
    mode = str(getattr(settings, "document_ai_review_mode", "shadow") or "shadow").strip().lower()
    return mode if mode in VALID_DOCUMENT_AI_REVIEW_MODES else "shadow"


def _final_text_has_sensitive_marker(final_text: str) -> bool:
    text = (final_text or "").lower()
    return any(marker in text for marker in FINAL_TEXT_SENSITIVE_MARKERS)


def _issue_is_source_only(issue: dict[str, Any], final_text: str) -> bool:
    haystack = " ".join(
        str(issue.get(key) or "")
        for key in ("code", "field", "message", "suggested_fix")
    ).lower()
    if not any(hint in haystack for hint in SOURCE_ONLY_HINTS):
        return False
    return not _final_text_has_sensitive_marker(final_text)


def _review_scoped_to_final_text(review: dict[str, Any], final_text: str) -> dict[str, Any]:
    scoped = dict(review or {})
    issues: list[dict[str, Any]] = []
    for raw_issue in scoped.get("issues") or []:
        if not isinstance(raw_issue, dict):
            continue
        issue = dict(raw_issue)
        if issue.get("severity") == "blocker" and _issue_is_source_only(issue, final_text):
            issue["severity"] = "warning"
            issue["source_only"] = True
            issue["message"] = (str(issue.get("message") or "") + " (source-only; not present in FINAL STATEMENT TEXT)").strip()
        issues.append(issue)
    scoped["issues"] = issues
    has_blocker = any(issue.get("severity") == "blocker" for issue in issues)
    has_warning = any(issue.get("severity") == "warning" for issue in issues)
    scoped["severity"] = "blocker" if has_blocker else ("warning" if has_warning else "ok")
    scoped["ok"] = not has_blocker
    if not has_blocker and all(issue.get("source_only") for issue in issues if issue.get("severity") == "warning"):
        scoped["needs_regeneration"] = False
    return scoped


def _review_issue_lines(review: dict[str, Any], *, applied_fixes: dict[str, str], case_id: int | None, document_path: str) -> list[str]:
    lines = [
        f"AI document review: case #{case_id or 'unknown'}",
        f"severity: {review.get('severity') or 'ok'}",
        f"needs_regeneration: {bool(review.get('needs_regeneration'))}",
        f"auto_fixed: {bool(applied_fixes)}",
        f"document: {document_path}",
    ]
    if review.get("mode"):
        lines.append(f"mode: {review.get('mode')}")
    for issue in review.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        field = str(issue.get("field") or "")
        lines.extend(
            [
                "",
                f"issue code: {issue.get('code') or ''}",
                f"field: {field}",
                f"severity: {issue.get('severity') or ''}",
                f"suggested_fix: {issue.get('suggested_fix') or ''}",
                f"source_verified: {bool(issue.get('source_verified'))}",
                f"source_fragment: {issue.get('source_fragment') or ''}",
                f"auto_fixed: {field in applied_fixes}",
                f"source_only: {bool(issue.get('source_only'))}",
                f"message: {issue.get('message') or ''}",
            ]
        )
    return lines


def _safe_review_fixes(data: dict[str, Any], review: dict[str, Any]) -> dict[str, str]:
    clean_fields = review.get("clean_fields") if isinstance(review.get("clean_fields"), dict) else {}
    source_verified_fields: set[str] = set()
    for issue in review.get("issues") or []:
        if not isinstance(issue, dict) or issue.get("source_only"):
            continue
        field = str(issue.get("field") or "").strip()
        if field and issue.get("source_verified") is True and str(issue.get("source_fragment") or "").strip():
            source_verified_fields.add(field)
    fixes: dict[str, str] = {}
    for field in SAFE_AI_REVIEW_FIELDS:
        value = str(clean_fields.get(field) or "").strip()
        if not value or field not in source_verified_fields:
            continue
        if str(data.get(field) or "").strip() == value:
            continue
        fixes[field] = value
    return fixes


def _review_blocks_delivery(review: dict[str, Any], applied_fixes: dict[str, str] | None = None) -> bool:
    for issue in review.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if issue.get("severity") == "blocker" and not issue.get("source_only"):
            return True
    return False


def _schedule_shadow_document_review(
    settings: Settings,
    *,
    case_id: int,
    user_id: int,
    document_text: str,
    source_data: dict[str, Any],
    visual_summary: dict[str, Any],
    document_path: str,
) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Document AI shadow review skipped: no running event loop case_id=%s", case_id)
        return
    task = loop.create_task(
        _run_shadow_document_review(
            settings,
            case_id=case_id,
            user_id=user_id,
            document_text=document_text,
            source_data=source_data,
            visual_summary=visual_summary,
            document_path=document_path,
        )
    )
    task.add_done_callback(_consume_shadow_review_exception)


def _consume_shadow_review_exception(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Document AI shadow review crashed")


async def _run_shadow_document_review(
    settings: Settings,
    *,
    case_id: int,
    user_id: int,
    document_text: str,
    source_data: dict[str, Any],
    visual_summary: dict[str, Any],
    document_path: str,
) -> None:
    from app.database import SessionLocal
    from app.services.crm_background import schedule_crm_sync

    async with SessionLocal() as session:
        raw_review = await review_generated_document(
            settings,
            session,
            case_id=case_id,
            user_id=user_id,
            document_text=document_text,
            source_data=source_data,
            visual_summary=visual_summary,
            regeneration_happened=False,
        )
        review = _review_scoped_to_final_text(raw_review, document_text)
        lines = _review_issue_lines(review, applied_fixes={}, case_id=case_id, document_path=document_path)
        report = "[shadow] " + "\n".join(lines)
        logger.info("Document AI shadow review case_id=%s severity=%s", case_id, review.get("severity"))
        schedule_crm_sync(settings, case_id, user_id, "document_ai_review_shadow", {"note": report[:65000]})


def _ai_review_failed_outcome(
    settings: Settings,
    *,
    case: Case,
    user: User,
    artifacts: DocumentArtifacts,
    error: Exception,
) -> DocumentReviewOutcome:
    from app.services.crm_background import schedule_crm_sync

    review = {
        "ok": False,
        "severity": "blocker",
        "needs_regeneration": False,
        "issues": [],
        "clean_fields": {},
        "ai_review_failed": True,
        "error": str(error)[:1000],
    }
    report = f"AI document review failed after retry/fallback: {error}"
    schedule_crm_sync(settings, case.id, user.id, "document_ai_review_failed", {"note": report[:65000]})
    return DocumentReviewOutcome(ok=False, artifacts=artifacts, review=review, admin_report=report)


async def create_case_documents_reviewed(
    case: Case,
    user: User,
    settings: Settings,
    session: AsyncSession | None,
    *,
    restore_reason: str | None = None,
) -> DocumentReviewOutcome:
    artifacts = create_case_documents_with_qa(case, user, settings, restore_reason=restore_reason)
    return DocumentReviewOutcome(
        ok=True,
        artifacts=artifacts,
        review={"ok": True, "severity": "ok", "needs_regeneration": False, "issues": [], "clean_fields": {}, "mode": "off"},
    )

    # Legacy AI review implementation is intentionally unreachable: production
    # delivery uses the single facts/render extraction response directly.
    mode = document_ai_review_mode(settings)
    if mode in {"off", "shadow"}:
        artifacts = create_case_documents_with_qa(case, user, settings, restore_reason=restore_reason)
        review = {"ok": True, "severity": "ok", "needs_regeneration": False, "issues": [], "clean_fields": {}, "mode": mode}
        if mode == "shadow":
            data = normalize_order_data(json.loads(case.extracted_json or "{}"))
            final_text = docx_text(str(artifacts.full_docx_path))
            _schedule_shadow_document_review(
                settings,
                case_id=case.id,
                user_id=user.id,
                document_text=final_text,
                source_data=data,
                visual_summary=artifacts.qa_report,
                document_path=str(artifacts.full_docx_path),
            )
        return DocumentReviewOutcome(ok=True, artifacts=artifacts, review=review)

    max_regenerations = max(0, int(getattr(settings, "max_ai_review_regenerations", 1)))
    applied_all: dict[str, str] = {}
    regeneration_count = 0
    last_review: dict[str, Any] = {}
    last_artifacts: DocumentArtifacts | None = None

    while True:
        artifacts = create_case_documents_with_qa(case, user, settings, restore_reason=restore_reason)
        last_artifacts = artifacts
        data = normalize_order_data(json.loads(case.extracted_json or "{}"))
        final_text = docx_text(str(artifacts.full_docx_path))
        try:
            review_kwargs = {
                "case_id": case.id,
                "user_id": user.id,
                "document_text": final_text,
                "source_data": data,
                "visual_summary": artifacts.qa_report,
                "regeneration_happened": regeneration_count > 0,
            }
            if case.order_photo_path:
                review_kwargs["source_image_path"] = case.order_photo_path
            raw_review = await review_generated_document(settings, session, **review_kwargs)
        except Exception as exc:
            if mode == "autofix":
                logger.warning("Document AI autofix failed; delivering deterministic QA-passed artifacts case_id=%s error=%s", case.id, exc)
                return _ai_review_failed_outcome(settings, case=case, user=user, artifacts=artifacts, error=exc)
            raise
        review = _review_scoped_to_final_text(raw_review, final_text)
        review["mode"] = mode
        last_review = review
        fixes = _safe_review_fixes(data, review)
        should_regenerate = bool(fixes) and regeneration_count < max_regenerations
        if should_regenerate:
            updated = dict(data)
            updated.update(fixes)
            updated = normalize_order_data(updated)
            case.extracted_json = json.dumps(updated, ensure_ascii=False)
            if session is not None:
                await session.commit()
            applied_all.update(fixes)
            regeneration_count += 1
            continue

        blocks = _review_blocks_delivery(review, fixes)
        report_lines = _review_issue_lines(
            review,
            applied_fixes=applied_all,
            case_id=case.id,
            document_path=str(artifacts.full_docx_path),
        )
        return DocumentReviewOutcome(
            ok=not blocks,
            artifacts=artifacts if not blocks else last_artifacts,
            review=last_review,
            applied_fixes=applied_all,
            regeneration_count=regeneration_count,
            admin_report="\n".join(report_lines),
        )

def build_statement_paragraphs(data: dict, received_date: date, deadline_date: date | None, restore_reason: str | None = None) -> list[str]:
    from app.services.document_templates.statement_templates import StatementContext, build_statement_paragraphs as _build

    ctx = StatementContext(
        data=data,
        received_date=received_date,
        deadline_date=deadline_date,
        document_date=date.today(),
        restore_reason=restore_reason,
    )
    return _build(ctx)


def create_case_documents(
    case: Case,
    user: User,
    settings: Settings,
    *,
    restore_reason: str | None = None,
) -> tuple[Path, Path | None, Path | None, Path | None, Path]:
    artifacts = _create_case_documents(case, user, settings, restore_reason=restore_reason)
    return (
        artifacts.full_docx_path,
        artifacts.full_pdf_path,
        artifacts.preview_pdf_path,
        None,
        artifacts.instruction_docx_path,
    )


def create_case_documents_with_qa(
    case: Case,
    user: User,
    settings: Settings,
    *,
    restore_reason: str | None = None,
) -> DocumentArtifacts:
    return _create_case_documents(case, user, settings, restore_reason=restore_reason)


def extraction_preview(
    data: dict,
    received_date: date | None,
    missing: list[str],
    deadline_date: date | None = None,
    *,
    include_name_debug: bool = True,
    title: str = "🔎 <b>Проверьте данные</b>",
) -> str:
    data = normalize_order_data(data)
    lines = [
        title,
        "",
        f"<b>Суд:</b> {h(data.get('court_name') or 'не заполнено')}",
        f"<b>Адрес суда:</b> {h(data.get('court_address') or 'не заполнено')}",
        f"<b>Должник:</b> {h(data.get('debtor_full_name') or 'не заполнено')}",
        f"<b>Адрес должника:</b> {h(data.get('debtor_address') or 'не заполнено')}",
        f"<b>Взыскатель:</b> {h(data.get('creditor_name') or 'не заполнено')}",
        f"<b>Адрес взыскателя:</b> {h(data.get('creditor_address') or 'не заполнено')}",
        f"<b>Номер дела:</b> {h(clean_case_number(data.get('case_number') or '') or 'не заполнено')}",
        f"<b>УИД:</b> {h(clean_uid(data.get('uid') or '') or 'не заполнено')}",
        f"<b>Дата приказа:</b> {h(data.get('order_date') or 'не заполнено')}",
        f"<b>Долг:</b> {h(format_money_rub_kop(data.get('debt_amount') or '') or 'не заполнено')}",
        f"<b>Госпошлина:</b> {h(format_money_rub_kop(data.get('state_duty') or '') or 'не заполнено')}",
        f"<b>Итого:</b> {h(format_money_rub_kop(data.get('total_amount') or '') or 'не заполнено')}",
    ]
    if data.get("debt_contract"):
        lines.append(f"<b>Договор:</b> {h(data.get('debt_contract'))}")
    if data.get("debt_period"):
        lines.append(f"<b>Период:</b> {h(data.get('debt_period'))}")
    if received_date:
        lines.append(f"<b>Дата получения:</b> {received_date.strftime('%d.%m.%Y')}")
    if deadline_date:
        lines.append(f"<b>Срок подачи:</b> до {deadline_date.strftime('%d.%m.%Y')}")
    if missing:
        lines.extend(["", "⚠️ <b>Не распознано:</b> " + ", ".join(h(FIELD_LABELS.get(field, field)) for field in missing)])
    del include_name_debug  # Structured debtor_full_name is the source of truth.
    return "\n".join(lines)
