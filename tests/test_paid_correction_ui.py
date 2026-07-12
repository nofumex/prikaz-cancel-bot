import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.adapters.max import keyboards as max_keyboards
from app.keyboards.common import document_details_menu, documents_menu, paid_document_actions, paid_edit_fields_menu, paid_review_menu
from app.services.documents import extraction_preview
from app.services.paid_correction import PAID_EDITABLE_FIELDS, correction_allowed, paid_regeneration_requires_new_date, record_corrected_field, regenerate_paid_case


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


def test_document_archive_paginates_five_items_with_stable_numbers():
    cases = [SimpleNamespace(id=value) for value in range(6, 11)]
    menu = documents_menu(cases, page=1, total_pages=3, start_index=5)
    assert [row[0].text for row in menu.inline_keyboard[:5]] == [f'📄 Заявление {value}' for value in range(6, 11)]
    nav = menu.inline_keyboard[5]
    assert [button.callback_data for button in nav] == ['case:my:0', 'case:my:noop', 'case:my:2']


def test_archive_data_title_does_not_repeat_review_heading():
    text = extraction_preview({}, None, [], title='📄 <b>Данные в заявлении:</b>')
    assert text.count('Данные в заявлении:') == 1
    assert 'Проверьте данные' not in text


def test_paid_correction_keeps_at_least_one_original_field():
    case = SimpleNamespace(paid_corrected_fields_json=None)
    fields = sorted(PAID_EDITABLE_FIELDS)
    for field in fields[:-1]:
        assert correction_allowed(case, field)
        record_corrected_field(case, field)
    assert set(json.loads(case.paid_corrected_fields_json)) == set(fields[:-1])
    assert not correction_allowed(case, fields[-1])
    assert correction_allowed(case, fields[0])


def test_paid_regeneration_requires_current_deadline_or_restore_reason():
    expired = SimpleNamespace(deadline_date=date.today() - timedelta(days=1), extracted_json='{}')
    assert paid_regeneration_requires_new_date(expired)
    expired.extracted_json = json.dumps({'restore_reason': 'Причина пропуска срока: болезнь.'})
    assert not paid_regeneration_requires_new_date(expired)
    current = SimpleNamespace(deadline_date=date.today() + timedelta(days=1), extracted_json='{}')
    assert not paid_regeneration_requires_new_date(current)


@pytest.mark.asyncio
async def test_paid_regeneration_repairs_legacy_amount_mismatch(monkeypatch):
    case = SimpleNamespace(
        id=23,
        extracted_json=json.dumps({
            'debt_amount': '78 742 руб. 00 коп.',
            'state_duty': '1 277 руб. 00 коп.',
            'total_amount': '79 749 руб. 87 коп.',
        }),
        full_doc_path=None,
        full_pdf_path=None,
        preview_pdf_path=None,
        instruction_path=None,
        paid_regeneration_count=0,
    )
    artifacts = SimpleNamespace(
        full_docx_path='statement.docx', full_pdf_path=None,
        preview_pdf_path=None, instruction_docx_path='instruction.docx',
    )
    review = AsyncMock(return_value=SimpleNamespace(ok=True, artifacts=artifacts))
    monkeypatch.setattr('app.services.paid_correction.create_case_documents_reviewed', review)
    monkeypatch.setattr('app.services.paid_correction.schedule_crm_sync', lambda *args, **kwargs: None)
    session = SimpleNamespace(commit=AsyncMock())
    user = SimpleNamespace(id=1)

    await regenerate_paid_case(session, SimpleNamespace(), case, user)

    saved = json.loads(case.extracted_json)
    assert saved['debt_amount'] == '78 472 руб. 87 коп.'
    assert case.paid_regeneration_count == 1
