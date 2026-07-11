import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.services.received_date import validate_received_date
from app.utils import parse_russian_date


@pytest.mark.parametrize('raw', [
    '10 07 2026',
    '10/07/26',
    '10/07/2026',
    '10,07,26',
    '10,07,2026',
    '10.07.2026',
    '10-07-2026',
])
def test_received_date_supported_formats(raw):
    assert parse_russian_date(raw) == date(2026, 7, 10)


def test_received_date_ambiguous_keeps_day_month_order():
    assert parse_russian_date('07/10/2026') == date(2026, 10, 7)


@pytest.mark.parametrize('raw', ['07/13/2026', '31.02.2026', '2026-07-10'])
def test_received_date_rejects_invalid_or_iso(raw):
    assert parse_russian_date(raw) is None


def test_received_date_rejects_before_order_and_future():
    case = SimpleNamespace(extracted_json=json.dumps({'order_date': '11.07.2026'}))
    _, error = validate_received_date(case, '10.07.2026', today=date(2026, 7, 11))
    assert 'раньше даты судебного приказа' in error
    case.extracted_json = json.dumps({'order_date': '01.07.2026'})
    _, error = validate_received_date(case, '12.07.2026', today=date(2026, 7, 11))
    assert 'не может быть в будущем' in error
