from __future__ import annotations

from enum import StrEnum


class CaseStatus(StrEnum):
    DRAFT = "draft"
    WAITING_ORDER_PHOTO = "waiting_order_photo"
    WAITING_ENVELOPE = "waiting_envelope"
    WAITING_RECEIVED_DATE = "waiting_received_date"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    PREVIEW_READY = "preview_ready"
    PAYMENT_PENDING = "payment_pending"
    PAID = "paid"
    DELIVERED = "delivered"
    CANCELED = "canceled"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    CANCELED = "canceled"


class ChatStatus(StrEnum):
    OPEN = "open"
    ACTIVE = "active"
    CLOSED = "closed"
