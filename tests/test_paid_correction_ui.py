import json
from types import SimpleNamespace

from app.adapters.max import keyboards as max_keyboards
from app.keyboards.common import document_details_menu, documents_menu, paid_document_actions, paid_edit_fields_menu, paid_review_menu
from app.services.paid_correction import PAID_EDITABLE_FIELDS, correction_allowed, record_corrected_field


def test_paid_correction_keyboards_have_only_safe_post_payment_actions():
    start = paid_document_actions()
    assert start.inline_keyboard[0][0].callback_data == 'paid:correction:start'

    edit_payloads = [button.callback_data for row in paid_edit_fields_menu().inline_keyboard for button in row]
    assert 'paid:field:court_name' in edit_payloads
    assert 'case:new' not in edit_payloads
    assert 'case:rephoto_order' not in edit_payloads

    review_payloads = [button.callback_data for row in paid_review_menu().inline_keyboard for button in row]
    assert 'paid:regenerate' in review_payloads

    max_payloads = [button.callback_data for row in max_keyboards.paid_edit_fields_menu() for button in row]
    assert 'paid:field:debtor_full_name' in max_payloads
    assert 'case:new' not in max_payloads


def test_document_archive_uses_user_sequence_but_routes_to_case_id():
    cases = [SimpleNamespace(id=81), SimpleNamespace(id=150)]
    menu = documents_menu(cases)
    assert [row[0].text for row in menu.inline_keyboard[:2]] == ['📄 Заявление 1', '📄 Заявление 2']
    assert [row[0].callback_data for row in menu.inline_keyboard[:2]] == ['case:document:81', 'case:document:150']
    assert document_details_menu(81).inline_keyboard[0][0].callback_data == 'paid:correction:start:81'
    assert max_keyboards.documents_menu(cases)[1][0].text == '📄 Заявление 2'


def test_paid_correction_keeps_at_least_one_original_field():
    case = SimpleNamespace(paid_corrected_fields_json=None)
    fields = sorted(PAID_EDITABLE_FIELDS)
    for field in fields[:-1]:
        assert correction_allowed(case, field)
        record_corrected_field(case, field)
    assert set(json.loads(case.paid_corrected_fields_json)) == set(fields[:-1])
    assert not correction_allowed(case, fields[-1])
    assert correction_allowed(case, fields[0])
