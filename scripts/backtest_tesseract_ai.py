from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from app.config import get_settings
from app.services.tesseract_ai import extract_order_data_from_tesseract_ai


KEY_FIELDS = (
    "debtor_full_name", "case_number", "order_date", "debt_amount", "state_duty", "total_amount",
)


def primary_candidates(case_id: int, extracted_json: str) -> dict:
    debug = Path("storage/debug") / f"case_{case_id}" / "order_ocr_raw.json"
    if debug.exists():
        try:
            return json.loads(debug.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return json.loads(extracted_json or "{}")


async def run_case(case_id: int, database: Path) -> dict:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "select user_id, order_photo_path, extracted_json from cases where id = ?", (case_id,)
    ).fetchone()
    connection.close()
    if row is None or not row["order_photo_path"]:
        return {"case_id": case_id, "error": "case or image not found"}
    result = await extract_order_data_from_tesseract_ai(
        get_settings(),
        None,
        case_id=case_id,
        user_id=row["user_id"],
        order_photo_path=row["order_photo_path"],
        primary_candidates=primary_candidates(case_id, row["extracted_json"]),
    )
    return {
        "case_id": case_id,
        "safe_to_generate": result.safe_to_generate,
        "fields": {key: result.data.get(key) for key in KEY_FIELDS},
        "debtor_full_name_source": result.debtor_full_name_source,
        "debtor_name_occurrences": result.debtor_name_occurrences,
        "issues": result.issues,
        "usage": result.llm_result.usage,
        "model": result.llm_result.model,
        "latency_ms": {
            "tesseract": result.ocr.latency_ms,
            "ai": result.llm_result.latency_ms,
        },
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_ids", nargs="+", type=int)
    parser.add_argument("--database", type=Path, default=Path("data/prikaz_bot.sqlite3"))
    args = parser.parse_args()
    for case_id in args.case_ids:
        try:
            output = await run_case(case_id, args.database)
        except Exception as exc:
            output = {"case_id": case_id, "error": str(exc)}
        print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
