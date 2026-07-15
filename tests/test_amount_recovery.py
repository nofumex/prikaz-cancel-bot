from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import get_settings
from app.services.amount_recovery import (
    format_amount_mismatch_admin_report,
    recover_amounts_from_mismatch,
)
from app.services.legal_data import money_to_decimal, normalize_order_data, parse_money, validate_amounts


def test_debug_case_amounts_script_exists():
    assert Path("scripts/debug_case_amounts.py").exists()


def test_parse_money_rubles_kopecks():
    assert parse_money("78 472 руб. 87 коп.") == Decimal("78472.87")
    assert parse_money("78 472,87 руб.") == Decimal("78472.87")
    assert parse_money("1 277 руб. 00 коп.") == Decimal("1277.00")
    assert parse_money("79 749 руб. 87 коп.") == Decimal("79749.87")
    assert parse_money("78472 руб. 87 коп.") == Decimal("78472.87")
    assert parse_money("78.472 руб. 87 коп.") == Decimal("78472.87")


def test_amount_mismatch_triggers_targeted_retry():
    settings = get_settings()
    assert settings.amount_retry_on_mismatch is True
    data = normalize_order_data(
        {
            "debt_amount": "78 472 руб. 00 коп.",
            "state_duty": "1 277 руб. 00 коп.",
            "total_amount": "79 749 руб. 87 коп.",
        }
    )
    check = validate_amounts(data)
    assert not check.ok
    assert "amount_mismatch" in check.errors


def test_retry_amounts_fix_mismatch():
    primary = normalize_order_data(
        {
            "debt_amount": "78 472 руб. 00 коп.",
            "state_duty": "1 277 руб. 00 коп.",
            "total_amount": "79 749 руб. 87 коп.",
        }
    )
    retry = {
        "debt_amount": "78 472 руб. 87 коп.",
        "debt_amount_fragment": "задолженность 78 472 руб. 87 коп.",
        "state_duty": "1 277 руб. 00 коп.",
        "state_duty_fragment": "госпошлина 1 277 руб. 00 коп.",
        "total_amount": "79 749 руб. 87 коп.",
        "total_amount_fragment": "всего к взысканию 79 749 руб. 87 коп.",
        "confidence": 0.92,
        "comment": "ok",
    }
    recovery = recover_amounts_from_mismatch(primary, retry)
    assert recovery.applied
    assert recovery.recovery_method == "amounts_recovered_by_retry"
    assert validate_amounts(recovery.order_data).ok


def test_total_minus_state_duty_recovery():
    primary = normalize_order_data(
        {
            "debt_amount": "78 742 руб. 00 коп.",
            "state_duty": "1 277 руб. 00 коп.",
            "total_amount": "79 749 руб. 87 коп.",
        }
    )
    retry = {
        "debt_amount": "78 742 руб. 00 коп.",
        "debt_amount_fragment": "задолженность 78 742 руб. 00 коп.",
        "state_duty": "1 277 руб. 00 коп.",
        "state_duty_fragment": "расходы по оплате государственной пошлины 1 277 руб. 00 коп.",
        "total_amount": "79 749 руб. 87 коп.",
        "total_amount_fragment": "всего к взысканию 79 749 руб. 87 коп.",
        "confidence": 0.88,
        "comment": "debt misread",
    }
    recovery = recover_amounts_from_mismatch(primary, retry)
    assert recovery.applied
    assert recovery.recovery_method == "total_minus_state_duty"
    assert recovery.new_debt_amount == "78 472 руб. 87 коп."
    assert validate_amounts(recovery.order_data).ok


def test_recovery_does_not_change_when_amounts_consistent():
    primary = normalize_order_data(
        {
            "debt_amount": "78 472 руб. 87 коп.",
            "state_duty": "1 277 руб. 00 коп.",
            "total_amount": "79 749 руб. 87 коп.",
        }
    )
    recovery = recover_amounts_from_mismatch(primary, None)
    assert not recovery.applied
    assert recovery.reason == "amounts_already_consistent"


def test_unrecoverable_amounts_go_needs_review():
    primary = normalize_order_data(
        {
            "debt_amount": "78 472 руб. 00 коп.",
            "state_duty": "1 277 руб. 00 коп.",
            "total_amount": "79 749 руб. 87 коп.",
        }
    )
    retry = {
        "debt_amount": "78 472 руб. 00 коп.",
        "debt_amount_fragment": "задолженность 78 472 руб. 87 коп.",
        "state_duty": "1 277 руб. 00 коп.",
        "state_duty_fragment": "госпошлина 1 277 руб. 00 коп.",
        "total_amount": "79 749 руб. 87 коп.",
        "total_amount_fragment": "итого",
        "confidence": 0.4,
        "comment": "low confidence",
    }
    recovery = recover_amounts_from_mismatch(primary, retry)
    assert not recovery.applied


def test_user_not_blocked_when_amounts_recovered():
    primary = normalize_order_data(
        {
            "debt_amount": "78 472 руб. 00 коп.",
            "state_duty": "1 277 руб. 00 коп.",
            "total_amount": "79 749 руб. 87 коп.",
        }
    )
    retry = {
        "debt_amount": "78 472 руб. 87 коп.",
        "debt_amount_fragment": "задолженность 78 472 руб. 87 коп.",
        "state_duty": "1 277 руб. 00 коп.",
        "state_duty_fragment": "госпошлина 1 277 руб. 00 коп.",
        "total_amount": "79 749 руб. 87 коп.",
        "total_amount_fragment": "всего к взысканию 79 749 руб. 87 коп.",
        "confidence": 0.9,
        "comment": "",
    }
    recovery = recover_amounts_from_mismatch(primary, retry)
    assert recovery.applied
    assert validate_amounts(recovery.order_data).ok


def test_admin_message_contains_raw_retry_and_recovery_details():
    primary = normalize_order_data(
        {
            "debt_amount": "78 472 руб. 00 коп.",
            "state_duty": "1 277 руб. 00 коп.",
            "total_amount": "79 749 руб. 87 коп.",
        }
    )
    check = validate_amounts(primary)
    retry = {
        "debt_amount": "78 472 руб. 87 коп.",
        "debt_amount_fragment": "задолженность 78 472 руб. 87 коп.",
        "state_duty": "1 277 руб. 00 коп.",
        "state_duty_fragment": "госпошлина 1 277 руб. 00 коп.",
        "total_amount": "79 749 руб. 87 коп.",
        "total_amount_fragment": "всего к взысканию 79 749 руб. 87 коп.",
        "confidence": 0.9,
        "comment": "",
    }
    recovery = recover_amounts_from_mismatch(primary, retry)
    report = format_amount_mismatch_admin_report(25, primary, retry, check, recovery)
    assert "Первичное распознавание" in report
    assert "Повторное распознавание сумм" in report
    assert "Фрагмент" in report
    assert "Расхождение" in report
    assert "78 472 руб. 87 коп." in report


@pytest.mark.asyncio
async def test_resolve_amount_mismatch_integration():
    from app.handlers.case_flow import _resolve_amount_mismatch
    from app.models import Case, User

    case = Case(id=99, user_id=1, platform="telegram", status="processing", order_photo_path="storage/test.jpg")
    user = User(id=1, platform="telegram", platform_user_id="1")
    data = normalize_order_data(
        {
            "debt_amount": "78 472 руб. 00 коп.",
            "state_duty": "1 277 руб. 00 коп.",
            "total_amount": "79 749 руб. 87 коп.",
        }
    )
    retry = {
        "debt_amount": "78 472 руб. 87 коп.",
        "debt_amount_fragment": "задолженность 78 472 руб. 87 коп.",
        "state_duty": "1 277 руб. 00 коп.",
        "state_duty_fragment": "госпошлина 1 277 руб. 00 коп.",
        "total_amount": "79 749 руб. 87 коп.",
        "total_amount_fragment": "всего к взысканию 79 749 руб. 87 коп.",
        "confidence": 0.95,
        "comment": "",
    }
    settings = get_settings()
    with patch("app.handlers.case_flow.extract_order_amounts", new=AsyncMock(return_value=retry)):
        updated, check, recovery, _ = await _resolve_amount_mismatch(
            settings, None, case, user, data, force_retry=True
        )
    assert check.ok
    assert recovery and recovery.applied
