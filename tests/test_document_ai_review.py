from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import get_settings
from app.handlers.case_flow import _notify_admin_qa_failure
from app.models import Case
from app.services.documents import (
    _review_blocks_delivery,
    _review_scoped_to_final_text,
    _safe_review_fixes,
    create_case_documents_reviewed,
    document_ai_review_mode,
)


FIXED_ADDRESS = "\u0433. \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, \u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e, \u0434. 14"


def test_ai_review_safe_fix_requires_confidence_and_safe_field():
    data = {"debtor_address": "\u0430\u0434\u0440\u0435\u0441: \u0433. \u0410\u0447\u0438\u043d\u0441\u043a, \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c\u0443 \u0432 \u0433\u043e\u0440\u043e\u0434\u0435 \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438"}
    review = {
        "issues": [
            {
                "field": "debtor_address",
                "severity": "blocker",
                "confidence": 0.95,
                "suggested_fix": FIXED_ADDRESS,
            },
            {"field": "case_number", "severity": "blocker", "confidence": 0.99, "suggested_fix": "2-123/2026"},
        ],
        "clean_fields": {
            "debtor_address": FIXED_ADDRESS,
            "case_number": "2-123/2026",
        },
    }

    assert _safe_review_fixes(data, review) == {"debtor_address": FIXED_ADDRESS}


def test_ai_review_blocks_delivery_when_blocker_has_no_safe_fix():
    review = {
        "ok": False,
        "severity": "blocker",
        "needs_regeneration": False,
        "issues": [
            {"field": "debtor_address", "severity": "blocker", "confidence": 0.5, "suggested_fix": ""},
        ],
    }

    assert _review_blocks_delivery(review, {}) is True


def test_ai_review_allows_delivery_after_auto_fixed_blocker():
    review = {
        "ok": True,
        "severity": "ok",
        "needs_regeneration": False,
        "issues": [],
    }

    assert _review_blocks_delivery(review, {"debtor_address": FIXED_ADDRESS}) is False


def test_source_data_passport_with_clean_final_text_is_not_blocker():
    review = {
        "ok": False,
        "severity": "blocker",
        "needs_regeneration": True,
        "issues": [
            {"field": "source_data", "severity": "blocker", "code": "PASSPORT", "message": "passport data in OCR"},
        ],
    }

    scoped = _review_scoped_to_final_text(review, "????????? ?? ?????? ????????? ???????. ?????????? ?????? ???.")

    assert _review_blocks_delivery(scoped, {}) is False
    assert scoped["issues"][0]["severity"] == "warning"
    assert scoped["issues"][0]["source_only"] is True


def test_final_text_passport_remains_blocker():
    review = {
        "ok": False,
        "severity": "blocker",
        "needs_regeneration": False,
        "issues": [
            {"field": "statement", "severity": "blocker", "code": "PASSPORT", "message": "passport in final text"},
        ],
    }

    scoped = _review_scoped_to_final_text(review, "\u041f\u0430\u0441\u043f\u043e\u0440\u0442 1234 567890 \u0432\u044b\u0434\u0430\u043d \u0423\u0424\u041c\u0421 \u0432\u043a\u043b\u044e\u0447\u0435\u043d \u0432 \u0437\u0430\u044f\u0432\u043b\u0435\u043d\u0438\u0435.")

    assert _review_blocks_delivery(scoped, {}) is True


def test_warning_without_blocker_does_not_need_manual_review():
    review = {
        "ok": False,
        "severity": "warning",
        "needs_regeneration": True,
        "issues": [{"field": "creditor_address", "severity": "warning", "code": "MULTIPLE_CREDITOR_ADDRESSES"}],
        "clean_fields": {"creditor_address": FIXED_ADDRESS},
    }

    scoped = _review_scoped_to_final_text(review, "????????? ????? ??? ????????? ??????.")

    assert _review_blocks_delivery(scoped, {}) is False


def test_document_ai_review_mode_defaults_to_shadow(monkeypatch):
    monkeypatch.delenv("DOCUMENT_AI_REVIEW_MODE", raising=False)
    get_settings.cache_clear()
    try:
        assert document_ai_review_mode(get_settings()) == "shadow"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_shadow_mode_returns_documents_without_waiting_for_ai(monkeypatch, tmp_path):
    full_docx = tmp_path / "full.docx"
    full_docx.write_bytes(b"docx")
    artifacts = SimpleNamespace(full_docx_path=full_docx, qa_report={})
    scheduled = []
    settings = get_settings()
    settings = SimpleNamespace(**{**settings.__dict__, "document_ai_review_mode": "shadow"})
    case = Case(id=10, user_id=1, extracted_json="{}")
    user = SimpleNamespace(id=1)

    monkeypatch.setattr("app.services.documents.create_case_documents_with_qa", lambda *args, **kwargs: artifacts)
    monkeypatch.setattr("app.services.documents.docx_text", lambda path: "clean final text")
    monkeypatch.setattr("app.services.documents._schedule_shadow_document_review", lambda *args, **kwargs: scheduled.append(kwargs))
    monkeypatch.setattr(
        "app.services.documents.review_generated_document",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AI review must not run inline in shadow mode")),
    )

    outcome = await create_case_documents_reviewed(case, user, settings, None)

    assert outcome.ok is True
    assert outcome.artifacts is artifacts
    assert scheduled and scheduled[0]["case_id"] == 10


@pytest.mark.asyncio
async def test_admin_debug_false_does_not_send_ai_report_to_user_chat():
    bot = SimpleNamespace(send_message=AsyncMock())
    settings = SimpleNamespace(admin_ids={123}, admin_debug_to_telegram=False)
    case = Case(id=1)

    await _notify_admin_qa_failure(bot, settings, case, "AI document review: case #1")

    bot.send_message.assert_not_awaited()
