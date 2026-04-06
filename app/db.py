"""SQLAlchemy 非同期 DB: オンライン予約（booking）のみ。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import Text, text
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


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
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
        await _ensure_sqlite_column(
            conn,
            "booking_orgs",
            "auto_confirm",
            '"auto_confirm" BOOLEAN NOT NULL DEFAULT 0',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_orgs",
            "ga4_measurement_id",
            '"ga4_measurement_id" VARCHAR(64)',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_orgs",
            "email_settings_json",
            '"email_settings_json" TEXT',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_staff",
            "line_user_id",
            '"line_user_id" VARCHAR(256)',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "customer_reminder_sent_at",
            '"customer_reminder_sent_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "staff_reminder_sent_at",
            '"staff_reminder_sent_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "last_outreach_at",
            '"last_outreach_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_customers",
            "repeat_outreach_sent_at",
            '"repeat_outreach_sent_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "customer_reminder_1h_sent_at",
            '"customer_reminder_1h_sent_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "staff_reminder_1h_sent_at",
            '"staff_reminder_1h_sent_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "booking_link_title_snapshot",
            '"booking_link_title_snapshot" VARCHAR(256)',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "customer_confirmation_email_last_attempt_at",
            '"customer_confirmation_email_last_attempt_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "customer_confirmation_email_sent_at",
            '"customer_confirmation_email_sent_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "customer_confirmation_email_error",
            '"customer_confirmation_email_error" TEXT',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "staff_notification_email_last_attempt_at",
            '"staff_notification_email_last_attempt_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "staff_notification_email_sent_at",
            '"staff_notification_email_sent_at" DATETIME',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "staff_notification_email_error",
            '"staff_notification_email_error" TEXT',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_staff",
            "google_profile_email",
            '"google_profile_email" VARCHAR(320)',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_staff",
            "google_profile_name",
            '"google_profile_name" VARCHAR(256)',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_staff",
            "zoom_meeting_url",
            '"zoom_meeting_url" TEXT',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_staff",
            "skill_tags",
            '"skill_tags" VARCHAR(1024) NOT NULL DEFAULT \'\'',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_public_links",
            "service_id",
            '"service_id" INTEGER',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_public_links",
            "active",
            '"active" BOOLEAN NOT NULL DEFAULT 1',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_public_links",
            "block_next_days",
            '"block_next_days" INTEGER NOT NULL DEFAULT 0',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_public_links",
            "staff_priority_overrides_json",
            '"staff_priority_overrides_json" TEXT',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_public_links",
            "buffer_minutes",
            '"buffer_minutes" INTEGER',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_public_links",
            "max_advance_booking_days",
            '"max_advance_booking_days" INTEGER',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_public_links",
            "bookable_until_date",
            '"bookable_until_date" VARCHAR(10)',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_public_links",
            "pre_booking_notice",
            '"pre_booking_notice" TEXT',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_public_links",
            "post_booking_message",
            '"post_booking_message" TEXT',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "company_name",
            '"company_name" VARCHAR(256)',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "calendar_title_note",
            '"calendar_title_note" TEXT',
        )
        await _ensure_sqlite_column(
            conn,
            "bookings",
            "staff_display_name",
            '"staff_display_name" VARCHAR(256)',
        )
        await _ensure_sqlite_column(
            conn,
            "booking_app_users",
            "default_org_slug",
            '"default_org_slug" VARCHAR(128)',
        )


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
    await _sqlite_migrate_bookings_nullable_staff(_get_engine())


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        _get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )
