from datetime import date

from app.services.order_confirmation import apply_confirmation_answer, next_confirmation, reduce_and_validate


def _record(name: str, value: str, *, status: str = "confirmed") -> dict:
    return {
        "field_name": name, "raw_ocr_value": value, "extracted_value": value,
        "normalized_value": value, "derived_value": "", "verified_value": "", "user_value": "",
        "document_value": value if status == "confirmed" else "", "status": status,
        "source_word_ids": [f"{name}_w1"], "verification_reason": "test",
    }


def _data() -> dict:
    values = {
        "court_name": "Судебный участок № 1",
        "court_address": "644000, г. Омск, ул. Судебная, д. 1",
        "judge": "Иванов И.И.",
        "debtor_full_name": "Вараюн Валерий Александрович",
        "debtor_address": "г. Омск, ул. Редкая, д. 1",
        "creditor_name": "ООО Взыскатель",
        "creditor_legal_address": "644001, г. Омск, ул. Ленина, д. 2",
        "creditor_correspondence_address": "644002, г. Омск, ул. Почтовая, д. 3",
        "order_date": "05.06.2026",
        "debt_contract": "договор № 1",
        "debt_period": "с 01.01.2026 по 31.01.2026",
        "debt_amount": "1000 руб. 00 коп.",
        "state_duty": "100 руб. 00 коп.",
        "total_amount": "1100 руб. 00 коп.",
        "case_number": "02-1388/2026",
        "uid": "24MS0001-01-2026-000001-01",
    }
    provenance = {name: _record(name, value) for name, value in values.items()}
    provenance["case_number"] = _record("case_number", values["case_number"], status="disputed")
    return {
        **{name: value for name, value in values.items() if name != "case_number"},
        "_document_kind": "court_order", "_document_values_locked": "1",
        "_pipeline_status": "awaiting_user_confirmation", "_field_provenance": provenance,
    }


def test_confirmation_runs_full_reducer_before_ready() -> None:
    data = _data()
    step = next_confirmation(data)
    assert step and step.field_name == "case_number"
    result = apply_confirmation_answer(data, "case_number", "02-1388/2026", date(2026, 6, 19))
    assert result.ready
    assert result.data["_pipeline_status"] == "ready"
    saved = result.data["_field_provenance"]["case_number"]
    assert saved["raw_ocr_value"] == "02-1388/2026"
    assert saved["extracted_value"] == "02-1388/2026"
    assert saved["user_value"] == "02-1388/2026"


def test_confirmation_cannot_make_missing_required_field_ready() -> None:
    data = _data()
    data.pop("creditor_name")
    data["_field_provenance"].pop("creditor_name")
    result = apply_confirmation_answer(data, "case_number", "02-1388/2026", date(2026, 6, 19))
    assert not result.ready
    assert result.data["_pipeline_status"] == "awaiting_user_confirmation"
    assert "Взыскатель" in result.missing_fields


def test_confirmation_cannot_conflate_case_number_and_uid() -> None:
    data = _data()
    result = apply_confirmation_answer(data, "case_number", data["uid"], date(2026, 6, 19))
    assert not result.ready
    assert result.data["_pipeline_status"] == "awaiting_user_confirmation"
    assert "Номер дела" in result.missing_fields
    assert "УИД" in result.missing_fields


def test_reducer_never_trusts_pipeline_status_without_validation() -> None:
    data = _data()
    data["_pipeline_status"] = "ready"
    result = reduce_and_validate(data, date(2026, 6, 19))
    assert not result.ready
    assert result.data["_pipeline_status"] == "awaiting_user_confirmation"

def test_invalid_user_value_returns_field_to_confirmation() -> None:
    data = _data()
    result = apply_confirmation_answer(data, "case_number", "без номера", date(2026, 6, 19))
    assert not result.ready
    assert result.data["_pipeline_status"] == "awaiting_user_confirmation"
    record = result.data["_field_provenance"]["case_number"]
    assert record["status"] == "disputed"
    assert record["raw_ocr_value"] == "02-1388/2026"
    assert record["user_value"] == "без номера"
    assert result.next_step and result.next_step.field_name == "case_number"


def test_disputed_mandatory_field_always_blocks_ready() -> None:
    data = _data()
    data["_pipeline_version"] = "tesseract-text-v3"
    record = data["_field_provenance"]["creditor_legal_address"]
    record.update(status="disputed", document_value="", verification_reason="ocr_disagreement")
    data.pop("creditor_legal_address", None)
    result = reduce_and_validate(data, date(2026, 6, 19))
    assert not result.ready
    assert result.data["_pipeline_status"] == "awaiting_user_confirmation"
    assert result.next_step is None