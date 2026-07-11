from __future__ import annotations

import json
from pathlib import Path

from app.utils import ensure_dir


SETTINGS_PATH = Path("data/app_settings.json")
DEFAULTS = {
    'payments_enabled': True,
    'reminder_try_text': 'Вы начали знакомство с ботом, но еще не отправили судебный приказ. Нажмите кнопку ниже, чтобы попробовать подготовить заявление.',
    'reminder_pay_text': 'Предпросмотр заявления готов, но оплата еще не завершена. Не откладывайте: срок подачи заявления ограничен.',
    'reminder_consultation_text': 'Документы готовы. Если нужна помощь с дальнейшими действиями, можно связаться с менеджером и получить консультацию.',
    'reminder_try_hours': 24,
    'reminder_pay_hours': 24,
    'reminder_consultation_hours': 24,
}


def load_app_settings() -> dict:
    ensure_dir(SETTINGS_PATH.parent)
    if not SETTINGS_PATH.exists():
        save_app_settings(DEFAULTS.copy())
        return DEFAULTS.copy()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    result = DEFAULTS.copy()
    result.update({key: data[key] for key in DEFAULTS if key in data})
    return result


def save_app_settings(data: dict) -> None:
    ensure_dir(SETTINGS_PATH.parent)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def payments_enabled() -> bool:
    return bool(load_app_settings().get("payments_enabled", True))


def toggle_payments() -> bool:
    data = load_app_settings()
    data["payments_enabled"] = not bool(data.get("payments_enabled", True))
    save_app_settings(data)
    return bool(data["payments_enabled"])


def reminder_settings() -> dict:
    return load_app_settings()


def update_reminder_setting(key: str, value) -> None:
    if key not in DEFAULTS or not key.startswith('reminder_'):
        raise ValueError('Unknown reminder setting')
    data = load_app_settings()
    data[key] = value
    save_app_settings(data)
