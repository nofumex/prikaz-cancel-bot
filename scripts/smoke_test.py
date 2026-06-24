"""Generate and validate PDF pipeline artifacts for a smoke case."""
from __future__ import annotations

import asyncio
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Case, User
from app.services.documents import create_case_documents
from app.services.legal_data import legal_deadline_from_received, normalize_order_data
from app.services.pdf_tools import check_pdf_dependencies, pdf_text


BELSKY_DATA = {
    "court_name": "судебный участок № 5 города Ессентуки Ставропольского края",
    "court_address": "357600, Ставропольский край, г. Ессентуки, ул. Шмидта, д. 72",
    "debtor_name_raw": "Бельскому Владимиру Геннадьевичу",
    "debtor_name_context": "взыскать с Бельского Владимира Геннадьевича",
    "debtor_full_name": "Бельский Владимир Геннадьевич",
    "debtor_address": "г. Ессентуки, ул. Володарского, д. 14, кв. 9",
    "creditor_name": "АО «Почта Банк»",
    "creditor_address": "107061, г. Москва, Преображенская пл., д. 8",
    "case_number": "2-146-09-434/2021",
    "uid": "26MS0031-01-2021-000169-72",
    "order_date": "18.01.2021",
    "debt_contract": "договор № 43006327 от 27 апреля 2019 года",
    "debt_period": "с 27.03.2020 по 28.11.2020",
    "debt_amount": "78 472 руб. 87 коп.",
    "state_duty": "1 277 руб. 00 коп.",
    "total_amount": "79 749 руб. 87 коп.",
}


async def main(allow_dev_fallback: bool) -> None:
    import os

    os.environ["ALLOW_DEV_DOCX_PREVIEW"] = "true" if allow_dev_fallback else "false"
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    deps_ok, dep_errors = check_pdf_dependencies(require_preview_pdf_for_payment=settings.require_pdf_preview_for_payment)
    if not deps_ok and not allow_dev_fallback:
        raise RuntimeError("PDF dependencies are not ready: " + "; ".join(dep_errors))
    await init_db()
    received = date(2026, 6, 19)
    data = normalize_order_data(BELSKY_DATA)
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.platform_user_id == "smoke"))
        user = result.scalar_one_or_none()
        if not user:
            user = User(platform="telegram", platform_user_id="smoke", telegram_id=999001, username="smoke_test")
            session.add(user)
            await session.flush()
        case = Case(
            user_id=user.id,
            platform="telegram",
            status="processing",
            received_date=received,
            deadline_date=legal_deadline_from_received(received),
            extracted_json=json.dumps(data, ensure_ascii=False),
        )
        session.add(case)
        await session.commit()
        await session.refresh(case)
        full_docx, full_pdf, preview_pdf, preview_docx, instruction = create_case_documents(case, user, settings)
        if not full_docx.exists():
            raise RuntimeError("full DOCX was not created")
        if not instruction.exists():
            raise RuntimeError("instruction DOCX was not created")
        if not full_pdf or not full_pdf.exists():
            raise RuntimeError("full PDF was not created")
        if not preview_pdf or not preview_pdf.exists():
            raise RuntimeError("preview PDF was not created")
        if preview_docx and not allow_dev_fallback:
            raise RuntimeError("dev DOCX preview fallback was used while ALLOW_DEV_DOCX_PREVIEW=false")
        if full_pdf.read_bytes() == preview_pdf.read_bytes():
            raise RuntimeError("preview PDF equals full PDF")
        full_text = pdf_text(full_pdf)
        preview_text = pdf_text(preview_pdf)
        if full_text and full_text.strip() and full_text.strip() == preview_text.strip():
            raise RuntimeError("preview PDF contains full readable text")
        case.full_doc_path = str(full_docx)
        case.full_pdf_path = str(full_pdf) if full_pdf else None
        case.preview_pdf_path = str(preview_pdf) if preview_pdf else None
        case.preview_doc_path = str(preview_docx) if preview_docx else None
        case.instruction_path = str(instruction)
        from app.models import OpenAIUsage

        session.add(
            OpenAIUsage(
                case_id=case.id,
                user_id=user.id,
                operation="order_ocr",
                model=settings.vision_model,
                input_tokens=4200,
                cached_input_tokens=0,
                output_tokens=850,
                total_tokens=5050,
                input_cost_usd=0.00315,
                output_cost_usd=0.003825,
                total_cost_usd=0.006975,
                success=True,
            )
        )
        await session.commit()
        manifest_dir = Path("storage/test_artifacts")
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = manifest_dir / "belsky_case_manifest.txt"
        manifest.write_text(
            "\n".join(
                [
                    f"case_id={case.id}",
                    f"full_docx={full_docx.resolve()}",
                    f"full_pdf={full_pdf.resolve() if full_pdf else ''}",
                    f"preview_pdf={preview_pdf.resolve() if preview_pdf else ''}",
                    f"instruction_docx={instruction.resolve()}",
                ]
            ),
            encoding="utf-8",
        )
        print("Smoke test artifacts:")
        print(f"  DOCX: {full_docx}")
        print(f"  PDF: {full_pdf}")
        print(f"  Preview PDF: {preview_pdf}")
        print(f"  Instruction: {instruction}")
        print(f"  Case ID: {case.id}")
        print(f"  Dependencies OK: {deps_ok}")
        if dep_errors:
            print(f"  Dependency warnings: {'; '.join(dep_errors)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-dev-fallback", action="store_true", help="Allow DOCX/PDF dev fallback when LibreOffice is unavailable")
    args = parser.parse_args()
    asyncio.run(main(args.allow_dev_fallback))
