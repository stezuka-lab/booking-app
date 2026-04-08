"""SQLAlchemy 非同期 DB: オンライン予約（booking）のみ。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateTable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


# booking テーブルを Base に登録
import app.auth.models  # noqa: E402, F401
import app.booking.db_models  # noqa: E402, F401

_engine = None


SCHEMA_DRIFT_COLUMNS: dict[str, list[tuple[str, str, str]]] = {
    "booking_orgs": [
        ("auto_confirm", '"auto_confirm" BOOLEAN NOT NULL DEFAULT 0', '"auto_confirm" BOOLEAN NOT NULL DEFAULT FALSE'),
        ("ga4_measurement_id", '"ga4_measurement_id" VARCHAR(64)', '"ga4_measurement_id" VARCHAR(64)'),
        ("email_settings_json", '"email_settings_json" TEXT', '"email_settings_json" JSON'),
    ],
    "booking_staff": [
        ("line_user_id", '"line_user_id" VARCHAR(256)', '"line_user_id" VARCHAR(256)'),
        ("google_profile_email", '"google_profile_email" VARCHAR(320)', '"google_profile_email" VARCHAR(320)'),
        ("google_profile_name", '"google_profile_name" VARCHAR(256)', '"google_profile_name" VARCHAR(256)'),
        ("zoom_meeting_url", '"zoom_meeting_url" TEXT', '"zoom_meeting_url" TEXT'),
        ("skill_tags", '"skill_tags" VARCHAR(1024) NOT NULL DEFAULT \'\'', '"skill_tags" VARCHAR(1024) NOT NULL DEFAULT \'\''),
    ],
    "booking_public_links": [
        ("service_id", '"service_id" INTEGER', '"service_id" INTEGER'),
        ("active", '"active" BOOLEAN NOT NULL DEFAULT 1', '"active" BOOLEAN NOT NULL DEFAULT TRUE'),
        ("block_next_days", '"block_next_days" INTEGER NOT NULL DEFAULT 0', '"block_next_days" INTEGER NOT NULL DEFAULT 0'),
        ("staff_priority_overrides_json", '"staff_priority_overrides_json" TEXT', '"staff_priority_overrides_json" JSON'),
        ("buffer_minutes", '"buffer_minutes" INTEGER', '"buffer_minutes" INTEGER'),
        ("max_advance_booking_days", '"max_advance_booking_days" INTEGER', '"max_advance_booking_days" INTEGER'),
        ("bookable_until_date", '"bookable_until_date" VARCHAR(10)', '"bookable_until_date" VARCHAR(10)'),
        ("pre_booking_notice", '"pre_booking_notice" TEXT', '"pre_booking_notice" TEXT'),
        ("post_booking_message", '"post_booking_message" TEXT', '"post_booking_message" TEXT'),
    ],
    "bookings": [
        ("customer_reminder_sent_at", '"customer_reminder_sent_at" DATETIME', '"customer_reminder_sent_at" TIMESTAMP WITH TIME ZONE'),
        ("staff_reminder_sent_at", '"staff_reminder_sent_at" DATETIME', '"staff_reminder_sent_at" TIMESTAMP WITH TIME ZONE'),
        ("last_outreach_at", '"last_outreach_at" DATETIME', '"last_outreach_at" TIMESTAMP WITH TIME ZONE'),
        ("customer_reminder_1h_sent_at", '"customer_reminder_1h_sent_at" DATETIME', '"customer_reminder_1h_sent_at" TIMESTAMP WITH TIME ZONE'),
        ("google_calendar_synced_at", '"google_calendar_synced_at" DATETIME', '"google_calendar_synced_at" TIMESTAMP WITH TIME ZONE'),
        ("google_calendar_sync_error", '"google_calendar_sync_error" TEXT', '"google_calendar_sync_error" TEXT'),
        ("staff_reminder_1h_sent_at", '"staff_reminder_1h_sent_at" DATETIME', '"staff_reminder_1h_sent_at" TIMESTAMP WITH TIME ZONE'),
        ("booking_link_title_snapshot", '"booking_link_title_snapshot" VARCHAR(256)', '"booking_link_title_snapshot" VARCHAR(256)'),
        ("customer_confirmation_email_last_attempt_at", '"customer_confirmation_email_last_attempt_at" DATETIME', '"customer_confirmation_email_last_attempt_at" TIMESTAMP WITH TIME ZONE'),
        ("customer_confirmation_email_sent_at", '"customer_confirmation_email_sent_at" DATETIME', '"customer_confirmation_email_sent_at" TIMESTAMP WITH TIME ZONE'),
        ("customer_confirmation_email_error", '"customer_confirmation_email_error" TEXT', '"customer_confirmation_email_error" TEXT'),
        ("staff_notification_email_last_attempt_at", '"staff_notification_email_last_attempt_at" DATETIME', '"staff_notification_email_last_attempt_at" TIMESTAMP WITH TIME ZONE'),
        ("staff_notification_email_sent_at", '"staff_notification_email_sent_at" DATETIME', '"staff_notification_email_sent_at" TIMESTAMP WITH TIME ZONE'),
        ("staff_notification_email_error", '"staff_notification_email_error" TEXT', '"staff_notification_email_error" TEXT'),
        ("company_name", '"company_name" VARCHAR(256)', '"company_name" VARCHAR(256)'),
        ("calendar_title_note", '"calendar_title_note" TEXT', '"calendar_title_note" TEXT'),
        ("staff_display_name", '"staff_display_name" VARCHAR(256)', '"staff_display_name" VARCHAR(256)'),
    ],
    "booking_customers": [
        ("repeat_outreach_sent_at", '"repeat_outreach_sent_at" DATETIME', '"repeat_outreach_sent_at" TIMESTAMP WITH TIME ZONE'),
    ],
    "booking_app_users": [
        ("default_org_slug", '"default_org_slug" VARCHAR(128)', '"default_org_slug" VARCHAR(128)'),
    ],
}


def _normalize_database_url(raw_url: str) -> tuple[str, dict[str, Any]]:
    connect_args: dict[str, Any] = {}
    url = (raw_url or "").strip()
    if not url.startswith("postgresql+asyncpg://"):
        return url, connect_args
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept: list[tuple[str, str]] = []
    sslmode = ""
    for key, value in pairs:
        key_l = key.strip().lower()
        if key_l == "sslmode":
            sslmode = (value or "").strip().lower()
            continue
        if key_l == "channel_binding":
            continue
        kept.append((key, value))
    if sslmode in {"require", "prefer", "verify-ca", "verify-full"}:
        connect_args["ssl"] = True
    normalized = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )
    return normalized, connect_args


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        database_url, connect_args = _normalize_database_url(settings.database_url)
        _engine = create_async_engine(
            database_url,
            echo=False,
            connect_args=connect_args,
        )
    return _engine


async def _ensure_sqlite_column(
    conn: Any,
    table: str,
    column: str,
    ddl: str,
) -> None:
    r = await conn.execute(text(f'PRAGMA table_info("{table}")'))
    names = {row[1] for row in r.fetchall()}
    if column not in names:
        await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {ddl}'))


async def _sqlite_add_missing_columns() -> None:
    """既存 SQLite に後から増えたカラムを追加（スキーマドリフト対策）。"""
    engine = _get_engine()
    if not str(engine.url).startswith("sqlite"):
        return
    async with engine.begin() as conn:
        for table, columns in SCHEMA_DRIFT_COLUMNS.items():
            for column, sqlite_ddl, _postgres_ddl in columns:
                await _ensure_sqlite_column(conn, table, column, sqlite_ddl)


def _postgres_add_missing_columns_sync(sync_conn: Any) -> None:
    inspector = inspect(sync_conn)
    for table, columns in SCHEMA_DRIFT_COLUMNS.items():
        existing = {col["name"] for col in inspector.get_columns(table)}
        for column, _sqlite_ddl, postgres_ddl in columns:
            if column in existing:
                continue
            sync_conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {postgres_ddl}'))


async def _postgres_add_missing_columns() -> None:
    engine = _get_engine()
    if not str(engine.url).startswith("postgresql"):
        return
    async with engine.begin() as conn:
        await conn.run_sync(_postgres_add_missing_columns_sync)


def _sqlite_rebuild_bookings_nullable_staff_sync(connection: Any) -> None:
    """staff_id を NULL 可・ON DELETE SET NULL にしたテーブルへ移行（既存 SQLite）。"""
    from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

    from app.booking.db_models import Booking

    r = connection.execute(text('PRAGMA table_info("bookings")'))
    rows = r.fetchall()
    staff_row = next((x for x in rows if x[1] == "staff_id"), None)
    if staff_row is None:
        return
    if staff_row[3] == 0:
        return
    connection.execute(text("PRAGMA foreign_keys=OFF"))
    try:
        connection.execute(
            text(
                "UPDATE bookings SET staff_display_name = (SELECT name FROM booking_staff "
                "WHERE booking_staff.id = bookings.staff_id) "
                "WHERE staff_id IS NOT NULL AND (staff_display_name IS NULL OR staff_display_name = '')"
            )
        )
        ddl = str(CreateTable(Booking.__table__).compile(dialect=sqlite_dialect()))
        ddl = ddl.replace("CREATE TABLE bookings ", "CREATE TABLE bookings__new ", 1)
        connection.execute(text("DROP TABLE IF EXISTS bookings__new"))
        connection.execute(text(ddl))
        r2 = connection.execute(text('PRAGMA table_info("bookings")'))
        old_cols = [row[1] for row in r2.fetchall()]
        r3 = connection.execute(text('PRAGMA table_info("bookings__new")'))
        new_cols = [row[1] for row in r3.fetchall()]
        common = [c for c in new_cols if c in old_cols]
        cols_sql = ", ".join(f'"{c}"' for c in common)
        connection.execute(text(f'INSERT INTO bookings__new ({cols_sql}) SELECT {cols_sql} FROM bookings'))
        connection.execute(text("DROP TABLE bookings"))
        connection.execute(text('ALTER TABLE bookings__new RENAME TO bookings'))
    finally:
        connection.execute(text("PRAGMA foreign_keys=ON"))


async def _sqlite_migrate_bookings_nullable_staff(engine: Any) -> None:
    if not str(engine.url).startswith("sqlite"):
        return
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: _sqlite_rebuild_bookings_nullable_staff_sync(sync_conn))


async def init_db() -> None:
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _sqlite_add_missing_columns()
    await _postgres_add_missing_columns()
    await _sqlite_migrate_bookings_nullable_staff(_get_engine())


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        _get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )
