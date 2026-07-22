from app.services.legal_data import clean_text
from app.services.tesseract_ai import _debt_period_from_ocr
from app.utils import parse_russian_date


def test_iso_order_date_from_ai_is_accepted():
    assert parse_russian_date("2026-06-29").strftime("%d.%m.%Y") == "29.06.2026"


def test_debt_period_falls_back_to_tesseract_sentence():
    text = (
        "29 июня 2026 г. дата рождения 26.09.1987. "
        "задолженность по договору от 01.11.2021 г. за "
        "период с 17.11.2021 г. по 10.02.2026 г. в размере 50000 руб."
    )
    assert _debt_period_from_ocr(text) == "с 17.11.2021 по 10.02.2026"


def test_ai_missing_marker_is_not_rendered_into_document():
    assert clean_text("MISSING") == ""
