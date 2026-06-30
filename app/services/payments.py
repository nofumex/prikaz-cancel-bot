from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.enums import CaseStatus, PaymentStatus
from app.models import Case, Payment
from app.services.cases import new_payment_label
from app.services.yookassa import YooKassaClient, YooKassaReceiptContactRequired, raw_json
from app.utils import normalize_receipt_contact


def build_yoomoney_url(settings: Settings, label: str, amount: int, target: str) -> str:
    if not settings.yoomoney_receiver:
        public = settings.payment_public_base_url or "https://example.com"
        return f"{public}/pay/manual?label={quote(label)}&sum={amount}"
    params = {
        "receiver": settings.yoomoney_receiver,
        "label": label,
        "quickpay-form": "button",
        "paymentType": "AC",
        "sum": f"{amount:.2f}",
        "targets": target[:150],
    }
    if settings.yoomoney_success_url:
        params["successURL"] = settings.yoomoney_success_url
    return "https://yoomoney.ru/quickpay/confirm?" + urlencode(params)


async def ensure_payment(session: AsyncSession, case: Case, settings: Settings) -> Payment:
    result = await session.execute(
        select(Payment)
        .where(Payment.case_id == case.id, Payment.status != PaymentStatus.CANCELED.value)
        .order_by(Payment.id.desc())
        .limit(1)
    )
    payment = result.scalar_one_or_none()
    is_new_payment = payment is None
    if is_new_payment:
        label = new_payment_label(case.id)
        payment = Payment(case_id=case.id, label=label, amount=settings.document_price_rub)
        case.payment_label = label
    else:
        case.payment_label = payment.label

    if settings.yookassa_enabled:
        if not payment.external_payment_id or not payment.confirmation_url or payment.provider != "yookassa":
            receipt_contact = _resolve_yookassa_receipt_contact(case, settings)
            if settings.yookassa_receipt_enabled and not receipt_contact:
                raise YooKassaReceiptContactRequired("Receipt contact is required for YooKassa payments")
            payment.provider = "yookassa"
            response = await YooKassaClient(settings).create_payment(case, payment, receipt_contact=receipt_contact)
            if is_new_payment:
                session.add(payment)
            _apply_yookassa_payment_response(payment, response)
        case.payment_url = payment.confirmation_url
    else:
        if is_new_payment:
            session.add(payment)
        payment.provider = payment.provider or "yoomoney"
        case.payment_url = build_yoomoney_url(
            settings,
            payment.label,
            payment.amount,
            f"Возражения относительно исполнения судебного приказа #{case.id}",
        )
    case.status = CaseStatus.PAYMENT_PENDING.value
    await session.commit()
    await session.refresh(payment)
    return payment


def _resolve_yookassa_receipt_contact(case: Case, settings: Settings) -> str | None:
    preferred = settings.yookassa_test_customer_email or None
    if preferred:
        return preferred
    user = getattr(case, "user", None)
    raw_contact = getattr(user, "email", None) or getattr(user, "phone", None)
    normalized = normalize_receipt_contact(raw_contact)
    return normalized[1] if normalized else None


def _apply_yookassa_payment_response(payment: Payment, response: dict) -> None:
    payment.provider = "yookassa"
    payment.external_payment_id = response.get("id") or payment.external_payment_id
    yookassa_status = str(response.get("status") or payment.status or PaymentStatus.PENDING.value)
    if yookassa_status == "succeeded":
        payment.status = PaymentStatus.PAID.value
    elif yookassa_status == "canceled":
        payment.status = PaymentStatus.CANCELED.value
    else:
        payment.status = PaymentStatus.PENDING.value
    confirmation = response.get("confirmation") if isinstance(response.get("confirmation"), dict) else {}
    payment.confirmation_url = confirmation.get("confirmation_url") or payment.confirmation_url
    payment.raw_notification = raw_json(response)


async def refresh_yookassa_payment(session: AsyncSession, payment: Payment, settings: Settings) -> Case | None:
    if payment.provider != "yookassa" or not payment.external_payment_id:
        return None
    response = await YooKassaClient(settings).get_payment(payment.external_payment_id)
    _apply_yookassa_payment_response(payment, response)
    case = await session.get(Case, payment.case_id)
    if case:
        case.payment_url = payment.confirmation_url or case.payment_url
        if payment.status == PaymentStatus.PAID.value:
            payment.paid_at = payment.paid_at or datetime.utcnow()
            case.status = CaseStatus.PAID.value
            case.paid_at = case.paid_at or payment.paid_at
        elif payment.status == PaymentStatus.CANCELED.value and case.status == CaseStatus.PAYMENT_PENDING.value:
            case.status = CaseStatus.CANCELED.value
    await session.commit()
    if case:
        await session.refresh(case)
    return case


async def refresh_yookassa_payment_for_case(session: AsyncSession, case: Case, settings: Settings) -> Case | None:
    result = await session.execute(
        select(Payment)
        .where(Payment.case_id == case.id, Payment.provider == "yookassa")
        .order_by(Payment.id.desc())
        .limit(1)
    )
    payment = result.scalar_one_or_none()
    if not payment:
        return None
    return await refresh_yookassa_payment(session, payment, settings)


def verify_yoomoney_sign(form: dict[str, str], secret: str | None) -> bool:
    if not secret:
        return True
    sign = form.get("sign")
    if not sign:
        return False
    pieces = []
    for key in sorted(k for k in form if k != "sign"):
        pieces.append(f"{key}={quote(str(form.get(key, '')), safe='')}")
    digest = hmac.new(secret.encode("utf-8"), "&".join(pieces).encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, sign)


async def mark_paid_by_label(session: AsyncSession, label: str, raw: dict | None = None) -> Case | None:
    result = await session.execute(select(Payment).where(Payment.label == label))
    payment = result.scalar_one_or_none()
    if not payment:
        return None
    return await _mark_payment_paid(session, payment, raw or {})


async def mark_paid_by_external_payment_id(session: AsyncSession, external_payment_id: str, raw: dict | None = None) -> tuple[Case | None, bool]:
    result = await session.execute(select(Payment).where(Payment.external_payment_id == external_payment_id))
    payment = result.scalar_one_or_none()
    if not payment:
        return None, False
    first_time = payment.status != PaymentStatus.PAID.value
    case = await _mark_payment_paid(session, payment, raw or {})
    return case, first_time


async def mark_yookassa_canceled(session: AsyncSession, external_payment_id: str, raw: dict | None = None) -> Case | None:
    result = await session.execute(select(Payment).where(Payment.external_payment_id == external_payment_id))
    payment = result.scalar_one_or_none()
    if not payment:
        return None
    payment.status = PaymentStatus.CANCELED.value
    payment.raw_notification = raw_json(raw or {})
    case = await session.get(Case, payment.case_id)
    if case and case.status == CaseStatus.PAYMENT_PENDING.value:
        case.status = CaseStatus.CANCELED.value
    await session.commit()
    if case:
        await session.refresh(case)
    return case


async def _mark_payment_paid(session: AsyncSession, payment: Payment, raw: dict | None = None) -> Case | None:
    paid_at = payment.paid_at or datetime.utcnow()
    payment.status = PaymentStatus.PAID.value
    payment.operation_id = (raw or {}).get("operation_id") or (raw or {}).get("id") or payment.operation_id
    payment.raw_notification = raw_json(raw or {})
    payment.paid_at = paid_at
    case = await session.get(Case, payment.case_id)
    if case:
        if not case.delivered_at:
            case.status = CaseStatus.PAID.value
        case.paid_at = case.paid_at or paid_at
    await session.commit()
    if case:
        await session.refresh(case)
    return case
