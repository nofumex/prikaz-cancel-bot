from datetime import date
from decimal import Decimal

from app.services.legal_data import (
    bad_tokens_in_text,
    clean_case_number,
    clean_debtor_address,
    clean_money_text,
    format_money_rub_kop,
    legal_deadline_from_received,
    money_to_decimal,
    money_from_source_fragment,
    normalize_case_identifiers,
    normalize_order_data,
)


def test_money_with_parenthetical_words_preserves_kopeks():
    value = "120821 (сто двадцать тысяч восемьсот двадцать один) рубль 10 копеек"
    assert money_to_decimal(value) == Decimal("120821.10")
    assert clean_money_text(value) == "120 821 руб. 10 коп."


def test_money_with_currency_inside_parentheses_preserves_kopeks():
    value = "2000 (две тысячи рублей 00 копеек)"
    assert money_to_decimal(value) == Decimal("2000.00")
    assert clean_money_text(value) == "2 000 руб. 00 коп."


def test_whole_rubles_without_kopeks_are_exact_money():
    assert money_to_decimal("2000 руб.") == Decimal("2000.00")
    assert clean_money_text("44 600 рублей") == "44 600 руб. 00 коп."
    assert clean_money_text("769 руб.") == "769 руб. 00 коп."


def test_amount_is_grounded_in_role_specific_source_fragment():
    fragment = "задолженность по договору займа №1906699913 в размере 25704,49"
    assert money_from_source_fragment(fragment) == Decimal("25704.49")
    assert money_from_source_fragment("государственной пошлины в размере 2000 руб. 00 коп.") == Decimal("2000.00")


def test_case_92_identifiers_are_separated():
    case_number, uid = normalize_case_identifiers(
        "09MS0020-01-2026-001641-42 Дело №2-1292/2026",
        "09MS0020-01-2026-001641-42",
    )
    assert case_number == "2-1292/2026"
    assert uid == "09MS0020-01-2026-001641-42"


def test_cyrillic_ms_uid_is_canonicalized_and_nonstandard_uid_is_preserved():
    assert normalize_case_identifiers(
        "09МС0020-01-2026-001641-42, Дело № 2-1292/2026", ""
    ) == ("2-1292/2026", "09MS0020-01-2026-001641-42")
    assert normalize_case_identifiers("133511", "АСВ_238_133511") == ("133511", "АСВ_238_133511")


def test_court_postal_address_is_not_part_of_court_name():
    data = normalize_order_data(
        {"court_name": "судебного участка № 1 Хабезского судебного района, 369400, КЧР, а. Хабез"}
    )
    assert data["court_name"] == "судебного участка № 1 Хабезского судебного района"


def test_common_legal_homoglyphs_and_duplicate_postcode_are_normalized():
    data = normalize_order_data(
        {
            "creditor_name": 'OOO ПКO "ЭОС"',
            "debtor_address": "662978, 662978, Красноярский край, г. Железногорск",
        }
    )
    assert data["creditor_name"] == 'ООО ПКО "ЭОС"'
    assert data["debtor_address"] == "662978, Красноярский край, г. Железногорск"
    assert normalize_order_data({"creditor_name": "ООО ПКЮ «Бустер.Ру»"})["creditor_name"] == "ООО ПКО «Бустер.Ру»"


def test_debt_basis_preserves_alphanumeric_contract_prefix():
    data = normalize_order_data({"debt_contract": "договор № СП356655 от 16.11.2021"})
    assert data["debt_basis_number"] == "СП356655"


def test_case_number_removes_ocr_spacing_around_separators():
    assert clean_case_number("Дело № 2-59 /2015") == "2-59/2015"


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


def test_debtor_address_registration_extracted_from_birthplace_ocr():
    raw = (
        "\u0443\u0440\u043e\u0436\u0435\u043d\u0435\u0446 \u0433. \u0410\u0447\u0438\u043d\u0441\u043a \u041a\u0440\u0430\u0441\u043d\u043e\u044f\u0440\u0441\u043a\u043e\u0433\u043e \u043a\u0440\u0430\u044f, "
        "\u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c\u0443 \u0432 \u0433\u043e\u0440\u043e\u0434\u0435 \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, "
        "\u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e \u0434. 14, \u043a\u0432. 9"
    )

    assert clean_debtor_address(raw) == "\u0433. \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, \u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e, \u0434. 14, \u043a\u0432. 9"

    normalized = normalize_order_data({"debtor_address": raw})
    assert normalized["debtor_address"] == "\u0433. \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, \u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e, \u0434. 14, \u043a\u0432. 9"
    assert "\u0410\u0447\u0438\u043d\u0441\u043a" not in normalized["debtor_address"]
    assert "\u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440" not in normalized["debtor_address"].lower()


def test_bad_tokens_reject_debtor_header_ocr_noise():
    bad = bad_tokens_in_text(
        "\u0430\u0434\u0440\u0435\u0441: \u0433. \u0410\u0447\u0438\u043d\u0441\u043a \u041a\u0440\u0430\u0441\u043d\u043e\u044f\u0440\u0441\u043a\u043e\u0433\u043e \u043a\u0440\u0430\u044f, "
        "\u0443\u0440\u043e\u0436\u0435\u043d\u0435\u0446, \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c\u0443, \u043f\u0430\u0441\u043f\u043e\u0440\u0442"
    )

    assert "\u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c\u0443" in bad
    assert "\u0443\u0440\u043e\u0436\u0435\u043d" in bad
    assert "\u043f\u0430\u0441\u043f\u043e\u0440\u0442" in bad


def test_debtor_address_supports_common_registration_markers():
    expected = "\u0433. \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, \u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e, \u0434. 14"
    variants = [
        "\u043f\u0430\u0441\u043f\u043e\u0440\u0442 1234 \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d \u043f\u043e \u0430\u0434\u0440\u0435\u0441\u0443: \u0433\u043e\u0440\u043e\u0434 \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, \u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e \u0434. 14 \u0432\u044b\u0434\u0430\u043d \u041c\u0412\u0414",
        "\u0434\u0430\u0442\u0430 \u0440\u043e\u0436\u0434\u0435\u043d\u0438\u044f 01.01.1980, \u043f\u0440\u043e\u0436\u0438\u0432\u0430\u0435\u0442 \u043f\u043e \u0430\u0434\u0440\u0435\u0441\u0443 \u0433. \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, \u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e \u0434. 14",
        "\u043c\u0435\u0441\u0442\u043e \u0436\u0438\u0442\u0435\u043b\u044c\u0441\u0442\u0432\u0430: \u0432 \u0433\u043e\u0440\u043e\u0434\u0435 \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, \u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e \u0434. 14",
        "\u0430\u0434\u0440\u0435\u0441 \u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u0438: \u0433. \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, \u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e \u0434. 14",
    ]

    assert [clean_debtor_address(value) for value in variants] == [expected] * len(variants)


def test_debtor_address_returns_empty_when_passport_noise_remains():
    assert clean_debtor_address("\u043f\u0430\u0441\u043f\u043e\u0440\u0442 1234 \u0432\u044b\u0434\u0430\u043d \u041c\u0412\u0414") == ""
