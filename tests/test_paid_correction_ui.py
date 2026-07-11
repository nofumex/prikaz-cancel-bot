from app.adapters.max import keyboards as max_keyboards
from app.keyboards.common import paid_document_actions, paid_edit_fields_menu, paid_review_menu


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
