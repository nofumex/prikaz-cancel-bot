from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base

settings = get_settings()

if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.rsplit("///", 1)[-1]
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _sqlite_columns(conn, table_name: str) -> set[str]:
    result = await conn.exec_driver_sql(f"PRAGMA table_info({table_name})")
    rows = result.fetchall()
    return {row[1] for row in rows}


async def _sqlite_add_columns(conn, table_name: str, columns: list[tuple[str, str]]) -> set[str]:
    existing = await _sqlite_columns(conn, table_name)
    added: set[str] = set()
    for column_name, ddl in columns:
        if column_name not in existing:
            await conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")
            added.add(column_name)
    return added


async def _sqlite_upgrade_crm_notification_cycle_unique(conn) -> None:
    result = await conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'crm_deal_notifications'"
    )
    row = result.first()
    table_sql = "".join(str(row[0] if row else "").lower().split())
    if "unique(amocrm_deal_id,notification_type,cycle)" in table_sql:
        return
    await conn.exec_driver_sql(
        "ALTER TABLE crm_deal_notifications RENAME TO crm_deal_notifications_legacy_cycle"
    )
    await conn.exec_driver_sql(
        """
        CREATE TABLE crm_deal_notifications (
            id INTEGER PRIMARY KEY,
            amocrm_deal_id BIGINT NOT NULL,
            notification_type VARCHAR(64) NOT NULL,
            cycle INTEGER NOT NULL DEFAULT 1,
            user_id INTEGER NOT NULL REFERENCES users(id),
            case_id INTEGER NOT NULL REFERENCES cases(id),
            lawyer_name VARCHAR(255) NOT NULL,
            lawyer_phone VARCHAR(64) NOT NULL,
            message_text TEXT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            claimed_at DATETIME,
            lease_until DATETIME,
            sent_at DATETIME,
            uncertain_at DATETIME,
            error_message TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT uq_crm_deal_notification_cycle
                UNIQUE (amocrm_deal_id, notification_type, cycle)
        )
        """
    )
    await conn.exec_driver_sql(
        """
        INSERT INTO crm_deal_notifications (
            id, amocrm_deal_id, notification_type, cycle, user_id, case_id,
            lawyer_name, lawyer_phone, message_text, status, attempts,
            claimed_at, lease_until, sent_at, uncertain_at, error_message, created_at
        )
        SELECT
            id, amocrm_deal_id, notification_type, cycle, user_id, case_id,
            lawyer_name, lawyer_phone, message_text, status, attempts,
            claimed_at, lease_until, sent_at, uncertain_at, error_message, created_at
        FROM crm_deal_notifications_legacy_cycle
        """
    )
    await conn.exec_driver_sql("DROP TABLE crm_deal_notifications_legacy_cycle")
    for index_sql in (
        "CREATE INDEX ix_crm_deal_notifications_amocrm_deal_id ON crm_deal_notifications (amocrm_deal_id)",
        "CREATE INDEX ix_crm_deal_notifications_notification_type ON crm_deal_notifications (notification_type)",
        "CREATE INDEX ix_crm_deal_notifications_user_id ON crm_deal_notifications (user_id)",
        "CREATE INDEX ix_crm_deal_notifications_case_id ON crm_deal_notifications (case_id)",
        "CREATE INDEX ix_crm_deal_notifications_status ON crm_deal_notifications (status)",
        "CREATE INDEX ix_crm_deal_notifications_lease_until ON crm_deal_notifications (lease_until)",
    ):
        await conn.exec_driver_sql(index_sql)


async def _upgrade_sqlite_schema(conn) -> None:
    added_case_columns = await _sqlite_add_columns(
        conn,
        "cases",
        [
            ("platform_chat_id", "platform_chat_id TEXT"),
            ("platform_user_id", "platform_user_id TEXT"),
            ("order_photo_uploaded_at", "order_photo_uploaded_at DATETIME"),
            ("full_pdf_path", "full_pdf_path TEXT"),
            ("preview_pdf_path", "preview_pdf_path TEXT"),
            ("amocrm_contact_id", "amocrm_contact_id INTEGER"),
            ("amocrm_lead_id", "amocrm_lead_id INTEGER"),
            ("amocrm_pipeline_id", "amocrm_pipeline_id INTEGER"),
            ("amocrm_status_id", "amocrm_status_id INTEGER"),
            ("amocrm_status_name", "amocrm_status_name TEXT"),
            ("amocrm_last_sync_at", "amocrm_last_sync_at DATETIME"),
            ("amocrm_sync_error", "amocrm_sync_error TEXT"),
            ("amocrm_synced", "amocrm_synced BOOLEAN DEFAULT 0"),
            ("order_rephoto_attempts", "order_rephoto_attempts INTEGER NOT NULL DEFAULT 0"),
            ("deadline_reminder_sent_at", "deadline_reminder_sent_at DATETIME"),
            ("post_payment_followup_sent_at", "post_payment_followup_sent_at DATETIME"),
            ("consultation_reminder_sent_at", "consultation_reminder_sent_at DATETIME"),
            ("reminder_delivery_blocked_at", "reminder_delivery_blocked_at DATETIME"),
            ("reminder_delivery_error", "reminder_delivery_error TEXT"),
            ("paid_regeneration_count", "paid_regeneration_count INTEGER NOT NULL DEFAULT 0"),
            ("paid_corrected_fields_json", "paid_corrected_fields_json TEXT"),
            ("paid_corrections_json", "paid_corrections_json TEXT"),
        ],
    )
    await _sqlite_add_columns(
        conn,
        "payments",
        [
            ("provider", "provider TEXT NOT NULL DEFAULT 'yoomoney'"),
            ("external_payment_id", "external_payment_id TEXT"),
            ("confirmation_url", "confirmation_url TEXT"),
            ("refunded_at", "refunded_at DATETIME"),
        ],
    )
    added_user_columns = await _sqlite_add_columns(
        conn,
        "users",
        [
            ("amocrm_contact_id", "amocrm_contact_id INTEGER"),
            ("amocrm_current_case_id", "amocrm_current_case_id INTEGER"),
            ("telegram_username", "telegram_username TEXT"),
            ("email", "email TEXT"),
            ("first_deadline_reminder_sent_at", "first_deadline_reminder_sent_at DATETIME"),
            ("first_consultation_reminder_sent_at", "first_consultation_reminder_sent_at DATETIME"),
            ("reminder_delivery_blocked_at", "reminder_delivery_blocked_at DATETIME"),
            ("reminder_delivery_error", "reminder_delivery_error TEXT"),
            ("inactivity_offer_sent_at", "inactivity_offer_sent_at DATETIME"),
            ("inactivity_offer_dismissed_at", "inactivity_offer_dismissed_at DATETIME"),
        ],
    )
    if "deadline_reminder_sent_at" in added_case_columns:
        await conn.exec_driver_sql(
            "UPDATE cases SET deadline_reminder_sent_at = CURRENT_TIMESTAMP WHERE deadline_reminder_sent_at IS NULL"
        )
    if "post_payment_followup_sent_at" in added_case_columns:
        await conn.exec_driver_sql(
            "UPDATE cases SET post_payment_followup_sent_at = CURRENT_TIMESTAMP WHERE post_payment_followup_sent_at IS NULL AND paid_at IS NOT NULL"
        )
    if "consultation_reminder_sent_at" in added_case_columns:
        await conn.exec_driver_sql(
            "UPDATE cases SET consultation_reminder_sent_at = CURRENT_TIMESTAMP WHERE consultation_reminder_sent_at IS NULL"
        )
    if "first_deadline_reminder_sent_at" in added_user_columns:
        await conn.exec_driver_sql(
            "UPDATE users SET first_deadline_reminder_sent_at = CURRENT_TIMESTAMP WHERE first_deadline_reminder_sent_at IS NULL"
        )
    if "first_consultation_reminder_sent_at" in added_user_columns:
        await conn.exec_driver_sql(
            "UPDATE users SET first_consultation_reminder_sent_at = CURRENT_TIMESTAMP WHERE first_consultation_reminder_sent_at IS NULL"
        )

    # One-time rollout cutoff: users that existed before this feature must not
    # receive an inactivity offer immediately after deployment.
    await conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    rollout = await conn.exec_driver_sql("SELECT 1 FROM app_migrations WHERE name = 'inactivity_offer_rollout_v1'")
    if rollout.fetchone() is None:
        await conn.exec_driver_sql("UPDATE users SET inactivity_offer_sent_at = CURRENT_TIMESTAMP WHERE inactivity_offer_sent_at IS NULL")
        await conn.exec_driver_sql("INSERT INTO app_migrations(name) VALUES ('inactivity_offer_rollout_v1')")

    await _sqlite_add_columns(
        conn,
        "chat_sessions",
        [
            ("case_id", "case_id INTEGER"),
            ("inactivity_notification_refs", "inactivity_notification_refs TEXT"),
        ],
    )
    if "order_photo_uploaded_at" in added_case_columns:
        await conn.exec_driver_sql(
            "UPDATE cases SET order_photo_uploaded_at = created_at "
            "WHERE order_photo_path IS NOT NULL AND order_photo_path != ''"
        )
    await _sqlite_add_columns(
        conn,
        "chat_messages",
        [
            ("attachment_path", "attachment_path TEXT"),
            ("attachment_name", "attachment_name VARCHAR(512)"),
            ("attachment_type", "attachment_type VARCHAR(64)"),
        ],
    )

    await conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS openai_usages (
            id INTEGER PRIMARY KEY,
            case_id INTEGER,
            user_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            provider VARCHAR(32) NOT NULL DEFAULT 'openai',
            endpoint VARCHAR(32) NOT NULL DEFAULT 'responses',
            operation VARCHAR(64) NOT NULL,
            model VARCHAR(255),
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            image_tokens INTEGER,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            input_cost_usd FLOAT NOT NULL DEFAULT 0.0,
            cached_input_cost_usd FLOAT NOT NULL DEFAULT 0.0,
            output_cost_usd FLOAT NOT NULL DEFAULT 0.0,
            total_cost_usd FLOAT NOT NULL DEFAULT 0.0,
            request_id VARCHAR(255),
            raw_usage_json TEXT,
            raw_response_model VARCHAR(255),
            success BOOLEAN NOT NULL DEFAULT 1,
            error_message TEXT,
            latency_ms INTEGER
        )
        """
    )
    await conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS crm_sync_logs (
            id INTEGER PRIMARY KEY,
            case_id INTEGER,
            user_id INTEGER,
            event_type VARCHAR(64) NOT NULL,
            dedupe_key VARCHAR(512),
            amo_entity_type VARCHAR(32),
            amo_entity_id INTEGER,
            request_payload TEXT,
            response_payload TEXT,
            success BOOLEAN NOT NULL DEFAULT 0,
            error_message TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )


    await _sqlite_add_columns(
        conn,
        "crm_sync_logs",
        [
            ("dedupe_key", "dedupe_key VARCHAR(512)"),
        ],
    )

    await _sqlite_add_columns(
        conn,
        "mailing_jobs",
        [
            ("lease_until", "lease_until DATETIME"),
            ("uncertain_at", "uncertain_at DATETIME"),
        ],
    )
    added_mailing_state_columns = await _sqlite_add_columns(
        conn,
        "mailing_states",
        [
            ("consultation_cycle", "consultation_cycle INTEGER NOT NULL DEFAULT 0"),
            ("consultation_state", "consultation_state VARCHAR(24) NOT NULL DEFAULT 'ready'"),
        ],
    )
    if "consultation_state" in added_mailing_state_columns:
        await conn.exec_driver_sql(
            """
            UPDATE mailing_states
            SET consultation_state = CASE
                WHEN awaiting_phone = 1 THEN 'awaiting_phone'
                WHEN consultation_completed = 1 AND consultation_no = 0 THEN 'completed'
                ELSE 'ready'
            END
            """
        )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_mailing_states_consultation_state ON mailing_states (consultation_state)"
    )
    await _sqlite_add_columns(
        conn,
        "mailing_actions",
        [
            ("note_text", "note_text TEXT"),
            ("payload_json", "payload_json TEXT"),
            ("status", "status VARCHAR(16) NOT NULL DEFAULT 'completed'"),
            ("attempts", "attempts INTEGER NOT NULL DEFAULT 0"),
            ("lease_until", "lease_until DATETIME"),
            ("completed_at", "completed_at DATETIME"),
            ("error_message", "error_message TEXT"),
        ],
    )
    await _sqlite_add_columns(
        conn,
        "crm_deal_notifications",
        [
            ("cycle", "cycle INTEGER NOT NULL DEFAULT 1"),
        ],
    )
    await _sqlite_upgrade_crm_notification_cycle_unique(conn)
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_mailing_jobs_lease_until ON mailing_jobs (lease_until)"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_mailing_actions_status ON mailing_actions (status)"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_mailing_actions_lease_until ON mailing_actions (lease_until)"
    )
    await conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS crm_mailing_cursors (
            name VARCHAR(64) PRIMARY KEY,
            cursor_at BIGINT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    await conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS crm_mailing_changes (
            event_id VARCHAR(64) PRIMARY KEY,
            amocrm_deal_id BIGINT NOT NULL,
            changed_at BIGINT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_until DATETIME,
            claim_token VARCHAR(64),
            processed_at DATETIME,
            error_message TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_crm_mailing_changes_deal ON crm_mailing_changes (amocrm_deal_id)"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_crm_mailing_changes_status ON crm_mailing_changes (status)"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_crm_mailing_changes_lease ON crm_mailing_changes (lease_until)"
    )
    await _sqlite_add_columns(
        conn,
        "crm_mailing_changes",
        [("claim_token", "claim_token VARCHAR(64)")],
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_crm_mailing_changes_claim ON crm_mailing_changes (claim_token)"
    )

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            await _upgrade_sqlite_schema(conn)


async def close_db() -> None:
    await engine.dispose()
