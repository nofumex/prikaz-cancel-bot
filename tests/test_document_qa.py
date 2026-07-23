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


def test_document_qa_rejects_iso_date_in_rendered_docx(tmp_path):
    docx = tmp_path / "iso.docx"
    _write_docx(docx, "Судебный приказ от 2026-07-03")
    qa = run_document_qa(
        data={
            "court_name": "x", "debtor_full_name": "Иванов Иван Иванович",
            "creditor_name": "x", "case_number": "1",
            "order_date": "2026-07-03", "debt_amount": "1", "state_duty": "1",
        },
        received_date=date(2026, 7, 3),
        deadline_date=date(2026, 7, 13),
        full_docx=docx,
        full_pdf=None,
        preview_pdf=None,
        instruction_docx=docx,
        require_preview_pdf=False,
    )

    assert "iso_date" in qa.bad_tokens


def test_document_qa_does_not_reject_structured_name_by_case(tmp_path):
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
    assert "debtor_full_name:dative" not in qa.bad_tokens


def test_document_qa_rejects_debtor_header_ocr_noise(tmp_path):
    docx = tmp_path / "header_noise.docx"
    _write_docx(
        docx,
        "\u0430\u0434\u0440\u0435\u0441: \u0433. \u0410\u0447\u0438\u043d\u0441\u043a \u041a\u0440\u0430\u0441\u043d\u043e\u044f\u0440\u0441\u043a\u043e\u0433\u043e \u043a\u0440\u0430\u044f, "
        "\u0443\u0440\u043e\u0436\u0435\u043d\u0435\u0446, \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c\u0443, \u043f\u0430\u0441\u043f\u043e\u0440\u0442"
    )
    qa = run_document_qa(
        data={"court_name": "x", "court_address": "x", "debtor_full_name": "\u0418\u0432\u0430\u043d\u043e\u0432 \u0418\u0432\u0430\u043d \u0418\u0432\u0430\u043d\u043e\u0432\u0438\u0447", "debtor_address": "x", "creditor_name": "x", "creditor_address": "x", "case_number": "1", "order_date": "01.01.2020", "debt_contract": "x", "debt_period": "x", "debt_amount": "1 \u0440\u0443\u0431. 00 \u043a\u043e\u043f."},
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        full_docx=docx,
        full_pdf=None,
        preview_pdf=None,
        instruction_docx=None,
        require_preview_pdf=False,
    )

    assert not qa.ok
    assert "\u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c\u0443" in qa.bad_tokens
    assert "\u0443\u0440\u043e\u0436\u0435\u043d" in qa.bad_tokens
    assert "\u043f\u0430\u0441\u043f\u043e\u0440\u0442" in qa.bad_tokens



def test_document_qa_accepts_feminine_nominative_name(tmp_path):
    name = "\u041a\u0430\u0440\u0438\u043c\u043e\u0432\u0430 \u0415\u043b\u0435\u043d\u0430 \u0412\u0438\u043a\u0442\u043e\u0440\u043e\u0432\u043d\u0430"
    docx = tmp_path / "female_nom.docx"
    _write_docx(docx, name)
    qa = run_document_qa(
        data={"court_name": "x", "court_address": "x", "debtor_full_name": name, "debtor_address": "x", "creditor_name": "x", "creditor_address": "x", "case_number": "1", "order_date": "01.01.2020", "debt_contract": "x", "debt_period": "x", "debt_amount": "1", "state_duty": "1"},
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        full_docx=docx,
        full_pdf=None,
        preview_pdf=None,
        instruction_docx=docx,
        require_preview_pdf=False,
    )
    assert "debtor_full_name:dative" not in qa.bad_tokens


def test_document_qa_does_not_infer_feminine_name_case(tmp_path):
    name = "\u041a\u0430\u0440\u0438\u043c\u043e\u0432\u043e\u0439 \u0415\u043b\u0435\u043d\u0435 \u0412\u0438\u043a\u0442\u043e\u0440\u043e\u0432\u043d\u0435"
    docx = tmp_path / "female_dat.docx"
    _write_docx(docx, name)
    qa = run_document_qa(
        data={"court_name": "x", "court_address": "x", "debtor_full_name": name, "debtor_address": "x", "creditor_name": "x", "creditor_address": "x", "case_number": "1", "order_date": "01.01.2020", "debt_contract": "x", "debt_period": "x", "debt_amount": "1", "state_duty": "1"},
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        full_docx=docx,
        full_pdf=None,
        preview_pdf=None,
        instruction_docx=docx,
        require_preview_pdf=False,
    )
    assert "debtor_full_name:dative" not in qa.bad_tokens


def test_document_qa_accepts_sizykh_name(tmp_path):
    name = "Сизых Инна Юрьевна"
    docx = tmp_path / "sizykh.docx"
    _write_docx(docx, name)
    qa = run_document_qa(
        data={"court_name": "x", "debtor_full_name": name, "creditor_name": "x",
              "case_number": "1", "order_date": "01.01.2020",
              "debt_amount": "1", "state_duty": "1"},
        received_date=date(2026, 6, 19),
        deadline_date=date(2026, 6, 29),
        full_docx=docx, full_pdf=None, preview_pdf=None,
        instruction_docx=docx, require_preview_pdf=False,
    )
    assert "debtor_full_name:dative" not in qa.bad_tokens
