from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.db_models import Booking, BookingOrg, BookingService, CustomerProfile, StaffMember
from app.booking.email_booking import (
    EMAIL_DISABLED_REASON,
    booking_customer_email_value,
    booking_customer_name_value,
    booking_meeting_url_value,
    send_customer_confirmation_email,
    send_simple_mail,
)
from app.booking.line_notify import send_line_push
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
            await _send_reminders(session, settings)
            await _repeat_outreach(session, settings)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _retry_customer_confirmation_emails(session: AsyncSession, settings: Settings) -> None:
    if not settings.smtp_host:
        return
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    q = await session.scalars(
        select(Booking).where(
            Booking.status == "confirmed",
            Booking.customer_confirmation_email_sent_at.is_(None),
            Booking.start_utc >= cutoff,
        )
    )
    for b in q.all():
        if (b.customer_confirmation_email_error or "").strip() == EMAIL_DISABLED_REASON:
            continue
        org = await session.get(BookingOrg, b.org_id)
        staff = await session.get(StaffMember, b.staff_id) if b.staff_id is not None else None
        if not org or not staff:
            continue
        svc = await session.get(BookingService, b.service_id) if b.service_id else None
        link_title = (b.booking_link_title_snapshot or "").strip() or (svc.name if svc else "予約")
        manage_url = f"{settings.public_base_url_value()}/app/manage/{b.manage_token}"
        ok, err = await send_customer_confirmation_email(
            settings,
            org,
            b,
            staff,
            booking_link_title=link_title,
            manage_url=manage_url,
            dry_run=settings.actions_dry_run,
        )
        b.customer_confirmation_email_last_attempt_at = now
        if ok:
            b.customer_confirmation_email_sent_at = now
            b.customer_confirmation_email_error = None
        elif err:
            b.customer_confirmation_email_error = str(err)[:500]


async def _send_reminders(session: AsyncSession, settings: Settings) -> None:
    now = datetime.now(timezone.utc)
    cust_first = timedelta(hours=settings.booking_reminder_hours_before)
    staff_first = timedelta(hours=settings.booking_staff_reminder_hours_before)
    second_h = int(getattr(settings, "booking_reminder_second_hours_before", 0) or 0)
    cust_second = timedelta(hours=second_h) if second_h > 0 else None

    q = await session.scalars(
        select(Booking).where(Booking.status == "confirmed")
    )
    for b in q.all():
        start = b.start_utc
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        customer_name = booking_customer_name_value(b, settings)
        customer_email = booking_customer_email_value(b, settings)
        staff = await session.get(StaffMember, b.staff_id) if b.staff_id is not None else None
        base = settings.public_base_url_value()
        url = f"{base}/app/manage/{b.manage_token}"

        if b.customer_reminder_sent_at is None and now >= start - cust_first and now < start:
            body = (
                f"予約のリマインドです（約{settings.booking_reminder_hours_before}時間前）。\n"
                f"{start.isoformat()}\n変更・キャンセル: {url}\n"
            )
            meeting_url = booking_meeting_url_value(b, settings)
            if meeting_url:
                body += f"会議 URL: {meeting_url}\n"
            sent = await send_simple_mail(
                settings,
                [customer_email],
                f"[リマインド] 予約 {start.date()}",
                body,
                dry_run=settings.actions_dry_run,
            )
            if sent:
                b.customer_reminder_sent_at = now

        if (
            cust_second is not None
            and b.customer_reminder_1h_sent_at is None
            and now >= start - cust_second
            and now < start
        ):
            body = f"もうすぐ予約です（約{second_h}時間前）。\n{start.isoformat()}\n{url}\n"
            sent = await send_simple_mail(
                settings,
                [customer_email],
                f"[リマインド·直前] 予約 {start.date()}",
                body,
                dry_run=settings.actions_dry_run,
            )
            if sent:
                b.customer_reminder_1h_sent_at = now

        if (
            staff is not None
            and staff.email
            and b.staff_reminder_sent_at is None
            and now >= start - staff_first
            and now < start
        ):
            sent = await send_simple_mail(
                settings,
                [staff.email],
                f"[担当リマインド] {customer_name} {start.date()}",
                f"{customer_name} / {customer_email}\n{start.isoformat()}\n{url}",
                dry_run=settings.actions_dry_run,
            )
            if sent:
                b.staff_reminder_sent_at = now

        if (
            cust_second is not None
            and staff is not None
            and staff.email
            and b.staff_reminder_1h_sent_at is None
            and now >= start - cust_second
            and now < start
        ):
            sent = await send_simple_mail(
                settings,
                [staff.email],
                f"[担当·直前] {customer_name} {start.date()}",
                f"約{second_h}時間後: {customer_name}\n{start.isoformat()}\n{url}",
                dry_run=settings.actions_dry_run,
            )
            if sent:
                b.staff_reminder_1h_sent_at = now
            if sent and (staff.line_user_id or "").strip():
                await send_line_push(
                    settings,
                    staff.line_user_id.strip(),
                    f"[リマインド·直前] {customer_name}\n{start.isoformat()}\n{url}",
                )


async def _repeat_outreach(session: AsyncSession, settings: Settings) -> None:
    days = max(1, settings.booking_repeat_outreach_days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = await session.scalars(select(CustomerProfile))
    for c in q.all():
        if not c.last_booking_utc or c.last_booking_utc > cutoff:
            continue
        if c.repeat_outreach_sent_at:
            continue
        sent = await send_simple_mail(
            settings,
            [c.email_normalized],
            "ご無沙汰しております（予約のご案内）",
            f"{c.display_name or 'お客様'}\n"
            f"前回ご予約から{days}日以上経過しました。またのご利用をお待ちしております。\n"
            f"{settings.public_base_url_value()}/booking/",
            dry_run=settings.actions_dry_run,
        )
        if sent:
            c.repeat_outreach_sent_at = datetime.now(timezone.utc)


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
