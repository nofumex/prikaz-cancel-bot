from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.models import Case
from app.services.legal_data import money_to_decimal, normalize_order_data, validate_amounts
from app.utils import safe_json_loads


def _load_raw_extraction(case_id: int) -> dict | None:
    path = Path("storage/debug") / f"case_{case_id}" / "order_ocr_raw.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


async def debug_case(case_id: int) -> int:
    await init_db()
    async with SessionLocal() as session:
        case = await session.get(Case, case_id)
        if not case:
            print(f"Case ID: {case_id}")
            print("ERROR: case not found")
            return 1

        raw = _load_raw_extraction(case_id)
        stored = safe_json_loads(case.extracted_json, {})

        print(f"Case ID: {case_id}")
        print(f"order_photo_path: {case.order_photo_path or '—'}")
        print()
        print("RAW extracted data:")
        if raw:
            for key in ("debt_amount", "state_duty", "total_amount"):
                print(f"{key}: {raw.get(key, '—')}")
        else:
            print("(order_ocr_raw.json not found — raw OCR was not saved for this case)")
            for key in ("debt_amount", "state_duty", "total_amount"):
                print(f"{key}: {stored.get(key, '—')}  [from stored extracted_json, post-OCR]")

        print()
        print("After normalize_order_data:")
        normalized = normalize_order_data(raw if raw else stored)
        for key in ("debt_amount", "state_duty", "total_amount"):
            print(f"{key}: {normalized.get(key, '—')}")

        print()
        print("After parse_money:")
        for key in ("debt_amount", "state_duty", "total_amount"):
            dec = money_to_decimal(normalized.get(key))
            print(f"{key}_decimal: {dec}")

        print()
        print("Computed:")
        debt = money_to_decimal(normalized.get("debt_amount"))
        duty = money_to_decimal(normalized.get("state_duty"))
        total = money_to_decimal(normalized.get("total_amount"))
        if debt is not None and duty is not None:
            computed = debt + duty
            print(f"debt + state_duty = {computed}")
            if total is not None:
                print(f"difference = {abs(total - computed)}")
        else:
            print("debt + state_duty = —")

        print()
        amount_check = validate_amounts(normalized)
        print("QA result:")
        print(f"ok: {amount_check.ok}")
        if amount_check.errors:
            print(f"errors: {', '.join(amount_check.errors)}")

        retry_path = Path("storage/debug") / f"case_{case_id}" / "amounts_ocr_retry.json"
        if retry_path.exists():
            retry = json.loads(retry_path.read_text(encoding="utf-8"))
            print()
            print("Targeted amount OCR retry:")
            for key in ("debt_amount", "state_duty", "total_amount", "confidence"):
                print(f"{key}: {retry.get(key, '—')}")

        recovery_path = Path("storage/debug") / f"case_{case_id}" / "amount_recovery.json"
        if recovery_path.exists():
            print()
            print("Amount recovery debug:")
            print(recovery_path.read_text(encoding="utf-8"))

        if not raw and stored:
            print()
            print("Diagnosis hint:")
            if (
                stored.get("total_amount")
                and stored.get("debt_amount")
                and money_to_decimal(stored.get("total_amount"))
                and money_to_decimal(stored.get("debt_amount"))
            ):
                total_dec = money_to_decimal(stored.get("total_amount"))
                debt_dec = money_to_decimal(stored.get("debt_amount"))
                duty_dec = money_to_decimal(stored.get("state_duty"))
                if total_dec and debt_dec and duty_dec and abs(total_dec - (debt_dec + duty_dec)) == 0.87:
                    print(
                        "Likely OpenAI OCR error on debt kopecks: total has .87 kopecks, "
                        "but debt was read as .00 kopecks. Parser/merge did not change values."
                    )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", type=int, required=True)
    args = parser.parse_args()
    return asyncio.run(debug_case(args.case_id))


if __name__ == "__main__":
    raise SystemExit(main())
