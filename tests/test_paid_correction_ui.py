import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.adapters.max import keyboards as max_keyboards
from app.keyboards.common import document_details_menu, documents_menu, paid_document_actions, paid_edit_fields_menu, paid_review_menu
from app.services.documents import create_case_documents_reviewed, extraction_preview
from app.services.document_render_contract import select_creditor_address_for_render
from app.services.document_templates.renderer import _render_statement_docx
from app.services.document_templates.statement_templates import StatementContext, build_statement_paragraphs
from app.services.document_templates.styles import StyleProfile
from app.services.legal_data import docx_text, normalize_order_data, validate_amounts
from app.services.paid_correction import PAID_EDITABLE_FIELDS, apply_paid_correction, correction_allowed, paid_regeneration_requires_new_date, prepare_paid_case_data, record_corrected_field, regenerate_paid_case


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
    for field in ('judge_name', 'interest', 'penalty', 'total_amount', 'creditor_legal_address', 'creditor_correspondence_address'):
        assert f'paid:field:{field}' in edit_payloads
        assert f'paid:field:{field}' in max_payloads


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


def test_document_archive_shows_newest_user_number_first():
    cases = [SimpleNamespace(id=value) for value in range(14, 9, -1)]
    menu = documents_menu(cases, page=0, total_pages=3, start_index=14, descending=True)
    assert [row[0].text for row in menu.inline_keyboard[:5]] == [f'📄 Заявление {value}' for value in range(14, 9, -1)]
    max_menu = max_keyboards.documents_menu(cases, page=0, total_pages=3, start_index=14, descending=True)
    assert [row[0].text for row in max_menu[:5]] == [f'📄 Заявление {value}' for value in range(14, 9, -1)]


def test_archive_data_title_does_not_repeat_review_heading():
    text = extraction_preview({}, None, [], title='📄 <b>Данные в заявлении:</b>')
    assert text.count('Данные в заявлении:') == 1
    assert 'Проверьте данные' not in text


def test_paid_correction_allows_every_editable_source_field():
    case = SimpleNamespace(paid_corrected_fields_json=None)
    fields = sorted(PAID_EDITABLE_FIELDS)
    for field in fields[:-1]:
        assert correction_allowed(case, field)
        record_corrected_field(case, field)
    assert set(json.loads(case.paid_corrected_fields_json)) == set(fields[:-1])
    assert correction_allowed(case, fields[-1])
    assert correction_allowed(case, fields[0])


def test_paid_regeneration_requires_current_deadline_or_restore_reason():
    expired = SimpleNamespace(deadline_date=date.today() - timedelta(days=1), extracted_json='{}')
    assert paid_regeneration_requires_new_date(expired)
    expired.extracted_json = json.dumps({'restore_reason': 'Причина пропуска срока: болезнь.'})
    assert not paid_regeneration_requires_new_date(expired)
    current = SimpleNamespace(deadline_date=date.today() + timedelta(days=1), extracted_json='{}')
    assert not paid_regeneration_requires_new_date(current)


@pytest.mark.asyncio
async def test_paid_regeneration_blocks_legacy_amount_mismatch_without_rewriting(monkeypatch):
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

    with pytest.raises(ValueError, match='Суммы не совпадают'):
        await regenerate_paid_case(session, SimpleNamespace(), case, user)

    saved = json.loads(case.extracted_json)
    assert saved['debt_amount'] == '78 742 руб. 00 коп.'
    assert case.paid_regeneration_count == 0
    review.assert_not_awaited()


def test_multiple_manual_corrections_accumulate_and_clear_stale_render_fields():
    case = SimpleNamespace(
        extracted_json=json.dumps({
            'court_name': 'Старый суд',
            'debtor_full_name': 'Старое Имя',
            'debt_amount': '100 руб. 00 коп.',
            'render': {'debtor_full_name': 'Старое Имя'},
            'render_debtor_short_name': 'С. И.',
        }),
        paid_corrected_fields_json=None,
        paid_corrections_json=None,
    )

    apply_paid_correction(case, 'court_name', 'Новый судебный участок № 7')
    apply_paid_correction(case, 'debtor_full_name', 'Иванов Иван Иванович')
    result = apply_paid_correction(case, 'debt_amount', '777 руб. 77 коп.')

    assert result['court_name'] == 'Новый судебный участок № 7'
    assert result['debtor_full_name'] == 'Иванов Иван Иванович'
    assert result['debt_amount'] == '777 руб. 77 коп.'
    assert result['debtor_short_name'] == 'Иванов И.И.'
    assert 'render' not in result
    assert not any(key.startswith('render_') for key in result)
    assert prepare_paid_case_data(case) == result
    renormalized = normalize_order_data(result)
    assert renormalized['debtor_full_name'] == 'Иванов Иван Иванович'
    assert renormalized['debt_amount'] == '777 руб. 77 коп.'


def test_interest_and_penalty_are_breakdown_not_extra_total_addends():
    data = {
        'court_name': 'Судебный участок № 7',
        'debtor_full_name': 'Иванов Иван Иванович',
        'creditor_name': 'ООО Банк',
        'case_number': '2-100/2026',
        'order_date': '01.07.2026',
        'debt_amount': '32 200 руб. 00 коп.',
        'interest': '17 241 руб. 25 коп.',
        'penalty': '958 руб. 75 коп.',
        'state_duty': '2 000 руб. 00 коп.',
        'total_amount': '34 200 руб. 00 коп.',
    }
    check = validate_amounts(data)
    assert check.ok
    assert str(check.computed_total) == '34200.00'
    text = ' '.join(build_statement_paragraphs(StatementContext(
        data=normalize_order_data(data), received_date=date(2026, 7, 2),
        deadline_date=date(2099, 1, 1), document_date=date(2026, 7, 3),
    )))
    assert 'в том числе проценты в размере 17 241 руб. 25 коп., пени в размере 958 руб. 75 коп.' in text


def test_generic_creditor_address_and_manual_total_change_rendered_sources():
    case = SimpleNamespace(
        extracted_json=json.dumps({
            'creditor_address': 'Старый общий адрес',
            'creditor_legal_address': 'Старый юридический адрес',
            'debt_amount': '100 руб. 00 коп.',
            'state_duty': '20 руб. 00 коп.',
            'total_amount': '120 руб. 00 коп.',
        }),
        paid_corrected_fields_json=None,
        paid_corrections_json=None,
    )
    apply_paid_correction(case, 'creditor_address', 'Новый адрес взыскателя')
    result = apply_paid_correction(case, 'total_amount', '120 руб. 00 коп.')
    assert select_creditor_address_for_render(result) == ('creditor_legal_address', 'Новый адрес взыскателя')
    assert result['amount_render_mode'] == 'explicit_total'


def test_render_only_legacy_payload_recovers_debt_facts_before_cleanup():
    case = SimpleNamespace(
        extracted_json=json.dumps({'render': {
            'court_addressee': 'Мировому судье судебного участка № 7',
            'judge_name': 'Петрова А.А.',
            'debtor_full_name': 'Иванов Иван Иванович',
            'creditor_name': 'ООО Банк',
            'case_identifier': '№ 2-100/2026, УИД 24MS0001-01-2026-000001-01',
            'order_date_long': '1 июля 2026 года',
            'order_facts_sentence': (
                '1 июля 2026 года вынесен приказ о взыскании задолженности '
                'по договору № 55 за период с 01.01.2026 по 30.06.2026 '
                'в размере 32 200 руб. 00 коп. (в том числе проценты в размере '
                '17 241 руб. 25 коп., пени в размере 958 руб. 75 коп.), а также '
                'государственной пошлины в размере 2 000 руб. 00 коп. Общая сумма '
                'взыскания составляет 34 200 руб. 00 коп.'
            ),
        }}),
        paid_corrected_fields_json=None,
        paid_corrections_json=None,
    )
    result = prepare_paid_case_data(case)
    assert result['debt_amount'] == '32 200 руб. 00 коп.'
    assert result['interest'] == '17 241 руб. 25 коп.'
    assert result['penalty'] == '958 руб. 75 коп.'
    assert result['state_duty'] == '2 000 руб. 00 коп.'
    assert result['total_amount'] == '34 200 руб. 00 коп.'
    assert result['debt_contract'] == 'договору № 55'
    assert result['debt_period'] == 'с 01.01.2026 по 30.06.2026'
    assert 'render' not in result


def test_real_docx_contains_all_corrected_values(tmp_path):
    case = SimpleNamespace(
        extracted_json=json.dumps({
            'court_name': 'Судебный участок № 1', 'judge_name': 'Петрова А.А.',
            'debtor_full_name': 'Старое Имя', 'debtor_address': 'г. Омск, ул. Мира, д. 1',
            'creditor_name': 'ООО Банк', 'creditor_address': 'Старый адрес',
            'creditor_legal_address': 'Старый юридический адрес',
            'case_number': '2-1/2025', 'order_date': '01.01.2025',
            'debt_amount': '100 руб. 00 коп.', 'state_duty': '20 руб. 00 коп.',
            'total_amount': '120 руб. 00 коп.',
            'render': {'debtor_full_name': 'Старое Имя', 'case_identifier': '№ 2-1/2025'},
        }),
        paid_corrected_fields_json=None, paid_corrections_json=None,
    )
    apply_paid_correction(case, 'debtor_full_name', 'Иванов Иван Иванович')
    apply_paid_correction(case, 'case_number', '2-777/2026')
    apply_paid_correction(case, 'order_date', '03.07.2026')
    apply_paid_correction(case, 'creditor_address', 'Новый адрес взыскателя')
    data = apply_paid_correction(case, 'total_amount', '120 руб. 00 коп.')
    path = tmp_path / 'corrected.docx'
    _render_statement_docx(path, StatementContext(
        data=data, received_date=date(2026, 7, 4), deadline_date=date(2099, 1, 1),
        document_date=date(2026, 7, 5),
    ), StyleProfile.normal())
    text = docx_text(path)
    assert 'Иванов Иван Иванович' in text
    assert 'Иванов И.И.' in text
    assert text.count('2-777/2026') >= 2
    assert text.count('3 июля 2026 года') >= 2
    assert 'Новый адрес взыскателя' in text
    assert 'Общая сумма взыскания составляет 120 руб. 00 коп.' in text
    assert 'Старое Имя' not in text
    assert 'Старый юридический адрес' not in text


@pytest.mark.asyncio
async def test_reviewed_regeneration_path_never_calls_ocr_or_llm(monkeypatch, tmp_path):
    artifact = SimpleNamespace(full_docx_path=tmp_path / 'statement.docx')
    generate = AsyncMock(side_effect=AssertionError('OCR must not run'))
    review = AsyncMock(side_effect=AssertionError('LLM must not run'))
    monkeypatch.setattr('app.services.llm.extract_order_data', generate)
    monkeypatch.setattr('app.services.documents.review_generated_document', review)
    monkeypatch.setattr(
        'app.services.documents.create_case_documents_with_qa',
        lambda *args, **kwargs: artifact,
    )
    outcome = await create_case_documents_reviewed(
        SimpleNamespace(id=1), SimpleNamespace(id=1), SimpleNamespace(), None,
    )
    assert outcome.ok
    assert outcome.artifacts is artifact
    generate.assert_not_awaited()
    review.assert_not_awaited()
