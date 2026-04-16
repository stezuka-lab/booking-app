from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.db_models import Booking, BookingOrg, BookingService, StaffMember
from app.booking.calendar_google import get_calendar_event_status
from app.booking.routing_service import _load_google_busy_map
from app.config import Settings, get_settings
from app.db import get_session_factory

logger = logging.getLogger(__name__)

_booking_scheduler: AsyncIOScheduler | None = None


async def run_booking_reminders_and_crm() -> None:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        try:
            await _retry_customer_confirmation_emails(session, settings)
            await _retry_staff_calendar_syncs(session, settings)
            await _send_reminders(session, settings)
            await _repeat_outreach(session, settings)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _retry_customer_confirmation_emails(session: AsyncSession, settings: Settings) -> None:
    return


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
        _interval_overlaps_any,
        _staff_google_refresh_token,
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
    min_age = timedelta(minutes=10)

    for b in rows:
        org = org_map.get(b.org_id)
        staff = staff_map.get(int(b.staff_id or 0))
        if not org or not staff:
            continue
        if staff.id in google_busy_errors:
            continue
        if (b.google_event_id or "").strip():
            exists, err = await get_calendar_event_status(
                _staff_google_refresh_token(staff, settings),
                staff.google_calendar_id,
                b.google_event_id,
                settings,
            )
            if exists is False:
                b.status = "cancelled"
                b.cancelled_at = now
                b.google_event_id = None
                b.google_calendar_synced_at = None
                b.google_calendar_sync_error = "Googleカレンダー上で予定が削除されたため自動で解放しました"
            elif err:
                b.google_calendar_sync_error = err[:500]
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
