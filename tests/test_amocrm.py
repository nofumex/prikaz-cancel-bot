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
        require_pdf_preview_for_payment=True,
        allow_dev_docx_preview=False,
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
