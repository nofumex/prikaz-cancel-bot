from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Case, User
from app.services.document_qa import run_document_qa
from app.services.document_templates.renderer import create_case_documents
from app.services.document_templates.statement_templates import StatementContext, build_attachments, build_statement_paragraphs
from app.services.document_templates.styles import A4_HEIGHT, A4_WIDTH, MARGIN_LEFT
from app.services.document_visual_qa import run_visual_qa
from app.services.legal_data import legal_deadline_from_received, normalize_order_data, validate_amounts
from app.services.pdf_tools import pdf_page_count, pdf_text
from docx import Document

BELSKY_DATA = {
    "court_name": "судебный участок №5 города Ессентуки Ставропольского края",
    "court_address": "357600, Ставропольский край, город Ессентуки, улица Шмидта, дом № 72",
    "debtor_name_raw": "Бельскому Владимиру Геннадьевичу",
    "debtor_full_name": "Бельский Владимир Геннадьевич",
    "debtor_address": "г. Ессентуки, ул. Володарского, д. 14, кв. 9",
    "creditor_name": "АО «Почта Банк»",
    "creditor_address": "107061, г. Москва, Преображенская пл., д. 8",
    "case_number": "2-146-09-434/2021",
    "uid": "26MS0031-01-2021-000169-72",
    "order_date": "18.01.2021",
    "debt_contract": "договор № 43006327 от 27 апреля 2019 года",
    "debt_period": "с 27.03.2020 по 28.11.2020",
    "debt_amount": "78 472 руб. 87 коп.",
    "state_duty": "1 277 руб. 00 коп.",
    "total_amount": "79 749 руб. 87 коп.",
}


def _settings(**kwargs):
    base = dict(
        telegram_bot_token="",
        max_bot_token="",
            max_api_base_url="https://platform-api2.max.ru",
            max_use_webhook=False,
            max_webhook_url=None,
            max_webhook_secret=None,
            max_webhook_host="0.0.0.0",
            max_webhook_port=8081,
            max_longpoll_timeout_seconds=30,
            max_download_dir="storage/max",
            max_upload_retry_attempts=5,
            max_upload_retry_base_seconds=1,
            max_admin_ids=set(),
        run_telegram=True,
        run_max=False,
        admin_ids=set(),
        manager_ids=set(),
        database_url="sqlite+aiosqlite:///:memory:",
        drop_pending_updates=True,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        vision_model="gpt-5.4-mini",
        text_model="gpt-5.4-mini",
        llm_timeout_seconds=90,
        document_price_rub=990,
        document_preview_mode="pdf",
        enable_pdf_preview=True,
        require_pdf_preview_for_payment=False,
        allow_dev_docx_preview=True,
        document_template_version="test",
        show_user_confirmation_step=False,
        yoomoney_receiver=None,
        yoomoney_success_url=None,
        yoomoney_notification_secret=None,
        payment_public_base_url=None,
        payment_web_host="0.0.0.0",
        payment_web_port=8080,
        openai_input_price_per_1m=0.75,
        openai_cached_input_price_per_1m=0.075,
        openai_output_price_per_1m=4.50,
        openai_model_pricing_json="",
        amocrm_base_url="https://example.amocrm.ru",
        amocrm_access_token="token",
        amocrm_enabled=False,
        amocrm_pipeline_name="Судебный приказ",
        amocrm_auto_create_pipeline=False,
        amocrm_auto_create_statuses=True,
        amocrm_attach_files=True,
        amocrm_debug=False,
        amocrm_rps_limit=5,
        amocrm_pipeline_id=None,
        crm_sync_background=True,
        crm_sync_timeout_seconds=5,
        crm_sync_max_attempts=3,
        crm_sync_retry_base_seconds=2,
        crm_sync_debug=False,
        amount_retry_on_mismatch=True,
        auto_recover_amount_mismatch=True,
        auto_recover_amount_min_confidence=0.75,
        company_name="test",
        manager_contact_text="test",
    )
    base.update(kwargs)
    return Settings(**base)


def _make_case(data: dict | None = None, *, envelope: bool = False) -> tuple[Case, User]:
    received = date(2026, 6, 19)
    user = User(id=1, platform="telegram", platform_user_id="test", telegram_id=1)
    case = Case(
        id=42,
        user_id=1,
        platform="telegram",
        status="processing",
        received_date=received,
        deadline_date=legal_deadline_from_received(received),
        extracted_json=json.dumps(normalize_order_data(data or BELSKY_DATA), ensure_ascii=False),
        envelope_photo_path="storage/photos/env.jpg" if envelope else None,
    )
    return case, user


def _generate(tmp_path, monkeypatch, **case_kwargs):
    monkeypatch.setattr("app.services.document_templates.renderer.DOCUMENT_DIR", tmp_path / "documents")
    case, user = _make_case(**case_kwargs)
    return create_case_documents(case, user, _settings())


def test_amounts_belsky_are_correct():
    check = validate_amounts(normalize_order_data(BELSKY_DATA))
    assert check.ok
    assert str(check.computed_total) == "79749.87"


def test_amount_mismatch_fails_qa():
    bad = dict(BELSKY_DATA)
    bad["debt_amount"] = "78 742 руб. 00 коп."
    check = validate_amounts(normalize_order_data(bad))
    assert not check.ok
    assert "amount_mismatch" in check.errors


def test_total_amount_computed_from_debt_and_state_duty():
    data = normalize_order_data({"debt_amount": "78 472 руб. 87 коп.", "state_duty": "1 277 руб. 00 коп."})
    assert data["total_amount"] == "79 749 руб. 87 коп."


def test_attachments_with_envelope():
    ctx = StatementContext(
        data=normalize_order_data(BELSKY_DATA),
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        document_date=date(2026, 6, 24),
        has_envelope=True,
    )
    items = build_attachments(ctx)
    assert any("конверта" in item for item in items)
    assert any("настоящих возражений" in item for item in items)


def test_attachments_with_manual_date():
    ctx = StatementContext(
        data=normalize_order_data(BELSKY_DATA),
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        document_date=date(2026, 6, 24),
        manual_date_only=True,
    )
    items = build_attachments(ctx)
    assert any("при наличии" in item for item in items)
    assert all("настоящего заявления" not in item for item in items)


def test_statement_uses_correct_title():
    ctx = StatementContext(
        data=normalize_order_data(BELSKY_DATA),
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        document_date=date(2026, 6, 24),
    )
    paragraphs = build_statement_paragraphs(ctx)
    assert "Считаю требования взыскателя спорными" not in " ".join(paragraphs)
    assert "78 472 руб. 87 коп." in paragraphs[0]
    assert "1 277 руб. 00 коп." in paragraphs[0]
    assert "всего" not in paragraphs[0].lower()


def test_statement_no_old_dispute_text():
    ctx = StatementContext(
        data=normalize_order_data(BELSKY_DATA),
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        document_date=date(2026, 6, 24),
    )
    text = " ".join(build_statement_paragraphs(ctx))
    for forbidden in (
        "искового производства",
        "проценты",
        "неустойк",
        "комисс",
        "Считаю требования",
    ):
        assert forbidden not in text


def test_statement_belsky_fits_one_page(tmp_path, monkeypatch):
    artifacts = _generate(tmp_path, monkeypatch)
    if artifacts.full_pdf_path:
        assert pdf_page_count(artifacts.full_pdf_path) == 1


def test_statement_has_a4_margins(tmp_path, monkeypatch):
    artifacts = _generate(tmp_path, monkeypatch)
    doc = Document(str(artifacts.full_docx_path))
    section = doc.sections[0]
    assert abs(section.page_width - A4_WIDTH) <= MARGIN_LEFT * 0.02 + MARGIN_LEFT * 0.001
    assert abs(section.page_height - A4_HEIGHT) <= MARGIN_LEFT * 0.02 + MARGIN_LEFT * 0.001


def test_statement_has_no_placeholder_fields(tmp_path, monkeypatch):
    from app.services.legal_data import docx_text

    artifacts = _generate(tmp_path, monkeypatch)
    text = docx_text(str(artifacts.full_docx_path))
    for token in ("________________", "{{", "None", "уточнить", "20__"):
        assert token not in text


def test_statement_signature_is_filled(tmp_path, monkeypatch):
    from app.services.legal_data import docx_text

    artifacts = _generate(tmp_path, monkeypatch)
    text = pdf_text(artifacts.full_pdf_path) if artifacts.full_pdf_path else ""
    if "Бельский В.Г." not in text:
        text = docx_text(str(artifacts.full_docx_path))
    assert "/Бельский В.Г./" in text or "Бельский В.Г." in text


def test_statement_no_weird_justified_spaces(tmp_path, monkeypatch):
    artifacts = _generate(tmp_path, monkeypatch)
    if artifacts.full_pdf_path:
        visual = run_visual_qa(
            full_docx=artifacts.full_docx_path,
            full_pdf=artifacts.full_pdf_path,
            preview_pdf=artifacts.preview_pdf_path,
            data=normalize_order_data(BELSKY_DATA),
            restore_term=False,
            amount_check=validate_amounts(normalize_order_data(BELSKY_DATA)),
        )
        assert "weird_justified_spaces" not in visual.errors


def test_proshu_not_orphaned_at_page_bottom(tmp_path, monkeypatch):
    from app.services.legal_data import docx_text

    artifacts = _generate(tmp_path, monkeypatch)
    if artifacts.full_pdf_path and pdf_page_count(artifacts.full_pdf_path) == 1:
        text = pdf_text(artifacts.full_pdf_path)
        if "ПРОШУ:" not in text:
            text = docx_text(str(artifacts.full_docx_path))
        assert "ПРОШУ:" in text
        assert "1. Отменить" in text


def test_document_qa_amount_mismatch(tmp_path):
    bad = normalize_order_data({**BELSKY_DATA, "debt_amount": "78 742 руб. 00 коп."})
    check = validate_amounts(bad)
    qa = run_document_qa(
        data=bad,
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        full_docx=None,
        full_pdf=None,
        preview_pdf=None,
        instruction_docx=None,
        require_preview_pdf=False,
        amount_check=check,
    )
    assert not qa.ok
    assert "amount_mismatch" in qa.bad_tokens
