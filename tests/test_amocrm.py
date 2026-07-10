import pytest

from app.config import Settings
from app.models import Case, User
from app.services.amocrm import AmoCrmService, EVENT_STATUS_MAP, _stage_can_move, crm_event_dedupe_key


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
        ai_review_model="gpt-4.1",
        ai_review_fallback_model="gpt-4.1-mini",
        llm_timeout_seconds=90,
        max_ai_review_regenerations=1,
        document_ai_review_mode="shadow",
        admin_debug_to_telegram=False,
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
    assert EVENT_STATUS_MAP["document_qa_failed"] == "Указал дату"
    assert EVENT_STATUS_MAP["reminder_sent"] == "Получил напоминание"
    assert EVENT_STATUS_MAP["payment_abandoned"] == "Получил напоминание"
    assert EVENT_STATUS_MAP["payment_paid"] == "Оплатил"
    assert EVENT_STATUS_MAP["documents_delivered"] == "Оплатил"
    assert EVENT_STATUS_MAP["paid_court_followup_sent"] == "Получил предложение о консультации"
    assert EVENT_STATUS_MAP["consultation_offer_sent"] == "Получил предложение о консультации"


def test_notification_stages_are_right_of_bot_stages_and_paid_after_reminder_still_allowed():
    from app.services.amocrm import STAGE_RANK

    assert STAGE_RANK["Оплатил"] < STAGE_RANK["Получил напоминание"]
    assert STAGE_RANK["Оплатил"] < STAGE_RANK["Получил предложение о консультации"]
    assert _stage_can_move(
        1,
        1,
        "Получил напоминание",
        "Оплатил",
    ) is True
    assert _stage_can_move(
        1,
        1,
        "Оплатил",
        "Получил предложение о консультации",
    ) is True



@pytest.mark.asyncio
async def test_attach_file_to_lead_uploads_and_creates_visible_attachment_note(tmp_path):
    service = AmoCrmService(_settings(amocrm_enabled=True))
    calls = []

    async def fake_request(method, path, *, json_body=None, params=None, files=None, retries=3):
        calls.append((method, path, json_body, params))
        if method == "GET" and path == "/account":
            return {"drive_url": "https://drive.example.amocrm.ru"}, None
        if method == "PUT" and path == "/leads/123/files":
            return {}, None
        if method == "GET" and path == "/leads/123/files":
            return {"_embedded": {"files": [{"file_uuid": "file-uuid-1", "name": "statement.pdf"}]}}, None
        if method == "POST" and path == "/leads/123/notes":
            assert json_body[0]["note_type"] == "attachment"
            assert json_body[0]["params"]["file_uuid"] == "file-uuid-1"
            assert json_body[0]["params"]["file_name"] == "statement.pdf"
            return {"_embedded": {"notes": [{"id": 777}]}}, None
        if method == "GET" and path == "/leads/123/notes":
            assert params == {"filter[note_type]": "attachment", "limit": 250}
            return {"_embedded": {"notes": [{"id": 777, "note_type": "attachment", "params": {"file_uuid": "file-uuid-1", "file_name": "statement.pdf"}}]}}, None
        return {}, None

    raw_calls = []

    async def fake_raw(method, url, *, json_body=None, data=None, content_type=None):
        raw_calls.append((method, url, json_body, data, content_type))
        if url.endswith("/v1.0/sessions"):
            return {"upload_url": "https://drive.example.amocrm.ru/v1.0/sessions/upload/token", "max_part_size": 100}, None
        return {"uuid": "file-uuid-1", "file_name": "statement.pdf"}, None

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

    assert await service.attach_file_to_lead(case, file_path, "Preview PDF") is True

    assert ("PUT", "/leads/123/files", [{"file_uuid": "file-uuid-1"}], None) in calls
    assert any(call[0] == "POST" and call[1] == "/leads/123/notes" and call[2][0]["note_type"] == "attachment" for call in calls)
    assert raw_calls[0][2]["file_name"] == "statement.pdf"
    assert raw_calls[0][2]["content_type"] == "application/pdf"
    assert raw_calls[1][3] == b"pdf data"
    assert notes == []



@pytest.mark.asyncio
async def test_attach_file_to_lead_fallback_note_includes_api_error(tmp_path):
    service = AmoCrmService(_settings(amocrm_enabled=True))

    async def fake_upload(path):
        return None, "HTTP 403: access denied"

    notes = []

    async def fake_note(case, text):
        notes.append(text)
        return True

    service.upload_file_to_drive_info = fake_upload
    service.add_lead_note = fake_note
    file_path = tmp_path / "order.jpg"
    file_path.write_bytes(b"jpg")
    case = Case(id=1, user_id=1, amocrm_lead_id=123)

    assert await service.attach_file_to_lead(case, file_path, "\u0424\u043e\u0442\u043e \u043f\u0440\u0438\u043a\u0430\u0437\u0430") is False

    assert "\u0444\u0430\u0439\u043b \u043d\u0435 \u043f\u0440\u0438\u043a\u0440\u0435\u043f\u043b\u0435\u043d \u043a \u0441\u0434\u0435\u043b\u043a\u0435" in notes[0]
    assert "\u041f\u0440\u0438\u0447\u0438\u043d\u0430: HTTP 403: access denied" in notes[0]
    assert "fallback path" not in notes[0]



def test_crm_stage_moves_forward_only_inside_same_case():
    assert _stage_can_move(63, 63, "\u041e\u0442\u043f\u0440\u0430\u0432\u0438\u043b \u043f\u0440\u0438\u043a\u0430\u0437", "\u0423\u043a\u0430\u0437\u0430\u043b \u0434\u0430\u0442\u0443") is True
    assert _stage_can_move(63, 63, "\u041e\u043f\u043b\u0430\u0442\u0438\u043b", "\u041f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0441\u044f \u043d\u0430 \u0431\u043e\u0442\u0430") is False
    assert _stage_can_move(63, 64, "\u041e\u043f\u043b\u0430\u0442\u0438\u043b", "\u041f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0441\u044f \u043d\u0430 \u0431\u043e\u0442\u0430") is True


def test_crm_event_dedupe_key_uses_stable_payload_fields():
    first = crm_event_dedupe_key(63, "order_photo_uploaded", {"files": [{"path": "b.jpg"}, {"path": "a.jpg"}]})
    same = crm_event_dedupe_key(63, "order_photo_uploaded", {"files": [{"path": "a.jpg"}, {"path": "b.jpg"}]})
    different = crm_event_dedupe_key(63, "order_photo_uploaded", {"files": [{"path": "c.jpg"}]})

    assert first == same
    assert first != different


@pytest.mark.asyncio
async def test_duplicate_crm_event_skips_note_and_network_calls():
    service = AmoCrmService(_settings(amocrm_enabled=True))
    case = Case(id=63, user_id=1, amocrm_lead_id=123, amocrm_status_name="\u041e\u0442\u043f\u0440\u0430\u0432\u0438\u043b \u043f\u0440\u0438\u043a\u0430\u0437")
    user = User(id=1, platform="telegram", platform_user_id="1", amocrm_current_case_id=63)
    notes = []
    requests = []

    class ExistingResult:
        def scalar_one_or_none(self):
            return 1

    class FakeSession:
        async def execute(self, stmt):
            return ExistingResult()

    async def fake_note(case, text):
        notes.append(text)
        return True

    async def fake_request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return {}, None

    service.add_lead_note = fake_note
    service.request = fake_request

    await service.sync_case_event(
        FakeSession(),
        case,
        user,
        "order_photo_uploaded",
        {"files": [{"path": "same-order.jpg", "caption": "order"}]},
    )

    assert notes == []
    assert requests == []


@pytest.mark.asyncio
async def test_update_lead_status_skips_backward_without_new_cycle():
    service = AmoCrmService(_settings(amocrm_enabled=True))
    calls = []

    async def fake_request(method, path, *, json_body=None, params=None, files=None, retries=3):
        calls.append((method, path, json_body))
        return {}, None

    service.request = fake_request
    case = Case(id=63, user_id=1, amocrm_lead_id=123, amocrm_status_name="\u041e\u043f\u043b\u0430\u0442\u0438\u043b")

    assert await service.update_lead_status(case, "\u041f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0441\u044f \u043d\u0430 \u0431\u043e\u0442\u0430", current_case_id=63, event_case_id=63) is True
    assert calls == []
    assert case.amocrm_status_name == "\u041e\u043f\u043b\u0430\u0442\u0438\u043b"


@pytest.mark.asyncio
async def test_update_lead_status_allows_reset_for_new_cycle():
    service = AmoCrmService(_settings(amocrm_enabled=True))
    calls = []

    async def fake_ensure_pipeline():
        return {
            "id": 1,
            "_embedded": {"statuses": [{"id": 10, "name": "\u041f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0441\u044f \u043d\u0430 \u0431\u043e\u0442\u0430"}]},
        }

    async def fake_request(method, path, *, json_body=None, params=None, files=None, retries=3):
        calls.append((method, path, json_body))
        return {}, None

    async def fake_ensure_statuses(pipeline_id):
        return {"\u041f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0441\u044f \u043d\u0430 \u0431\u043e\u0442\u0430": 10}

    service.ensure_pipeline = fake_ensure_pipeline
    service.ensure_statuses = fake_ensure_statuses
    service.request = fake_request
    case = Case(id=64, user_id=1, amocrm_lead_id=123, amocrm_status_name="\u041e\u043f\u043b\u0430\u0442\u0438\u043b")

    assert await service.update_lead_status(case, "\u041f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0441\u044f \u043d\u0430 \u0431\u043e\u0442\u0430", current_case_id=63, event_case_id=64) is True
    assert calls == [("PATCH", "/leads", [{"id": 123, "pipeline_id": 1, "status_id": 10}])]
    assert case.amocrm_status_name == "\u041f\u043e\u0434\u043f\u0438\u0441\u0430\u043b\u0441\u044f \u043d\u0430 \u0431\u043e\u0442\u0430"



@pytest.mark.asyncio
async def test_attach_file_to_lead_requires_verified_link(tmp_path):
    service = AmoCrmService(_settings(amocrm_enabled=True))

    async def fake_upload(path):
        return {"file_uuid": "file-uuid-1", "file_name": "statement.pdf"}, None

    async def fake_link(lead_id, file_uuid):
        return True, None

    async def fake_verify(lead_id, file_uuid):
        return False, "not visible on lead", None

    notes = []

    async def fake_note(case, text):
        notes.append(text)
        return True

    service.upload_file_to_drive_info = fake_upload
    service.link_file_to_lead = fake_link
    service.verify_file_linked_to_lead = fake_verify
    service.add_lead_note = fake_note
    file_path = tmp_path / "statement.pdf"
    file_path.write_bytes(b"pdf")
    case = Case(id=1, user_id=1, amocrm_lead_id=123)

    assert await service.attach_file_to_lead(case, file_path, "Preview PDF") is False
    assert "not visible on lead" in notes[0]
    assert "fallback path" not in notes[0]


@pytest.mark.asyncio
async def test_sync_case_event_with_file_fails_until_attach_verified():
    service = AmoCrmService(_settings(amocrm_enabled=True))
    case = Case(id=1, user_id=1, amocrm_lead_id=123)
    user = User(id=1, platform="telegram", platform_user_id="1", amocrm_current_case_id=1)
    logs = []
    notes = []

    async def fake_update(case, status_name, **kwargs):
        return True

    async def fake_attach(case, path, caption):
        return False

    async def fake_note(case, text):
        notes.append(text)
        return True

    async def fake_log_sync(session, **kwargs):
        logs.append(kwargs)

    service.update_lead_status = fake_update
    service.attach_file_to_lead = fake_attach
    service.add_lead_note = fake_note
    service._log_sync = fake_log_sync

    class NoDuplicateSession:
        async def execute(self, stmt):
            class Result:
                def scalar_one_or_none(self):
                    return None
            return Result()

    with pytest.raises(RuntimeError, match="amoCRM file attach failed"):
        await service.sync_case_event(NoDuplicateSession(), case, user, "preview_generated", {"files": [{"path": "preview.pdf", "caption": "Preview"}]})

    assert logs[-1]["success"] is False
    assert "amoCRM file attach failed" in logs[-1]["error_message"]
    assert notes == []
