from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from os import getenv

from dotenv import load_dotenv


def _parse_int_set(raw: str | None) -> set[int]:
    result: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on", "да"}


def _parse_int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    max_bot_token: str
    max_api_base_url: str
    max_use_webhook: bool
    max_webhook_url: str | None
    max_webhook_secret: str | None
    max_webhook_host: str
    max_webhook_port: int
    max_longpoll_timeout_seconds: int
    max_download_dir: str
    max_upload_retry_attempts: int
    max_upload_retry_base_seconds: int
    max_admin_ids: set[int]
    max_debug_raw_updates: bool
    run_telegram: bool
    run_max: bool
    admin_ids: set[int]
    manager_ids: set[int]
    database_url: str
    drop_pending_updates: bool

    openai_api_key: str | None
    openai_base_url: str
    vision_model: str
    text_model: str
    llm_timeout_seconds: int
    max_ai_review_regenerations: int

    document_price_rub: int
    document_preview_mode: str
    enable_pdf_preview: bool
    require_pdf_preview_for_payment: bool
    allow_dev_docx_preview: bool
    document_template_version: str
    show_user_confirmation_step: bool
    yoomoney_receiver: str | None
    yoomoney_success_url: str | None
    yoomoney_notification_secret: str | None
    yookassa_enabled: bool
    yookassa_shop_id: str | None
    yookassa_secret_key: str | None
    yookassa_return_url: str | None
    yookassa_webhook_path: str
    yookassa_test_mode: bool
    yookassa_receipt_enabled: bool
    yookassa_vat_code: int
    yookassa_payment_subject: str
    yookassa_payment_mode: str
    yookassa_receipt_description: str
    yookassa_test_customer_email: str | None
    yookassa_tax_system_code: int | None
    payment_public_base_url: str | None
    payment_web_host: str
    payment_web_port: int

    openai_input_price_per_1m: float
    openai_cached_input_price_per_1m: float
    openai_output_price_per_1m: float
    openai_model_pricing_json: str

    amocrm_base_url: str | None
    amocrm_access_token: str | None
    amocrm_enabled: bool
    amocrm_pipeline_name: str
    amocrm_auto_create_pipeline: bool
    amocrm_auto_create_statuses: bool
    amocrm_attach_files: bool
    amocrm_file_upload_enabled: bool
    amocrm_file_upload_timeout_seconds: int
    amocrm_debug: bool
    amocrm_rps_limit: int
    amocrm_pipeline_id: int | None

    crm_sync_background: bool
    crm_sync_timeout_seconds: int
    crm_sync_max_attempts: int
    crm_sync_retry_base_seconds: int
    crm_sync_debug: bool

    amount_retry_on_mismatch: bool
    auto_recover_amount_mismatch: bool
    auto_recover_amount_min_confidence: float

    company_name: str
    manager_contact_text: str

    @property
    def staff_ids(self) -> set[int]:
        return self.admin_ids | self.manager_ids


@lru_cache
def get_settings() -> Settings:
    load_dotenv()
    admin_ids = _parse_int_set(getenv("ADMIN_IDS")) | _parse_int_set(getenv("ADMIN_ID"))
    manager_ids = _parse_int_set(getenv("MANAGER_IDS"))
    return Settings(
        telegram_bot_token=(getenv("TG_BOT_TOKEN") or getenv("TELEGRAM_BOT_TOKEN") or getenv("BOT_TOKEN") or "").strip(),
        max_bot_token=(getenv("MAX_BOT_TOKEN") or "").strip(),
        max_api_base_url=(getenv("MAX_API_BASE_URL") or "https://platform-api2.max.ru").strip().rstrip("/"),
        max_use_webhook=_parse_bool(getenv("MAX_USE_WEBHOOK"), False),
        max_webhook_url=(getenv("MAX_WEBHOOK_URL") or "").strip() or None,
        max_webhook_secret=(getenv("MAX_WEBHOOK_SECRET") or "").strip() or None,
        max_webhook_host=(getenv("MAX_WEBHOOK_HOST") or "0.0.0.0").strip(),
        max_webhook_port=_parse_int(getenv("MAX_WEBHOOK_PORT"), 8081),
        max_longpoll_timeout_seconds=_parse_int(getenv("MAX_LONGPOLL_TIMEOUT_SECONDS"), 30),
        max_download_dir=(getenv("MAX_DOWNLOAD_DIR") or "storage/max").strip(),
        max_upload_retry_attempts=_parse_int(getenv("MAX_UPLOAD_RETRY_ATTEMPTS"), 5),
        max_upload_retry_base_seconds=_parse_int(getenv("MAX_UPLOAD_RETRY_BASE_SECONDS"), 1),
        max_admin_ids=_parse_int_set(getenv("MAX_ADMIN_IDS")),
        max_debug_raw_updates=_parse_bool(getenv("MAX_DEBUG_RAW_UPDATES"), False),
        run_telegram=_parse_bool(getenv("RUN_TELEGRAM"), True),
        run_max=_parse_bool(getenv("RUN_MAX"), False),
        admin_ids=admin_ids,
        manager_ids=manager_ids,
        database_url=getenv("DATABASE_URL", "sqlite+aiosqlite:///data/prikaz_bot.sqlite3"),
        drop_pending_updates=_parse_bool(getenv("DROP_PENDING_UPDATES"), True),
        openai_api_key=(getenv("OPENAI_API_KEY") or "").strip() or None,
        openai_base_url=(getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
        vision_model=(getenv("VISION_MODEL") or "gpt-5.4-mini").strip(),
        text_model=(getenv("TEXT_MODEL") or getenv("VISION_MODEL") or "gpt-5.4-mini").strip(),
        llm_timeout_seconds=_parse_int(getenv("LLM_TIMEOUT_SECONDS"), 90),
        max_ai_review_regenerations=_parse_int(getenv("MAX_AI_REVIEW_REGENERATIONS"), 1),
        document_price_rub=_parse_int(getenv("DOCUMENT_PRICE_RUB"), 990),
        document_preview_mode=(getenv("DOCUMENT_PREVIEW_MODE") or "pdf").strip().lower(),
        enable_pdf_preview=_parse_bool(getenv("ENABLE_PDF_PREVIEW"), True),
        require_pdf_preview_for_payment=_parse_bool(getenv("REQUIRE_PDF_PREVIEW_FOR_PAYMENT"), True),
        allow_dev_docx_preview=_parse_bool(getenv("ALLOW_DEV_DOCX_PREVIEW"), False),
        document_template_version=(getenv("DOCUMENT_TEMPLATE_VERSION") or "2026-06-legal-v1").strip(),
        show_user_confirmation_step=_parse_bool(getenv("SHOW_USER_CONFIRMATION_STEP"), False),
        yoomoney_receiver=(getenv("YOOMONEY_RECEIVER") or "").strip() or None,
        yoomoney_success_url=(getenv("YOOMONEY_SUCCESS_URL") or "").strip() or None,
        yoomoney_notification_secret=(getenv("YOOMONEY_NOTIFICATION_SECRET") or "").strip() or None,
        yookassa_enabled=_parse_bool(getenv("YOOKASSA_ENABLED"), False),
        yookassa_shop_id=(getenv("YOOKASSA_SHOP_ID") or "").strip() or None,
        yookassa_secret_key=(getenv("YOOKASSA_SECRET_KEY") or "").strip() or None,
        yookassa_return_url=(getenv("YOOKASSA_RETURN_URL") or "").strip() or None,
        yookassa_webhook_path=(getenv("YOOKASSA_WEBHOOK_PATH") or "/payments/yookassa").strip() or "/payments/yookassa",
        yookassa_test_mode=_parse_bool(getenv("YOOKASSA_TEST_MODE"), False),
        yookassa_receipt_enabled=_parse_bool(getenv("YOOKASSA_RECEIPT_ENABLED"), False),
        yookassa_vat_code=_parse_int(getenv("YOOKASSA_VAT_CODE"), 1),
        yookassa_payment_subject=(getenv("YOOKASSA_PAYMENT_SUBJECT") or "service").strip(),
        yookassa_payment_mode=(getenv("YOOKASSA_PAYMENT_MODE") or "full_payment").strip(),
        yookassa_receipt_description=(getenv("YOOKASSA_RECEIPT_DESCRIPTION") or "Подготовка заявления об отмене судебного приказа").strip(),
        yookassa_test_customer_email=(getenv("YOOKASSA_TEST_CUSTOMER_EMAIL") or "").strip() or None,
        yookassa_tax_system_code=_parse_int(getenv("YOOKASSA_TAX_SYSTEM_CODE"), 0) or None,
        payment_public_base_url=(getenv("PAYMENT_PUBLIC_BASE_URL") or "").strip().rstrip("/") or None,
        payment_web_host=getenv("PAYMENT_WEB_HOST", "0.0.0.0"),
        payment_web_port=_parse_int(getenv("PAYMENT_WEB_PORT"), 8080),
        openai_input_price_per_1m=float(getenv("OPENAI_INPUT_PRICE_PER_1M") or 0.75),
        openai_cached_input_price_per_1m=float(getenv("OPENAI_CACHED_INPUT_PRICE_PER_1M") or 0.075),
        openai_output_price_per_1m=float(getenv("OPENAI_OUTPUT_PRICE_PER_1M") or 4.50),
        openai_model_pricing_json=(getenv("OPENAI_MODEL_PRICING_JSON") or "").strip(),
        amocrm_base_url=(getenv("AMOCRM_BASE_URL") or "").strip().rstrip("/") or None,
        amocrm_access_token=(getenv("AMOCRM_ACCESS_TOKEN") or "").strip() or None,
        amocrm_enabled=_parse_bool(
            getenv("AMOCRM_ENABLED"),
            bool((getenv("AMOCRM_ACCESS_TOKEN") or "").strip() and (getenv("AMOCRM_BASE_URL") or "").strip()),
        ),
        amocrm_pipeline_name=(getenv("AMOCRM_PIPELINE_NAME") or "Судебный приказ").strip(),
        amocrm_auto_create_pipeline=_parse_bool(getenv("AMOCRM_AUTO_CREATE_PIPELINE"), False),
        amocrm_auto_create_statuses=_parse_bool(getenv("AMOCRM_AUTO_CREATE_STATUSES"), True),
        amocrm_attach_files=_parse_bool(getenv("AMOCRM_ATTACH_FILES"), True),
        amocrm_file_upload_enabled=_parse_bool(getenv("AMOCRM_FILE_UPLOAD_ENABLED"), True),
        amocrm_file_upload_timeout_seconds=_parse_int(getenv("AMOCRM_FILE_UPLOAD_TIMEOUT_SECONDS"), 30),
        amocrm_debug=_parse_bool(getenv("AMOCRM_DEBUG"), False),
        amocrm_rps_limit=_parse_int(getenv("AMOCRM_RPS_LIMIT"), 5),
        amocrm_pipeline_id=_parse_int(getenv("AMOCRM_PIPELINE_ID"), 0) or None,
        crm_sync_background=_parse_bool(getenv("CRM_SYNC_BACKGROUND"), True),
        crm_sync_timeout_seconds=_parse_int(getenv("CRM_SYNC_TIMEOUT_SECONDS"), 5),
        crm_sync_max_attempts=_parse_int(getenv("CRM_SYNC_MAX_ATTEMPTS"), 3),
        crm_sync_retry_base_seconds=_parse_int(getenv("CRM_SYNC_RETRY_BASE_SECONDS"), 2),
        crm_sync_debug=_parse_bool(getenv("CRM_SYNC_DEBUG"), False),
        amount_retry_on_mismatch=_parse_bool(getenv("AMOUNT_RETRY_ON_MISMATCH"), True),
        auto_recover_amount_mismatch=_parse_bool(getenv("AUTO_RECOVER_AMOUNT_MISMATCH"), True),
        auto_recover_amount_min_confidence=float(getenv("AUTO_RECOVER_AMOUNT_MIN_CONFIDENCE") or 0.75),
        company_name=getenv("COMPANY_NAME", "Юридическая компания «Синай»"),
        manager_contact_text=getenv("MANAGER_CONTACT_TEXT", "Напишите менеджеру, и мы подключимся к диалогу."),
    )
