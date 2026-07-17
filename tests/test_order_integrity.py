from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.document_templates.statement_templates import StatementContext, build_header_lines, build_statement_paragraphs
from app.services.legal_data import money_from_source_fragment, normalize_order_data
from app.services.classic_ocr import classic_court_name
from app.services.order_integrity import (
    conflicting_fields,
    evidence_payload_fields,
    merge_verified_order_data,
)


@pytest.mark.asyncio
async def test_confident_non_order_skips_verifier_amounts_and_adjudicator(monkeypatch) -> None:
    from app.services import llm

    primary = llm.PrimaryOrderExtraction(
        data={"court_name": "", "case_number": "", "debtor_full_name": ""},
        document_kind="other",
        is_court_order=False,
        confidence=0.99,
        reason="FSSP enforcement proceeding card",
    )
    verifier = AsyncMock()
    amounts = AsyncMock()
    adjudicator = AsyncMock()
    classic = AsyncMock(return_value="Исполнительное производство №964723/26/66023-ИП")
    monkeypatch.setattr(llm, "_extract_order_data_primary", AsyncMock(return_value=primary))
    monkeypatch.setattr(llm, "_extract_order_evidence", verifier)
    monkeypatch.setattr(llm, "_extract_order_amounts_result", amounts)
    monkeypatch.setattr(llm, "_adjudicate_order_conflicts", adjudicator)
    monkeypatch.setattr(llm, "extract_classic_ocr_text", classic)

    result = await llm._extract_order_data_uncached(
        SimpleNamespace(order_integrity_enabled=True),
        None,
        case_id=106,
        user_id=79,
        order_photo_path="case_106_order.jpg",
    )

    assert result["_document_kind"] == "other"
    assert result["_document_type_confidence"] == "0.99"
    verifier.assert_not_awaited()
    amounts.assert_not_awaited()
    adjudicator.assert_not_awaited()
def test_contract_number_is_not_accepted_as_uid() -> None:
    data = normalize_order_data({"case_number": "2-59/2015", "uid": "0012297461"})
    assert data["case_number"] == "2-59/2015"
    assert data["uid"] == ""


def test_short_court_locality_is_not_duplicated_as_address() -> None:
    data = normalize_order_data({
        "court_name": "судебного участка № 41 с. Георгиевское и Межевского района Костромской области",
        "court_address": "с. Георгиевское",
    })
    assert data["court_address"] == ""


def test_money_fragment_does_not_confuse_contract_date_with_amount() -> None:
    fragment = "договору № 4319870 от 21.07.2025 в размере 5283 руб. 59 коп."
    assert money_from_source_fragment(fragment) == Decimal("5283.59")


def test_classic_court_name_extracts_region_without_judge() -> None:
    text = "Мировой судья судебного участка № 1 Перелюбского района Саратовской области Бишева А.А."
    assert classic_court_name(text) == "судебного участка № 1 Перелюбского района Саратовской области"


def _payload(**values):
    fields = {}
    names = (
        "court_name", "judge", "debtor_full_name", "debtor_address",
        "creditor_name", "creditor_address", "case_number", "uid",
        "order_date", "debt_contract", "debt_period", "debt_amount",
        "state_duty", "total_amount",
    )
    for name in names:
        value = values.get(name, "")
        fields[name] = {
            "value": value,
            "source_fragment": f"source: {value}" if value else "",
        }
    return {"fields": fields, "document_comment": ""}


def test_court_role_prefix_is_semantically_equal_and_normalized():
    primary = {"court_name": "Мировой суд судебного участка № 41 с. Георгиевское"}
    verifier = evidence_payload_fields(
        _payload(court_name="судебного участка № 41 с. Георгиевское")
    )
    assert conflicting_fields(primary, verifier) == []
    normalized = normalize_order_data(primary)
    assert normalized["court_name"] == "судебного участка № 41 с. Георгиевское"
    assert normalized["court_addressee"] == "Мировому судье судебного участка № 41 с. Георгиевское"


def test_adjudicator_repairs_single_letter_address_error():
    primary = {
        "debtor_address": "д. Поленьевица, д. 19 Межевского района",
        "debt_amount": "119 030 руб. 79 коп.",
        "state_duty": "1 790 руб. 31 коп.",
        "total_amount": "120 821 руб. 10 коп.",
    }
    verifier = _payload(
        debtor_address="д. Поденьевица, д. 19 Межевского района",
        debt_amount="119 030 руб. 79 коп.",
        state_duty="1 790 руб. 31 коп.",
        total_amount="120 821 руб. 10 коп.",
    )
    adjudicator = _payload(debtor_address="д. Поденьевица, д. 19 Межевского района")
    decision = merge_verified_order_data(primary, verifier, adjudicator)
    assert decision.conflicts == ["debtor_address"]
    assert decision.data["debtor_address"] == "д. Поденьевица, д. 19 Межевского района"
    assert decision.applied_fields["debtor_address"].startswith("д. Поденьевица")


def test_verifier_repairs_debt_total_role_swap():
    primary = {
        "debt_amount": "120 821 руб. 10 коп.",
        "state_duty": "1 790 руб. 31 коп.",
        "total_amount": "120 821 руб. 10 коп.",
    }
    verifier = _payload(
        debt_amount="119 030 руб. 79 коп.",
        state_duty="1 790 руб. 31 коп.",
        total_amount="120 821 руб. 10 коп.",
    )
    adjudicator = _payload(debt_amount="119 030 руб. 79 коп.")
    decision = merge_verified_order_data(primary, verifier, adjudicator)
    assert decision.data["debt_amount"] == "119 030 руб. 79 коп."
    assert decision.data["total_amount"] == "120 821 руб. 10 коп."


def test_case_89_regression_produces_only_source_grounded_facts():
    primary = {
        "court_name": "Мировой суд судебного участка № 41 с. Георгиевское и Межевского района Костромской области",
        "judge": "Ларионова Е.Ф.",
        "debtor_full_name": "Саматуга Юрий Алексеевич",
        "debtor_address": "д. Поленьевица, д. 19 Межевского района Костромской области",
        "creditor_name": "ТИНЬКОФФ Кредитные Системы Банк (ЗАО)",
        "creditor_address": "123060 г. Москва, 1-й Волоколамский пр-д, д. 10, стр. 1",
        "case_number": "2-59 /2015",
        "uid": "",
        "order_date": "05.03.2015",
        "debt_contract": "0012297461 от 20.04.2011",
        "debt_period": "по состоянию на 05.02.2015",
        "debt_amount": "119030 руб. 79 коп.",
        "state_duty": "1790 руб. 31 коп.",
        "total_amount": "120821 (сто двадцать тысяч восемьсот двадцать один) рубль 10 копеек",
    }
    verifier = _payload(**{
        **primary,
        "court_name": "судебного участка № 41 с. Георгиевское и Межевского района Костромской области",
        "debtor_address": "д. Поденьевица, д. 19 Межевского района Костромской области",
        "case_number": "2-59/2015",
    })
    adjudicator = _payload(
        court_name="судебного участка № 41 с. Георгиевское и Межевского района Костромской области",
        debtor_address="д. Поденьевица, д. 19 Межевского района Костромской области",
    )

    decision = merge_verified_order_data(primary, verifier, adjudicator)
    assert decision.unresolved_fields == []
    assert decision.data["case_number"] == "2-59/2015"
    assert decision.data["debtor_address"].startswith("д. Поденьевица")
    assert decision.data["total_amount"] == "120 821 руб. 10 коп."

    ctx = StatementContext(
        data=decision.data,
        received_date=date(2026, 7, 15),
        deadline_date=date(2026, 7, 27),
        document_date=date(2026, 7, 15),
    )
    final_text = "\n".join(build_header_lines(ctx) + build_statement_paragraphs(ctx))
    assert "Мировому судье Мировой суд" not in final_text
    assert "Поленьевица" not in final_text
    assert "2-59 /2015" not in final_text
    assert "договору № 0012297461 от 20.04.2011" in final_text
    assert "119 030 руб. 79 коп." in final_text
