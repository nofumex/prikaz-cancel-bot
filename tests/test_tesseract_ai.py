from app.services.legal_data import normalize_debtor_name_fields, normalize_order_data
from app.services.tesseract_ai import (
    _contract_ok,
    TESSERACT_RECONCILIATION_SCHEMA,
    compact_tesseract_texts,
    lock_selected_tesseract_name,
    normalize_tesseract_ai_data,
)


def test_schema_has_no_confidence_or_document_kind() -> None:
    schema_text = str(TESSERACT_RECONCILIATION_SCHEMA).lower()
    assert "confidence" not in schema_text
    assert "document_kind" not in schema_text
    assert "selected_name_occurrence" in TESSERACT_RECONCILIATION_SCHEMA["required"]


def test_extracted_name_must_be_exact_selected_nominative() -> None:
    fields = {key: "x" for key in TESSERACT_RECONCILIATION_SCHEMA["properties"]["fields"]["required"]}
    fragments = {key: "x" for key in TESSERACT_RECONCILIATION_SCHEMA["properties"]["source_fragments"]["required"]}
    fields["debtor_full_name"] = "Вараюн Валерий Александрович"
    payload = {
        "fields": fields,
        "source_fragments": fragments,
        "debtor_full_name_source": "extracted",
        "selected_name_occurrence": "Вараюн Валерий Александрович",
        "debtor_name_occurrences": [
            {"text": "Вараюна Валерия Александровича", "grammatical_case": "other", "source_fragment": "с должника"},
            {"text": "Вараюн Валерий Александрович", "grammatical_case": "nominative", "source_fragment": "Вараюн Валерий Александрович"},
        ],
    }
    assert _contract_ok(payload)
    payload["fields"]["debtor_full_name"] = "Варанов Валерий Александрович"
    assert _contract_ok(payload)
    assert lock_selected_tesseract_name(payload)["debtor_full_name"] == "Вараюн Валерий Александрович"


def test_generated_name_is_forbidden_when_nominative_exists() -> None:
    fields = {key: "x" for key in TESSERACT_RECONCILIATION_SCHEMA["properties"]["fields"]["required"]}
    fragments = {key: "x" for key in TESSERACT_RECONCILIATION_SCHEMA["properties"]["source_fragments"]["required"]}
    payload = {
        "fields": fields,
        "source_fragments": fragments,
        "debtor_full_name_source": "generated",
        "selected_name_occurrence": "",
        "debtor_name_occurrences": [
            {"text": "Вараюн Валерий Александрович", "grammatical_case": "nominative", "source_fragment": "строка"},
        ],
    }
    assert not _contract_ok(payload)


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
