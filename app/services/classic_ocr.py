from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.services.image_preprocessing import build_order_ocr_variants
from app.services.legal_data import format_money_rub_kop, money_from_source_fragment


def _tesseract_one(path: Path) -> str:
    if not shutil.which("tesseract") or not path.exists():
        return ""
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "rus+eng", "--psm", "6"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=35,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


async def extract_classic_ocr_text(order_photo_path: str | Path, *, case_id: int | None = None) -> str:
    variants = build_order_ocr_variants(order_photo_path, case_id=case_id)
    # The overlapping middle tile covers both the header and operative block
    # on normal pages while keeping classic OCR latency bounded.
    selected = variants[2:3] if len(variants) >= 3 else variants[:1]
    chunks = await asyncio.gather(*(asyncio.to_thread(_tesseract_one, path) for path in selected))
    return "\n\n--- OCR TILE ---\n\n".join(chunk for chunk in chunks if chunk)[:24000]


def classic_amount_facts(text: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        context = " ".join(lines[max(0, index - 2) : index + 1])
        lower = context.lower()
        amount = money_from_source_fragment(context)
        if amount is None:
            continue
        if "пошлин" in lower:
            facts["state_duty"] = format_money_rub_kop(amount)
            facts["state_duty_fragment"] = context
        elif any(token in lower for token in ("всего", "итого", "общая сумма", "а всего")):
            facts["total_amount"] = format_money_rub_kop(amount)
            facts["total_amount_fragment"] = context
        elif any(token in lower for token in ("задолж", "сумма долга", "в размере")):
            facts["debt_amount"] = format_money_rub_kop(amount)
            facts["debt_amount_fragment"] = context
    return facts


def classic_court_name(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text or " ")
    match = re.search(
        r"(судебн\w*\s+участк\w*\s*№\s*\d+[\w-]*\s+.{2,140}?(?:области|края|республики))\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    value = match.group(1).strip(" ,.;")
    value = re.sub(r"^судебный участок", "судебного участка", value, flags=re.IGNORECASE)
    return value
