from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("platform", "platform_user_id", name="uq_users_platform_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), default="telegram", index=True, nullable=False)
    platform_user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), index=True)
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    amocrm_contact_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    amocrm_current_case_id: Mapped[int | None] = mapped_column(Integer, index=True)
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    admin_notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_deadline_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_consultation_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="telegram", index=True, nullable=False)
    platform_chat_id: Mapped[str | None] = mapped_column(String(255), index=True)
    platform_user_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True, nullable=False)
    order_photo_path: Mapped[str | None] = mapped_column(Text)
    envelope_photo_path: Mapped[str | None] = mapped_column(Text)
    received_date: Mapped[date | None] = mapped_column(Date)
    deadline_date: Mapped[date | None] = mapped_column(Date)
    extracted_json: Mapped[str | None] = mapped_column(Text)
    missing_fields: Mapped[str | None] = mapped_column(Text)
    full_doc_path: Mapped[str | None] = mapped_column(Text)
    full_pdf_path: Mapped[str | None] = mapped_column(Text)
    preview_pdf_path: Mapped[str | None] = mapped_column(Text)
    preview_doc_path: Mapped[str | None] = mapped_column(Text)
    instruction_path: Mapped[str | None] = mapped_column(Text)
    order_rephoto_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payment_label: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    payment_url: Mapped[str | None] = mapped_column(Text)
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime)
    deadline_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    post_payment_followup_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    consultation_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    amo_lead_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    amocrm_contact_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    amocrm_lead_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    amocrm_pipeline_id: Mapped[int | None] = mapped_column(BigInteger)
    amocrm_status_id: Mapped[int | None] = mapped_column(BigInteger)
    amocrm_status_name: Mapped[str | None] = mapped_column(String(255))
    amocrm_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    amocrm_sync_error: Mapped[str | None] = mapped_column(Text)
    amocrm_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    amo_sync_status: Mapped[str | None] = mapped_column(String(32))
    amo_sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(lazy="selectin")


class OpenAIUsage(Base):
    __tablename__ = "openai_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="openai", nullable=False)
    endpoint: Mapped[str] = mapped_column(String(32), default="responses", nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(255))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    image_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cached_input_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    output_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(255), index=True)
    raw_usage_json: Mapped[str | None] = mapped_column(Text)
    raw_response_model: Mapped[str | None] = mapped_column(String(255))
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class CrmSyncLog(Base):
    __tablename__ = "crm_sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(512), index=True)
    amo_entity_type: Mapped[str | None] = mapped_column(String(32))
    amo_entity_id: Mapped[int | None] = mapped_column(BigInteger)
    request_payload: Mapped[str | None] = mapped_column(Text)
    response_payload: Mapped[str | None] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="yoomoney", index=True, nullable=False)
    external_payment_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    confirmation_url: Mapped[str | None] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(String(255), index=True)
    raw_notification: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime)

    case: Mapped[Case] = relationship(lazy="selectin")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="selectin")
    manager: Mapped[User | None] = relationship(foreign_keys=[manager_id], lazy="selectin")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True, nullable=False)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    sender_role: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class UserState(Base):
    __tablename__ = "user_states"
    __table_args__ = (UniqueConstraint("platform", "platform_user_id", name="uq_user_states_platform_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    platform_user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    state: Mapped[str | None] = mapped_column(String(255))
    data_json: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
