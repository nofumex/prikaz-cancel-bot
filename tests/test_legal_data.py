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


def test_deadline_received_19_06_2026():
    received = date(2026, 6, 19)
    deadline = legal_deadline_from_received(received)
    assert deadline == date(2026, 6, 29)


def test_money_decimal_sum():
    debt = money_to_decimal("78472 руб. 87 коп.")
    duty = money_to_decimal("1277 руб. 00 коп.")
    assert debt + duty == Decimal("79749.87")
