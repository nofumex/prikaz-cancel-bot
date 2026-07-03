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

    scoped = _review_scoped_to_final_text(review, "Clean final statement text without passport data.")

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

    scoped = _review_scoped_to_final_text(review, "Clean final text without critical errors.")

    assert _review_blocks_delivery(scoped, {}) is False


def test_document_ai_review_mode_defaults_to_autofix(monkeypatch):
    monkeypatch.delenv("DOCUMENT_AI_REVIEW_MODE", raising=False)
    get_settings.cache_clear()
    try:
        assert document_ai_review_mode(get_settings()) == "autofix"
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



@pytest.mark.asyncio
async def test_autofix_mode_applies_clean_fields_and_rechecks_final_text(monkeypatch, tmp_path):
    first_docx = tmp_path / "first.docx"
    second_docx = tmp_path / "second.docx"
    first_docx.write_bytes(b"first")
    second_docx.write_bytes(b"second")
    artifacts = [SimpleNamespace(full_docx_path=first_docx, qa_report={}), SimpleNamespace(full_docx_path=second_docx, qa_report={})]
    review_calls = []
    settings = get_settings()
    settings = SimpleNamespace(**{**settings.__dict__, "document_ai_review_mode": "autofix", "max_ai_review_regenerations": 1})
    case = Case(id=11, user_id=1, extracted_json='{"debtor_full_name":"\\u0418\\u0432\\u0430\\u043d\\u043e\\u0432\\u0443 \\u0418\\u0432\\u0430\\u043d\\u0443"}')
    user = SimpleNamespace(id=1)
    session = SimpleNamespace(commit=AsyncMock())

    def fake_generate(*args, **kwargs):
        return artifacts.pop(0)

    def fake_docx_text(path):
        return "bad final" if path.endswith("first.docx") else "clean final"

    async def fake_review(settings, session, *, case_id, user_id, document_text, source_data, visual_summary, regeneration_happened):
        review_calls.append((document_text, source_data, regeneration_happened))
        if not regeneration_happened:
            return {
                "ok": False,
                "severity": "blocker",
                "needs_regeneration": True,
                "confidence": 0.95,
                "issues": [{"field": "debtor_full_name", "severity": "blocker", "confidence": 0.95, "suggested_fix": "\u0418\u0432\u0430\u043d\u043e\u0432 \u0418\u0432\u0430\u043d \u0418\u0432\u0430\u043d\u043e\u0432\u0438\u0447", "message": "case", "code": "NAME_CASE"}],
                "clean_fields": {"debtor_full_name": "\u0418\u0432\u0430\u043d\u043e\u0432 \u0418\u0432\u0430\u043d \u0418\u0432\u0430\u043d\u043e\u0432\u0438\u0447"},
            }
        return {"ok": True, "severity": "ok", "needs_regeneration": False, "confidence": 1.0, "issues": [], "clean_fields": {}}

    monkeypatch.setattr("app.services.documents.create_case_documents_with_qa", fake_generate)
    monkeypatch.setattr("app.services.documents.docx_text", fake_docx_text)
    monkeypatch.setattr("app.services.documents.review_generated_document", fake_review)

    outcome = await create_case_documents_reviewed(case, user, settings, session)

    assert outcome.ok is True
    assert outcome.regeneration_count == 1
    assert outcome.applied_fixes == {"debtor_full_name": "\u0418\u0432\u0430\u043d\u043e\u0432 \u0418\u0432\u0430\u043d \u0418\u0432\u0430\u043d\u043e\u0432\u0438\u0447"}
    assert len(review_calls) == 2
    assert review_calls[1][2] is True
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_autofix_mode_blocks_when_safe_fix_is_unavailable(monkeypatch, tmp_path):
    full_docx = tmp_path / "full.docx"
    full_docx.write_bytes(b"docx")
    settings = get_settings()
    settings = SimpleNamespace(**{**settings.__dict__, "document_ai_review_mode": "autofix", "max_ai_review_regenerations": 1})
    case = Case(id=12, user_id=1, extracted_json="{}")
    user = SimpleNamespace(id=1)

    monkeypatch.setattr("app.services.documents.create_case_documents_with_qa", lambda *args, **kwargs: SimpleNamespace(full_docx_path=full_docx, qa_report={}))
    monkeypatch.setattr("app.services.documents.docx_text", lambda path: "\u041f\u0430\u0441\u043f\u043e\u0440\u0442 1234 \u0432 \u0444\u0438\u043d\u0430\u043b\u044c\u043d\u043e\u043c \u0442\u0435\u043a\u0441\u0442\u0435")

    async def fake_review(*args, **kwargs):
        return {
            "ok": False,
            "severity": "blocker",
            "needs_regeneration": False,
            "confidence": 0.9,
            "issues": [{"field": "statement", "severity": "blocker", "confidence": 0.9, "suggested_fix": "", "message": "passport", "code": "PASSPORT"}],
            "clean_fields": {},
        }

    monkeypatch.setattr("app.services.documents.review_generated_document", fake_review)

    outcome = await create_case_documents_reviewed(case, user, settings, None)

    assert outcome.ok is False
    assert outcome.regeneration_count == 0



@pytest.mark.asyncio
async def test_review_generated_document_retries_then_fallback_model(monkeypatch):
    from app.services import llm

    settings = SimpleNamespace(
        openai_api_key="key",
        ai_review_model="gpt-4.1",
        ai_review_fallback_model="gpt-4.1-mini",
        text_model="gpt-4.1",
    )
    calls = []

    async def fake_responses_json(settings, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) < 3:
            raise RuntimeError("empty structured output")
        return SimpleNamespace(
            data={
                "ok": True,
                "severity": "ok",
                "needs_regeneration": False,
                "confidence": 1.0,
                "issues": [],
                "clean_fields": {},
            },
            model=kwargs["model"],
        )

    async def fake_record(*args, **kwargs):
        return None

    monkeypatch.setattr(llm, "_responses_json", fake_responses_json)
    monkeypatch.setattr(llm, "record_openai_usage", fake_record)

    review = await llm.review_generated_document(
        settings,
        None,
        case_id=1,
        user_id=1,
        document_text="clean",
        source_data={},
        visual_summary={},
    )

    assert review["ok"] is True
    assert calls == ["gpt-4.1", "gpt-4.1", "gpt-4.1-mini"]


@pytest.mark.asyncio
async def test_autofix_ai_failure_delivers_deterministic_artifacts_and_logs_crm(monkeypatch, tmp_path):
    full_docx = tmp_path / "full.docx"
    full_docx.write_bytes(b"docx")
    artifacts = SimpleNamespace(full_docx_path=full_docx, qa_report={})
    scheduled = []
    settings = get_settings()
    settings = SimpleNamespace(**{**settings.__dict__, "document_ai_review_mode": "autofix", "amocrm_enabled": True})
    case = Case(id=13, user_id=1, extracted_json="{}")
    user = SimpleNamespace(id=1)

    async def broken_review(*args, **kwargs):
        raise RuntimeError("document_ai_review failed after retry/fallback")

    monkeypatch.setattr("app.services.documents.create_case_documents_with_qa", lambda *args, **kwargs: artifacts)
    monkeypatch.setattr("app.services.documents.docx_text", lambda path: "clean final")
    monkeypatch.setattr("app.services.documents.review_generated_document", broken_review)
    monkeypatch.setattr("app.services.crm_background.schedule_crm_sync", lambda *args, **kwargs: scheduled.append((args, kwargs)))

    outcome = await create_case_documents_reviewed(case, user, settings, None)

    assert outcome.ok is True
    assert outcome.artifacts is artifacts
    assert outcome.review["ai_review_failed"] is True
    assert scheduled[0][0][3] == "document_ai_review_failed"
