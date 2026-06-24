from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.enums import CaseStatus, PaymentStatus
from app.models import Case, Payment
from app.services.cases import new_payment_label


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
    result = await session.execute(select(Payment).where(Payment.case_id == case.id))
    payment = result.scalar_one_or_none()
    if not payment:
        label = new_payment_label(case.id)
        payment = Payment(case_id=case.id, label=label, amount=settings.document_price_rub)
        session.add(payment)
        case.payment_label = label
    case.payment_url = build_yoomoney_url(settings, payment.label, payment.amount, f"Возражения относительно исполнения судебного приказа #{case.id}")
    case.status = CaseStatus.PAYMENT_PENDING.value
    await session.commit()
    await session.refresh(payment)
    return payment


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
    payment.status = PaymentStatus.PAID.value
    payment.operation_id = (raw or {}).get("operation_id") if raw else payment.operation_id
    payment.raw_notification = json.dumps(raw or {}, ensure_ascii=False)
    payment.paid_at = datetime.utcnow()
    case = await session.get(Case, payment.case_id)
    if case:
        case.status = CaseStatus.PAID.value
        case.paid_at = payment.paid_at
    await session.commit()
    if case:
        await session.refresh(case)
    return case
