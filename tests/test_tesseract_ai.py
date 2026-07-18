from app.services.legal_data import normalize_debtor_name_fields, normalize_order_data
from app.services.tesseract_ai import (
    TESSERACT_RECONCILIATION_SCHEMA,
    compact_tesseract_texts,
    normalize_tesseract_ai_data,
)


def test_schema_has_no_confidence_or_document_kind() -> None:
    schema = str(TESSERACT_RECONCILIATION_SCHEMA).lower()
    assert "confidence" not in schema
    assert "document_kind" not in schema


def test_tesseract_variants_are_deduplicated() -> None:
    text = compact_tesseract_texts([
        "Лошакова Наталья Борисовна\nДело № 1",
        "Лошакова Наталья Борисовна\nВзыскать с Лошаковой Натальи Борисовны",
    ])
    assert text.count("Лошакова Наталья Борисовна") == 1
    assert "Взыскать с Лошаковой Натальи Борисовны" in text


def test_tesseract_name_is_locked_against_legacy_normalizer() -> None:
    data = normalize_tesseract_ai_data({
        "debtor_name_raw": "Вараюна Валерия Александровича",
        "debtor_full_name": "Вараюн Валерий Александрович",
        "case_number": "02-1899/9/2026",
        "order_date": "05.06.2026",
    })
    normalized = normalize_order_data(data)
    normalized, result = normalize_debtor_name_fields(normalized)
    assert result is None
    assert normalized["debtor_full_name"] == "Вараюн Валерий Александрович"
    assert normalized["debtor_short_name"] == "Вараюн В.А."
    assert "debtor_name_confidence" not in normalized


def test_year_only_is_not_order_date() -> None:
    assert normalize_tesseract_ai_data({"order_date": "2026"})["order_date"] == ""
