from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
import time as time_module
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import ensure_booking_admin, get_current_app_user
from app.booking.calendar_policy import day_is_blocked_for_booking
from app.booking.calendar_title import format_calendar_event_title
from app.booking.calendar_google import (
    create_event_for_booking,
    create_event_for_booking_detailed,
    delete_event_for_booking,
    get_calendar_event_status,
    insert_customer_primary_calendar_with_access_token,
    patch_event_for_booking,
    verify_calendar_write_access_detailed,
)
from app.booking.db_models import (
    Booking,
    BookingFormDefinition,
    BookingOrg,
    BookingService,
    PublicBookingLink,
    StaffMember,
)
from app.booking.email_booking import google_calendar_template_url, send_booking_emails
from app.booking.initial_setup import (
    default_org_availability_defaults,
    default_org_cancel_policy,
    ensure_org_initial_setup,
)
from app.booking.meeting_service import build_meeting_url, resolve_meeting_provider_for_staff
from app.booking.rate_limit import check_public_booking_rate_limit
from app.booking.policies import can_change_or_cancel_online
from app.booking.routing_service import (
    AVAILABILITY_RANGE_END_SLACK,
    BOOKING_SLOT_STEP_MINUTES,
    _db_booking_intervals_map_for_staff,
    _load_google_busy_map,
    availability_defaults_positive_int,
    availability_zone,
    available_slots_for_link,
    blocked_iso_dates_in_range_for_link,
    link_lead_blocked_dates,
    booking_conflict_detail_json,
    db_booking_busy_intervals_for_staff,
    eligible_staff,
    fallback_open_hour_slots_for_link,
    link_bookable_until_date,
    link_buffer_minutes,
    org_buffer_minutes,
    org_calendar_day_bounds_utc,
    org_local_date_for_utc_instant,
    json_list_or_empty,
    json_object_or_empty,
    link_max_advance_booking_days,
    pick_staff_for_slot,
    scheduling_hints_json,
    staff_is_free,
)
from app.booking.oauth_util import (
    google_calendar_authorization_url,
    sign_staff_oauth_link,
    verify_staff_oauth_link,
)
from app.booking.schemas import (
    BookingCreate,
    FormDefinitionUpdate,
    OAuthLinkRequest,
    OrgCreate,
    OrgPatch,
    PublicLinkCreate,
    PublicLinkPatch,
    RescheduleBody,
    ServiceCreate,
    ServicePatch,
    StaffCreate,
    StaffPatch,
)
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.security.crypto import decrypt_secret, encrypt_secret
from app.security.audit import write_audit_log

logger = logging.getLogger(__name__)

router = APIRouter(tags=["booking"])

_PUBLIC_AVAILABILITY_CACHE: dict[tuple[str, str, str, str], tuple[float, dict[str, Any]]] = {}


def _coerce_google_busy_result(
    result: Any,
) -> tuple[dict[int, list[tuple[datetime, datetime]]], dict[int, str]]:
    if isinstance(result, tuple) and len(result) == 2:
        gmap, errors = result
        return dict(gmap or {}), dict(errors or {})
    if isinstance(result, dict):
        return dict(result), {}
    return {}, {}


def _filter_slots_by_blocked_dates(
    slots: list[dict[str, Any]],
    blocked_dates: list[str],
    org: BookingOrg,
) -> list[dict[str, Any]]:
    if not slots or not blocked_dates:
        return slots
    blocked = set(blocked_dates)
    loc_tz = availability_zone(json_object_or_empty(org.availability_defaults_json))
    out: list[dict[str, Any]] = []
    for slot in slots:
        raw_start = slot.get("start_utc")
        if not raw_start:
            continue
        try:
            start = datetime.fromisoformat(str(raw_start))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            local_day = start.astimezone(loc_tz).date().isoformat()
        except Exception:
            out.append(slot)
            continue
        if local_day not in blocked:
            out.append(slot)
    return out


def _public_availability_cache_key(
    token: str,
    from_ts: datetime,
    to_ts: datetime,
    service_id: int | None,
) -> tuple[str, str, str, str]:
    return (
        token,
        from_ts.isoformat(),
        to_ts.isoformat(),
        "" if service_id is None else str(int(service_id)),
    )


def _get_cached_public_availability(
    token: str,
    from_ts: datetime,
    to_ts: datetime,
    service_id: int | None,
    settings: Settings,
) -> dict[str, Any] | None:
    ttl = max(0, int(getattr(settings, "booking_public_availability_cache_sec", 0) or 0))
    if ttl <= 0:
        return None
    key = _public_availability_cache_key(token, from_ts, to_ts, service_id)
    cached = _PUBLIC_AVAILABILITY_CACHE.get(key)
    if not cached:
        return None
    expires_at, payload = cached
    if expires_at <= time_module.monotonic():
        _PUBLIC_AVAILABILITY_CACHE.pop(key, None)
        return None
    return copy.deepcopy(payload)


def _store_cached_public_availability(
    token: str,
    from_ts: datetime,
    to_ts: datetime,
    service_id: int | None,
    payload: dict[str, Any],
    settings: Settings,
) -> None:
    ttl = max(0, int(getattr(settings, "booking_public_availability_cache_sec", 0) or 0))
    if ttl <= 0:
        return
    if payload.get("availability_error"):
        return
    key = _public_availability_cache_key(token, from_ts, to_ts, service_id)
    _PUBLIC_AVAILABILITY_CACHE[key] = (
        time_module.monotonic() + ttl,
        copy.deepcopy(payload),
    )


def _clear_public_availability_cache(token: str | None = None) -> None:
    if token is None:
        _PUBLIC_AVAILABILITY_CACHE.clear()
        return
    doomed = [key for key in _PUBLIC_AVAILABILITY_CACHE if key[0] == token]
    for key in doomed:
        _PUBLIC_AVAILABILITY_CACHE.pop(key, None)


async def _reconcile_staff_calendar_blocks(
    db: AsyncSession,
    settings: Settings,
    staff_list: list[StaffMember],
    range_start: datetime,
    range_end: datetime,
    *,
    google_busy_map: dict[int, list[tuple[datetime, datetime]]] | None = None,
    google_busy_errors: dict[int, str] | None = None,
) -> dict[str, Any]:
    if not staff_list:
        return {
            "released_total": 0,
            "released_missing_google_events": 0,
            "released_unsynced_orphans": 0,
            "released_stale_synced": 0,
            "google_busy_errors": {},
            "google_busy_map": {},
        }
    gmap = google_busy_map
    gerr = dict(google_busy_errors or {})
    if gmap is None:
        gmap, gerr = _coerce_google_busy_result(
            await _load_google_busy_map(
                staff_list,
                range_start,
                range_end,
                settings,
            )
        )
    released_missing = await _release_bookings_with_missing_google_events(
        db,
        settings,
        staff_list,
        range_start,
        range_end,
    )
    released_orphans = await _release_unsynced_orphan_bookings(
        db,
        settings,
        staff_list,
        range_start,
        range_end,
        gmap,
        gerr,
    )
    released_stale = await _release_stale_synced_bookings_without_google_busy(
        db,
        settings,
        staff_list,
        range_start,
        range_end,
        gmap,
        gerr,
    )
    return {
        "released_total": int(released_missing + released_orphans + released_stale),
        "released_missing_google_events": int(released_missing),
        "released_unsynced_orphans": int(released_orphans),
        "released_stale_synced": int(released_stale),
        "google_busy_errors": gerr,
        "google_busy_map": gmap,
    }


async def _release_bookings_with_missing_google_events(
    db: AsyncSession,
    settings: Settings,
    staff_list: list[StaffMember],
    range_start: datetime,
    range_end: datetime,
) -> int:
    if not staff_list:
        return 0
    staff_map = {s.id: s for s in staff_list}
    q = select(Booking).where(
        Booking.staff_id.in_(list(staff_map)),
        Booking.status == "confirmed",
        Booking.google_event_id.is_not(None),
        Booking.start_utc < range_end,
        Booking.end_utc > range_start,
    )
    rows = list((await db.scalars(q)).all())
    released = 0
    for b in rows:
        event_id = (b.google_event_id or "").strip()
        staff = staff_map.get(int(b.staff_id or 0))
        if not event_id or not staff:
            continue
        exists, err = await get_calendar_event_status(
            _staff_google_refresh_token(staff, settings),
            staff.google_calendar_id,
            event_id,
            settings,
        )
        if exists is False:
            b.status = "cancelled"
            b.cancelled_at = datetime.now(timezone.utc)
            b.google_event_id = None
            b.google_calendar_synced_at = None
            b.google_calendar_sync_error = "Googleカレンダー上で予定が削除されたため自動で解放しました"
            released += 1
        elif err:
            logger.warning(
                "Google event existence check failed booking_id=%s staff_id=%s err=%s",
                b.id,
                getattr(staff, "id", None),
                err,
            )
    if released:
        await db.flush()
    return released


async def _release_stale_synced_bookings_without_google_busy(
    db: AsyncSession,
    settings: Settings,
    staff_list: list[StaffMember],
    range_start: datetime,
    range_end: datetime,
    google_busy_map: dict[int, list[tuple[datetime, datetime]]],
    google_busy_errors: dict[int, str],
    *,
    min_age_minutes: int = 10,
) -> int:
    if not staff_list:
        return 0
    staff_map = {s.id: s for s in staff_list}
    now_utc = datetime.now(timezone.utc)
    min_age = timedelta(minutes=max(1, int(min_age_minutes or 10)))
    q = (
        select(Booking)
        .join(BookingOrg, Booking.org_id == BookingOrg.id)
        .where(
            Booking.staff_id.in_(list(staff_map)),
            Booking.status == "confirmed",
            Booking.google_event_id.is_not(None),
            Booking.google_calendar_synced_at.is_not(None),
            Booking.start_utc < range_end,
            Booking.end_utc > range_start,
            BookingOrg.auto_confirm.is_(True),
        )
    )
    rows = list((await db.scalars(q)).all())
    released = 0
    for b in rows:
        staff = staff_map.get(int(b.staff_id or 0))
        if not staff:
            continue
        if staff.id in google_busy_errors:
            continue
        synced_at = getattr(b, "google_calendar_synced_at", None)
        synced_at_utc = (
            synced_at if synced_at and synced_at.tzinfo else synced_at.replace(tzinfo=timezone.utc)
            if synced_at
            else None
        )
        if not synced_at_utc or (now_utc - synced_at_utc) < min_age:
            continue
        start_utc = b.start_utc if b.start_utc.tzinfo else b.start_utc.replace(tzinfo=timezone.utc)
        end_utc = b.end_utc if b.end_utc.tzinfo else b.end_utc.replace(tzinfo=timezone.utc)
        current_busy = google_busy_map.get(staff.id) or []
        if _interval_overlaps_any(start_utc, end_utc, current_busy):
            continue
        b.status = "cancelled"
        b.cancelled_at = now_utc
        b.google_event_id = None
        b.google_calendar_synced_at = None
        existing_reason = (b.google_calendar_sync_error or "").strip()
        b.google_calendar_sync_error = (
            existing_reason
            or "Googleカレンダー上に同時間帯の予定が見つからないため自動で解放しました"
        )
        released += 1
    if released:
        await db.flush()
    return released


def _interval_overlaps_any(
    start_utc: datetime,
    end_utc: datetime,
    intervals: list[tuple[datetime, datetime]],
) -> bool:
    for a, b in intervals:
        aa = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
        bb = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
        if start_utc < bb and end_utc > aa:
            return True
    return False


async def _debug_db_busy_booking_details(
    db: AsyncSession,
    staff_ids: list[int],
    range_start: datetime,
    range_end: datetime,
) -> list[dict[str, Any]]:
    if not staff_ids:
        return []
    q = (
        select(Booking)
        .where(
            Booking.staff_id.in_(staff_ids),
            Booking.status.in_(("pending", "confirmed")),
            Booking.start_utc < range_end,
            Booking.end_utc > range_start,
        )
        .order_by(Booking.start_utc.asc(), Booking.id.asc())
        .limit(10)
    )
    rows = list((await db.scalars(q)).all())
    out: list[dict[str, Any]] = []
    for b in rows:
        out.append(
            {
                "booking_id": b.id,
                "staff_id": b.staff_id,
                "status": b.status,
                "start_utc": b.start_utc.isoformat() if b.start_utc else None,
                "end_utc": b.end_utc.isoformat() if b.end_utc else None,
                "google_event_id_present": bool((b.google_event_id or "").strip()),
                "google_calendar_synced_at": (
                    b.google_calendar_synced_at.isoformat() if b.google_calendar_synced_at else None
                ),
                "google_calendar_sync_error": b.google_calendar_sync_error,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
        )
    return out


async def _release_unsynced_orphan_bookings(
    db: AsyncSession,
    settings: Settings,
    staff_list: list[StaffMember],
    range_start: datetime,
    range_end: datetime,
    google_busy_map: dict[int, list[tuple[datetime, datetime]]],
    google_busy_errors: dict[int, str],
    *,
    min_age_minutes: int = 10,
) -> int:
    if not staff_list:
        return 0
    staff_map = {s.id: s for s in staff_list}
    now_utc = datetime.now(timezone.utc)
    q = (
        select(Booking)
        .join(BookingOrg, Booking.org_id == BookingOrg.id)
        .where(
            Booking.staff_id.in_(list(staff_map)),
            Booking.status == "confirmed",
            Booking.google_event_id.is_(None),
            Booking.google_calendar_synced_at.is_(None),
            Booking.start_utc < range_end,
            Booking.end_utc > range_start,
            BookingOrg.auto_confirm.is_(True),
        )
    )
    rows = list((await db.scalars(q)).all())
    released = 0
    min_age = timedelta(minutes=max(1, int(min_age_minutes or 10)))
    for b in rows:
        staff = staff_map.get(int(b.staff_id or 0))
        if not staff:
            continue
        if staff.id in google_busy_errors:
            continue
        created_at = b.created_at if getattr(b, "created_at", None) else None
        created_at_utc = (
            created_at if created_at and created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
            if created_at
            else None
        )
        if created_at_utc and (now_utc - created_at_utc) < min_age:
            continue
        if not created_at_utc:
            continue
        start_utc = b.start_utc if b.start_utc.tzinfo else b.start_utc.replace(tzinfo=timezone.utc)
        end_utc = b.end_utc if b.end_utc.tzinfo else b.end_utc.replace(tzinfo=timezone.utc)
        current_busy = google_busy_map.get(staff.id) or []
        if _interval_overlaps_any(start_utc, end_utc, current_busy):
            continue
        b.status = "cancelled"
        b.cancelled_at = now_utc
        b.google_calendar_synced_at = None
        existing_reason = (b.google_calendar_sync_error or "").strip()
        b.google_calendar_sync_error = (
            existing_reason
            or "Googleカレンダーに反映されていない古い予約を自動で解放しました"
        )
        released += 1
    if released:
        await db.flush()
    return released


def _staff_google_refresh_token(staff: StaffMember | None, settings: Settings) -> str | None:
    if staff is None:
        return None
    return decrypt_secret(getattr(staff, "google_refresh_token", None), settings)


def _staff_google_profile_email(staff: StaffMember | None, settings: Settings) -> str:
    if staff is None:
        return ""
    return (decrypt_secret(getattr(staff, "google_profile_email", None), settings) or "").strip()


def _staff_zoom_meeting_url(staff: StaffMember | None, settings: Settings) -> str:
    if staff is None:
        return ""
    return (decrypt_secret(getattr(staff, "zoom_meeting_url", None), settings) or "").strip()


def _booking_meeting_url(booking: Booking | None, settings: Settings) -> str:
    if booking is None:
        return ""
    return (decrypt_secret(getattr(booking, "meeting_url", None), settings) or "").strip()


def _booking_customer_name(booking: Booking | None, settings: Settings) -> str:
    if booking is None:
        return ""
    return (decrypt_secret(getattr(booking, "customer_name", None), settings) or "").strip()


def _booking_customer_email(booking: Booking | None, settings: Settings) -> str:
    if booking is None:
        return ""
    return (decrypt_secret(getattr(booking, "customer_email", None), settings) or "").strip()


def _scrub_booking_personal_data(booking: Booking) -> None:
    booking.customer_name = ""
    booking.customer_email = ""
    booking.customer_phone = None
    booking.company_name = None
    booking.calendar_title_note = None
    booking.form_answers_json = {}
    booking.utm_source = None
    booking.utm_medium = None
    booking.utm_campaign = None
    booking.referrer = None
    booking.ga_client_id = None


def _booking_calendar_description(
    booking: Booking,
    staff: StaffMember,
    settings: Settings,
    *,
    booking_link_title: str,
    manage_url: str,
    post_booking_message: str = "",
) -> tuple[list[str], str]:
    customer_name = _booking_customer_name(booking, settings)
    customer_email = _booking_customer_email(booking, settings)
    cust_no = ""
    if isinstance(booking.form_answers_json, dict):
        cust_no = str(booking.form_answers_json.get("customer_number") or "").strip()
    meeting_url = _booking_meeting_url(booking, settings)
    lines: list[str] = []
    if meeting_url:
        lines.append(f"Zoom URL: {meeting_url}")
    if (post_booking_message or "").strip():
        lines.append(f"ご案内: {(post_booking_message or '').strip()}")
    lines.append(f"予約リンク: {booking_link_title}")
    lines.append(f"予約者: {customer_name}")
    lines.append(f"メール: {customer_email}")
    if cust_no:
        lines.append(f"顧客番号: {cust_no}")
    if (booking.company_name or "").strip():
        lines.append(f"会社名: {(booking.company_name or '').strip()}")
    lines.append(f"担当: {(staff.name or '').strip()}")
    lines.append(f"変更・キャンセル: {manage_url}")
    return lines, meeting_url


async def _sync_booking_to_staff_calendar(
    session: AsyncSession,
    settings: Settings,
    booking: Booking,
    staff: StaffMember,
    org: BookingOrg,
    *,
    service_name: str,
    booking_link_title: str,
    post_booking_message: str = "",
) -> bool:
    summary = format_calendar_event_title(org, service_name, booking)
    meet = booking.meeting_provider == "meet"
    manage_url = f"{settings.public_base_url_value()}/app/manage/{booking.manage_token}"
    meeting_url = _booking_meeting_url(booking, settings)
    if booking.meeting_provider == "zoom" and not meeting_url:
        sz = _staff_zoom_meeting_url(staff, settings)
        if sz:
            meeting_url = sz
        elif settings.zoom_default_meeting_url:
            meeting_url = settings.zoom_default_meeting_url
    if booking.meeting_provider == "teams" and not meeting_url and settings.teams_default_meeting_url:
        meeting_url = settings.teams_default_meeting_url
    booking.meeting_url = encrypt_secret(meeting_url, settings) if meeting_url else None
    lines, meeting_url = _booking_calendar_description(
        booking,
        staff,
        settings,
        booking_link_title=booking_link_title,
        manage_url=manage_url,
        post_booking_message=post_booking_message,
    )
    if (booking.google_event_id or "").strip():
        await delete_event_for_booking(
            _staff_google_refresh_token(staff, settings),
            staff.google_calendar_id,
            booking.google_event_id,
            settings,
        )
        booking.google_event_id = None
    refresh_token = _staff_google_refresh_token(staff, settings)
    ev, cal_err = await create_event_for_booking_detailed(
        refresh_token,
        staff.google_calendar_id,
        summary,
        booking.start_utc.isoformat(),
        booking.end_utc.isoformat(),
        settings,
        with_meet=meet,
        attendees_emails=None,
        description="\n".join(lines),
        location=meeting_url or None,
    )
    if not ev and refresh_token and cal_err:
        await asyncio.sleep(0.35)
        ev, retry_err = await create_event_for_booking_detailed(
            refresh_token,
            staff.google_calendar_id,
            summary,
            booking.start_utc.isoformat(),
            booking.end_utc.isoformat(),
            settings,
            with_meet=meet,
            attendees_emails=None,
            description="\n".join(lines),
            location=meeting_url or None,
        )
        if ev:
            cal_err = None
        elif retry_err:
            cal_err = retry_err
    if ev:
        booking.google_event_id = ev.get("id")
        booking.google_calendar_synced_at = datetime.now(timezone.utc)
        booking.google_calendar_sync_error = None
        if meet:
            hang = (ev.get("conferenceData") or {}).get("entryPoints") or []
            for ep in hang:
                if ep.get("entryPointType") == "video" and ep.get("uri"):
                    booking.meeting_url = encrypt_secret(ep["uri"], settings)
                    break
        await session.flush()
        return True
    booking.google_calendar_synced_at = None
    booking.google_calendar_sync_error = cal_err or "Googleカレンダー登録に失敗しました"
    await session.flush()
    return False


async def get_db() -> AsyncSession:
    factory = get_session_factory()
    async with factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def _validate_staff_ids_for_org(
    session: AsyncSession, org_id: int, staff_ids: list[int]
) -> None:
    if not staff_ids:
        return
    seen = set()
    for sid in staff_ids:
        if sid in seen:
            raise HTTPException(400, "duplicate staff_id in list")
        seen.add(sid)
        st = await session.get(StaffMember, sid)
        if not st or st.org_id != org_id:
            raise HTTPException(400, f"staff {sid} not in this organization")


async def _resolve_valid_link_staff_ids(
    session: AsyncSession,
    org_id: int,
    raw_staff_ids: list[Any],
) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for raw in raw_staff_ids:
        try:
            sid = int(raw)
        except (TypeError, ValueError):
            continue
        if sid in seen:
            continue
        seen.add(sid)
        ids.append(sid)
    if not ids:
        return []
    rows = (
        await session.scalars(
            select(StaffMember.id).where(
                StaffMember.org_id == org_id,
                StaffMember.active.is_(True),
                StaffMember.id.in_(ids),
            )
        )
    ).all()
    valid = {int(x) for x in rows}
    return [sid for sid in ids if sid in valid]


def _normalize_link_priority_overrides(
    raw: Any,
    allowed_staff_ids: list[int] | None = None,
) -> dict[str, int]:
    allowed = {int(x) for x in (allowed_staff_ids or [])}
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        try:
            staff_id = int(key)
            priority = max(0, int(value))
        except (TypeError, ValueError):
            continue
        if allowed and staff_id not in allowed:
            continue
        out[str(staff_id)] = priority
    return out


def _normalize_optional_non_negative_int(
    raw: Any,
    *,
    max_value: int,
) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return max(0, min(max_value, int(raw)))
    except (TypeError, ValueError):
        return None


def _normalize_optional_text(
    raw: Any,
    *,
    max_length: int,
) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text[:max_length]


def _normalize_optional_iso_date(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date().isoformat() if "T" in text else date.fromisoformat(text).isoformat()
    except ValueError:
        return None


async def _assert_staff_belong_to_org(
    session: AsyncSession,
    org_id: int,
    staff_ids: list[int],
) -> None:
    if not staff_ids:
        return
    rows = (
        await session.scalars(
            select(StaffMember.id).where(
                StaffMember.org_id == org_id,
                StaffMember.id.in_(staff_ids),
            )
        )
    ).all()
    if len(set(rows)) != len(set(staff_ids)):
        raise HTTPException(400, "staff_ids must belong to this org")


@router.get("/booking/public/{token}")
async def legacy_booking_public_redirect(token: str) -> RedirectResponse:
    """旧 URL → Web アプリの予約画面へリダイレクト。"""
    return RedirectResponse(url=f"/app/booking/{token}", status_code=307)


@router.get("/api/booking/links/{token}/meta")
async def link_meta(token: str, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    link = await db.scalar(select(PublicBookingLink).where(PublicBookingLink.token == token))
    if not link:
        raise HTTPException(404, "link not found")
    if getattr(link, "active", True) is False:
        raise HTTPException(403, "この予約リンクは無効です")
    org = await db.get(BookingOrg, link.org_id)
    if not org:
        raise HTTPException(404, "org missing")
    initial_service: dict[str, Any] | None = None
    if link.service_id is None:
        initial_service_row = await db.scalar(
            select(BookingService)
            .where(
                BookingService.org_id == org.id,
                BookingService.active.is_(True),
            )
            .order_by(BookingService.id.asc())
            .limit(1)
        )
        if initial_service_row:
            initial_service = {
                "id": initial_service_row.id,
                "name": initial_service_row.name,
                "duration_minutes": initial_service_row.duration_minutes,
            }
    form = await db.scalar(
        select(BookingFormDefinition).where(
            BookingFormDefinition.org_id == org.id,
            BookingFormDefinition.active.is_(True),
        )
    )
    fixed_service: dict[str, Any] | None = None
    if link.service_id:
        fs = await db.get(BookingService, link.service_id)
        if not fs or fs.org_id != org.id:
            raise HTTPException(
                404,
                "この予約リンクに紐づく予約区分が見つかりません。設定で予約区分を確認してください。",
            )
        fixed_service = {
            "id": fs.id,
            "name": fs.name,
            "duration_minutes": fs.duration_minutes,
        }
    return {
        "fixed_service": fixed_service,
        "initial_service": initial_service,
        "form_fields": json_list_or_empty(form.fields_json) if form else [],
        "ga4_measurement_id": org.ga4_measurement_id,
        "link": {
            "title": link.title,
            "service_id": link.service_id,
            "buffer_minutes": link_buffer_minutes(link, org, settings),
            "max_advance_booking_days": link_max_advance_booking_days(link, org),
            "bookable_until_date": getattr(link, "bookable_until_date", None) or "",
            "pre_booking_notice": getattr(link, "pre_booking_notice", None) or "",
            "post_booking_message": getattr(link, "post_booking_message", None) or "",
        },
        "availability_defaults": json_object_or_empty(org.availability_defaults_json),
    }


@router.get("/api/booking/links/{token}/availability")
async def link_availability(
    token: str,
    db: DbSession,
    settings: SettingsDep,
    from_ts: datetime,
    to_ts: datetime,
    service_id: int | None = None,
) -> dict[str, Any]:
    cached_payload = _get_cached_public_availability(token, from_ts, to_ts, service_id, settings)
    if cached_payload is not None:
        cached_payload["cached"] = True
        return cached_payload
    link = await db.scalar(select(PublicBookingLink).where(PublicBookingLink.token == token))
    if not link:
        raise HTTPException(404, "link not found")
    if getattr(link, "active", True) is False:
        raise HTTPException(403, "この予約リンクは無効です")
    org = await db.get(BookingOrg, link.org_id)
    if not org:
        raise HTTPException(404, "org missing")
    effective_sid = link.service_id if link.service_id is not None else service_id
    service = await db.get(BookingService, effective_sid) if effective_sid else None
    if effective_sid and (not service or service.org_id != org.id):
        raise HTTPException(400, "invalid service")
    defaults = json_object_or_empty(org.availability_defaults_json)
    buf_min = link_buffer_minutes(link, org, settings)
    link_max_adv_days = link_max_advance_booking_days(link, org)
    link_cutoff_date = link_bookable_until_date(link)
    duration_min = (
        max(1, int(service.duration_minutes))
        if service
        else availability_defaults_positive_int(defaults, "duration", BOOKING_SLOT_STEP_MINUTES)
    )
    try:
        staff_ids = await _resolve_valid_link_staff_ids(
            db,
            org.id,
            json_list_or_empty(link.staff_ids_json),
        )
        link_priority_overrides = _normalize_link_priority_overrides(
            getattr(link, "staff_priority_overrides_json", None),
            staff_ids,
        )
        staff_list = await eligible_staff(db, org, staff_ids, service, settings)
        fts = from_ts if from_ts.tzinfo else from_ts.replace(tzinfo=timezone.utc)
        tts = to_ts if to_ts.tzinfo else to_ts.replace(tzinfo=timezone.utc)
        linked_staff_ids = {
            s.id for s in staff_list if (_staff_google_refresh_token(s, settings) or "").strip()
        }
        _gpad = timedelta(minutes=max(0, buf_min))
        gmap_failed = False
        google_busy_errors: dict[int, str] = {}
        try:
            gmap, google_busy_errors = _coerce_google_busy_result(
                await _load_google_busy_map(
                    staff_list,
                    fts - _gpad,
                    tts + _gpad,
                    settings,
                )
            )
            failed_linked_staff_ids = set(google_busy_errors).intersection(linked_staff_ids)
            if failed_linked_staff_ids:
                gmap_failed = True
                staff_list = [s for s in staff_list if s.id not in failed_linked_staff_ids]
                staff_ids = [sid for sid in staff_ids if sid not in failed_linked_staff_ids]
                link_priority_overrides = _normalize_link_priority_overrides(
                    link_priority_overrides,
                    staff_ids,
                )
        except Exception:
            gmap_failed = True
            logger.exception("Public link Google busy load failed: token=%s org_id=%s", token, org.id)
            gmap = {}
            google_busy_errors = {}
        db_busy_map = await _db_booking_intervals_map_for_staff(
            db,
            [int(s.id) for s in staff_list],
            fts,
            tts + AVAILABILITY_RANGE_END_SLACK,
        )
        lead_blocked = link_lead_blocked_dates(org, link)
        slots, slot_generation_step, had_slot_errors, slot_error_message = await available_slots_for_link(
            db,
            org,
            staff_ids,
            service,
            from_ts,
            to_ts,
            settings,
            slot_minutes=None,
            staff_list=staff_list,
            google_busy_map=gmap,
            db_busy_map=db_busy_map,
            extra_blocked_dates=lead_blocked,
            link_priority_overrides=link_priority_overrides,
            buffer_minutes_override=buf_min,
            max_advance_days_override=link_max_adv_days,
            bookable_until_date_override=link_cutoff_date,
        )
        oauth_on = settings.is_google_oauth_configured()
        linked_n = sum(1 for s in staff_list if (_staff_google_refresh_token(s, settings) or "").strip())
        allow_open_hours_fallback = False
        fallback_open_hours_used = False
        if (
            not slots
            and staff_list
            and not gmap
            and allow_open_hours_fallback
            and (gmap_failed or had_slot_errors)
        ):
            fallback_slots, fallback_step = fallback_open_hour_slots_for_link(
                org,
                staff_list,
                from_ts,
                to_ts,
                service=service,
                link_priority_overrides=link_priority_overrides,
                extra_blocked_dates=lead_blocked,
                max_advance_days_override=link_max_adv_days,
                bookable_until_date_override=link_cutoff_date,
            )
            if fallback_slots:
                slots = fallback_slots
                slot_generation_step = fallback_step
                fallback_open_hours_used = True
        unlinked_fallback = bool(oauth_on and staff_list and linked_n == 0)
        blocked_dates = blocked_iso_dates_in_range_for_link(org, link, from_ts, to_ts)
        slots = _filter_slots_by_blocked_dates(slots, blocked_dates, org)
        availability_error = None
        if unlinked_fallback:
            slots = []
            availability_error = "担当者の Google カレンダー連携が未完了のため、予約受付を停止しています。管理画面から各担当のカレンダー連携を完了してください。"
        if not slots and linked_staff_ids and (gmap_failed or had_slot_errors):
            availability_error = "Google カレンダーの予定を確認できませんでした。担当のカレンダー認証または連携状態を確認してください。"
        response_payload = {
            "slots": slots,
            "busy_intervals": [],
            "blocked_dates": blocked_dates,
            "slot_minutes": slot_generation_step,
            "service_duration_minutes": duration_min,
            "buffer_minutes": buf_min,
            "max_advance_booking_days": link_max_adv_days,
            "bookable_until_date": link_cutoff_date.isoformat() if link_cutoff_date else None,
            "eligible_staff_count": len(staff_list),
            "availability_error": availability_error,
            "scheduling_hints": scheduling_hints_json(
                duration_min,
                buf_min,
                eligible_staff_count=len(staff_list),
            ),
            "calendar_integration": {
                "oauth_configured": oauth_on,
                "google_linked_staff_count": linked_n,
                "google_busy_failed_staff_count": len(google_busy_errors),
                "unlinked_fallback_active": unlinked_fallback,
                "warning_ja": (
                    "Google カレンダーの予定を確認できなかった担当がいるため、その担当は空き表示から除外しています。"
                    if google_busy_errors and staff_list
                    else
                    "Google カレンダーの予定を確認できませんでした。担当のカレンダー認証または連携状態を確認してください。"
                    if linked_staff_ids and (gmap_failed or had_slot_errors) and not slots
                    else
                    "カレンダーの空き情報を安全に読み切れなかったため、受付時間ベースの候補枠を表示しています。予約確定時に最終確認します。"
                    if fallback_open_hours_used
                    else
                    "Google カレンダーが誰も連携していないため、予約受付を停止しています。各担当の Google 連携を完了してください。"
                    if unlinked_fallback
                    else None
                ),
            },
        }
        _store_cached_public_availability(token, from_ts, to_ts, service_id, response_payload, settings)
        response_payload["cached"] = False
        return response_payload
    except Exception:
        logger.exception("Public link availability failed: token=%s org_id=%s", token, org.id)
        blocked_dates = blocked_iso_dates_in_range_for_link(org, link, from_ts, to_ts)
        response_payload = {
            "slots": [],
            "busy_intervals": [],
            "blocked_dates": blocked_dates,
            "slot_minutes": max(1, min(BOOKING_SLOT_STEP_MINUTES, duration_min)),
            "service_duration_minutes": duration_min,
            "buffer_minutes": buf_min,
            "max_advance_booking_days": link_max_adv_days,
            "bookable_until_date": link_cutoff_date.isoformat() if link_cutoff_date else None,
            "eligible_staff_count": 0,
            "availability_error": "空き枠の取得に失敗しました。カレンダー認証または予約リンク設定を確認してください。",
            "scheduling_hints": scheduling_hints_json(
                duration_min,
                buf_min,
                eligible_staff_count=0,
            ),
            "calendar_integration": {
                "oauth_configured": settings.is_google_oauth_configured(),
                "google_linked_staff_count": 0,
                "unlinked_fallback_active": False,
                "warning_ja": None,
            },
        }
        _store_cached_public_availability(token, from_ts, to_ts, service_id, response_payload, settings)
        response_payload["cached"] = False
        return response_payload


async def _create_booking_from_body(
    token: str,
    body: BookingCreate,
    db: AsyncSession,
    settings: Settings,
) -> tuple[Booking, StaffMember, bool, str, str]:
    if body.link_token != token:
        raise HTTPException(400, "token mismatch")
    link = await db.scalar(select(PublicBookingLink).where(PublicBookingLink.token == token))
    if not link:
        raise HTTPException(404, "link not found")
    if getattr(link, "active", True) is False:
        raise HTTPException(403, "この予約リンクは無効です")
    org = await db.get(BookingOrg, link.org_id)
    if not org:
        raise HTTPException(404, "org missing")
    effective_sid = link.service_id if link.service_id is not None else body.service_id
    if effective_sid is None:
        raise HTTPException(400, "service_id is required")
    service = await db.get(BookingService, effective_sid)
    if not service or service.org_id != org.id:
        raise HTTPException(400, "invalid service")
    if link.service_id is not None and body.service_id is not None and body.service_id != link.service_id:
        raise HTTPException(400, "service does not match this link")
    staff_ids = await _resolve_valid_link_staff_ids(
        db,
        org.id,
        json_list_or_empty(link.staff_ids_json),
    )
    link_priority_overrides = _normalize_link_priority_overrides(
        getattr(link, "staff_priority_overrides_json", None),
        staff_ids,
    )
    start = body.start_utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    staff_probe_list = await eligible_staff(db, org, staff_ids, service, settings)
    gmap_probe, gmap_probe_errors = _coerce_google_busy_result(
        await _load_google_busy_map(
            staff_probe_list,
            start - timedelta(days=1),
            start + timedelta(days=1) + timedelta(minutes=max(1, int(service.duration_minutes or 30))),
            settings,
        )
    )
    await _release_bookings_with_missing_google_events(
        db,
        settings,
        staff_probe_list,
        start - timedelta(days=1),
        start + timedelta(days=1) + timedelta(minutes=max(1, int(service.duration_minutes or 30))),
    )
    await _reconcile_staff_calendar_blocks(
        db,
        settings,
        staff_probe_list,
        start - timedelta(days=1),
        start + timedelta(days=1) + timedelta(minutes=max(1, int(service.duration_minutes or 30))),
        google_busy_map=gmap_probe,
        google_busy_errors=gmap_probe_errors,
    )
    end = start + timedelta(minutes=service.duration_minutes)

    defaults_avail = json_object_or_empty(org.availability_defaults_json)
    local_booking_date = org_local_date_for_utc_instant(start, org)
    max_adv_days = link_max_advance_booking_days(link, org)
    link_cutoff_date = link_bookable_until_date(link)
    if max_adv_days > 0:
        loc_tz = availability_zone(defaults_avail)
        today = datetime.now(loc_tz).date()
        last_ok = today + timedelta(days=max_adv_days)
        if local_booking_date > last_ok:
            raise HTTPException(
                400,
                "この日付は予約受付の範囲外です（先行予約の上限日数の設定）。",
            )
    if link_cutoff_date is not None and local_booking_date > link_cutoff_date:
        raise HTTPException(
            400,
            f"この予約リンクは {link_cutoff_date.isoformat()} 以降の予約を受け付けていません。",
        )
    if day_is_blocked_for_booking(local_booking_date, defaults_avail):
        raise HTTPException(
            400,
            "この日は予約を受け付けていません（土日・祝日設定をご確認ください）",
        )
    if local_booking_date in link_lead_blocked_dates(org, link):
        raise HTTPException(
            400,
            "この期間はこの予約リンクでは受け付けていません（直近の受付開始までお待ちください）",
        )

    buf_org = link_buffer_minutes(link, org, settings)
    fb_a = body.availability_from_ts
    fb_b = body.availability_to_ts
    if fb_a is not None and fb_b is not None:
        if fb_a.tzinfo is None:
            fb_a = fb_a.replace(tzinfo=timezone.utc)
        if fb_b.tzinfo is None:
            fb_b = fb_b.replace(tzinfo=timezone.utc)
        if fb_a > fb_b:
            fb_a, fb_b = fb_b, fb_a
        fb_from, fb_to = fb_a, fb_b
    else:
        fb_from = start - timedelta(days=7)
        fb_to = start + timedelta(days=7)
    # 予約開始・終了が FreeBusy の窓ぴったりだと欠けることがあるため必ず含める
    fb_pad = timedelta(minutes=max(0, buf_org))
    fb_from = min(fb_from, start - fb_pad)
    fb_to = max(fb_to, end + fb_pad)
    if fb_from >= fb_to:
        fb_to = fb_from + timedelta(seconds=1)

    staff: StaffMember | None = None
    if body.staff_id:
        staff = await db.get(StaffMember, body.staff_id)
        if not staff or staff.org_id != org.id:
            raise HTTPException(400, "invalid staff")
        eligible_for_link = await eligible_staff(db, org, staff_ids, service, settings)
        if not any(s.id == staff.id for s in eligible_for_link):
            raise HTTPException(
                400,
                "この担当はこの予約リンク・区分の対象外です（担当割当・スキル・Google連携をご確認ください）",
            )
        gmap, _google_busy_errors = _coerce_google_busy_result(
            await _load_google_busy_map([staff], fb_from, fb_to, settings)
        )
        if not await staff_is_free(
            db,
            staff,
            start,
            end,
            settings,
            gmap,
            buffer_minutes=buf_org,
        ):
            logger.warning(
                "booking rejected slot_not_available staff_id=%s start=%s end=%s buf=%s",
                staff.id,
                start.isoformat(),
                end.isoformat(),
                buf_org,
            )
            raise HTTPException(
                status_code=409,
                detail=booking_conflict_detail_json(
                    "slot_not_available",
                    "この時間は予約できません（担当のカレンダーまたは他の予約と重なります）。",
                    duration_minutes=service.duration_minutes,
                    buffer_minutes=buf_org,
                ),
            )
    else:
        staff_list = await eligible_staff(db, org, staff_ids, service, settings)
        if not staff_list:
            raise HTTPException(
                status_code=409,
                detail=booking_conflict_detail_json(
                    "no_eligible_staff",
                    "割り当て可能な担当がいません（Google連携・スキル条件をご確認ください）。",
                    duration_minutes=service.duration_minutes,
                    buffer_minutes=buf_org,
                ),
            )
        gmap, _google_busy_errors = _coerce_google_busy_result(
            await _load_google_busy_map(staff_list, fb_from, fb_to, settings)
        )
        picked = await pick_staff_for_slot(
            db,
            org,
            staff_ids,
            service,
            start,
            end,
            settings,
            link_priority_overrides=link_priority_overrides,
            buffer_minutes_override=buf_org,
            google_busy_map=gmap,
            dry_run=False,
        )
        if not picked:
            logger.warning(
                "booking rejected no_staff_for_slot start=%s end=%s buf=%s org_id=%s",
                start.isoformat(),
                end.isoformat(),
                buf_org,
                org.id,
            )
            raise HTTPException(
                status_code=409,
                detail=booking_conflict_detail_json(
                    "no_staff_for_slot",
                    "この時間は予約できません（空きがないか、前後余白の都合で取れないか、緑枠と時刻が一致していません）。別の緑の枠を選び直してください。",
                    duration_minutes=service.duration_minutes,
                    buffer_minutes=buf_org,
                ),
            )
        staff = picked

    if not org.auto_confirm:
        raise HTTPException(
            503,
            "個人情報をアプリ内に保持しない運用のため、手動承認モードは利用できません。設定で自動確定を有効にしてください。",
        )

    manage_token = secrets.token_urlsafe(24)
    status = "confirmed"
    _mp_raw = (body.meeting_provider or "").strip()
    mp = _mp_raw.lower() if _mp_raw else resolve_meeting_provider_for_staff(staff, settings)
    meet_url_placeholder, provider_resolved = build_meeting_url(mp, settings, staff)
    _staff_label = (staff.name or "").strip() or None
    b = Booking(
        org_id=org.id,
        staff_id=staff.id,
        staff_display_name=_staff_label,
        service_id=service.id,
        start_utc=start,
        end_utc=end,
        status=status,
        customer_name=encrypt_secret(body.customer_name, settings) or body.customer_name,
        customer_email=encrypt_secret(str(body.customer_email), settings) or str(body.customer_email),
        booking_link_title_snapshot=(link.title or "予約"),
        customer_phone=body.customer_phone,
        company_name=(body.company_name or "").strip() or None,
        calendar_title_note=(body.calendar_title_note or "").strip() or None,
        form_answers_json=body.form_answers or {},
        meeting_provider=provider_resolved,
        meeting_url=encrypt_secret(meet_url_placeholder, settings) if meet_url_placeholder else None,
        manage_token=manage_token,
        utm_source=body.utm_source,
        utm_medium=body.utm_medium,
        utm_campaign=body.utm_campaign,
        referrer=body.referrer,
        ga_client_id=body.ga_client_id,
    )
    db.add(b)
    await db.flush()

    customer_cal = False
    if status == "confirmed":
        try:
            customer_cal = await _finalize_confirmed_booking(
                db,
                settings,
                b,
                staff,
                org,
                service.name,
                booking_link_title=(link.title or "予約"),
                post_booking_message=(getattr(link, "post_booking_message", None) or "").strip(),
                customer_google_access_token=body.customer_google_access_token,
            )
        except Exception:
            logger.exception("booking finalize failed booking_id=%s staff_id=%s", b.id, staff.id)
            customer_cal = False
    return (
        b,
        staff,
        customer_cal,
        (link.title or "予約"),
        (getattr(link, "post_booking_message", None) or "").strip(),
    )


def _public_booking_response(
    settings: Settings,
    org: BookingOrg,
    b: Booking,
    staff: StaffMember,
    service_name: str,
    *,
    booking_link_title: str,
    customer_calendar_added: bool,
    post_booking_message: str,
) -> dict[str, Any]:
    base = settings.public_base_url_value()
    manage_url = f"{base}/app/manage/{b.manage_token}"
    title = format_calendar_event_title(org, service_name, b)
    meet = _booking_meeting_url(b, settings)
    details_lines: list[str] = []
    if meet:
        details_lines.append(f"オンライン: {meet}")
    if (post_booking_message or "").strip():
        details_lines.append(f"ご案内: {(post_booking_message or '').strip()}")
    details_lines.append(f"変更・キャンセル: {manage_url}")
    gcal_url = google_calendar_template_url(
        title,
        b.start_utc,
        b.end_utc,
        details="\n".join(details_lines),
        location=meet or "",
    )
    return {
        "booking_id": b.id,
        "status": b.status,
        "link_title": booking_link_title,
        "manage_url": manage_url,
        "meeting_url": meet,
        "staff_name": staff.name or "",
        "google_calendar_add_url": gcal_url,
        "customer_calendar_added": customer_calendar_added,
        "post_booking_message": post_booking_message,
    }


@router.post("/api/booking/links/{token}/book")
async def book_appointment(
    request: Request,
    token: str,
    body: BookingCreate,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    check_public_booking_rate_limit(
        request,
        max_requests=max(1, int(settings.booking_public_rate_limit_max_requests or 40)),
        window_sec=max(60, int(settings.booking_public_rate_limit_window_sec or 3600)),
    )
    b, staff, customer_cal, booking_link_title, post_booking_message = await _create_booking_from_body(token, body, db, settings)
    await db.commit()
    _clear_public_availability_cache(token)
    org = await db.get(BookingOrg, b.org_id)
    svc = await db.get(BookingService, b.service_id) if b.service_id else None
    service_name = svc.name if svc else "予約"
    if not org:
        raise HTTPException(500, "org missing")
    try:
        return _public_booking_response(
            settings,
            org,
            b,
            staff,
            service_name,
            booking_link_title=booking_link_title,
            customer_calendar_added=customer_cal,
            post_booking_message=post_booking_message,
        )
    except Exception:
        logger.exception("public booking response build failed booking_id=%s", b.id)
        base = settings.public_base_url_value()
        return {
            "booking_id": b.id,
            "status": b.status,
            "link_title": booking_link_title,
            "manage_url": f"{base}/app/manage/{b.manage_token}",
            "meeting_url": _booking_meeting_url(b, settings),
            "staff_name": staff.name or "",
            "google_calendar_add_url": "",
            "customer_calendar_added": bool(customer_cal),
            "post_booking_message": post_booking_message,
            "response_partial": True,
        }


async def _finalize_confirmed_booking(
    session: AsyncSession,
    settings: Settings,
    b: Booking,
    staff: StaffMember,
    org: BookingOrg,
    service_name: str,
    *,
    booking_link_title: str,
    post_booking_message: str = "",
    customer_google_access_token: str | None = None,
) -> bool:
    """カレンダー反映・会議 URL・メール・CRM。顧客の Google カレンダー追加に成功したら True。"""
    customer_name = _booking_customer_name(b, settings)
    customer_email = _booking_customer_email(b, settings)
    manage_url = f"{settings.public_base_url_value()}/app/manage/{b.manage_token}"
    customer_cal_ok = False
    try:
        await _sync_booking_to_staff_calendar(
            session,
            settings,
            b,
            staff,
            org,
            service_name=service_name,
            booking_link_title=booking_link_title,
            post_booking_message=post_booking_message,
        )
        meeting_url = _booking_meeting_url(b, settings)
        cust_tok = (customer_google_access_token or "").strip()
        if cust_tok:
            cust_no = ""
            if isinstance(b.form_answers_json, dict):
                cust_no = str(b.form_answers_json.get("customer_number") or "").strip()
            desc_lines: list[str] = []
            if meeting_url:
                desc_lines.append(f"Zoom URL: {meeting_url}")
            if (post_booking_message or "").strip():
                desc_lines.append(f"ご案内: {(post_booking_message or '').strip()}")
            desc_lines.append(f"予約リンク: {booking_link_title}")
            desc_lines.append(f"予約者: {customer_name}")
            desc_lines.append(f"メール: {customer_email}")
            if cust_no:
                desc_lines.append(f"顧客番号: {cust_no}")
            if (b.company_name or "").strip():
                desc_lines.append(f"会社名: {(b.company_name or '').strip()}")
            desc_lines.append(f"担当: {(staff.name or '').strip()}")
            desc_lines.append(f"予約の変更・キャンセル: {manage_url}")
            ev_c = await insert_customer_primary_calendar_with_access_token(
                cust_tok,
                summary,
                b.start_utc.isoformat(),
                b.end_utc.isoformat(),
                description="\n".join(desc_lines),
                location=meeting_url or None,
            )
            customer_cal_ok = ev_c is not None

        email_results = await send_booking_emails(
            settings,
            org,
            b,
            staff,
            service_name,
            booking_link_title=booking_link_title,
            manage_url=manage_url,
            post_booking_message=post_booking_message,
            dry_run=settings.actions_dry_run,
        )
        email_now = datetime.now(timezone.utc)
        b.customer_confirmation_email_last_attempt_at = email_now
        if email_results.get("customer"):
            b.customer_confirmation_email_sent_at = email_now
            b.customer_confirmation_email_error = None
        elif email_results.get("customer_error"):
            b.customer_confirmation_email_error = str(email_results.get("customer_error"))[:500]
        b.staff_notification_email_last_attempt_at = email_now
        if email_results.get("staff"):
            b.staff_notification_email_sent_at = email_now
            b.staff_notification_email_error = None
        elif email_results.get("staff_error"):
            b.staff_notification_email_error = str(email_results.get("staff_error"))[:500]
        await session.flush()
        return customer_cal_ok
    finally:
        _scrub_booking_personal_data(b)
        await session.flush()


async def _delete_staff_calendar_event_if_present(
    booking: Booking,
    staff: StaffMember | None,
    settings: Settings,
) -> bool:
    event_id = (booking.google_event_id or "").strip()
    if not event_id or not staff:
        return False
    await delete_event_for_booking(
        _staff_google_refresh_token(staff, settings),
        staff.google_calendar_id,
        event_id,
        settings,
    )
    booking.google_event_id = None
    return True


@router.post("/api/booking/links/{token}/book-upload")
async def book_with_files(
    request: Request,
    token: str,
    db: DbSession,
    settings: SettingsDep,
    payload: str = Form(...),
    files: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    raise HTTPException(
        400,
        "個人情報をアプリ内に残さない運用へ変更したため、予約時のファイルアップロードは無効です。",
    )


@router.get("/api/booking/manage/{manage_token}")
async def manage_info(manage_token: str, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    b = await db.scalar(select(Booking).where(Booking.manage_token == manage_token))
    if not b:
        raise HTTPException(404, "not found")
    org = await db.get(BookingOrg, b.org_id)
    allowed, reason = can_change_or_cancel_online(org, b) if org else (False, "no_org")
    return {
        "booking": {
            "id": b.id,
            "status": b.status,
            "start_utc": b.start_utc.isoformat(),
            "end_utc": b.end_utc.isoformat(),
            "meeting_url": _booking_meeting_url(b, settings),
            "link_title": (b.booking_link_title_snapshot or "").strip() or "予約",
        },
        "can_cancel_online": allowed,
        "can_reschedule_online": allowed,
        "policy_reason": reason,
    }


@router.post("/api/booking/manage/{manage_token}/cancel")
async def manage_cancel(manage_token: str, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    b = await db.scalar(select(Booking).where(Booking.manage_token == manage_token))
    if not b:
        raise HTTPException(404, "not found")
    org = await db.get(BookingOrg, b.org_id)
    if not org:
        raise HTTPException(404, "org missing")
    allowed, reason = can_change_or_cancel_online(org, b)
    if not allowed:
        raise HTTPException(400, reason)
    staff = await db.get(StaffMember, b.staff_id)
    b.status = "cancelled"
    b.cancelled_at = datetime.now(timezone.utc)
    await _delete_staff_calendar_event_if_present(b, staff, settings)
    await db.commit()
    _clear_public_availability_cache()
    return {"ok": True, "status": b.status}


@router.post("/api/booking/manage/{manage_token}/reschedule")
async def manage_reschedule(
    manage_token: str,
    body: RescheduleBody,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    b = await db.scalar(select(Booking).where(Booking.manage_token == manage_token))
    if not b:
        raise HTTPException(404, "not found")
    if b.status not in ("pending", "confirmed"):
        raise HTTPException(400, "cannot reschedule this booking")
    org = await db.get(BookingOrg, b.org_id)
    if not org:
        raise HTTPException(404, "org missing")
    allowed, reason = can_change_or_cancel_online(org, b)
    if not allowed:
        raise HTTPException(400, reason)
    staff = await db.get(StaffMember, b.staff_id) if b.staff_id is not None else None
    svc = await db.get(BookingService, b.service_id) if b.service_id else None
    if not svc:
        raise HTTPException(400, "invalid booking data")
    if not staff:
        raise HTTPException(
            400,
            "担当が削除されているため、オンラインでの変更はできません。担当者へお問い合わせください。",
        )
    new_start = body.new_start_utc
    if new_start.tzinfo is None:
        new_start = new_start.replace(tzinfo=timezone.utc)
    new_end = new_start + timedelta(minutes=svc.duration_minutes)
    defaults_avail = org.availability_defaults_json or {}
    if day_is_blocked_for_booking(org_local_date_for_utc_instant(new_start, org), defaults_avail):
        raise HTTPException(
            400,
            "この日は予約を受け付けていません（土日祝・休業設定）",
        )
    ws, we = org_calendar_day_bounds_utc(new_start, org)
    _pad = timedelta(days=1)
    gmap, _google_busy_errors = _coerce_google_busy_result(
        await _load_google_busy_map([staff], ws - _pad, we + _pad, settings)
    )
    buf_res = org_buffer_minutes(org, settings)
    if not await staff_is_free(
        db,
        staff,
        new_start,
        new_end,
        settings,
        gmap,
        exclude_booking_id=b.id,
        buffer_minutes=buf_res,
    ):
        raise HTTPException(
            status_code=409,
            detail=booking_conflict_detail_json(
                "slot_not_available",
                "この時間は予約できません（カレンダーまたは他の予約と重なります）。",
                duration_minutes=svc.duration_minutes,
                buffer_minutes=buf_res,
            ),
        )
    b.start_utc = new_start
    b.end_utc = new_end
    if b.status == "confirmed" and b.google_event_id:
        await patch_event_for_booking(
            _staff_google_refresh_token(staff, settings),
            staff.google_calendar_id,
            b.google_event_id,
            b.start_utc.isoformat(),
            b.end_utc.isoformat(),
            settings,
        )
    await db.commit()
    _clear_public_availability_cache()
    return {"ok": True, "start_utc": b.start_utc.isoformat(), "end_utc": b.end_utc.isoformat()}


@router.post("/api/booking/admin/orgs")
async def admin_create_org(
    request: Request,
    body: OrgCreate,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=None)
    org = BookingOrg(
        name=body.name,
        slug=body.slug,
        routing_mode=body.routing_mode,
        auto_confirm=True,
        cancel_policy_json=default_org_cancel_policy(),
        availability_defaults_json=default_org_availability_defaults(),
    )
    db.add(org)
    try:
        await db.flush()
        await ensure_org_initial_setup(db, org)
        await db.commit()
        await db.refresh(org)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, "この slug は既に使われています")
    return {"id": org.id, "slug": org.slug}


@router.patch("/api/booking/admin/orgs/{slug}")
async def admin_patch_org(
    request: Request,
    slug: str,
    body: OrgPatch,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    if body.name is not None:
        org.name = body.name
    if body.slug is not None:
        new_slug = body.slug
        if new_slug != org.slug:
            other = await db.scalar(select(BookingOrg).where(BookingOrg.slug == new_slug))
            if other is not None and other.id != org.id:
                raise HTTPException(400, "この slug は既に使われています")
            org.slug = new_slug
    if body.auto_confirm is not None:
        org.auto_confirm = body.auto_confirm
    if body.routing_mode is not None:
        org.routing_mode = body.routing_mode
    if body.ga4_measurement_id is not None:
        org.ga4_measurement_id = body.ga4_measurement_id or None
    if body.cancel_policy is not None:
        org.cancel_policy_json = body.cancel_policy
    if body.availability_defaults is not None:
        org.availability_defaults_json = body.availability_defaults
    if body.email_settings is not None:
        org.email_settings_json = body.email_settings
    try:
        await db.commit()
        await db.refresh(org)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, "この slug は既に使われています")
    return {"ok": True, "slug": org.slug, "name": org.name}


@router.get("/api/booking/admin/orgs")
async def admin_list_orgs(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    sec = (settings.booking_admin_secret or "").strip()
    if sec and (x_admin_secret or "").strip() == sec:
        rows = (await db.scalars(select(BookingOrg).order_by(BookingOrg.id))).all()
        return {
            "orgs": [
                {
                    "id": o.id,
                    "name": o.name,
                    "slug": o.slug,
                    "routing_mode": o.routing_mode,
                    "auto_confirm": o.auto_confirm,
                }
                for o in rows
            ]
        }
    user = await get_current_app_user(request, db)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="ログインするか、正しい X-Admin-Secret を指定してください",
        )
    if user.role == "admin":
        rows = (await db.scalars(select(BookingOrg).order_by(BookingOrg.id))).all()
        return {
            "orgs": [
                {
                    "id": o.id,
                    "name": o.name,
                    "slug": o.slug,
                    "routing_mode": o.routing_mode,
                    "auto_confirm": o.auto_confirm,
                }
                for o in rows
            ]
        }
    uslug = (user.default_org_slug or "").strip()
    if not uslug:
        return {"orgs": []}
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == uslug))
    if not org:
        return {"orgs": []}
    return {
        "orgs": [
            {
                "id": org.id,
                "name": org.name,
                "slug": org.slug,
                "routing_mode": org.routing_mode,
                "auto_confirm": org.auto_confirm,
            }
        ]
    }


@router.get("/api/booking/admin/orgs/{slug}/summary")
async def admin_org_summary(
    request: Request,
    slug: str,
    db: DbSession,
    settings: SettingsDep,
    include_staff: bool = True,
    include_services: bool = True,
    include_links: bool = True,
    include_forms: bool = True,
    include_counts: bool = False,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    staff = (
        (await db.scalars(select(StaffMember).where(StaffMember.org_id == org.id))).all()
        if include_staff
        else []
    )
    services = (
        (await db.scalars(select(BookingService).where(BookingService.org_id == org.id))).all()
        if include_services or include_links
        else []
    )
    links = (
        (await db.scalars(select(PublicBookingLink).where(PublicBookingLink.org_id == org.id))).all()
        if include_links
        else []
    )
    forms = (
        (await db.scalars(select(BookingFormDefinition).where(BookingFormDefinition.org_id == org.id))).all()
        if include_forms
        else []
    )
    base = settings.public_base_url_value()
    service_map = {int(s.id): s for s in services}
    if include_staff:
        active_staff_ids = {int(s.id) for s in staff if bool(getattr(s, "active", True))}
    elif include_links:
        active_staff_ids = {
            int(x)
            for x in (
                await db.scalars(
                    select(StaffMember.id).where(
                        StaffMember.org_id == org.id,
                        StaffMember.active.is_(True),
                    )
                )
            ).all()
        }
    else:
        active_staff_ids = set()
    counts: dict[str, int] = {}
    if include_counts:
        if include_staff:
            counts["staff"] = len(
                [
                    s
                    for s in staff
                    if bool(getattr(s, "active", True)) and bool(getattr(s, "google_refresh_token", None))
                ]
            )
        else:
            counts["staff"] = int(
                await db.scalar(
                    select(func.count())
                    .select_from(StaffMember)
                    .where(
                        StaffMember.org_id == org.id,
                        StaffMember.active.is_(True),
                        StaffMember.google_refresh_token.is_not(None),
                    )
                )
                or 0
            )
        counts["services"] = len(services) if include_services else int(
            await db.scalar(select(func.count()).select_from(BookingService).where(BookingService.org_id == org.id)) or 0
        )
        counts["links"] = len(links) if include_links else int(
            await db.scalar(select(func.count()).select_from(PublicBookingLink).where(PublicBookingLink.org_id == org.id)) or 0
        )
        counts["forms"] = len(forms) if include_forms else int(
            await db.scalar(select(func.count()).select_from(BookingFormDefinition).where(BookingFormDefinition.org_id == org.id)) or 0
        )
    links_payload: list[dict[str, Any]] = []
    for l in links:
        raw_staff_ids = json_list_or_empty(l.staff_ids_json)
        seen_staff_ids: set[int] = set()
        valid_staff_ids: list[int] = []
        for raw in raw_staff_ids:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            if sid in seen_staff_ids:
                continue
            seen_staff_ids.add(sid)
            if not active_staff_ids or sid in active_staff_ids:
                valid_staff_ids.append(sid)
        svc = service_map.get(int(l.service_id)) if l.service_id is not None else None
        links_payload.append(
            {
                "id": l.id,
                "token": l.token,
                "title": l.title,
                "service_id": l.service_id,
                "service_name": svc.name if svc else None,
                "service_duration_minutes": svc.duration_minutes if svc else None,
                "staff_ids": valid_staff_ids,
                "staff_priority_overrides": _normalize_link_priority_overrides(
                    getattr(l, "staff_priority_overrides_json", None),
                    valid_staff_ids,
                ),
                "buffer_minutes": _normalize_optional_non_negative_int(
                    getattr(l, "buffer_minutes", None),
                    max_value=180,
                ),
                "max_advance_booking_days": _normalize_optional_non_negative_int(
                    getattr(l, "max_advance_booking_days", None),
                    max_value=730,
                ),
                "bookable_until_date": getattr(l, "bookable_until_date", None) or "",
                "pre_booking_notice": getattr(l, "pre_booking_notice", None) or "",
                "post_booking_message": getattr(l, "post_booking_message", None) or "",
                "active": getattr(l, "active", True),
                "block_next_days": int(getattr(l, "block_next_days", 0) or 0),
                "public_url": f"{base}/app/booking/{l.token}",
                "public_path": f"/app/booking/{l.token}",
            }
        )
    return {
        "public_base_url": base,
        "org": {
            "id": org.id,
            "name": org.name,
            "slug": org.slug,
            "routing_mode": org.routing_mode,
            "auto_confirm": org.auto_confirm,
            "ga4_measurement_id": org.ga4_measurement_id,
            "cancel_policy_json": org.cancel_policy_json,
            "availability_defaults_json": org.availability_defaults_json,
            "email_settings_json": getattr(org, "email_settings_json", None) or {},
        },
        "staff": [
            {
                "id": s.id,
                "name": s.name,
                "email": s.email,
                "priority_rank": s.priority_rank,
                "active": s.active,
                "has_google": bool(_staff_google_refresh_token(s, settings)),
                "google_calendar_id": s.google_calendar_id or "",
                "google_profile_email": _staff_google_profile_email(s, settings),
                "google_profile_name": s.google_profile_name,
                "zoom_meeting_url": _staff_zoom_meeting_url(s, settings),
            }
            for s in staff
        ],
        "services": [{"id": s.id, "name": s.name, "duration_minutes": s.duration_minutes, "active": s.active} for s in services],
        "links": links_payload,
        "google_oauth_ready": settings.is_google_oauth_configured(),
        "counts": counts,
        "forms": [{"id": f.id, "name": f.name, "active": f.active} for f in forms],
    }


@router.get("/api/booking/admin/orgs/{slug}/calendar-diagnostics")
async def admin_calendar_diagnostics(
    request: Request,
    slug: str,
    db: DbSession,
    settings: SettingsDep,
    include_write_check: bool = False,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    staff = list(
        (
            await db.scalars(
                select(StaffMember).where(
                    StaffMember.org_id == org.id,
                    StaffMember.active.is_(True),
                )
            )
        ).all()
    )
    now_utc = datetime.now(timezone.utc)
    until_utc = now_utc + timedelta(days=14)
    gmap, errors = _coerce_google_busy_result(
        await _load_google_busy_map(staff, now_utc, until_utc, settings)
    )
    rows: list[dict[str, Any]] = []
    for s in staff:
        refresh_token = (_staff_google_refresh_token(s, settings) or "").strip()
        busy = list(gmap.get(s.id) or [])
        err = (errors.get(s.id) or "").strip()
        write_ok = None
        write_error = None
        if include_write_check and refresh_token and not err:
            write_ok, write_error = await verify_calendar_write_access_detailed(
                refresh_token,
                s.google_calendar_id,
                settings,
            )
        rows.append(
            {
                "staff_id": s.id,
                "name": s.name,
                "email": s.email or "",
                "google_profile_email": _staff_google_profile_email(s, settings),
                "google_calendar_id": s.google_calendar_id or "primary",
                "has_google_refresh_token": bool(refresh_token),
                "busy_interval_count": len(busy),
                "busy_sample": [
                    {"start_utc": a.isoformat(), "end_utc": b.isoformat()}
                    for a, b in busy[:5]
                ],
                "status": "error" if err else ("ok" if refresh_token else "unlinked"),
                "error": err or None,
                "write_status": (
                    "ok" if write_ok is True else "error" if write_ok is False else "skipped"
                ),
                "write_error": write_error,
            }
        )
    return {
        "org": {"slug": org.slug, "name": org.name},
        "window": {"from_utc": now_utc.isoformat(), "to_utc": until_utc.isoformat()},
        "google_oauth_ready": settings.is_google_oauth_configured(),
        "staff": rows,
    }


@router.post("/api/booking/admin/orgs/{slug}/reconcile-calendar-blocks")
async def admin_reconcile_calendar_blocks(
    request: Request,
    slug: str,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    staff = list(
        (
            await db.scalars(
                select(StaffMember).where(
                    StaffMember.org_id == org.id,
                    StaffMember.active.is_(True),
                )
            )
        ).all()
    )
    now_utc = datetime.now(timezone.utc)
    from_utc = now_utc - timedelta(days=30)
    to_utc = now_utc + timedelta(days=120)
    reconcile = await _reconcile_staff_calendar_blocks(
        db,
        settings,
        staff,
        from_utc,
        to_utc,
    )
    await write_audit_log(
        db,
        request,
        action="booking.calendar_blocks_reconciled",
        org_slug=org.slug,
        target_type="booking_org",
        target_id=org.id,
        detail={
            "released_total": reconcile["released_total"],
            "released_missing_google_events": reconcile["released_missing_google_events"],
            "released_unsynced_orphans": reconcile["released_unsynced_orphans"],
            "released_stale_synced": reconcile["released_stale_synced"],
        },
    )
    await db.commit()
    return {
        "ok": True,
        "org": {"slug": org.slug, "name": org.name},
        "window": {"from_utc": from_utc.isoformat(), "to_utc": to_utc.isoformat()},
        "released_total": reconcile["released_total"],
        "released_missing_google_events": reconcile["released_missing_google_events"],
        "released_unsynced_orphans": reconcile["released_unsynced_orphans"],
        "released_stale_synced": reconcile["released_stale_synced"],
        "google_busy_error_staff_count": len(reconcile["google_busy_errors"]),
    }


@router.get("/api/booking/admin/orgs/{slug}/bookings")
async def admin_list_bookings(
    request: Request,
    slug: str,
    db: DbSession,
    settings: SettingsDep,
    status: str | None = None,
    limit: int = 100,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    q = select(Booking).where(Booking.org_id == org.id)
    if status:
        q = q.where(Booking.status == status)
    q = q.order_by(Booking.start_utc.desc()).limit(min(max(limit, 1), 500))
    rows = (await db.scalars(q)).all()
    return {
        "bookings": [
            {
                "id": b.id,
                "status": b.status,
                "start_utc": b.start_utc.isoformat(),
                "end_utc": b.end_utc.isoformat(),
                "booking_link_title_snapshot": b.booking_link_title_snapshot,
                "staff_id": b.staff_id,
                "staff_display_name": b.staff_display_name,
                "service_id": b.service_id,
                "google_event_id": b.google_event_id,
                "google_calendar_synced_at": (
                    b.google_calendar_synced_at.isoformat()
                    if b.google_calendar_synced_at
                    else None
                ),
                "google_calendar_sync_error": b.google_calendar_sync_error,
                "customer_confirmation_email_sent_at": (
                    b.customer_confirmation_email_sent_at.isoformat()
                    if b.customer_confirmation_email_sent_at
                    else None
                ),
                "customer_confirmation_email_error": b.customer_confirmation_email_error,
                "staff_notification_email_sent_at": (
                    b.staff_notification_email_sent_at.isoformat()
                    if b.staff_notification_email_sent_at
                    else None
                ),
                "staff_notification_email_error": b.staff_notification_email_error,
            }
            for b in rows
        ]
    }


@router.get("/api/booking/admin/orgs/{slug}/calendar")
async def admin_org_calendar(
    request: Request,
    slug: str,
    from_ts: datetime,
    to_ts: datetime,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """週表示など用: 期間内の予約を担当・時刻付きで返す（DB 上の予約。Google 同期済みの内容と一致）。"""
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    if from_ts >= to_ts:
        raise HTTPException(400, "from_ts must be before to_ts")
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    staff_list = list(
        (await db.scalars(select(StaffMember).where(StaffMember.org_id == org.id))).all()
    )
    staff_map = {s.id: s for s in staff_list}
    q = (
        select(Booking)
        .where(
            Booking.org_id == org.id,
            Booking.status.in_(("pending", "confirmed")),
            Booking.start_utc < to_ts,
            Booking.end_utc > from_ts,
        )
        .order_by(Booking.start_utc)
    )
    rows = (await db.scalars(q)).all()
    events: list[dict[str, Any]] = []
    for b in rows:
        st = staff_map.get(b.staff_id) if b.staff_id is not None else None
        svc = await db.get(BookingService, b.service_id) if b.service_id else None
        svc_name = svc.name if svc else "予約"
        staff_label = (st.name if st else None) or (b.staff_display_name or "") or "（削除済み担当）"
        event_title = f"{svc_name} — 予約 #{b.id}"
        events.append(
            {
                "id": b.id,
                "staff_id": b.staff_id,
                "staff_name": staff_label,
                "service_name": svc_name,
                "title": event_title,
                "start_utc": b.start_utc.isoformat(),
                "end_utc": b.end_utc.isoformat(),
                "status": b.status,
            }
        )
    return {
        "org_slug": org.slug,
        "org_name": org.name,
        "from_ts": from_ts.isoformat(),
        "to_ts": to_ts.isoformat(),
        "google_oauth_ready": settings.is_google_oauth_configured(),
        "staff": [
            {
                "id": s.id,
                "name": s.name,
                "email": s.email,
                "has_google": bool(_staff_google_refresh_token(s, settings)),
                "active": s.active,
            }
            for s in staff_list
        ],
        "events": events,
    }


@router.post("/api/booking/admin/orgs/{slug}/staff")
async def admin_add_staff(
    request: Request,
    slug: str,
    body: StaffCreate,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    name = (body.name or "").strip() or "（Google 連携で名前を取得）"
    email = (body.email or "").strip()
    s = StaffMember(
        org_id=org.id,
        name=name,
        email=email,
        priority_rank=body.priority_rank,
        google_calendar_id=body.google_calendar_id,
        zoom_meeting_url=encrypt_secret(body.zoom_meeting_url, settings) if body.zoom_meeting_url else None,
    )
    db.add(s)
    await db.flush()
    await write_audit_log(
        db,
        request,
        action="booking.staff_created",
        org_slug=org.slug,
        target_type="staff",
        target_id=s.id,
        detail={"name": s.name, "email": s.email},
    )
    await db.commit()
    await db.refresh(s)
    return {"id": s.id}


@router.patch("/api/booking/admin/staff/{staff_id}")
async def admin_patch_staff(
    request: Request,
    staff_id: int,
    body: StaffPatch,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    s = await db.get(StaffMember, staff_id)
    if not s:
        raise HTTPException(404, "staff not found")
    org = await db.get(BookingOrg, s.org_id)
    if not org:
        raise HTTPException(404, "org not found")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    if body.name is not None:
        s.name = body.name
    if body.email is not None:
        s.email = body.email
    if body.priority_rank is not None:
        s.priority_rank = body.priority_rank
    if body.google_calendar_id is not None:
        s.google_calendar_id = body.google_calendar_id
    if body.zoom_meeting_url is not None:
        s.zoom_meeting_url = encrypt_secret(body.zoom_meeting_url.strip(), settings) if body.zoom_meeting_url.strip() else None
    if body.active is not None:
        s.active = body.active
    if body.clear_google_oauth is True:
        s.google_refresh_token = None
        s.google_calendar_id = None
        s.google_profile_email = None
        s.google_profile_name = None
    await write_audit_log(
        db,
        request,
        action="booking.staff_updated",
        org_slug=org.slug,
        target_type="staff",
        target_id=s.id,
        detail={"name": s.name, "active": s.active, "cleared_google": body.clear_google_oauth is True},
    )
    await db.commit()
    return {"ok": True}


@router.delete("/api/booking/admin/staff/{staff_id}")
async def admin_delete_staff(
    request: Request,
    staff_id: int,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """担当を削除。既存予約は staff_id を NULL にし、表示名は staff_display_name を参照。"""
    s = await db.get(StaffMember, staff_id)
    if not s:
        raise HTTPException(404, "staff not found")
    org = await db.get(BookingOrg, s.org_id)
    if not org:
        raise HTTPException(404, "org not found")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    snap = (s.name or "").strip() or None
    if snap:
        await db.execute(
            update(Booking)
            .where(Booking.staff_id == staff_id)
            .where((Booking.staff_display_name.is_(None)) | (Booking.staff_display_name == ""))
            .values(staff_display_name=snap)
        )
    links = (
        await db.scalars(select(PublicBookingLink).where(PublicBookingLink.org_id == s.org_id))
    ).all()
    for link in links:
        raw = list(json_list_or_empty(link.staff_ids_json))
        if staff_id in raw:
            link.staff_ids_json = [x for x in raw if x != staff_id]
        pri_map = _normalize_link_priority_overrides(
            getattr(link, "staff_priority_overrides_json", None),
        )
        if str(staff_id) in pri_map:
            pri_map.pop(str(staff_id), None)
            link.staff_priority_overrides_json = pri_map
    await write_audit_log(
        db,
        request,
        action="booking.staff_deleted",
        org_slug=org.slug,
        target_type="staff",
        target_id=staff_id,
        detail={"name": s.name, "email": s.email},
    )
    await db.execute(delete(StaffMember).where(StaffMember.id == staff_id))
    await db.commit()
    return {"ok": True, "deleted_id": staff_id}


@router.post("/api/booking/admin/orgs/{slug}/forms")
async def admin_upsert_form(
    request: Request,
    slug: str,
    body: FormDefinitionUpdate,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    await db.execute(
        update(BookingFormDefinition)
        .where(BookingFormDefinition.org_id == org.id)
        .values(active=False)
    )
    f = BookingFormDefinition(org_id=org.id, name=body.name, fields_json=body.fields_json, active=True)
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return {"id": f.id}


@router.put("/api/booking/admin/forms/{form_id}")
async def admin_put_form(
    request: Request,
    form_id: int,
    body: FormDefinitionUpdate,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    f = await db.get(BookingFormDefinition, form_id)
    if not f:
        raise HTTPException(404, "form not found")
    org = await db.get(BookingOrg, f.org_id)
    if not org:
        raise HTTPException(404, "org not found")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    f.name = body.name
    f.fields_json = body.fields_json
    f.active = True
    await db.commit()
    return {"ok": True}


@router.post("/api/booking/admin/orgs/{slug}/services")
async def admin_add_service(
    request: Request,
    slug: str,
    body: ServiceCreate,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    n = (body.name or "").strip()
    if not n:
        raise HTTPException(400, "区分名を入力してください")
    s = BookingService(
        org_id=org.id,
        name=n,
        duration_minutes=body.duration_minutes,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return {"id": s.id}


@router.patch("/api/booking/admin/services/{service_id}")
async def admin_patch_service(
    request: Request,
    service_id: int,
    body: ServicePatch,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    svc = await db.get(BookingService, service_id)
    if not svc:
        raise HTTPException(404, "service not found")
    org = await db.get(BookingOrg, svc.org_id)
    if not org:
        raise HTTPException(404, "org not found")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    if body.name is not None:
        n = body.name.strip()
        if not n:
            raise HTTPException(400, "name cannot be empty")
        svc.name = n
    if body.duration_minutes is not None:
        d = int(body.duration_minutes)
        if d < 5 or d > 480:
            raise HTTPException(400, "duration_minutes must be between 5 and 480")
        svc.duration_minutes = d
    if body.active is not None:
        svc.active = body.active
    await db.commit()
    await db.refresh(svc)
    return {
        "id": svc.id,
        "name": svc.name,
        "duration_minutes": svc.duration_minutes,
        "active": svc.active,
    }


async def _delete_orphaned_service(db: AsyncSession, service_id: int | None) -> None:
    if service_id is None:
        return
    remaining = int(
        await db.scalar(
            select(func.count())
            .select_from(PublicBookingLink)
            .where(PublicBookingLink.service_id == service_id)
        )
        or 0
    )
    if remaining > 0:
        return
    await db.execute(delete(BookingService).where(BookingService.id == service_id))


async def _resolve_inline_link_service(
    db: AsyncSession,
    org: BookingOrg,
    *,
    title: str,
    service_id: int | None,
    service_name: str | None,
    service_duration_minutes: int | None,
    existing_link: PublicBookingLink | None = None,
) -> BookingService:
    target_name = (service_name or title or "予約").strip() or "予約"
    target_duration = int(service_duration_minutes or 30)
    if target_duration < 5 or target_duration > 480:
        raise HTTPException(400, "service_duration_minutes must be between 5 and 480")

    if service_id is not None and existing_link is None:
        svc = await db.get(BookingService, service_id)
        if not svc or svc.org_id != org.id:
            raise HTTPException(400, "invalid service_id for this organization")
        return svc

    current_service_id = existing_link.service_id if existing_link else None
    current_service = await db.get(BookingService, current_service_id) if current_service_id else None

    if service_id is not None:
        svc = await db.get(BookingService, service_id)
        if not svc or svc.org_id != org.id:
            raise HTTPException(400, "invalid service_id for this organization")
        current_service = svc

    if current_service is None:
        svc = BookingService(
            org_id=org.id,
            name=target_name,
            duration_minutes=target_duration,
            active=True,
        )
        db.add(svc)
        await db.flush()
        return svc

    shared_links = int(
        await db.scalar(
            select(func.count())
            .select_from(PublicBookingLink)
            .where(PublicBookingLink.service_id == current_service.id)
            .where(PublicBookingLink.id != (existing_link.id if existing_link else -1))
        )
        or 0
    )
    if shared_links > 0:
        clone = BookingService(
            org_id=org.id,
            name=target_name,
            duration_minutes=target_duration,
            active=True,
        )
        db.add(clone)
        await db.flush()
        return clone

    current_service.name = target_name
    current_service.duration_minutes = target_duration
    current_service.active = True
    await db.flush()
    return current_service


@router.post("/api/booking/admin/orgs/{slug}/links")
async def admin_add_link(
    request: Request,
    slug: str,
    body: PublicLinkCreate,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=slug)
    org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if not org:
        raise HTTPException(404, "org not found")
    title = body.title.strip() or "予約"
    svc = await _resolve_inline_link_service(
        db,
        org,
        title=title,
        service_id=body.service_id,
        service_name=body.service_name,
        service_duration_minutes=body.service_duration_minutes,
    )
    ids = list(body.staff_ids)
    await _validate_staff_ids_for_org(db, org.id, ids)
    pri_map = _normalize_link_priority_overrides(body.staff_priority_overrides, ids)
    buffer_minutes = _normalize_optional_non_negative_int(body.buffer_minutes, max_value=180)
    max_advance_days = _normalize_optional_non_negative_int(
        body.max_advance_booking_days,
        max_value=730,
    )
    bookable_until_date = _normalize_optional_iso_date(body.bookable_until_date)
    pre_booking_notice = _normalize_optional_text(body.pre_booking_notice, max_length=4000)
    post_booking_message = _normalize_optional_text(body.post_booking_message, max_length=4000)
    token = secrets.token_urlsafe(16)
    bnd = max(0, min(366, int(body.block_next_days)))
    link = PublicBookingLink(
        org_id=org.id,
        token=token,
        title=title,
        service_id=svc.id,
        staff_ids_json=ids,
        staff_priority_overrides_json=pri_map,
        buffer_minutes=buffer_minutes,
        max_advance_booking_days=max_advance_days,
        bookable_until_date=bookable_until_date,
        pre_booking_notice=pre_booking_notice,
        post_booking_message=post_booking_message,
        block_next_days=bnd,
    )
    db.add(link)
    await db.flush()
    await write_audit_log(
        db,
        request,
        action="booking.link_created",
        org_slug=org.slug,
        target_type="booking_link",
        target_id=link.id,
        detail={"title": link.title, "service_id": link.service_id, "active": link.active},
    )
    await db.commit()
    await db.refresh(link)
    _clear_public_availability_cache(token)
    base = settings.public_base_url_value()
    app_path = f"{base}/app/booking/{token}"
    return {
        "id": link.id,
        "token": token,
        "title": link.title,
        "service_id": svc.id,
        "service_name": svc.name,
        "service_duration_minutes": svc.duration_minutes,
        "staff_ids": ids,
        "staff_priority_overrides": pri_map,
        "buffer_minutes": buffer_minutes,
        "max_advance_booking_days": max_advance_days,
        "bookable_until_date": bookable_until_date or "",
        "pre_booking_notice": pre_booking_notice or "",
        "post_booking_message": post_booking_message or "",
        "active": link.active,
        "block_next_days": int(link.block_next_days or 0),
        "public_url": app_path,
        "public_path": f"/app/booking/{token}",
        "legacy_url": f"{base}/booking/public/{token}",
    }


@router.patch("/api/booking/admin/links/{link_id}")
async def admin_patch_link(
    request: Request,
    link_id: int,
    body: PublicLinkPatch,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    link = await db.get(PublicBookingLink, link_id)
    if not link:
        raise HTTPException(404, "link not found")
    org = await db.get(BookingOrg, link.org_id)
    if not org:
        raise HTTPException(404, "org missing")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    if body.title is not None:
        link.title = body.title.strip() or link.title
    old_service_id = link.service_id
    if (
        body.service_id is not None
        or body.service_name is not None
        or body.service_duration_minutes is not None
    ):
        svc = await _resolve_inline_link_service(
            db,
            org,
            title=link.title,
            service_id=body.service_id,
            service_name=body.service_name,
            service_duration_minutes=body.service_duration_minutes,
            existing_link=link,
        )
        link.service_id = svc.id
    staff_ids_for_priority = list(json_list_or_empty(link.staff_ids_json))
    if body.staff_ids is not None:
        staff_ids_for_priority = list(body.staff_ids)
        await _validate_staff_ids_for_org(db, org.id, staff_ids_for_priority)
        link.staff_ids_json = staff_ids_for_priority
    if body.staff_priority_overrides is not None:
        link.staff_priority_overrides_json = _normalize_link_priority_overrides(
            body.staff_priority_overrides,
            staff_ids_for_priority,
        )
    elif body.staff_ids is not None:
        link.staff_priority_overrides_json = _normalize_link_priority_overrides(
            getattr(link, "staff_priority_overrides_json", None),
            staff_ids_for_priority,
        )
    if body.buffer_minutes is not None:
        link.buffer_minutes = _normalize_optional_non_negative_int(
            body.buffer_minutes,
            max_value=180,
        )
    if body.max_advance_booking_days is not None:
        link.max_advance_booking_days = _normalize_optional_non_negative_int(
            body.max_advance_booking_days,
            max_value=730,
        )
    if body.bookable_until_date is not None:
        link.bookable_until_date = _normalize_optional_iso_date(body.bookable_until_date)
    if body.pre_booking_notice is not None:
        link.pre_booking_notice = _normalize_optional_text(body.pre_booking_notice, max_length=4000)
    if body.post_booking_message is not None:
        link.post_booking_message = _normalize_optional_text(body.post_booking_message, max_length=4000)
    if body.active is not None:
        link.active = bool(body.active)
    if body.block_next_days is not None:
        link.block_next_days = max(0, min(366, int(body.block_next_days)))
    await write_audit_log(
        db,
        request,
        action="booking.link_updated",
        org_slug=org.slug,
        target_type="booking_link",
        target_id=link.id,
        detail={"title": link.title, "service_id": link.service_id, "active": link.active},
    )
    await db.commit()
    if old_service_id != link.service_id:
        async with db.begin():
            await _delete_orphaned_service(db, old_service_id)
    _clear_public_availability_cache(link.token)
    base = settings.public_base_url_value()
    valid_staff_ids = await _resolve_valid_link_staff_ids(
        db,
        org.id,
        json_list_or_empty(link.staff_ids_json),
    )
    current_service = await db.get(BookingService, link.service_id) if link.service_id else None
    return {
        "ok": True,
        "id": link.id,
        "token": link.token,
        "title": link.title,
        "service_id": link.service_id,
        "service_name": current_service.name if current_service else None,
        "service_duration_minutes": current_service.duration_minutes if current_service else None,
        "staff_ids": valid_staff_ids,
        "staff_priority_overrides": _normalize_link_priority_overrides(
            getattr(link, "staff_priority_overrides_json", None),
            valid_staff_ids,
        ),
        "buffer_minutes": _normalize_optional_non_negative_int(
            getattr(link, "buffer_minutes", None),
            max_value=180,
        ),
        "max_advance_booking_days": _normalize_optional_non_negative_int(
            getattr(link, "max_advance_booking_days", None),
            max_value=730,
        ),
        "bookable_until_date": getattr(link, "bookable_until_date", None) or "",
        "pre_booking_notice": getattr(link, "pre_booking_notice", None) or "",
        "post_booking_message": getattr(link, "post_booking_message", None) or "",
        "active": link.active,
        "block_next_days": int(link.block_next_days or 0),
        "public_url": f"{base}/app/booking/{link.token}",
        "public_path": f"/app/booking/{link.token}",
    }


@router.delete("/api/booking/admin/links/{link_id}")
async def admin_delete_link(
    request: Request,
    link_id: int,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    link = await db.get(PublicBookingLink, link_id)
    if not link:
        raise HTTPException(404, "link not found")
    org = await db.get(BookingOrg, link.org_id)
    if not org:
        raise HTTPException(404, "org not found")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    await write_audit_log(
        db,
        request,
        action="booking.link_deleted",
        org_slug=org.slug,
        target_type="booking_link",
        target_id=link.id,
        detail={"title": link.title, "service_id": link.service_id},
    )
    old_service_id = link.service_id
    await db.execute(delete(PublicBookingLink).where(PublicBookingLink.id == link_id))
    await db.commit()
    async with db.begin():
        await _delete_orphaned_service(db, old_service_id)
    _clear_public_availability_cache(link.token)
    return {"ok": True}


@router.post("/api/booking/admin/bookings/{booking_id}/approve")
async def admin_approve_booking(
    request: Request,
    booking_id: int,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    b = await db.scalar(select(Booking).where(Booking.id == booking_id))
    if not b:
        raise HTTPException(404, "booking not found")
    if b.status != "pending":
        raise HTTPException(400, "not pending")
    org = await db.get(BookingOrg, b.org_id)
    if not org:
        raise HTTPException(400, "invalid booking")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    staff = await db.get(StaffMember, b.staff_id) if b.staff_id is not None else None
    svc = await db.get(BookingService, b.service_id) if b.service_id else None
    if not staff:
        raise HTTPException(
            400,
            "担当が削除されているため、管理画面からの確定処理ができません。担当者側で対応してください。",
        )
    b.status = "confirmed"
    b.approved_at = datetime.now(timezone.utc)
    await _finalize_confirmed_booking(
        db,
        settings,
        b,
        staff,
        org,
        svc.name if svc else "予約",
        booking_link_title=(b.booking_link_title_snapshot or svc.name or "予約"),
    )
    await write_audit_log(
        db,
        request,
        action="booking.booking_approved",
        org_slug=org.slug,
        target_type="booking",
        target_id=b.id,
        detail={"staff_id": b.staff_id},
    )
    await db.commit()
    return {"ok": True, "status": b.status}


@router.post("/api/booking/admin/bookings/{booking_id}/reject")
async def admin_reject_booking(
    request: Request,
    booking_id: int,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    b = await db.scalar(select(Booking).where(Booking.id == booking_id))
    if not b:
        raise HTTPException(404, "booking not found")
    if b.status != "pending":
        raise HTTPException(400, "not pending")
    org = await db.get(BookingOrg, b.org_id)
    if not org:
        raise HTTPException(400, "invalid booking")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    b.status = "rejected"
    await write_audit_log(
        db,
        request,
        action="booking.booking_rejected",
        org_slug=org.slug,
        target_type="booking",
        target_id=b.id,
        detail={"staff_id": b.staff_id},
    )
    await db.commit()
    return {"ok": True, "status": b.status}


@router.post("/api/booking/admin/bookings/{booking_id}/resync-calendar")
async def admin_resync_booking_calendar(
    request: Request,
    booking_id: int,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    b = await db.scalar(select(Booking).where(Booking.id == booking_id))
    if not b:
        raise HTTPException(404, "booking not found")
    org = await db.get(BookingOrg, b.org_id)
    if not org:
        raise HTTPException(400, "invalid booking")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    if b.status != "confirmed":
        raise HTTPException(400, "confirmed booking only")
    staff = await db.get(StaffMember, b.staff_id) if b.staff_id is not None else None
    if not staff:
        raise HTTPException(400, "担当が見つかりません")
    svc = await db.get(BookingService, b.service_id) if b.service_id else None
    ok = await _sync_booking_to_staff_calendar(
        db,
        settings,
        b,
        staff,
        org,
        service_name=(svc.name if svc else "予約"),
        booking_link_title=(b.booking_link_title_snapshot or (svc.name if svc else "予約")),
    )
    await write_audit_log(
        db,
        request,
        action="booking.booking_calendar_resynced",
        org_slug=org.slug,
        target_type="booking",
        target_id=b.id,
        detail={"ok": ok, "staff_id": b.staff_id, "google_event_id": b.google_event_id},
    )
    await db.commit()
    return {
        "ok": ok,
        "booking_id": b.id,
        "google_event_id": b.google_event_id,
        "google_calendar_synced_at": (
            b.google_calendar_synced_at.isoformat() if b.google_calendar_synced_at else None
        ),
        "google_calendar_sync_error": b.google_calendar_sync_error,
    }


@router.get("/api/booking/oauth/google/status")
async def oauth_google_status(settings: SettingsDep) -> dict[str, Any]:
    """公開情報のみ（シークレットは出さない）。UI で OAuth 設定状況とリダイレクト URI を表示する用。"""
    cid = (settings.google_oauth_client_id or "").strip()
    rid = settings.google_oauth_redirect_uri_value()
    has_secret = bool((settings.google_oauth_client_secret or "").strip())
    ready = settings.is_google_oauth_configured()
    public_base = settings.public_base_url_value()
    suggested_redirect = f"{public_base}/api/booking/oauth/google/callback"
    missing: list[str] = []
    if not cid:
        missing.append("GOOGLE_OAUTH_CLIENT_ID")
    if not has_secret:
        missing.append("GOOGLE_OAUTH_CLIENT_SECRET")
    if not rid:
        missing.append("GOOGLE_OAUTH_REDIRECT_URI")
    display_redirect = rid or suggested_redirect
    env_snippet = (
        "# Google OAuth（カレンダー連携）\n"
        "GOOGLE_OAUTH_CLIENT_ID=your-client-id.apps.googleusercontent.com\n"
        "GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret\n"
        f"GOOGLE_OAUTH_REDIRECT_URI={display_redirect}\n"
    )
    return {
        "google_oauth_ready": ready,
        "client_id": cid,
        "redirect_uri": rid,
        "has_client_secret": has_secret,
        "missing": missing,
        "suggested_redirect_uri": suggested_redirect,
        "display_redirect_uri": display_redirect,
        "env_snippet": env_snippet,
        "console_credentials_url": "https://console.cloud.google.com/apis/credentials",
    }


@router.get("/api/booking/oauth/google/start")
async def oauth_google_start(
    request: Request,
    staff_id: int,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    st = await db.get(StaffMember, staff_id)
    if not st:
        raise HTTPException(404, "staff not found")
    org = await db.get(BookingOrg, st.org_id)
    if not org:
        raise HTTPException(404, "org not found")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    if not settings.google_oauth_client_id or not settings.google_oauth_redirect_uri_value():
        raise HTTPException(503, "Google OAuth not configured")
    try:
        url = google_calendar_authorization_url(staff_id, settings)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    return {"authorization_url": url}


@router.post("/api/booking/admin/oauth-google-link")
async def admin_oauth_google_link(
    request: Request,
    body: OAuthLinkRequest,
    db: DbSession,
    settings: SettingsDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """署名付き URL を返す。ブラウザで開くと Google ログインへ進む（カレンダー画面のワンクリック連携用）。"""
    staff = await db.get(StaffMember, body.staff_id)
    if not staff:
        raise HTTPException(404, "staff not found")
    org = await db.get(BookingOrg, staff.org_id)
    if not org:
        raise HTTPException(404, "org not found")
    await ensure_booking_admin(request, settings, db, x_admin_secret, org_slug=org.slug)
    if not settings.google_oauth_client_id or not settings.google_oauth_redirect_uri_value():
        raise HTTPException(503, "Google OAuth not configured")
    try:
        url = google_calendar_authorization_url(body.staff_id, settings)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    return {"url": url}


@router.get("/api/booking/oauth/google/authorize")
async def oauth_google_authorize_redirect(
    staff_id: int,
    ts: int,
    sig: str,
    db: DbSession,
    settings: SettingsDep,
) -> RedirectResponse:
    """署名を検証し、Google の OAuth 画面へ 303 リダイレクト（ヘッダ不要）。"""
    if not settings.booking_admin_secret.strip():
        raise HTTPException(503, "Admin secret not configured")
    if not verify_staff_oauth_link(staff_id, ts, sig, settings.booking_admin_secret):
        raise HTTPException(401, "invalid or expired link; reload calendar and try again")
    staff = await db.get(StaffMember, staff_id)
    if not staff:
        raise HTTPException(404, "staff not found")
    if not settings.google_oauth_client_id or not settings.google_oauth_redirect_uri_value():
        raise HTTPException(503, "Google OAuth not configured")
    try:
        url = google_calendar_authorization_url(staff_id, settings)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    return RedirectResponse(url=url, status_code=303)


@router.get("/api/booking/oauth/google/callback")
async def oauth_google_callback(
    db: DbSession,
    settings: SettingsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    base = settings.public_base_url_value()
    cal = f"{base}/app/calendar"

    def redirect_err(reason: str, detail: str = "") -> RedirectResponse:
        q = urlencode({"google_oauth": "err", "reason": reason, "detail": detail[:200]})
        return RedirectResponse(url=f"{cal}?{q}", status_code=303)

    if error:
        logger.warning("Google OAuth error: %s %s", error, error_description or "")
        return redirect_err(error, error_description or "")
    if not code or not state:
        return redirect_err("missing_code")
    try:
        staff_id = int(state)
    except ValueError:
        return redirect_err("bad_state")
    staff = await db.get(StaffMember, staff_id)
    if not staff:
        return redirect_err("staff_not_found")
    if not settings.google_oauth_client_secret:
        return redirect_err("oauth_not_configured")
    cid = settings.google_oauth_client_id.strip()
    csec = settings.google_oauth_client_secret.strip()
    ruri = settings.google_oauth_redirect_uri_value()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": cid,
                "client_secret": csec,
                "redirect_uri": ruri,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        detail = r.text[:500]
        try:
            err_json = r.json()
            detail = (
                err_json.get("error_description")
                or err_json.get("error")
                or detail
            )
            if isinstance(detail, str):
                detail = detail[:500]
            else:
                detail = str(detail)[:500]
        except Exception:
            pass
        logger.error("OAuth token error status=%s body=%s", r.status_code, r.text[:800])
        return redirect_err("token_exchange", detail if isinstance(detail, str) else str(detail))
    data = r.json()
    new_rt = data.get("refresh_token")
    access_token = (data.get("access_token") or "").strip()
    if new_rt:
        staff.google_refresh_token = encrypt_secret(new_rt, settings)
    elif not _staff_google_refresh_token(staff, settings):
        logger.warning(
            "OAuth response had no refresh_token; user may need to revoke app access and reconnect"
        )
        return redirect_err(
            "no_refresh_token",
            "Googleアカウントの「アプリへのアクセス」から当該アプリを削除し、もう一度連携してください。",
        )
    if access_token:
        try:
            async with httpx.AsyncClient(timeout=15.0) as uclient:
                ur = await uclient.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            if ur.status_code == 200:
                uj = ur.json()
                em = (uj.get("email") or "").strip()
                nm = (uj.get("name") or uj.get("given_name") or "").strip()
                if em:
                    staff.google_profile_email = encrypt_secret(em[:320], settings)
                    staff.email = em[:320]
                if nm:
                    staff.google_profile_name = nm[:256]
                    staff.name = nm[:256]
        except Exception as ex:
            logger.warning("Google userinfo fetch failed: %s", ex)
    await write_audit_log(
        db,
        None,
        action="booking.google_oauth_connected",
        org_slug=None,
        target_type="staff",
        target_id=staff.id,
        detail={"email": _staff_google_profile_email(staff, settings) or staff.email or "", "name": staff.google_profile_name or staff.name or ""},
    )
    write_ok, write_err = await verify_calendar_write_access_detailed(
        _staff_google_refresh_token(staff, settings),
        staff.google_calendar_id,
        settings,
    )
    await db.commit()
    if not write_ok:
        return RedirectResponse(
            url=f"{cal}?google_oauth=err&reason=write_check_failed&detail={urlencode({'d': (write_err or 'Google Calendar write check failed')[:180]})[2:]}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"{cal}?google_oauth=ok&staff_id={staff_id}",
        status_code=303,
    )
