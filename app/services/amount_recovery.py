from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from decimal import Decimal
import re
from typing import Any

from app.services.legal_data import format_money_rub_kop, money_to_decimal, normalize_order_data, validate_amounts


@dataclass
class AmountRecoveryResult:
    applied: bool = False
    recovery_method: str | None = None
    old_debt_amount: str | None = None
    new_debt_amount: str | None = None
    old_state_duty: str | None = None
    new_state_duty: str | None = None
    old_total_amount: str | None = None
    new_total_amount: str | None = None
    reason: str | None = None
    note: str | None = None
    order_data: dict[str, Any] = field(default_factory=dict)
    qa_report: dict[str, Any] = field(default_factory=dict)


_TOTAL_FRAGMENT_KEYWORDS = ("всего", "итого", "всего к взысканию")
_DUTY_FRAGMENT_KEYWORDS = ("пошлин", "госпошлин")
_DEBT_FRAGMENT_KEYWORDS = ("задолженност", "сумма долга", "сумму долга", "долг")


def _fragment_matches(fragment: str, keywords: tuple[str, ...]) -> bool:
    lower = (fragment or "").lower()
    return any(keyword in lower for keyword in keywords)


def _fragment_supports_amount(fragment: str, amount: Any, keywords: tuple[str, ...]) -> bool:
    if not _fragment_matches(fragment, keywords):
        return False
    expected_digits = "".join(re.findall(r"\d+", str(amount or "")))
    fragment_digits = "".join(re.findall(r"\d+", fragment or ""))
    if expected_digits and expected_digits in fragment_digits:
        return True
    # A quote may legally state whole rubles without printing "00 коп.".
    return bool(expected_digits.endswith("00") and expected_digits[:-2] in fragment_digits)


def _retry_amounts_consistent(retry_amounts: dict[str, Any] | None) -> bool:
    if not retry_amounts:
        return False
    check = validate_amounts(
        {
            "debt_amount": retry_amounts.get("debt_amount"),
            "state_duty": retry_amounts.get("state_duty"),
            "total_amount": retry_amounts.get("total_amount"),
        }
    )
    return check.ok


def _retry_has_role_evidence(retry_amounts: dict[str, Any] | None) -> bool:
    if not retry_amounts:
        return False
    debt_and_duty_supported = (
        _fragment_supports_amount(
            str(retry_amounts.get("debt_amount_fragment") or ""), retry_amounts.get("debt_amount"), _DEBT_FRAGMENT_KEYWORDS
        )
        and _fragment_supports_amount(
            str(retry_amounts.get("state_duty_fragment") or ""), retry_amounts.get("state_duty"), _DUTY_FRAGMENT_KEYWORDS
        )
    )
    total_value = str(retry_amounts.get("total_amount") or "").strip()
    total_fragment = str(retry_amounts.get("total_amount_fragment") or "").strip()
    total_supported_or_absent = (
        (not total_value and not total_fragment)
        or _fragment_supports_amount(
            str(retry_amounts.get("total_amount_fragment") or ""), retry_amounts.get("total_amount"), _TOTAL_FRAGMENT_KEYWORDS
        )
    )
    return debt_and_duty_supported and total_supported_or_absent


def recover_amounts_from_mismatch(
    order_data: dict[str, Any],
    retry_amounts: dict[str, Any] | None,
    *,
    min_confidence: float = 0.75,
    auto_recover: bool = True,
) -> AmountRecoveryResult:
    normalized = normalize_order_data(order_data)
    primary_check = validate_amounts(normalized)
    result = AmountRecoveryResult(
        order_data=dict(normalized),
        old_debt_amount=normalized.get("debt_amount"),
        old_state_duty=normalized.get("state_duty"),
        old_total_amount=normalized.get("total_amount"),
    )

    if primary_check.ok:
        result.reason = "amounts_already_consistent"
        return result

    if not auto_recover:
        result.reason = "auto_recover_disabled"
        return result

    # min_confidence is retained only for call-site compatibility. Model
    # self-confidence is deliberately not used to accept legal facts.
    del min_confidence

    if retry_amounts and _retry_amounts_consistent(retry_amounts) and _retry_has_role_evidence(retry_amounts):
        updated = dict(normalized)
        updated["debt_amount"] = retry_amounts.get("debt_amount", "")
        updated["state_duty"] = retry_amounts.get("state_duty", "")
        updated["total_amount"] = retry_amounts.get("total_amount", "")
        updated = normalize_order_data(updated)
        result.applied = True
        result.recovery_method = "amounts_recovered_by_retry"
        result.new_debt_amount = updated.get("debt_amount")
        result.new_state_duty = updated.get("state_duty")
        result.new_total_amount = updated.get("total_amount")
        result.note = "amounts_recovered_by_retry"
        result.reason = retry_amounts.get("comment") or "targeted amount OCR gave consistent sums"
        result.order_data = updated
        result.qa_report = _build_qa_report(result, primary_check, retry_amounts)
        return result

    if not retry_amounts or not _retry_has_role_evidence(retry_amounts):
        result.reason = "retry_source_fragments_missing_or_unproven"
        result.qa_report = _build_qa_report(result, primary_check, retry_amounts)
        return result

    total_text = str(retry_amounts.get("total_amount") or normalized.get("total_amount") or "")
    duty_text = str(retry_amounts.get("state_duty") or normalized.get("state_duty") or "")
    debt_text = str(retry_amounts.get("debt_amount") or normalized.get("debt_amount") or "")
    total_fragment = str(retry_amounts.get("total_amount_fragment") or "")
    duty_fragment = str(retry_amounts.get("state_duty_fragment") or "")
    debt_fragment = str(retry_amounts.get("debt_amount_fragment") or "")

    total = money_to_decimal(total_text)
    duty = money_to_decimal(duty_text)
    debt = money_to_decimal(debt_text)

    if total is None or duty is None:
        result.reason = "total_or_duty_unparseable"
        result.qa_report = _build_qa_report(result, primary_check, retry_amounts)
        return result

    if not _fragment_matches(total_fragment, _TOTAL_FRAGMENT_KEYWORDS):
        result.reason = "total_fragment_not_confirmed"
        result.qa_report = _build_qa_report(result, primary_check, retry_amounts)
        return result

    if not _fragment_matches(duty_fragment, _DUTY_FRAGMENT_KEYWORDS):
        result.reason = "state_duty_fragment_not_confirmed"
        result.qa_report = _build_qa_report(result, primary_check, retry_amounts)
        return result

    debt_candidate = (total - duty).quantize(Decimal("0.01"))
    if debt_candidate <= Decimal("0"):
        result.reason = "debt_candidate_not_positive"
        result.qa_report = _build_qa_report(result, primary_check, retry_amounts)
        return result

    debt_candidate_text = format_money_rub_kop(debt_candidate)
    explains_mismatch = primary_check.computed_total is not None and total is not None
    debt_fragment_ok = _fragment_matches(debt_fragment, _DEBT_FRAGMENT_KEYWORDS)
    debt_similar = False
    if debt is not None and debt != debt_candidate:
        debt_similar = abs(debt - debt_candidate) <= Decimal("1.00")
    elif debt_fragment_ok:
        debt_similar = True

    if not explains_mismatch and not debt_similar:
        result.reason = "debt_candidate_does_not_explain_mismatch"
        result.qa_report = _build_qa_report(result, primary_check, retry_amounts, debt_candidate_text)
        return result

    updated = dict(normalized)
    updated["debt_amount"] = debt_candidate_text
    updated["state_duty"] = duty_text
    updated["total_amount"] = total_text
    updated = normalize_order_data(updated)
    final_check = validate_amounts(updated)
    if not final_check.ok:
        result.reason = "recovery_still_inconsistent"
        result.qa_report = _build_qa_report(result, primary_check, retry_amounts, debt_candidate_text)
        return result

    result.applied = True
    result.recovery_method = "total_minus_state_duty"
    result.new_debt_amount = updated.get("debt_amount")
    result.new_state_duty = updated.get("state_duty")
    result.new_total_amount = updated.get("total_amount")
    result.note = "amounts_recovered_by_retry"
    result.reason = (
        f"debt corrected from {debt_text or 'unknown'} to {debt_candidate_text} "
        f"using total ({total_text}) minus state duty ({duty_text})"
    )
    result.order_data = updated
    result.qa_report = _build_qa_report(result, primary_check, retry_amounts, debt_candidate_text)
    return result


def _build_qa_report(
    recovery: AmountRecoveryResult,
    primary_check,
    retry_amounts: dict[str, Any] | None,
    debt_candidate: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "amount_recovery_applied": recovery.applied,
        "recovery_method": recovery.recovery_method,
        "old_debt_amount": recovery.old_debt_amount,
        "new_debt_amount": recovery.new_debt_amount,
        "old_state_duty": recovery.old_state_duty,
        "new_state_duty": recovery.new_state_duty,
        "old_total_amount": recovery.old_total_amount,
        "new_total_amount": recovery.new_total_amount,
        "reason": recovery.reason,
    }
    if retry_amounts:
        report["retry_amounts"] = {
            key: retry_amounts.get(key)
            for key in (
                "debt_amount",
                "debt_amount_fragment",
                "state_duty",
                "state_duty_fragment",
                "total_amount",
                "total_amount_fragment",
                "comment",
            )
        }
    if debt_candidate:
        report["debt_candidate"] = debt_candidate
    if primary_check.computed_total is not None and primary_check.total_amount is not None:
        report["primary_difference"] = str(abs(primary_check.total_amount - primary_check.computed_total))
    return report


def format_amount_mismatch_admin_report(
    case_id: int,
    primary: dict[str, Any],
    retry_amounts: dict[str, Any] | None,
    amount_check,
    recovery: AmountRecoveryResult | None = None,
) -> str:
    debt = primary.get("debt_amount") or "—"
    duty = primary.get("state_duty") or "—"
    total = primary.get("total_amount") or "—"
    computed = amount_check.computed_total
    computed_text = format_money_rub_kop(computed) if computed is not None else "—"
    diff_text = "—"
    if computed is not None and amount_check.total_amount is not None:
        diff_text = format_money_rub_kop(abs(amount_check.total_amount - computed))

    lines = [
        f"⚠️ Суммы требуют проверки по заявке #{case_id}",
        "",
        "Бот обнаружил арифметическое расхождение между распознанными суммами.",
        "",
        "Первичное распознавание:",
        f"Долг: {debt}",
        f"Госпошлина: {duty}",
        f"Итого: {total}",
    ]

    if retry_amounts:
        lines.extend(
            [
                "",
                "Повторное распознавание сумм:",
                f"Долг: {retry_amounts.get('debt_amount') or '—'}",
                f"Фрагмент: {retry_amounts.get('debt_amount_fragment') or '—'}",
                f"Госпошлина: {retry_amounts.get('state_duty') or '—'}",
                f"Фрагмент: {retry_amounts.get('state_duty_fragment') or '—'}",
                f"Итого: {retry_amounts.get('total_amount') or '—'}",
                f"Фрагмент: {retry_amounts.get('total_amount_fragment') or '—'}",
            ]
        )

    lines.extend(
        [
            "",
            "Расчет:",
            f"Долг + госпошлина = {computed_text}",
            "",
            f"Расхождение: {diff_text}",
        ]
    )

    if recovery and recovery.recovery_method == "total_minus_state_duty" and recovery.new_debt_amount:
        lines.extend(
            [
                "",
                "Подсказка:",
                f"если госпошлина и итог распознаны верно, долг может быть {recovery.new_debt_amount}",
            ]
        )
    elif retry_amounts and retry_amounts.get("total_amount") and retry_amounts.get("state_duty"):
        total_dec = money_to_decimal(retry_amounts.get("total_amount"))
        duty_dec = money_to_decimal(retry_amounts.get("state_duty"))
        if total_dec is not None and duty_dec is not None:
            candidate = format_money_rub_kop(total_dec - duty_dec)
            lines.extend(["", "Подсказка:", f"если госпошлина и итог распознаны верно, долг может быть {candidate}"])

    if recovery and recovery.applied:
        lines.extend(
            [
                "",
                f"✅ Автовосстановление: {recovery.recovery_method}",
                f"Новый долг: {recovery.new_debt_amount}",
            ]
        )

    return "\n".join(lines)


def save_amount_debug_snapshot(case_id: int, payload: dict[str, Any]) -> Path:
    from app.utils import ensure_dir
    import json

    debug_dir = ensure_dir(Path("storage/debug") / f"case_{case_id}")
    path = debug_dir / "amount_recovery.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
