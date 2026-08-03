from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import get_settings
from app.services import llm


def _settings():
    current = get_settings()
    return current.__class__(
        **{
            **current.__dict__,
            "order_recognition_mode": "tesseract_text",
            "order_type_precheck_model": "gpt-4.1-mini",
        }
    )


@pytest.mark.asyncio
async def test_precheck_uses_cheap_request_and_separate_usage_operation(monkeypatch):
    response = llm.LLMResult(
        data={"classification": "court_order"},
        usage={},
        model="cheap-vision",
        request_id="precheck-1",
        latency_ms=3,
    )
    request = AsyncMock(return_value=response)
    usage = AsyncMock()
    monkeypatch.setattr(llm, "prepare_order_preflight_image", lambda *args, **kwargs: "small.jpg")
    monkeypatch.setattr(llm, "_responses_json", request)
    monkeypatch.setattr(llm, "record_openai_usage", usage)

    result = await llm.classify_order_image_preflight(
        _settings(), None, case_id=None, user_id=22, order_photo_path="large.jpg"
    )

    assert result == "court_order"
    assert request.await_args.kwargs["image_path"] == "small.jpg"
    assert request.await_args.kwargs["image_detail"] == "low"
    assert request.await_args.kwargs["max_output_tokens"] == 32
    assert request.await_args.kwargs["schema"] == llm.ORDER_TYPE_PRECHECK_SCHEMA
    assert usage.await_args.kwargs["operation"] == "order_type_precheck"


@pytest.mark.parametrize("classification", ["other", "unclear"])
@pytest.mark.asyncio
async def test_precheck_rejects_non_order_before_tesseract_and_main_extraction(
    monkeypatch, classification
):
    precheck = AsyncMock(return_value=classification)
    tesseract = AsyncMock()
    extraction = AsyncMock()
    monkeypatch.setattr(llm, "classify_order_image_preflight", precheck)
    monkeypatch.setattr(llm, "extract_fast_tesseract_text", tesseract)
    monkeypatch.setattr(llm, "extract_order_data_from_tesseract_ai", extraction)

    result = await llm.extract_order_data(
        _settings(),
        None,
        case_id=11,
        user_id=22,
        order_photo_path="not-an-order.jpg",
    )

    assert result["_document_kind"] == classification
    assert result["_preflight_blocked"] == "1"
    precheck.assert_awaited_once()
    tesseract.assert_not_awaited()
    extraction.assert_not_awaited()


@pytest.mark.asyncio
async def test_precheck_allows_court_order_pipeline_exactly_once(monkeypatch):
    precheck = AsyncMock(return_value="court_order")
    ocr_result = SimpleNamespace(latency_ms=7)
    extraction_result = SimpleNamespace(
        data={"court_name": "судебный участок № 1"},
        safe_to_generate=True,
        issues=[],
        debtor_name_occurrences=[],
        ocr=ocr_result,
        llm_result=SimpleNamespace(latency_ms=9, request_id="req-1", model="test-model"),
    )
    tesseract = AsyncMock(return_value=ocr_result)
    extraction = AsyncMock(return_value=extraction_result)
    monkeypatch.setattr(llm, "classify_order_image_preflight", precheck)
    monkeypatch.setattr(llm, "extract_fast_tesseract_text", tesseract)
    monkeypatch.setattr(llm, "extract_order_data_from_tesseract_ai", extraction)

    result = await llm.extract_order_data(
        _settings(),
        None,
        case_id=None,
        user_id=22,
        order_photo_path="court-order.jpg",
    )

    assert result["court_name"] == "судебного участка № 1"
    precheck.assert_awaited_once()
    tesseract.assert_awaited_once()
    extraction.assert_awaited_once()
