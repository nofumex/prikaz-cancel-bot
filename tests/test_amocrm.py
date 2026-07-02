import pytest

from app.config import Settings
from app.models import Case, User
from app.services.amocrm import AmoCrmService, EVENT_STATUS_MAP


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
        max_debug_raw_updates=False,
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
        max_ai_review_regenerations=1,
        document_price_rub=990,
        document_preview_mode="pdf",
        enable_pdf_preview=True,
        require_pdf_preview_for_payment=True,
        allow_dev_docx_preview=False,
        document_template_version="test",
        show_user_confirmation_step=False,
        yoomoney_receiver=None,
        yoomoney_success_url=None,
        yoomoney_notification_secret=None,
        yookassa_enabled=False,
        yookassa_shop_id=None,
        yookassa_secret_key=None,
        yookassa_return_url=None,
        yookassa_webhook_path="/payments/yookassa",
        yookassa_test_mode=False,
        yookassa_receipt_enabled=True,
        yookassa_vat_code=1,
        yookassa_payment_subject="service",
        yookassa_payment_mode="full_payment",
        yookassa_receipt_description="test",
        yookassa_test_customer_email="test@example.com",
        yookassa_tax_system_code=None,
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
        amocrm_file_upload_enabled=True,
        amocrm_file_upload_timeout_seconds=30,
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


@pytest.mark.asyncio
async def test_crm_disabled_does_not_crash():
    service = AmoCrmService(_settings(amocrm_enabled=False))
    case = Case(id=1, user_id=1)
    user = User(id=1, platform="telegram", platform_user_id="1", telegram_id=1)
    await service.sync_case_event(None, case, user, "user_started_bot")


def test_event_status_map_contains_required_stages():
    assert EVENT_STATUS_MAP["user_started_bot"] == "Подписался на бота"
    assert EVENT_STATUS_MAP["order_photo_uploaded"] == "Отправил приказ"
    assert EVENT_STATUS_MAP["received_date_entered"] == "Указал дату"
    assert EVENT_STATUS_MAP["payment_paid"] == "Оплатил"
    assert EVENT_STATUS_MAP["documents_delivered"] == "Оплатил"
    assert EVENT_STATUS_MAP["document_qa_failed"] == "Указал дату"


@pytest.mark.asyncio
async def test_attach_file_to_lead_uploads_and_links_file(tmp_path):
    service = AmoCrmService(_settings(amocrm_enabled=True))
    calls = []

    async def fake_request(method, path, *, json_body=None, params=None, files=None, retries=3):
        calls.append((method, path, json_body, params))
        if method == "GET" and path == "/account":
            return {"drive_url": "https://drive.example.amocrm.ru"}, None
        if method == "PUT" and path == "/leads/123/files":
            return {}, None
        return {}, None

    raw_calls = []

    async def fake_raw(method, url, *, json_body=None, data=None, content_type=None):
        raw_calls.append((method, url, json_body, data, content_type))
        if url.endswith("/v1.0/sessions"):
            return {"upload_url": "https://drive.example.amocrm.ru/v1.0/sessions/upload/token", "max_part_size": 100}, None
        return {"uuid": "file-uuid-1"}, None

    notes = []

    async def fake_note(case, text):
        notes.append(text)
        return True

    service.request = fake_request
    service._request_raw_url = fake_raw
    service.add_lead_note = fake_note
    file_path = tmp_path / "statement.pdf"
    file_path.write_bytes(b"pdf data")
    case = Case(id=1, user_id=1, amocrm_lead_id=123)

    assert await service.attach_file_to_lead(case, file_path, "Превью PDF") is True

    assert ("PUT", "/leads/123/files", [{"file_uuid": "file-uuid-1"}], None) in calls
    assert raw_calls[0][2]["file_name"] == "statement.pdf"
    assert raw_calls[0][2]["content_type"] == "application/pdf"
    assert raw_calls[1][3] == b"pdf data"
    assert notes == ["Превью PDF: файл загружен в amoCRM (uuid: file-uuid-1)"]
    assert all("fallback path" not in note for note in notes)


@pytest.mark.asyncio
async def test_attach_file_to_lead_fallback_note_includes_api_error(tmp_path):
    service = AmoCrmService(_settings(amocrm_enabled=True))

    async def fake_upload(path):
        return None, "HTTP 403: access denied"

    notes = []

    async def fake_note(case, text):
        notes.append(text)
        return True

    service.upload_file_to_drive = fake_upload
    service.add_lead_note = fake_note
    file_path = tmp_path / "order.jpg"
    file_path.write_bytes(b"jpg")
    case = Case(id=1, user_id=1, amocrm_lead_id=123)

    assert await service.attach_file_to_lead(case, file_path, "Фото приказа") is True

    assert "Файл не загружен в amoCRM, fallback path" in notes[0]
    assert "Причина: HTTP 403: access denied" in notes[0]
