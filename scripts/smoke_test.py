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
from app.services.document_templates import create_case_documents
from app.services.legal_data import legal_deadline_from_received, normalize_order_data, validate_amounts
from app.services.pdf_tools import check_pdf_dependencies, pdf_page_count, pdf_text


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


def _visual_qa_report(artifacts) -> str:
    vqa = artifacts.visual_qa
    lines = [
        "=== Visual QA Report ===",
        f"page_count: {vqa.page_count if vqa else 'n/a'}",
        f"font_name: {vqa.font_name if vqa else 'n/a'}",
        f"body_font_size: {vqa.body_font_size if vqa else 'n/a'}",
        f"margins: {vqa.margins if vqa else 'n/a'}",
        f"amounts: {vqa.amounts if vqa else 'n/a'}",
        f"qa_passed: {artifacts.qa_report.get('visual_qa_ok')}",
        f"qa_errors: {artifacts.qa_report.get('visual_qa_errors')}",
        f"qa_warnings: {artifacts.qa_report.get('visual_qa_warnings')}",
        f"no_bad_tokens: {not artifacts.qa_report.get('document_qa_bad_tokens')}",
        f"no_amount_mismatch: {'amount_mismatch' not in (artifacts.qa_report.get('document_qa_bad_tokens') or [])}",
        f"no_weird_spaces: {'weird_justified_spaces' not in (artifacts.qa_report.get('visual_qa_errors') or [])}",
    ]
    if vqa and vqa.weird_space_lines:
        lines.append(f"weird_space_lines: {vqa.weird_space_lines}")
    return "\n".join(lines)


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
        artifacts = create_case_documents(case, user, settings)
        full_docx = artifacts.full_docx_path
        full_pdf = artifacts.full_pdf_path
        preview_pdf = artifacts.preview_pdf_path
        instruction = artifacts.instruction_docx_path
        if not full_docx.exists():
            raise RuntimeError("full DOCX was not created")
        if not instruction.exists():
            raise RuntimeError("instruction DOCX was not created")
        if not full_pdf or not full_pdf.exists():
            raise RuntimeError("full PDF was not created")
        if not preview_pdf or not preview_pdf.exists():
            raise RuntimeError("preview PDF was not created")
        if full_pdf.read_bytes() == preview_pdf.read_bytes():
            raise RuntimeError("preview PDF equals full PDF")
        full_text = pdf_text(full_pdf)
        preview_text = pdf_text(preview_pdf)
        if full_text and full_text.strip() and full_text.strip() == preview_text.strip():
            raise RuntimeError("preview PDF contains full readable text")
        page_count = pdf_page_count(full_pdf)
        if page_count != 1:
            raise RuntimeError(f"Belsky in-time document must fit 1 page, got {page_count}")
        if "ВОЗРАЖЕНИЯ" not in full_text:
            raise RuntimeError("missing title ВОЗРАЖЕНИЯ")
        if "/Бельский В.Г./" not in full_text and "Бельский В.Г." not in full_text:
            raise RuntimeError("signature not filled")
        amounts = validate_amounts(data)
        if not amounts.ok:
            raise RuntimeError("amount validation failed")
        case.full_doc_path = str(full_docx)
        case.full_pdf_path = str(full_pdf)
        case.preview_pdf_path = str(preview_pdf)
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
        qa = artifacts.qa_report
        manifest.write_text(
            "\n".join(
                [
                    f"case_id={case.id}",
                    f"full_docx_path={full_docx.resolve()}",
                    f"full_pdf_path={full_pdf.resolve()}",
                    f"preview_pdf_path={preview_pdf.resolve()}",
                    f"instruction_docx_path={instruction.resolve()}",
                    f"page_count={page_count}",
                    f"font_name={qa.get('font_name')}",
                    f"body_font_size={qa.get('body_font_size')}",
                    f"margins={qa.get('margins')}",
                    f"amounts={qa.get('amounts')}",
                    f"qa_passed={qa.get('document_qa_ok') and qa.get('visual_qa_ok')}",
                    f"qa_errors={qa.get('visual_qa_errors')}",
                    f"qa_warnings={qa.get('visual_qa_warnings')}",
                    "",
                    _visual_qa_report(artifacts),
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
        print(_visual_qa_report(artifacts))
        if dep_errors:
            print(f"  Dependency warnings: {'; '.join(dep_errors)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-dev-fallback", action="store_true", help="Allow DOCX/PDF dev fallback when LibreOffice is unavailable")
    args = parser.parse_args()
    asyncio.run(main(args.allow_dev_fallback))
