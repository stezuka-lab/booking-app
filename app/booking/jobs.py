from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.db_models import Booking, BookingOrg, BookingService, PublicBookingLink, StaffMember
from app.booking.email_booking import (
    EMAIL_DISABLED_REASON,
    send_customer_confirmation_email,
    send_staff_notification_email,
)
from app.booking.routing_service import _load_google_busy_map
from app.config import Settings, get_settings
from app.db import get_session_factory

logger = logging.getLogger(__name__)

_booking_scheduler: AsyncIOScheduler | None = None
_booking_job_lock = asyncio.Lock()
_BOOKING_JOB_ADVISORY_LOCK_KEY = 709280719184424603


async def _try_pg_booking_job_lock(session: AsyncSession, settings: Settings) -> bool:
    if not str(settings.database_url or "").strip().startswith("postgresql"):
        return True
    got = await session.scalar(
        text("SELECT pg_try_advisory_lock(:lock_key)"),
        {"lock_key": _BOOKING_JOB_ADVISORY_LOCK_KEY},
    )
    return bool(got)


async def _release_pg_booking_job_lock(session: AsyncSession, settings: Settings) -> None:
    if not str(settings.database_url or "").strip().startswith("postgresql"):
        return
    await session.execute(
        text("SELECT pg_advisory_unlock(:lock_key)"),
        {"lock_key": _BOOKING_JOB_ADVISORY_LOCK_KEY},
    )


async def run_booking_reminders_and_crm() -> dict[str, object]:
    settings = get_settings()
    if _booking_job_lock.locked():
        logger.info("Booking jobs skipped: already running in this process")
        return {"ok": True, "skipped": True, "reason": "already_running"}
    started = time.monotonic()
    async with _booking_job_lock:
        return await _run_booking_jobs_locked(settings, started)


async def _run_booking_jobs_locked(settings: Settings, started: float) -> dict[str, object]:
    factory = get_session_factory()
    async with factory() as session:
        pg_lock_acquired = False
        try:
            pg_lock_acquired = await _try_pg_booking_job_lock(session, settings)
            if not pg_lock_acquired:
                logger.info("Booking jobs skipped: already running in another process")
                return {"ok": True, "skipped": True, "reason": "already_running"}
            await _retry_customer_confirmation_emails(session, settings)
            await _retry_staff_calendar_syncs(session, settings)
            await _send_reminders(session, settings)
            await _repeat_outreach(session, settings)
            await session.commit()
            return {
                "ok": True,
                "skipped": False,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
        except Exception:
            await session.rollback()
            raise
        finally:
            if pg_lock_acquired:
                await _release_pg_booking_job_lock(session, settings)


async def _retry_customer_confirmation_emails(session: AsyncSession, settings: Settings) -> None:
    if not settings.smtp_host:
        return
    now = datetime.now(timezone.utc)
    retry_before = now - timedelta(minutes=15)
    q = await session.scalars(
        select(Booking)
        .where(
            Booking.status == "confirmed",
            Booking.start_utc >= now - timedelta(days=1),
            (
                (Booking.customer_confirmation_email_sent_at.is_(None))
                | (Booking.staff_notification_email_sent_at.is_(None))
            ),
            (
                (Booking.customer_confirmation_email_last_attempt_at.is_(None))
                | (Booking.customer_confirmation_email_last_attempt_at <= retry_before)
                | (Booking.staff_notification_email_last_attempt_at.is_(None))
                | (Booking.staff_notification_email_last_attempt_at <= retry_before)
            ),
        )
        .order_by(Booking.created_at.asc())
        .limit(50)
    )
    rows = list(q.all())
    for b in rows:
        org = await session.get(BookingOrg, b.org_id)
        staff = await session.get(StaffMember, b.staff_id) if b.staff_id else None
        if not org or not staff:
            continue
        link = await session.get(PublicBookingLink, b.public_link_id) if b.public_link_id else None
        link_title = (b.booking_link_title_snapshot or "").strip() or (
            (link.title or "").strip() if link else "予約"
        )
        manage_url = f"{settings.public_base_url_value().rstrip('/')}/app/manage/{b.manage_token}"
        post_booking_message = (getattr(link, "post_booking_message", None) or "").strip() if link else ""
        if b.customer_confirmation_email_sent_at is None:
            b.customer_confirmation_email_last_attempt_at = now
            ok, err = await send_customer_confirmation_email(
                settings,
                org,
                b,
                staff,
                booking_link_title=link_title,
                manage_url=manage_url,
                post_booking_message=post_booking_message,
                dry_run=bool(settings.actions_dry_run),
            )
            if ok:
                b.customer_confirmation_email_sent_at = now
                b.customer_confirmation_email_error = None
            elif err != EMAIL_DISABLED_REASON:
                b.customer_confirmation_email_error = (err or "send failed")[:500]
        if b.staff_notification_email_sent_at is None:
            b.staff_notification_email_last_attempt_at = now
            ok, err = await send_staff_notification_email(
                settings,
                org,
                b,
                staff,
                booking_link_title=link_title,
                manage_url=manage_url,
                post_booking_message=post_booking_message,
                dry_run=bool(settings.actions_dry_run),
            )
            if ok:
                b.staff_notification_email_sent_at = now
                b.staff_notification_email_error = None
            elif err != EMAIL_DISABLED_REASON:
                b.staff_notification_email_error = (err or "send failed")[:500]


async def _retry_staff_calendar_syncs(session: AsyncSession, settings: Settings) -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=1)
    q = await session.scalars(
        select(Booking).where(
            Booking.status == "confirmed",
            Booking.start_utc >= cutoff,
        )
    )
    from app.booking.router import (
        _clear_public_availability_cache_for_org,
        _interval_overlaps_any,
        _release_bookings_with_missing_google_events,
        _sync_booking_to_staff_calendar,
    )
    rows = list(q.all())
    if not rows:
        return
    staff_map: dict[int, StaffMember] = {}
    org_map: dict[int, BookingOrg] = {}
    for b in rows:
        if b.org_id not in org_map:
            org = await session.get(BookingOrg, b.org_id)
            if org:
                org_map[b.org_id] = org
        if b.staff_id is not None and b.staff_id not in staff_map:
            staff = await session.get(StaffMember, b.staff_id)
            if staff:
                staff_map[staff.id] = staff
    if not staff_map:
        return
    window_start = min((b.start_utc for b in rows if b.start_utc), default=now)
    window_end = max((b.end_utc for b in rows if b.end_utc), default=now) + timedelta(days=1)
    google_busy_map, google_busy_errors = await _load_google_busy_map(
        list(staff_map.values()),
        window_start,
        window_end,
        settings,
    )
    released_google_deleted = await _release_bookings_with_missing_google_events(
        session,
        settings,
        list(staff_map.values()),
        window_start,
        window_end,
    )
    if released_google_deleted:
        for org_id in set(org_map):
            await _clear_public_availability_cache_for_org(session, int(org_id))
    min_age = timedelta(minutes=10)

    for b in rows:
        if b.status != "confirmed":
            continue
        org = org_map.get(b.org_id)
        staff = staff_map.get(int(b.staff_id or 0))
        if not org or not staff:
            continue
        if staff.id in google_busy_errors:
            continue
        if (b.google_event_id or "").strip():
            continue
        created_at = getattr(b, "created_at", None)
        created_at_utc = (
            created_at if created_at and created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
            if created_at
            else None
        )
        if (
            getattr(org, "auto_confirm", False)
            and created_at_utc
            and (now - created_at_utc) >= min_age
            and not b.google_calendar_synced_at
        ):
            current_busy = google_busy_map.get(staff.id) or []
            start_utc = b.start_utc if b.start_utc.tzinfo else b.start_utc.replace(tzinfo=timezone.utc)
            end_utc = b.end_utc if b.end_utc.tzinfo else b.end_utc.replace(tzinfo=timezone.utc)
            if not _interval_overlaps_any(start_utc, end_utc, current_busy):
                b.status = "cancelled"
                b.cancelled_at = now
                b.google_calendar_synced_at = None
                b.google_calendar_sync_error = (
                    (b.google_calendar_sync_error or "").strip()
                    or "Googleカレンダーに反映されていない古い予約を自動で解放しました"
                )
                continue
        svc = await session.get(BookingService, b.service_id) if b.service_id else None
        link_title = (b.booking_link_title_snapshot or "").strip() or (svc.name if svc else "予約")
        service_name = svc.name if svc else "予約"
        await _sync_booking_to_staff_calendar(
            session,
            settings,
            b,
            staff,
            org,
            service_name=service_name,
            booking_link_title=link_title,
        )


async def _send_reminders(session: AsyncSession, settings: Settings) -> None:
    return


async def _repeat_outreach(session: AsyncSession, settings: Settings) -> None:
    return


def setup_booking_scheduler() -> AsyncIOScheduler | None:
    global _booking_scheduler
    settings = get_settings()
    cron = (settings.booking_jobs_cron or "").strip()
    if not cron or cron.lower() in ("-", "none", "off"):
        return None
    parts = cron.split()
    if len(parts) != 5:
        logger.warning("Invalid BOOKING_JOBS_CRON; booking scheduler disabled")
        return None
    minute, hour, day, month, dow = parts
    _booking_scheduler = AsyncIOScheduler()
    _booking_scheduler.add_job(
        run_booking_reminders_and_crm,
        CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow),
        id="booking_reminders_crm",
        replace_existing=True,
    )
    _booking_scheduler.start()
    logger.info("Booking scheduler started: %s", cron)
    return _booking_scheduler


def shutdown_booking_scheduler() -> None:
    global _booking_scheduler
    if _booking_scheduler:
        _booking_scheduler.shutdown(wait=False)
        _booking_scheduler = None
