from __future__ import annotations

from datetime import date
from pathlib import Path

from app.config import Settings
from app.models import Case, User
from app.services.document_templates import DocumentArtifacts, create_case_documents as _create_case_documents
from app.services.legal_data import (
    FIELD_LABELS,
    clean_case_number,
    clean_uid,
    format_money_rub_kop,
    is_deadline_missed,
    missing_order_fields,
    normalize_debtor_name_fields,
    normalize_order_data,
    suggest_nominative_full_name,
    validate_before_generation,
)
from app.utils import h


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
) -> str:
    data = normalize_order_data(data)
    lines = [
        "🔎 <b>Проверьте данные</b>",
        "",
        f"<b>Суд:</b> {h(data.get('court_name') or 'не заполнено')}",
        f"<b>Адрес суда:</b> {h(data.get('court_address') or 'не заполнено')}",
        f"<b>Должник:</b> {h(data.get('debtor_full_name') or 'не заполнено')}",
        f"<b>Адрес должника:</b> {h(data.get('debtor_address') or 'не заполнено')}",
        f"<b>Взыскатель:</b> {h(data.get('creditor_name') or 'не заполнено')}",
        f"<b>Номер дела:</b> {h(data.get('case_number') or 'не заполнено')}",
        f"<b>УИД:</b> {h(data.get('uid') or 'нет в приказе')}",
        f"<b>Дата приказа:</b> {h(data.get('order_date') or 'не заполнено')}",
        f"<b>Договор:</b> {h(data.get('debt_contract') or 'не заполнено')}",
        f"<b>Период:</b> {h(data.get('debt_period') or 'не заполнено')}",
        f"<b>Сумма долга:</b> {h(data.get('debt_amount') or 'не заполнено')}",
        f"<b>Госпошлина:</b> {h(data.get('state_duty') or 'не указана')}",
        f"<b>Дата получения:</b> {received_date.strftime('%d.%m.%Y') if received_date else 'не указана'}",
    ]
    if deadline_date:
        lines.append(f"<b>Срок до:</b> {deadline_date.strftime('%d.%m.%Y')} включительно")
    if include_name_debug:
        raw_name = data.get("debtor_name_raw") or ""
        if raw_name and raw_name != data.get("debtor_full_name"):
            lines.append(f"<i>Исходно распознано:</i> {h(raw_name)}")
            lines.append(f"<i>Нормализовано:</i> {h(data.get('debtor_full_name') or '')}")
    if missing:
        labels = [FIELD_LABELS.get(field, field) for field in missing]
        lines.extend(["", "⚠️ <b>Перед генерацией нужно заполнить:</b>", ", ".join(labels)])
    else:
        lines.extend(["", "Если все верно, можно готовить документы. Если видите ошибку OCR, исправьте поле кнопкой ниже."])
    return "\n".join(lines)
