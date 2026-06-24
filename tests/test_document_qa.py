from datetime import date
from pathlib import Path

from docx import Document

from app.services.document_qa import run_document_qa


def _write_docx(path: Path, text: str) -> None:
    doc = Document()
    doc.add_paragraph(text)
    doc.save(path)


def test_document_qa_rejects_bad_tokens(tmp_path):
    docx = tmp_path / "bad.docx"
    _write_docx(docx, "ЗАЯВЛЕНИЕ об отмене судебного приказа")
    qa = run_document_qa(
        data={"court_name": "x", "court_address": "x", "debtor_full_name": "Иванов Иван Иванович", "debtor_address": "x", "creditor_name": "x", "creditor_address": "x", "case_number": "1", "order_date": "01.01.2020", "debt_contract": "x", "debt_period": "x", "debt_amount": "1 руб. 00 коп."},
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        full_docx=docx,
        full_pdf=None,
        preview_pdf=None,
        instruction_docx=None,
        require_preview_pdf=False,
    )
    assert not qa.ok
    assert "old_statement_title" in qa.bad_tokens


def test_document_qa_rejects_dative_name(tmp_path):
    docx = tmp_path / "dative.docx"
    _write_docx(docx, "Бельскому Владимиру Геннадьевичу")
    qa = run_document_qa(
        data={"court_name": "x", "court_address": "x", "debtor_full_name": "Бельскому Владимиру Геннадьевичу", "debtor_address": "x", "creditor_name": "x", "creditor_address": "x", "case_number": "1", "order_date": "01.01.2020", "debt_contract": "x", "debt_period": "x", "debt_amount": "1 руб. 00 коп."},
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        full_docx=docx,
        full_pdf=None,
        preview_pdf=None,
        instruction_docx=None,
        require_preview_pdf=False,
    )
    assert not qa.ok
