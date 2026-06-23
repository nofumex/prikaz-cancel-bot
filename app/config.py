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

    document_price_rub: int
    yoomoney_receiver: str | None
    yoomoney_success_url: str | None
    yoomoney_notification_secret: str | None
    payment_public_base_url: str | None
    payment_web_host: str
    payment_web_port: int

    amocrm_base_url: str | None
    amocrm_access_token: str | None
    amocrm_pipeline_id: int | None
    amocrm_status_id_new: int | None
    amocrm_status_id_in_progress: int | None
    amocrm_status_id_consultation: int | None
    amocrm_write_enabled: bool

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
        document_price_rub=_parse_int(getenv("DOCUMENT_PRICE_RUB"), 990),
        yoomoney_receiver=(getenv("YOOMONEY_RECEIVER") or "").strip() or None,
        yoomoney_success_url=(getenv("YOOMONEY_SUCCESS_URL") or "").strip() or None,
        yoomoney_notification_secret=(getenv("YOOMONEY_NOTIFICATION_SECRET") or "").strip() or None,
        payment_public_base_url=(getenv("PAYMENT_PUBLIC_BASE_URL") or "").strip().rstrip("/") or None,
        payment_web_host=getenv("PAYMENT_WEB_HOST", "0.0.0.0"),
        payment_web_port=_parse_int(getenv("PAYMENT_WEB_PORT"), 8080),
        amocrm_base_url=(getenv("AMOCRM_BASE_URL") or "").strip().rstrip("/") or None,
        amocrm_access_token=(getenv("AMOCRM_ACCESS_TOKEN") or "").strip() or None,
        amocrm_pipeline_id=_parse_int(getenv("AMOCRM_PIPELINE_ID"), 10915210) or None,
        amocrm_status_id_new=_parse_int(getenv("AMOCRM_STATUS_ID_NEW"), 85847178) or None,
        amocrm_status_id_in_progress=_parse_int(getenv("AMOCRM_STATUS_ID_IN_PROGRESS"), 85847182) or None,
        amocrm_status_id_consultation=_parse_int(getenv("AMOCRM_STATUS_ID_CONSULTATION"), 85847186) or None,
        amocrm_write_enabled=_parse_bool(getenv("AMOCRM_WRITE_ENABLED"), False),
        company_name=getenv("COMPANY_NAME", "Юридическая компания «Синай»"),
        manager_contact_text=getenv("MANAGER_CONTACT_TEXT", "Напишите менеджеру, и мы подключимся к диалогу."),
    )
