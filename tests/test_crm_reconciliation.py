from datetime import date, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Case, CrmSyncLog, User
from app.services import crm_reconciliation
from app.services.amocrm import crm_event_dedupe_key


def test_reconciliation_interval_is_fifteen_minutes():
    assert crm_reconciliation.CRM_RECONCILIATION_INTERVAL_SECONDS == 900


@pytest.mark.asyncio
async def test_history_replay_skips_actions_already_synced_to_current_lead(monkeypatch, tmp_path):
    order = tmp_path / "order.jpg"
    envelope = tmp_path / "envelope.jpg"
    preview = tmp_path / "preview.pdf"
    full_doc = tmp_path / "statement.docx"
    for path in (order, envelope, preview, full_doc):
        path.write_bytes(b"file")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        async with session_factory() as session:
            user = User(platform="telegram", platform_user_id="100")
            session.add(user)
            await session.flush()
            case = Case(
                user_id=user.id,
                status="delivered",
                amocrm_lead_id=32404037,
                order_photo_path=str(order),
                envelope_photo_path=str(envelope),
                preview_pdf_path=str(preview),
                full_doc_path=str(full_doc),
                received_date=date(2026, 7, 1),
                deadline_date=date(2026, 7, 11),
                payment_label="PAY-1",
                paid_at=datetime(2026, 7, 2),
                delivered_at=datetime(2026, 7, 2),
                reminders_sent=1,
            )
            session.add(case)
            await session.flush()
            for event_type in (
                "user_started_bot",
                "order_photo_uploaded",
                "envelope_photo_uploaded",
                "received_date_entered",
                "preview_generated",
                "payment_created",
                "payment_paid",
                "documents_delivered",
                "reminder_sent",
            ):
                session.add(
                    CrmSyncLog(
                        case_id=case.id,
                        user_id=user.id,
                        event_type=event_type,
                        amo_entity_type="lead",
                        amo_entity_id=32404037,
                        success=True,
                    )
                )
            session.add(
                CrmSyncLog(
                    case_id=case.id,
                    user_id=user.id,
                    event_type="order_photo_uploaded",
                    dedupe_key="old-order-attempt",
                    amo_entity_type="lead",
                    amo_entity_id=32404037,
                    request_payload='{"payload":{"note":"first attempt"}}',
                    success=False,
                    error_message="temporary error",
                )
            )
            await session.commit()
            user_id = user.id
            case_id = case.id

        monkeypatch.setattr(crm_reconciliation, "SessionLocal", session_factory)

        assert await crm_reconciliation._history_jobs_for_user(user_id, case_id) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_replay_does_not_trust_success_from_another_lead(monkeypatch, tmp_path):
    order = tmp_path / "order.jpg"
    order.write_bytes(b"file")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        async with session_factory() as session:
            user = User(platform="telegram", platform_user_id="101")
            session.add(user)
            await session.flush()
            case = Case(
                user_id=user.id,
                status="waiting_envelope",
                amocrm_lead_id=222,
                order_photo_path=str(order),
            )
            session.add(case)
            await session.flush()
            session.add(
                CrmSyncLog(
                    case_id=case.id,
                    user_id=user.id,
                    event_type="order_photo_uploaded",
                    amo_entity_type="lead",
                    amo_entity_id=111,
                    success=True,
                )
            )
            await session.commit()
            user_id = user.id
            case_id = case.id

        monkeypatch.setattr(crm_reconciliation, "SessionLocal", session_factory)
        jobs = await crm_reconciliation._history_jobs_for_user(user_id, case_id)

        assert "case:%s:order_uploaded" % case_id in {source_key for _, source_key in jobs}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_history_replay_is_not_scheduled_again(monkeypatch, tmp_path):
    order = tmp_path / "order.jpg"
    order.write_bytes(b"file")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        async with session_factory() as session:
            user = User(platform="telegram", platform_user_id="102")
            session.add(user)
            await session.flush()
            case = Case(
                user_id=user.id,
                status="waiting_envelope",
                amocrm_lead_id=333,
                order_photo_path=str(order),
            )
            session.add(case)
            await session.flush()
            source_key = f"case:{case.id}:order_uploaded"
            payload = {"source_event_key": source_key, "note": "restored order"}
            session.add(
                CrmSyncLog(
                    case_id=case.id,
                    user_id=user.id,
                    event_type="history_replay",
                    dedupe_key=crm_event_dedupe_key(case.id, "history_replay", payload),
                    amo_entity_type="lead",
                    amo_entity_id=333,
                    success=True,
                )
            )
            await session.commit()
            user_id = user.id
            case_id = case.id

        monkeypatch.setattr(crm_reconciliation, "SessionLocal", session_factory)
        jobs = await crm_reconciliation._history_jobs_for_user(user_id, case_id)

        assert source_key not in {job_source_key for _, job_source_key in jobs}
    finally:
        await engine.dispose()
