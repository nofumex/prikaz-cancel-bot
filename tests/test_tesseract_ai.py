import asyncio
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.services.legal_data import normalize_order_data
from app.services.tesseract_ai import (
    OcrLine,
    OcrWord,
    TESSERACT_RECONCILIATION_SCHEMA,
    TesseractOcrResult,
    _crop_candidate,
    _creditor_labeled_spans,
    _document_value,
    _select_crop_consensus,
    _parse_tsv,
    apply_user_field_confirmation,
    compact_tesseract_texts,
    extract_fast_tesseract_text,
    extract_order_data_from_tesseract_ai,
    normalize_tesseract_ai_data,
    validate_assignments,
    verify_disputed_fields,
)


def _ocr(tokens: list[str]) -> TesseractOcrResult:
    words = [OcrWord(f"p1_w{i}", "p1_b1_p1_l1", 1, token, (i * 10, 0, i * 10 + 9, 10), 92.0) for i, token in enumerate(tokens, 1)]
    line = OcrLine("p1_b1_p1_l1", 1, " ".join(tokens), (10, 0, len(tokens) * 10 + 9, 10), 92.0, tuple(word.word_id for word in words))
    return TesseractOcrResult(line.text, [line.text], [Path("prepared.png")], 1, words, [line], "hash")


def _entity(entity_id: str, role: str, name: str, ids: list[str]) -> dict:
    return {"entity_id": entity_id, "role": role, "name": name, "source_word_ids": ids, "status": "candidate"}


def _assignment(field: str, value: str, ids: list[str], owner: str, relation_ids: list[str], **extra) -> dict:
    semantic = {
        "court_name": "court_name", "court_address": "court_address", "judge": "judge_name",
        "debtor_full_name": "debtor_name", "debtor_address": "debtor_address",
        "creditor_name": "creditor_name", "creditor_address": "creditor_legal_address",
        "case_number": "court_order_case_number", "uid": "court_order_uid",
        "order_date": "court_order_issue_date", "debt_contract": "debt_basis",
        "debt_period": "debt_period",
        "debt_amount": "principal_debt", "state_duty": "state_duty", "total_amount": "total_recovery",
    }[field]
    return {
        "field_name": field, "extracted_value": value, "normalized_value": value,
        "derived_value": "", "source_word_ids": ids, "owner_entity_id": owner,
        "semantic_role": semantic, "relation_evidence_word_ids": relation_ids,
        "alternatives": [], "status": "candidate", **extra,
    }


def test_schema_allows_only_llm_candidate_statuses_and_word_sources() -> None:
    text = str(TESSERACT_RECONCILIATION_SCHEMA).lower()
    assert "confirmed" not in text
    assert "extracted_value" not in text
    assert "normalized_value" not in text
    assert "source_word_ids" not in text
    assert "printed_value" in text
    assert "bbox" not in text
    assert "page" not in text
    assert "input_image" not in text


def test_tsv_produces_lines_words_bbox_and_confidence() -> None:
    tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t91.5\tДело\n5\t1\t1\t1\t1\t2\t45\t20\t80\t10\t88\t02-1/2026\n"
    words, lines = _parse_tsv(tsv)
    assert [word.word_id for word in words] == ["p1_w1", "p1_w2"]
    assert lines[0].text == "Дело 02-1/2026"
    assert lines[0].bbox == (10, 20, 125, 30)
    assert lines[0].confidence == 89.75


def test_wrong_entity_assignment_is_disputed_even_when_value_exists() -> None:
    ocr = _ocr(["Взыскатель", "Банк", "адрес", "Москва", "Должник", "Иванов", "адрес", "Омск"])
    payload = {
        "entities": [
            _entity("creditor_1", "creditor", "Банк", ["p1_w2"]),
            _entity("debtor_1", "debtor", "Иванов", ["p1_w6"]),
        ],
        "field_assignments": [
            _assignment("debtor_address", "Москва", ["p1_w4"], "debtor_1", ["p1_w1", "p1_w2"]),
        ],
        "debtor_name_occurrences": [],
    }
    records, issues = validate_assignments(payload, ocr)
    assert records["debtor_address"]["status"] == "disputed"
    assert records["debtor_address"]["document_value"] == ""
    assert any("relation_evidence_invalid" in issue for issue in issues)
    rechecked, _ = validate_assignments(
        payload, ocr, verified_values={"debtor_address": {"value": "Москва", "reason": "targeted_crop"}},
    )
    assert rechecked["debtor_address"]["status"] == "disputed"
    assert "relation_evidence_invalid" in rechecked["debtor_address"]["verification_reason"]


def test_court_address_cannot_be_assigned_to_creditor() -> None:
    ocr = _ocr(["Суд", "Омск", "Взыскатель", "Банк"])
    payload = {
        "entities": [
            _entity("document_1", "document", "Судебный приказ", []),
            _entity("court_1", "court", "Суд", ["p1_w1"]),
            _entity("creditor_1", "creditor", "Банк", ["p1_w4"]),
        ],
        "field_assignments": [
            _assignment("creditor_address", "Омск", ["p1_w2"], "creditor_1", ["p1_w1"]),
        ],
        "debtor_name_occurrences": [],
    }
    records, _ = validate_assignments(payload, ocr)
    assert records["creditor_address"]["status"] == "disputed"


def test_creditor_address_span_rejects_bank_requisites() -> None:
    ocr = _ocr(["Взыскатель", "Банк", "адрес", "Омск", "ИНН", "1234567890"])
    entities = [
        _entity("document_1", "document", "ignored", []),
        _entity("creditor_1", "creditor", "ignored", ["p1_w2"]),
    ]
    bad = {
        "entities": entities,
        "field_assignments": [
            _assignment("creditor_address", "ignored", ["p1_w4", "p1_w5", "p1_w6"], "creditor_1", ["p1_w1", "p1_w2"]),
        ],
        "debtor_name_occurrences": [],
    }
    bad_record = validate_assignments(bad, ocr)[0]["creditor_address"]
    assert bad_record["status"] == "disputed"
    assert "address_span_invalid" in bad_record["verification_reason"]

    good = dict(bad)
    good["field_assignments"] = [
        _assignment("creditor_address", "ignored", ["p1_w4"], "creditor_1", ["p1_w1", "p1_w2"]),
    ]
    good_record = validate_assignments(good, ocr)[0]["creditor_address"]
    assert good_record["status"] == "confirmed"
    assert good_record["extracted_value"] == "Омск"

    multiple = dict(bad)
    multiple["field_assignments"] = [
        _assignment("creditor_address", "ignored", ["p1_w3", "p1_w4"], "creditor_1", ["p1_w1", "p1_w2"]),
    ]
    multiple_record = validate_assignments(multiple, ocr)[0]["creditor_address"]
    assert multiple_record["status"] == "disputed"

def test_creditor_name_cannot_be_assigned_to_debtor() -> None:
    ocr = _ocr(["Взыскатель", "Банк", "Должник", "Иванов"])
    payload = {
        "entities": [
            _entity("document_1", "document", "Судебный приказ", []),
            _entity("creditor_1", "creditor", "Банк", ["p1_w2"]),
            _entity("debtor_1", "debtor", "Иванов", ["p1_w4"]),
        ],
        "field_assignments": [
            _assignment("debtor_full_name", "Банк", ["p1_w2"], "debtor_1", ["p1_w1"]),
        ],
        "debtor_name_occurrences": [],
    }
    records, _ = validate_assignments(payload, ocr)
    assert records["debtor_full_name"]["status"] == "disputed"


def test_uid_label_cannot_confirm_case_number() -> None:
    ocr = _ocr(["УИД", "24MS0001-01-2026-000001-01"])
    payload = {
        "entities": [_entity("document_1", "document", "Судебный приказ", [])],
        "field_assignments": [
            _assignment("case_number", "24MS0001-01-2026-000001-01", ["p1_w2"], "document_1", ["p1_w1"]),
        ],
        "debtor_name_occurrences": [],
    }
    records, _ = validate_assignments(payload, ocr)
    assert records["case_number"]["status"] == "disputed"


def test_uid_forbidden_symbol_or_low_confidence_is_disputed() -> None:
    for value, confidence in (("26М$0031-01-2021-000169-72", 92.0), ("26MS0031-01-2021-000169-72", 59.35)):
        ocr = _ocr(["УИД", value])
        ocr.words = [
            OcrWord(word.word_id, word.line_id, word.page, word.text, word.bbox, confidence)
            for word in ocr.words
        ]
        payload = {
            "entities": [_entity("document_1", "document", "ignored", [])],
            "field_assignments": [
                _assignment("uid", "ignored", ["p1_w2"], "document_1", ["p1_w1"]),
            ],
            "debtor_name_occurrences": [],
        }
        record = validate_assignments(payload, ocr)[0]["uid"]
        assert record["status"] == "disputed"
        assert not record["document_value"]


def test_incomplete_state_duty_unit_requires_exact_arithmetic_provenance() -> None:
    ocr = _ocr(["Долг", "1000", "руб.", "Госпошлина", "100", "руб.", "00", "Итого", "1100", "руб."])
    payload = {
        "entities": [_entity("document_1", "document", "ignored", [])],
        "field_assignments": [
            _assignment("debt_amount", "ignored", ["p1_w2", "p1_w3"], "document_1", ["p1_w1"]),
            _assignment("state_duty", "ignored", ["p1_w5"], "document_1", ["p1_w4"]),
            _assignment("total_amount", "ignored", ["p1_w9", "p1_w10"], "document_1", ["p1_w8"]),
        ],
        "debtor_name_occurrences": [],
    }
    duty = validate_assignments(payload, ocr)[0]["state_duty"]
    assert duty["status"] == "confirmed"
    assert duty["raw_ocr_value"] == "100"
    assert duty["value_provenance"]["kind"] == "calculated_unit_completion"
    assert "коп." in duty["document_value"]

def test_contract_date_cannot_confirm_order_date() -> None:
    ocr = _ocr(["Договор", "от", "05.06.2026"])
    payload = {
        "entities": [_entity("document_1", "document", "Судебный приказ", [])],
        "field_assignments": [
            _assignment("order_date", "05.06.2026", ["p1_w3"], "document_1", ["p1_w1", "p1_w2"]),
        ],
        "debtor_name_occurrences": [],
    }
    records, _ = validate_assignments(payload, ocr)
    assert records["order_date"]["status"] == "disputed"


def test_duty_and_total_cannot_confirm_principal_debt() -> None:
    for label in ("Госпошлина", "Итого"):
        ocr = _ocr([label, "1100", "руб."])
        payload = {
            "entities": [_entity("document_1", "document", "Судебный приказ", [])],
            "field_assignments": [
                _assignment("debt_amount", "1100 руб.", ["p1_w2", "p1_w3"], "document_1", ["p1_w1"]),
            ],
            "debtor_name_occurrences": [],
        }
        records, _ = validate_assignments(payload, ocr)
        assert records["debt_amount"]["status"] == "disputed"


def test_ambiguous_address_owner_stays_disputed() -> None:
    ocr = _ocr(["Должник", "Иванов", "адрес", "Омск"])
    payload = {
        "entities": [
            _entity("document_1", "document", "Судебный приказ", []),
            _entity("debtor_1", "debtor", "Иванов", ["p1_w2"]),
        ],
        "field_assignments": [
            _assignment(
                "debtor_address", "Омск", ["p1_w4"], "debtor_1", ["p1_w1", "p1_w2"],
                status="ambiguous",
            ),
        ],
        "debtor_name_occurrences": [],
    }
    records, _ = validate_assignments(payload, ocr)
    assert records["debtor_address"]["status"] == "disputed"

def test_crop_evidence_is_confirmed_only_after_full_revalidation() -> None:
    ocr = _ocr(["Дело", "02-1388/2026"])
    ocr.words = [
        OcrWord(word.word_id, word.line_id, word.page, word.text, word.bbox, 20.0)
        for word in ocr.words
    ]
    payload = {
        "entities": [_entity("document_1", "document", "Судебный приказ", [])], "debtor_name_occurrences": [],
        "field_assignments": [
            _assignment("case_number", "02-1388/2026", ["p1_w2"], "document_1", ["p1_w1"]),
        ],
    }
    initial, _ = validate_assignments(payload, ocr)
    assert initial["case_number"]["status"] == "disputed"
    checked, _ = validate_assignments(
        payload, ocr,
        verified_values={"case_number": {"value": "02-1388/2026", "reason": "targeted_crop_ocr_psm_7"}},
    )
    assert checked["case_number"]["status"] == "confirmed"
    assert checked["case_number"]["verified_value"] == "02-1388/2026"

def test_rare_surname_is_built_only_from_selected_ocr_words() -> None:
    ocr = _ocr(["Должник", "Вараюн", "Валерий", "Александрович"])
    payload = {
        "entities": [
            _entity("document_1", "document", "Судебный приказ", []),
            _entity("debtor_1", "debtor", "ignored by program", ["p1_w2", "p1_w3", "p1_w4"]),
        ],
        "field_assignments": [
            _assignment("debtor_full_name", "Варанов Валерий Александрович", ["p1_w2", "p1_w3", "p1_w4"], "debtor_1", ["p1_w1"]),
        ],
        "debtor_name_occurrences": [],
    }
    records, _ = validate_assignments(payload, ocr)
    record = records["debtor_full_name"]
    assert record["raw_ocr_value"] == "Вараюн Валерий Александрович"
    assert record["extracted_value"] == "Вараюн Валерий Александрович"
    assert record["document_value"] == "Вараюн Валерий Александрович"
    assert record["status"] == "confirmed"


def test_dative_ocr_name_stays_extracted_and_nominative_is_only_derived() -> None:
    ocr = _ocr(["Должника", "Бельского", "Артема", "Игоревича"])
    assignment = _assignment(
        "debtor_full_name", "free text is ignored", ["p1_w2", "p1_w3", "p1_w4"],
        "debtor_1", ["p1_w1"], derived_value="Бельский Артем Игоревич",
    )
    payload = {
        "entities": [
            _entity("document_1", "document", "ignored", []),
            _entity("debtor_1", "debtor", "ignored", ["p1_w2", "p1_w3", "p1_w4"]),
        ],
        "field_assignments": [assignment],
        "debtor_name_occurrences": [
            {"grammatical_case": "nominative", "source_word_ids": ["p1_w2", "p1_w3", "p1_w4"]},
        ],
    }
    record = validate_assignments(payload, ocr)[0]["debtor_full_name"]
    assert record["raw_ocr_value"] == "Бельского Артема Игоревича"
    assert record["extracted_value"] == "Бельского Артема Игоревича"
    assert record["derived_value"] == "Бельский Артем Игоревич"
    assert record["document_value"] == "Бельский Артем Игоревич"
    assert record["value_provenance"] == {
        "kind": "derived_from_printed",
        "printed_value": "Бельского Артема Игоревича",
    }


def test_one_source_cannot_confirm_case_number_and_uid() -> None:
    ocr = _ocr(["Дело", "02-1388/2026"])
    payload = {
        "entities": [_entity("document_1", "document", "Судебный приказ", [])], "debtor_name_occurrences": [],
        "field_assignments": [
            _assignment("case_number", "02-1388/2026", ["p1_w2"], "document_1", ["p1_w1"]),
            _assignment("uid", "02-1388/2026", ["p1_w2"], "document_1", ["p1_w1"]),
        ],
    }
    records, _ = validate_assignments(payload, ocr)
    assert records["case_number"]["status"] == "disputed"
    assert records["uid"]["status"] == "disputed"


def test_user_confirmation_preserves_ocr_and_extracted_values() -> None:
    record = {
        "field_name": "case_number", "raw_ocr_value": "02-138B/2026", "extracted_value": "02-138B/2026",
        "normalized_value": "", "derived_value": "", "verified_value": "", "user_value": "",
        "document_value": "", "status": "disputed",
    }
    updated = apply_user_field_confirmation({"_field_provenance": {"case_number": record}}, "case_number", "02-1388/2026")
    saved = updated["_field_provenance"]["case_number"]
    assert saved["raw_ocr_value"] == "02-138B/2026"
    assert saved["extracted_value"] == "02-138B/2026"
    assert saved["document_value"] == "02-1388/2026"
    assert saved["status"] == "user_confirmed"


def test_locked_normalization_preserves_nested_provenance_and_address() -> None:
    provenance = {"debtor_address": {"raw_ocr_value": "г. Омск ул. Редкая 1"}}
    data = normalize_order_data({
        "_document_values_locked": "1", "_field_provenance": provenance,
        "debtor_address": "г. Омск ул. Редкая 1", "debtor_full_name": "Вараюн Валерий Александрович",
    })
    assert data["_field_provenance"] == provenance
    assert data["debtor_address"] == "г. Омск ул. Редкая 1"
    assert data["debtor_full_name"] == "Вараюн Валерий Александрович"


def test_two_tesseract_tsv_invocations(monkeypatch, tmp_path) -> None:
    source = tmp_path / "order.png"
    source.write_bytes(b"image")
    prepared = tmp_path / "prepared.png"
    prepared.write_bytes(b"prepared")
    calls = []
    monkeypatch.setattr("app.services.tesseract_ai.assess_order_image", lambda path: SimpleNamespace(ok=True, reason=""))
    monkeypatch.setattr("app.services.tesseract_ai.prepare_order_ocr_image", lambda *args, **kwargs: prepared)
    monkeypatch.setattr("app.services.tesseract_ai._tesseract_tsv", lambda path, psm: calls.append((path, psm)) or "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n")
    asyncio.run(extract_fast_tesseract_text(source))
    assert calls == [(prepared, 3), (prepared, 6)]


def test_variants_compatibility_deduplicates_without_rewriting() -> None:
    text = compact_tesseract_texts(["Вараюн Валерий\nДело № 1", "Вараюн Валерий\nВзыскать с Вараюна Валерия"])
    assert text.count("Вараюн Валерий") == 1
    assert "Вараюна Валерия" in text


def test_legacy_flat_helper_keeps_name_locked() -> None:
    data = normalize_tesseract_ai_data({
        "debtor_name_raw": "Вараюна Валерия Александровича",
        "debtor_full_name": "Вараюн Валерий Александрович",
        "case_number": "02-1899/9/2026", "order_date": "05.06.2026",
    })
    normalized = normalize_order_data(data)
    assert normalized["debtor_full_name"] == "Вараюн Валерий Александрович"
    assert normalized["debtor_short_name"] == "Вараюн В.А."


def test_document_reducer_ignores_unconfirmed_values() -> None:
    assert _document_value({"field_name": "uid", "status": "disputed", "normalized_value": "123"}) == ""


def test_text_llm_call_has_no_image_inputs(monkeypatch) -> None:
    from app.services import llm

    captured = {}

    async def fake_responses(settings, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            data={
                "is_court_order": False, "entities": [], "field_assignments": [],
                "debtor_name_occurrences": [], "proceeding_type": "unclear",
                "proceeding_type_source_word_ids": [], "issues": ["not an order"],
            },
            model="text-model", request_id="req", latency_ms=1, usage={},
        )

    async def fake_usage(*args, **kwargs):
        return None

    monkeypatch.setattr(llm, "_responses_json", fake_responses)
    monkeypatch.setattr(llm, "record_openai_usage", fake_usage)
    ocr = _ocr(["Заявление", "о", "выдаче", "приказа"])
    settings = SimpleNamespace(tesseract_ai_model="text-model", text_model="text-model")
    asyncio.run(extract_order_data_from_tesseract_ai(
        settings, None, case_id=1, user_id=2, order_photo_path="unused.png", ocr=ocr,
    ))
    assert "image_path" not in captured
    assert "image_paths" not in captured
    assert captured["model"] == "text-model"


def test_crop_ocr_is_risk_based_and_cached(monkeypatch, tmp_path) -> None:
    prepared = tmp_path / "prepared.png"
    Image.new("L", (1200, 300), "white").save(prepared)
    calls = []

    def fake_ensure(path):
        target = tmp_path / ("crops" if "crops" in str(path) else "debug")
        target.mkdir(exist_ok=True)
        return target

    monkeypatch.setattr("app.services.tesseract_ai.ensure_dir", fake_ensure)
    monkeypatch.setattr("app.services.tesseract_ai._crop_tesseract", lambda path, psm: calls.append((path, psm)) or "02-1388/2026")
    ocr = _ocr(["Дело", "02-1388/2026"])
    ocr.variant_paths = [prepared]
    record = {
        "field_name": "case_number", "extracted_value": "02-1388/2026", "raw_ocr_value": "02-1388/2026",
        "normalized_value": "02-1388/2026", "derived_value": "", "verified_value": "", "user_value": "",
        "document_value": "", "status": "disputed", "verification_reason": "low_ocr_confidence",
        "bbox": [10, 10, 220, 60],
    }
    evidence = asyncio.run(verify_disputed_fields({"case_number": record}, ocr, case_id=1))
    assert evidence["case_number"]["value"] == "02-1388/2026"
    assert record["status"] == "disputed"
    assert record["raw_ocr_value"] == "02-1388/2026"
    assert len(calls) == 3

    second = dict(record, status="disputed", verified_value="", document_value="", verification_reason="low_ocr_confidence")
    second_evidence = asyncio.run(verify_disputed_fields({"case_number": second}, ocr, case_id=1))
    assert second_evidence["case_number"]["value"] == "02-1388/2026"
    assert second["status"] == "disputed"
    assert len(calls) == 3

    good = dict(record, status="confirmed")
    assert asyncio.run(verify_disputed_fields({"case_number": good}, ocr, case_id=1)) == {}
    assert len(calls) == 3


def _layout_ocr(rows: list[tuple[int, int, list[str]]]) -> TesseractOcrResult:
    words: list[OcrWord] = []
    lines: list[OcrLine] = []
    word_number = 1
    for row_number, (block, line_number, tokens) in enumerate(rows):
        y = row_number * 20
        line_words = []
        for column, token in enumerate(tokens):
            word = OcrWord(
                f"p1_w{word_number}", f"p1_b{block}_p1_l{line_number}", 1, token,
                (column * 55, y, column * 55 + 50, y + 12), 92.0,
            )
            words.append(word)
            line_words.append(word)
            word_number += 1
        lines.append(OcrLine(
            f"p1_b{block}_p1_l{line_number}", 1, " ".join(tokens),
            (0, y, max(1, len(tokens)) * 55, y + 12), 92.0,
            tuple(word.word_id for word in line_words),
        ))
    return TesseractOcrResult(
        "\n".join(line.text for line in lines), [line.text for line in lines],
        [Path("prepared.png")], 1, words, lines, "layout-hash",
    )


def test_fixed_assignment_schema_has_every_key_once() -> None:
    assignments = TESSERACT_RECONCILIATION_SCHEMA["properties"]["field_assignments"]
    assert assignments["type"] == "object"
    assert assignments["additionalProperties"] is False
    assert assignments["required"] == list(assignments["properties"])
    assert "debtor_name_raw" not in assignments["properties"]
    assignment_schema = TESSERACT_RECONCILIATION_SCHEMA["$defs"]["field_assignment"]
    assert set(item["$ref"] for item in assignments["properties"].values()) == {"#/$defs/field_assignment"}
    assert "field_name" not in assignment_schema["properties"]


def test_court_address_on_adjacent_line_is_confirmed() -> None:
    ocr = _layout_ocr([(1, 1, ["Судебный", "участок", "№5"]), (1, 2, ["357600,", "Ессентуки", "Шмидта,", "72"])])
    payload = {
        "entities": [_entity("court_1", "court", "ignored", ["p1_w1", "p1_w2", "p1_w3"])],
        "field_assignments": [_assignment("court_address", "ignored", ["p1_w4", "p1_w5", "p1_w6", "p1_w7"], "court_1", ["p1_w1", "p1_w2"])],
        "debtor_name_occurrences": [],
    }
    record = validate_assignments(payload, ocr)[0]["court_address"]
    assert record["status"] == "confirmed"
    assert record["geometry_evidence"]["same_block"] is True


def test_debtor_address_in_same_paragraph_is_confirmed() -> None:
    ocr = _layout_ocr([(1, 1, ["Должник", "Иванов", "Иван", "Иванович", "адрес", "Омск,", "Ленина,", "1"])])
    payload = {
        "entities": [_entity("debtor_1", "debtor", "ignored", ["p1_w2", "p1_w3", "p1_w4"])],
        "field_assignments": [_assignment("debtor_address", "ignored", ["p1_w6", "p1_w7", "p1_w8"], "debtor_1", ["p1_w1", "p1_w2", "p1_w3", "p1_w4"])],
        "debtor_name_occurrences": [],
    }
    assert validate_assignments(payload, ocr)[0]["debtor_address"]["status"] == "confirmed"


def test_creditor_legal_address_with_explicit_subtype_is_confirmed() -> None:
    ocr = _layout_ocr([(1, 1, ["Взыскатель", "Банк", "юридический", "адрес"]), (1, 2, ["Москва,", "Тверская,", "10"])])
    payload = {
        "entities": [_entity("creditor_1", "creditor", "ignored", ["p1_w1", "p1_w2"])],
        "field_assignments": [_assignment("creditor_address", "ignored", ["p1_w5", "p1_w6", "p1_w7"], "creditor_1", ["p1_w1", "p1_w2", "p1_w3", "p1_w4"])],
        "debtor_name_occurrences": [],
    }
    assert validate_assignments(payload, ocr)[0]["creditor_address"]["status"] == "confirmed"


def test_address_of_another_entity_is_not_confirmed() -> None:
    ocr = _layout_ocr([(1, 1, ["Взыскатель", "Банк", "адрес", "Москва"]), (1, 2, ["Должник", "Иванов", "адрес", "Омск"])])
    payload = {
        "entities": [
            _entity("creditor_1", "creditor", "ignored", ["p1_w1", "p1_w2"]),
            _entity("debtor_1", "debtor", "ignored", ["p1_w6"]),
        ],
        "field_assignments": [_assignment("debtor_address", "ignored", ["p1_w4"], "debtor_1", ["p1_w1", "p1_w2"])],
        "debtor_name_occurrences": [],
    }
    assert validate_assignments(payload, ocr)[0]["debtor_address"]["status"] == "disputed"


def test_intervening_entity_makes_address_relation_disputed() -> None:
    ocr = _layout_ocr([(1, 1, ["Должник", "Иванов"]), (1, 2, ["Взыскатель", "Банк"]), (1, 3, ["Омск,", "Ленина,", "1"])])
    payload = {
        "entities": [
            _entity("debtor_1", "debtor", "ignored", ["p1_w1", "p1_w2"]),
            _entity("creditor_1", "creditor", "ignored", ["p1_w3", "p1_w4"]),
        ],
        "field_assignments": [_assignment("debtor_address", "ignored", ["p1_w5", "p1_w6", "p1_w7"], "debtor_1", ["p1_w1", "p1_w2"])],
        "debtor_name_occurrences": [],
    }
    record = validate_assignments(payload, ocr)[0]["debtor_address"]
    assert record["status"] == "disputed"
    assert record["geometry_evidence"]["intervening_entity_ids"] == ["creditor_1"]


def test_debt_period_is_program_normalized_with_provenance() -> None:
    ocr = _ocr(["период", "27.03.2020", "28.11.2020"])
    payload = {
        "entities": [_entity("document_1", "document", "ignored", [])],
        "field_assignments": [_assignment("debt_period", "ignored", ["p1_w2", "p1_w3"], "document_1", ["p1_w1"])],
        "debtor_name_occurrences": [],
    }
    record = validate_assignments(payload, ocr)[0]["debt_period"]
    assert record["document_value"] == "с 27.03.2020 по 28.11.2020"
    assert record["value_provenance"] == {
        "kind": "normalized_from_printed", "printed_value": "27.03.2020 28.11.2020",
    }

def test_ambiguous_debt_period_is_disputed() -> None:
    ocr = _ocr(["период", "27.03.2020"])
    payload = {
        "entities": [_entity("document_1", "document", "ignored", [])],
        "field_assignments": [_assignment("debt_period", "ignored", ["p1_w2"], "document_1", ["p1_w1"])],
        "debtor_name_occurrences": [],
    }
    record = validate_assignments(payload, ocr)[0]["debt_period"]
    assert record["status"] == "disputed"
    assert "format_invalid" in record["verification_reason"]


def test_two_creditor_addresses_without_subtype_are_disputed() -> None:
    ocr = _layout_ocr([
        (1, 1, ["Взыскатель", "Банк", "адрес", "101000,", "Москва"]),
        (1, 2, ["для", "корреспонденции", "443001,", "Самара"]),
    ])
    payload = {
        "entities": [_entity("creditor_1", "creditor", "ignored", ["p1_w1", "p1_w2"])],
        "field_assignments": [_assignment(
            "creditor_address", "ignored", ["p1_w4", "p1_w5"], "creditor_1", ["p1_w1", "p1_w2"],
        )],
        "debtor_name_occurrences": [],
    }
    record = validate_assignments(payload, ocr)[0]["creditor_address"]
    assert record["status"] == "disputed"
    assert "address_span_invalid" in record["verification_reason"]


def test_relation_word_ids_are_deduplicated_programmatically() -> None:
    ocr = _ocr(["Должник", "Иванов", "адрес", "Омск"])
    payload = {
        "entities": [_entity("debtor_1", "debtor", "ignored", ["p1_w1", "p1_w2"])],
        "field_assignments": [_assignment(
            "debtor_address", "ignored", ["p1_w4"], "debtor_1", ["p1_w1", "p1_w1", "p1_w2"],
        )],
        "debtor_name_occurrences": [],
    }
    record = validate_assignments(payload, ocr)[0]["debtor_address"]
    assert record["relation_evidence_word_ids"] == ["p1_w1", "p1_w2"]

def test_document_relation_uses_nearby_semantic_label_not_arbitrary_text() -> None:
    ocr = _ocr(["Дело", "№", "02-1388/2026", "случайный", "18.01.2021"])
    payload = {
        "entities": [_entity("document_1", "document", "ignored", [])],
        "field_assignments": [
            _assignment("case_number", "ignored", ["p1_w2", "p1_w3"], "document_1", ["p1_w2"]),
            _assignment("order_date", "ignored", ["p1_w5"], "document_1", ["p1_w4"]),
        ],
        "debtor_name_occurrences": [],
    }
    records, _ = validate_assignments(payload, ocr)
    assert records["case_number"]["status"] == "confirmed"
    assert records["case_number"]["relation_validation"] == "ocr_context_semantic_label"
    assert records["order_date"]["status"] == "disputed"

def test_text_assignment_is_mapped_to_program_owned_span_across_ocr_runs() -> None:
    first = _ocr(["Дело", "№", "2-146-09-434/2021"])
    second_words = [
        OcrWord(f"r2_p1_w{i}", "r2_p1_b1_p1_l1", 1, word.text, word.bbox, 88.0)
        for i, word in enumerate(first.words, 1)
    ]
    first.words = [
        OcrWord(f"r1_p1_w{i}", "r1_p1_b1_p1_l1", 1, word.text, word.bbox, 92.0)
        for i, word in enumerate(first.words, 1)
    ] + second_words
    payload = {
        "is_court_order": True,
        "field_assignments": {
            "case_number": {
                "printed_value": "2-146-09-434/2021", "semantic_role": "court_order_case_number",
                "derived_value": "", "alternatives": [], "status": "candidate",
            },
        },
        "debtor_name_occurrences": [],
    }
    record = validate_assignments(payload, first)[0]["case_number"]
    assert record["status"] == "confirmed"
    assert record["source_word_ids"] == ["r1_p1_w3"]
    assert record["extracted_value"] == "2-146-09-434/2021"
    assert record["match_provenance"]["two_run_agreement"] is True


def test_text_assignment_cannot_invent_value_absent_from_ocr() -> None:
    ocr = _ocr(["Дело", "№", "2-146-09-434/2021"])
    payload = {
        "is_court_order": True,
        "field_assignments": {
            "case_number": {
                "printed_value": "2-999/2021", "semantic_role": "court_order_case_number",
                "derived_value": "", "alternatives": [], "status": "candidate",
            },
        },
        "debtor_name_occurrences": [],
    }
    record = validate_assignments(payload, ocr)[0]["case_number"]
    assert record["status"] == "disputed"
    assert record["source_word_ids"] == []
    assert "source_text_not_found" in record["verification_reason"]

def _dual_run_ocr(tokens: list[str]) -> TesseractOcrResult:
    base = _ocr(tokens)
    all_words = []
    all_lines = []
    for run, confidence in (("r1_", 92.0), ("r2_", 89.0)):
        run_words = [
            OcrWord(f"{run}p1_w{i}", f"{run}p1_b1_p1_l1", 1, word.text, word.bbox, confidence)
            for i, word in enumerate(base.words, 1)
        ]
        all_words.extend(run_words)
        all_lines.append(OcrLine(
            f"{run}p1_b1_p1_l1", 1, " ".join(tokens), base.lines[0].bbox,
            confidence, tuple(word.word_id for word in run_words),
        ))
    return TesseractOcrResult(
        "\n".join(line.text for line in all_lines), [line.text for line in all_lines],
        [Path("prepared.png")], 1, all_words, all_lines, "dual-hash",
    )


def _text_assignment(value: str, semantic_role: str, *, status: str = "candidate", derived: str = "") -> dict:
    return {
        "printed_value": value, "semantic_role": semantic_role, "derived_value": derived,
        "alternatives": [], "status": status,
    }


def test_program_splits_two_creditor_addresses_by_labels() -> None:
    ocr = _dual_run_ocr([
        "юридический", "адрес:", "101000,", "Москва,", "Ленина,", "1",
        "для", "корреспонденции:", "443001,", "Самара,", "Мира,", "2",
        "реквизиты:", "ИНН", "123",
    ])
    payload = {
        "is_court_order": True,
        "field_assignments": {
            "creditor_legal_address": _text_assignment("", "", status="missing"),
            "creditor_correspondence_address": _text_assignment("", "", status="missing"),
        },
        "debtor_name_occurrences": [],
    }
    records, _ = validate_assignments(payload, ocr)
    assert records["creditor_legal_address"]["extracted_value"] == "101000, Москва, Ленина, 1"
    assert records["creditor_correspondence_address"]["extracted_value"] == "443001, Самара, Мира, 2"
    assert records["creditor_legal_address"]["status"] == "confirmed"
    assert records["creditor_correspondence_address"]["status"] == "confirmed"


def test_single_run_state_duty_requires_crop_before_confirmed() -> None:
    ocr = _ocr(["1277", "руб.", "00", "коп."])
    payload = {
        "is_court_order": True,
        "field_assignments": {"state_duty": _text_assignment("1277 руб. 00 коп.", "state_duty")},
        "debtor_name_occurrences": [],
    }
    initial, _ = validate_assignments(payload, ocr)
    assert initial["state_duty"]["status"] == "disputed"
    assert "ocr_single_run" in initial["state_duty"]["verification_reason"]
    checked, _ = validate_assignments(
        payload, ocr, verified_values={"state_duty": {"value": "1277 руб. 00 коп.", "reason": "targeted_crop_ocr_psm_6", "consensus_psms": [6, 11]}},
    )
    assert checked["state_duty"]["status"] == "confirmed"
    assert checked["state_duty"]["value_provenance"]["kind"] == "targeted_crop_ocr"


def test_crop_can_replace_invalid_uid_without_mutating_printed_source() -> None:
    ocr = _ocr(["26М$0031-01-2021-000169-72"])
    payload = {
        "is_court_order": True,
        "field_assignments": {"uid": _text_assignment("26М$0031-01-2021-000169-72", "court_order_uid")},
        "debtor_name_occurrences": [],
    }
    checked, _ = validate_assignments(
        payload, ocr, verified_values={"uid": {"value": "26М-0031-01-2021-000169-72", "reason": "targeted_crop_ocr_psm_7", "consensus_psms": [7, 11]}},
    )
    record = checked["uid"]
    assert record["status"] == "confirmed"
    assert record["extracted_value"] == "26М$0031-01-2021-000169-72"
    assert record["document_value"] == "26М-0031-01-2021-000169-72"

def test_one_valid_crop_uid_does_not_reach_consensus() -> None:
    value, psms = _select_crop_consensus([
        {"psm": 7, "raw_text": "26MS0031-01-2021-000169-72", "candidate": "26MS0031-01-2021-000169-72"},
        {"psm": 6, "raw_text": "noise", "candidate": ""},
    ])
    assert value == ""
    assert psms == []


def test_two_identical_crop_uid_candidates_reach_consensus() -> None:
    value, psms = _select_crop_consensus([
        {"psm": 7, "raw_text": "26MS0031-01-2021-000169-72", "candidate": "26MS0031-01-2021-000169-72"},
        {"psm": 11, "raw_text": "26MS0031-01-2021-000169-72", "candidate": "26MS0031-01-2021-000169-72"},
    ])
    assert value == "26MS0031-01-2021-000169-72"
    assert psms == [7, 11]


def test_two_different_crop_uid_candidates_do_not_reach_consensus() -> None:
    value, psms = _select_crop_consensus([
        {"psm": 7, "raw_text": "26M50031-01-2021-000169-72", "candidate": "26M50031-01-2021-000169-72"},
        {"psm": 11, "raw_text": "26MS0031-01-2021-000169-72", "candidate": "26MS0031-01-2021-000169-72"},
    ])
    assert value == ""
    assert psms == []


def test_crop_money_without_kopecks_is_not_a_candidate() -> None:
    assert _crop_candidate("state_duty", "1277 руб.", "1277 руб. 00 коп.") == ""


def test_creditor_address_prefers_llm_printed_value_before_fallback() -> None:
    ocr = _dual_run_ocr([
        "юридический", "адрес:", "101000,", "Москва,", "Мясницкая,", "35",
        "для", "корреспонденции:", "443001,", "Самара,", "Мира,", "2", "реквизиты:", "ИНН", "1",
    ])
    payload = {
        "is_court_order": True,
        "field_assignments": {
            "creditor_legal_address": _text_assignment("101000 Москва Мясницкая 35", "creditor_legal_address"),
        },
        "debtor_name_occurrences": [],
    }
    record = validate_assignments(payload, ocr)[0]["creditor_legal_address"]
    assert record["match_provenance"]["kind"] == "ocr_text_match"
    assert record["extracted_value"] == "101000, Москва, Мясницкая, 35"


def test_address_fallback_stops_at_hard_boundaries() -> None:
    ocr = _dual_run_ocr([
        "юридический", "адрес:", "101000,", "Москва,", "Ленина,", "1", "реквизиты:", "ИНН", "123",
        "РЕШИЛ", "должник", "паспорт", "юридический", "адрес:", "999999,", "Чужой,", "2",
        "для", "корреспонденции:", "443001,", "Самара,", "Мира,", "3", "реквизиты:", "БИК", "1",
    ])
    legal_spans = _creditor_labeled_spans("creditor_legal_address", ocr)
    correspondence_spans = _creditor_labeled_spans("creditor_correspondence_address", ocr)
    assert legal_spans and correspondence_spans
    all_values = " | ".join(" ".join(word.text for word in span) for span in [*legal_spans, *correspondence_spans])
    assert "ИНН" not in all_values
    assert "БИК" not in all_values
    assert "РЕШИЛ" not in all_values
    assert "паспорт" not in all_values