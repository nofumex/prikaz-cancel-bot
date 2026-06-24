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


async def _sqlite_add_columns(conn, table_name: str, columns: list[tuple[str, str]]) -> None:
    existing = await _sqlite_columns(conn, table_name)
    for column_name, ddl in columns:
        if column_name not in existing:
            await conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


async def _upgrade_sqlite_schema(conn) -> None:
    await _sqlite_add_columns(
        conn,
        "cases",
        [
            ("full_pdf_path", "full_pdf_path TEXT"),
            ("preview_pdf_path", "preview_pdf_path TEXT"),
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


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            await _upgrade_sqlite_schema(conn)


async def close_db() -> None:
    await engine.dispose()
