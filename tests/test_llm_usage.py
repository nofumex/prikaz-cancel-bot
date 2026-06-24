from app.config import Settings
from app.services.llm import _money_cost, _parse_usage, _pricing_for_model


def test_parse_usage_non_cached_tokens():
    usage = _parse_usage(
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "total_tokens": 1200,
                "input_tokens_details": {"cached_tokens": 400},
            }
        }
    )
    assert usage["cached_input_tokens"] == 400
    assert usage["non_cached_input_tokens"] == 600


def test_cost_not_double_counting_cached():
    settings = Settings(
        telegram_bot_token="",
        max_bot_token="",
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
        amocrm_base_url=None,
        amocrm_access_token=None,
        amocrm_enabled=False,
        amocrm_pipeline_name="Судебный приказ",
        amocrm_auto_create_pipeline=False,
        amocrm_auto_create_statuses=True,
        amocrm_attach_files=True,
        amocrm_debug=False,
        amocrm_rps_limit=5,
        amocrm_pipeline_id=None,
        amocrm_status_id_new=None,
        amocrm_status_id_in_progress=None,
        amocrm_status_id_consultation=None,
        amocrm_write_enabled=False,
        amount_retry_on_mismatch=True,
        auto_recover_amount_mismatch=True,
        auto_recover_amount_min_confidence=0.75,
        company_name="test",
        manager_contact_text="test",
    )
    prices = _pricing_for_model(settings, "gpt-5.4-mini")
    non_cached_cost = _money_cost(600, prices["input"])
    cached_cost = _money_cost(400, prices["cached_input"])
    output_cost = _money_cost(200, prices["output"])
    total = non_cached_cost + cached_cost + output_cost
    assert round(total, 6) == round((600 * 0.75 + 400 * 0.075 + 200 * 4.5) / 1_000_000, 6)


def test_generations_per_10_dollars():
    avg_cost = 0.02
    assert int(10 / avg_cost) == 500
