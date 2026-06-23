from __future__ import annotations

import json
from pathlib import Path

from app.utils import ensure_dir


SETTINGS_PATH = Path("data/app_settings.json")
DEFAULTS = {"payments_enabled": True}


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
