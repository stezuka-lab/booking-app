from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# OAuth 同意時: カレンダー + 連携アカウント表示用プロフィール
GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def _credentials_from_refresh(refresh_token: str, settings: Settings) -> Any:
    from google.oauth2.credentials import Credentials

    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise RuntimeError("Google OAuth client is not configured")
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=GOOGLE_CALENDAR_SCOPES,
    )


def create_calendar_event_sync(
    refresh_token: str,
    calendar_id: str,
    summary: str,
    start_iso: str,
    end_iso: str,
    settings: Settings,
    *,
    with_meet: bool,
    attendees_emails: list[str] | None = None,
    description: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    from googleapiclient.discovery import build

    creds = _credentials_from_refresh(refresh_token, settings)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start_iso, "timeZone": "UTC"},
        "end": {"dateTime": end_iso, "timeZone": "UTC"},
    }
    if description:
        body["description"] = description[:8000]
    if location:
        body["location"] = location[:2000]
    if attendees_emails:
        body["attendees"] = [{"email": e.strip()} for e in attendees_emails if e and e.strip()]
    if with_meet:
        from app.booking.meeting_service import meet_conference_request_id

        body["conferenceData"] = {
            "createRequest": {
                "requestId": meet_conference_request_id(),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    extra: dict[str, Any] = {}
    if with_meet:
        extra["conferenceDataVersion"] = 1
    if body.get("attendees"):
        extra["sendUpdates"] = "all"
    ev = (
        service.events()
        .insert(calendarId=calendar_id or "primary", body=body, **extra)
        .execute()
    )
    return ev


def patch_calendar_event_sync(
    refresh_token: str,
    calendar_id: str,
    event_id: str,
    start_iso: str,
    end_iso: str,
    settings: Settings,
) -> dict[str, Any]:
    from googleapiclient.discovery import build

    creds = _credentials_from_refresh(refresh_token, settings)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    body = {
        "start": {"dateTime": start_iso, "timeZone": "UTC"},
        "end": {"dateTime": end_iso, "timeZone": "UTC"},
    }
    return (
        service.events()
        .patch(calendarId=calendar_id or "primary", eventId=event_id, body=body)
        .execute()
    )


def delete_calendar_event_sync(
    refresh_token: str,
    calendar_id: str,
    event_id: str,
    settings: Settings,
) -> None:
    from googleapiclient.discovery import build

    creds = _credentials_from_refresh(refresh_token, settings)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    service.events().delete(calendarId=calendar_id or "primary", eventId=event_id).execute()


def freebusy_busy_intervals_sync(
    refresh_token: str,
    calendar_id: str,
    time_min_iso: str,
    time_max_iso: str,
    settings: Settings,
) -> list[tuple[datetime, datetime]]:
    """Google FreeBusy で busy 区間を返す。失敗時は空リスト。"""
    from googleapiclient.discovery import build

    try:
        creds = _credentials_from_refresh(refresh_token, settings)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        cid = calendar_id or "primary"
        body = {
            "timeMin": time_min_iso,
            "timeMax": time_max_iso,
            "items": [{"id": cid}],
        }
        fb = service.freebusy().query(body=body).execute()
        cal = (fb.get("calendars") or {}).get(cid) or {}
        busy = cal.get("busy") or []
        out: list[tuple[datetime, datetime]] = []
        for b in busy:
            start_s = b.get("start") or ""
            end_s = b.get("end") or ""
            if not start_s or not end_s:
                continue
            out.append((_parse_google_time(start_s), _parse_google_time(end_s)))
        return out
    except Exception:
        logger.exception("Google FreeBusy failed")
        return []


def _parse_google_time(s: str) -> datetime:
    """FreeBusy の時刻を UTC に正規化（naive やオフセット付きを混在させない）。"""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def freebusy_busy_intervals(
    refresh_token: str | None,
    calendar_id: str | None,
    time_min_iso: str,
    time_max_iso: str,
    settings: Settings,
) -> list[tuple[datetime, datetime]]:
    if not refresh_token:
        return []
    return await asyncio.to_thread(
        freebusy_busy_intervals_sync,
        refresh_token,
        calendar_id or "primary",
        time_min_iso,
        time_max_iso,
        settings,
    )


async def create_event_for_booking(
    refresh_token: str | None,
    calendar_id: str | None,
    summary: str,
    start_iso: str,
    end_iso: str,
    settings: Settings,
    *,
    with_meet: bool,
    attendees_emails: list[str] | None = None,
    description: str | None = None,
    location: str | None = None,
) -> dict[str, Any] | None:
    if not refresh_token:
        logger.warning("No Google refresh token; skipping Calendar create")
        return None
    try:
        return await asyncio.to_thread(
            create_calendar_event_sync,
            refresh_token,
            calendar_id or "primary",
            summary,
            start_iso,
            end_iso,
            settings,
            with_meet=with_meet,
            attendees_emails=attendees_emails,
            description=description,
            location=location,
        )
    except Exception:
        logger.exception("Google Calendar create failed")
        return None


async def patch_event_for_booking(
    refresh_token: str | None,
    calendar_id: str | None,
    event_id: str | None,
    start_iso: str,
    end_iso: str,
    settings: Settings,
) -> dict[str, Any] | None:
    if not refresh_token or not event_id:
        return None
    try:
        return await asyncio.to_thread(
            patch_calendar_event_sync,
            refresh_token,
            calendar_id or "primary",
            event_id,
            start_iso,
            end_iso,
            settings,
        )
    except Exception:
        logger.exception("Google Calendar patch failed")
        return None


async def delete_event_for_booking(
    refresh_token: str | None,
    calendar_id: str | None,
    event_id: str | None,
    settings: Settings,
) -> None:
    if not refresh_token or not event_id:
        return
    try:
        await asyncio.to_thread(
            delete_calendar_event_sync,
            refresh_token,
            calendar_id or "primary",
            event_id,
            settings,
        )
    except Exception:
        logger.exception("Google Calendar delete failed")


async def insert_customer_primary_calendar_with_access_token(
    access_token: str,
    summary: str,
    start_iso: str,
    end_iso: str,
    *,
    description: str | None = None,
    location: str | None = None,
) -> dict[str, Any] | None:
    """顧客が OAuth で渡したアクセストークンで、本人の primary カレンダーに予定を1件追加。"""
    tok = (access_token or "").strip()
    if not tok or len(tok) > 8192:
        return None
    body: dict[str, Any] = {
        "summary": summary[:1024],
        "start": {"dateTime": start_iso, "timeZone": "UTC"},
        "end": {"dateTime": end_iso, "timeZone": "UTC"},
    }
    if description:
        body["description"] = description[:8000]
    if location:
        body["location"] = location[:2000]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                headers={"Authorization": f"Bearer {tok}"},
                json=body,
            )
        if r.status_code not in (200, 201):
            logger.warning(
                "Customer Calendar insert failed: status=%s body=%s",
                r.status_code,
                (r.text or "")[:500],
            )
            return None
        return r.json()
    except Exception:
        logger.exception("Customer Calendar insert failed")
        return None
