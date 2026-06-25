from datetime import date
from decimal import Decimal

from app.services.legal_data import (
    clean_case_number,
    format_money_rub_kop,
    legal_deadline_from_received,
    money_to_decimal,
    normalize_order_data,
)


def test_case_number_normalization():
    assert clean_case_number("Производство № 2-146-09-434/2021") == "2-146-09-434/2021"
    assert clean_case_number("№ 2-146-09-434/2021") == "2-146-09-434/2021"


def test_money_formatting():
    assert format_money_rub_kop("78472 руб. 87 коп.") == "78 472 руб. 87 коп."
    assert format_money_rub_kop("1277 руб. 00 коп.") == "1 277 руб. 00 коп."


def test_total_amount_calculation():
    data = normalize_order_data(
        {
            "debt_amount": "78472 руб. 87 коп.",
            "state_duty": "1277 руб. 00 коп.",
        }
    )
    assert data["total_amount"] == "79 749 руб. 87 коп."


def test_state_duty_can_be_inferred_from_total_amount():
    data = normalize_order_data(
        {
            "debt_amount": "78472 руб. 87 коп.",
            "total_amount": "79 749 руб. 87 коп.",
        }
    )
    assert data["state_duty"] == "1 277 руб. 00 коп."


def test_missing_order_fields_allow_optional_blank_fields():
    from app.services.legal_data import missing_order_fields

    missing = missing_order_fields(
        {
            "court_name": "судебный участок № 5",
            "debtor_full_name": "Иванов Иван Иванович",
            "creditor_name": "АО «Почта Банк»",
            "order_date": "18.01.2021",
            "debt_amount": "78 472 руб. 87 коп.",
            "uid": "26MS0031-01-2021-000169-72",
        },
        date(2026, 6, 19),
    )
    assert "case_number_or_uid" not in missing
    assert "state_duty_or_total_amount" in missing


def test_deadline_received_19_06_2026():
    received = date(2026, 6, 19)
    deadline = legal_deadline_from_received(received)
    assert deadline == date(2026, 6, 29)


def test_money_decimal_sum():
    debt = money_to_decimal("78472 руб. 87 коп.")
    duty = money_to_decimal("1277 руб. 00 коп.")
    assert debt + duty == Decimal("79749.87")
