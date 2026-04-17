from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone as dt_timezone
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.booking.calendar_title import format_calendar_event_title
from app.booking.db_models import Booking, BookingOrg, StaffMember
from app.booking.routing_service import availability_zone
from app.config import Settings
from app.security.crypto import decrypt_secret

logger = logging.getLogger(__name__)

EMAIL_DISABLED_REASON = "disabled_by_settings"

DEFAULT_EMAIL_SETTINGS: dict[str, Any] = {
    "send_customer_confirmation": True,
    "send_staff_notification": True,
    "confirmation_intro_ja": "",
    "confirmation_footer_ja": "",
}


def booking_meeting_url_value(booking: Booking, settings: Settings) -> str:
    return (decrypt_secret(getattr(booking, "meeting_url", None), settings) or "").strip()


def booking_customer_name_value(booking: Booking, settings: Settings) -> str:
    return (decrypt_secret(getattr(booking, "customer_name", None), settings) or "").strip()


def booking_customer_email_value(booking: Booking, settings: Settings) -> str:
    return (decrypt_secret(getattr(booking, "customer_email", None), settings) or "").strip()


def merge_email_settings(raw: Any) -> dict[str, Any]:
    out = dict(DEFAULT_EMAIL_SETTINGS)
    if isinstance(raw, dict):
        for k in DEFAULT_EMAIL_SETTINGS:
            if k in raw:
                out[k] = raw[k]
    return out


def google_calendar_template_url(
    title: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    details: str = "",
    location: str = "",
) -> str:
    """ブラウザで Google カレンダーに追加するテンプレート URL（ログイン不要で利用可）。"""

    def _fmt(dt: datetime) -> str:
        u = dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)
        u = u.astimezone(dt_timezone.utc)
        return u.strftime("%Y%m%dT%H%M%SZ")

    dates = f"{_fmt(start_utc)}/{_fmt(end_utc)}"
    q = {
        "action": "TEMPLATE",
        "text": title[:1024],
        "dates": dates,
        "details": (details or "")[:5000],
        "location": (location or "")[:2000],
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(q)


_WD_JA = "月火水木金土日"


def format_booking_datetime_range_ja(org: BookingOrg, start: datetime, end: datetime) -> str:
    defaults = org.availability_defaults_json or {}
    tz = availability_zone(defaults)
    s = start if start.tzinfo else start.replace(tzinfo=ZoneInfo("UTC"))
    e = end if end.tzinfo else end.replace(tzinfo=ZoneInfo("UTC"))
    sl = s.astimezone(tz)
    el = e.astimezone(tz)
    wd = _WD_JA[sl.weekday()]
    tz_label = getattr(tz, "key", None) or str(tz)
    return (
        f"{sl.year}年{sl.month}月{sl.day}日（{wd}） "
        f"{sl.hour:02d}:{sl.minute:02d} — {el.hour:02d}:{el.minute:02d}（{tz_label}）"
    )


def meeting_kind_label_ja(booking: Booking) -> str:
    mp = (booking.meeting_provider or "none").lower()
    if mp == "meet":
        return "Google Meet（オンライン）"
    if mp == "zoom":
        return "Zoom（オンライン）"
    if mp == "teams":
        return "Microsoft Teams（オンライン）"
    return "会場・方法は別途ご案内します（オンライン URL なし）"


def build_booking_confirmation_email_body(
    settings: Settings,
    org: BookingOrg,
    booking: Booking,
    staff: StaffMember,
    booking_link_title: str,
    *,
    manage_url: str,
    email_settings: dict[str, Any],
    post_booking_message: str = "",
) -> tuple[str, str]:
    """件名と本文（プレーンテキスト）。"""
    em = merge_email_settings(email_settings)
    when = format_booking_datetime_range_ja(org, booking.start_utc, booking.end_utc)
    meet_url = booking_meeting_url_value(booking, settings)
    link_title = (booking_link_title or "").strip() or "予約"

    lines: list[str] = []
    intro = (em.get("confirmation_intro_ja") or "").strip()
    if intro:
        lines.append(intro)
        lines.append("")
    lines.append("ご予約ありがとうございます。")
    lines.append(f"予約リンク: {link_title}")
    lines.append(f"日時: {when}")
    if meet_url:
        lines.append(f"Zoom URL: {meet_url}")
    lines.append("予約内容の確認・変更はこちら:")
    lines.append(manage_url)
    extra = (post_booking_message or "").strip()
    if extra:
        lines.append("")
        lines.append(extra)
    footer = (em.get("confirmation_footer_ja") or "").strip()
    if footer:
        lines.append("")
        lines.append(footer)
    lines.append("")
    lines.append("—")
    lines.append((org.name or "").strip() or "予約システム")
    body = "\n".join(lines)
    subj = f"[予約確認] {link_title}"
    return subj, body


def build_staff_notification_email_body(
    settings: Settings,
    org: BookingOrg,
    booking: Booking,
    booking_link_title: str,
    *,
    manage_url: str,
    post_booking_message: str = "",
) -> tuple[str, str]:
    link_title = (booking_link_title or "").strip() or "予約"
    meet_url = booking_meeting_url_value(booking, settings)
    customer_name = booking_customer_name_value(booking, settings)
    customer_email = booking_customer_email_value(booking, settings)
    staff_lines = [
        f"新規予約: {customer_name} <{customer_email}>",
        f"予約リンク: {link_title}",
        f"日時: {format_booking_datetime_range_ja(org, booking.start_utc, booking.end_utc)}",
    ]
    if meet_url:
        staff_lines.append(f"Zoom URL: {meet_url}")
    staff_lines.append(f"確認・変更: {manage_url}")
    extra = (post_booking_message or "").strip()
    if extra:
        staff_lines.append("")
        staff_lines.append(extra)
    staff_body = "\n".join(staff_lines)
    staff_subject = f"[予約通知] {customer_name} {link_title}"
    return staff_subject, staff_body


def _send_sync(settings: Settings, to_addrs: list[str], subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = ", ".join(to_addrs)
    if settings.smtp_use_ssl:
        smtp_ctx = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port)
    else:
        smtp_ctx = smtplib.SMTP(settings.smtp_host, settings.smtp_port)
    with smtp_ctx as smtp:
        smtp.ehlo()
        if settings.smtp_starttls and not settings.smtp_use_ssl:
            if not smtp.has_extn("starttls"):
                raise RuntimeError("SMTP server does not support STARTTLS")
            smtp.starttls()
            smtp.ehlo()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(msg["From"], to_addrs, msg.as_string())


def _error_text(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]


async def send_customer_confirmation_email(
    settings: Settings,
    org: BookingOrg,
    booking: Booking,
    staff: StaffMember,
    *,
    booking_link_title: str,
    manage_url: str,
    post_booking_message: str = "",
    dry_run: bool,
) -> tuple[bool, str | None]:
    if not settings.smtp_host:
        return False, "SMTP not configured"
    em = merge_email_settings(getattr(org, "email_settings_json", None))
    if not bool(em.get("send_customer_confirmation", True)):
        return False, EMAIL_DISABLED_REASON
    subject, body = build_booking_confirmation_email_body(
        settings,
        org,
        booking,
        staff,
        booking_link_title,
        manage_url=manage_url,
        email_settings=em,
        post_booking_message=post_booking_message,
    )
    if dry_run:
        logger.info("DRY-RUN customer booking email booking_id=%s", getattr(booking, "id", None))
        return True, None
    import asyncio

    try:
        await asyncio.to_thread(
            _send_sync,
            settings,
            [booking_customer_email_value(booking, settings)],
            subject,
            body,
        )
        return True, None
    except Exception as exc:
        logger.exception("Customer email failed")
        return False, _error_text(exc)


async def send_staff_notification_email(
    settings: Settings,
    org: BookingOrg,
    booking: Booking,
    staff: StaffMember,
    *,
    booking_link_title: str,
    manage_url: str,
    post_booking_message: str = "",
    dry_run: bool,
) -> tuple[bool, str | None]:
    if not settings.smtp_host:
        return False, "SMTP not configured"
    em = merge_email_settings(getattr(org, "email_settings_json", None))
    if not bool(em.get("send_staff_notification", True)) or not staff.email:
        return False, EMAIL_DISABLED_REASON
    subject, body = build_staff_notification_email_body(
        settings,
        org,
        booking,
        booking_link_title,
        manage_url=manage_url,
        post_booking_message=post_booking_message,
    )
    if dry_run:
        logger.info("DRY-RUN staff booking email booking_id=%s", getattr(booking, "id", None))
        return True, None
    import asyncio

    try:
        await asyncio.to_thread(
            _send_sync,
            settings,
            [staff.email],
            subject,
            body,
        )
        return True, None
    except Exception as exc:
        logger.exception("Staff email failed")
        return False, _error_text(exc)


async def send_booking_emails(
    settings: Settings,
    org: BookingOrg,
    booking: Booking,
    staff: StaffMember,
    service_name: str,
    *,
    booking_link_title: str,
    manage_url: str,
    post_booking_message: str = "",
    dry_run: bool,
) -> dict[str, bool]:
    """顧客へ確定情報、担当へ通知。組織の email_settings_json で送信可否を切り替え。"""
    if not settings.smtp_host:
        logger.warning("SMTP not configured; skip booking emails")
        return {"customer": False, "staff": False}

    ok_c, err_c = await send_customer_confirmation_email(
        settings,
        org,
        booking,
        staff,
        booking_link_title=booking_link_title,
        manage_url=manage_url,
        post_booking_message=post_booking_message,
        dry_run=dry_run,
    )
    ok_s, err_s = await send_staff_notification_email(
        settings,
        org,
        booking,
        staff,
        booking_link_title=booking_link_title,
        manage_url=manage_url,
        post_booking_message=post_booking_message,
        dry_run=dry_run,
    )
    return {
        "customer": ok_c,
        "staff": ok_s,
        "customer_error": err_c,
        "staff_error": err_s,
    }


async def send_simple_mail(settings: Settings, to_addrs: list[str], subject: str, body: str, *, dry_run: bool) -> bool:
    if not settings.smtp_host or not to_addrs:
        return False
    if dry_run:
        logger.info("DRY-RUN mail to=%s subject=%s", to_addrs, subject)
        return True
    import asyncio

    try:
        await asyncio.to_thread(_send_sync, settings, to_addrs, subject, body)
        return True
    except Exception:
        logger.exception("send_simple_mail failed")
        return False
