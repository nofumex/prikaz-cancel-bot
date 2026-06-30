from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path


def h(value: object | None) -> str:
    return escape("" if value is None else str(value), quote=False)


def full_name(user) -> str:
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(part for part in parts if part)
    return h(name or getattr(user, "username", None) or getattr(user, "platform_user_id", None) or "без имени")


def username_text(user) -> str:
    username = getattr(user, "username", None)
    return f"@{h(username)}" if username else "не указан"


def platform_id_text(user) -> str:
    if getattr(user, "platform", "") == "telegram":
        return str(getattr(user, "telegram_id", None) or getattr(user, "platform_user_id", "") or "")
    return str(getattr(user, "platform_user_id", "") or "")


def normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) < 10:
        return None
    return "+" + digits


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return value
    return None


def normalize_receipt_contact(raw: str | None) -> tuple[str, str] | None:
    email = normalize_email(raw)
    if email:
        return "email", email
    phone = normalize_phone(raw)
    if phone:
        return "phone", phone
    return None


def parse_russian_date(raw: str | None) -> date | None:
    if not raw:
        return None
    text = raw.strip().lower()
    months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    match = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text)
    if match and match.group(2) in months:
        return date(int(match.group(3)), months[match.group(2)], int(match.group(1)))
    return None


def deadline_from_received(received: date) -> date:
    return received + timedelta(days=10)


def safe_json_loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def ensure_dir(path: str | Path) -> Path:
    result = Path(path)
    result.mkdir(parents=True, exist_ok=True)
    return result


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ")
