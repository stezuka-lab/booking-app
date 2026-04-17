from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import unquote

import pytest

from app.booking.db_models import Booking, BookingOrg, BookingService, PublicBookingLink, StaffMember
from app.booking.routing_service import db_booking_busy_intervals_for_staff
from app.booking.router import (
    _delete_staff_calendar_event_if_present,
    _finalize_confirmed_booking,
    _public_booking_response,
    _release_bookings_with_missing_google_events,
    _release_stale_synced_bookings_without_google_busy,
    _release_unsynced_orphan_bookings,
    _sync_booking_to_staff_calendar,
)
from app.booking.router import manage_info
from app.booking.email_booking import build_booking_confirmation_email_body
from app.config import get_settings
from app.security.crypto import encrypt_secret


def test_booking_endpoint_survives_finalize_failure(client, monkeypatch) -> None:
    import app.booking.router as booking_router

    health = client.get("/health")
    assert health.status_code == 200
    token = (health.json().get("booking_demo") or {}).get("token")
    assert token

    now = datetime.now(timezone.utc)
    avail = client.get(
        f"/api/booking/links/{token}/availability",
        params={
            "from_ts": now.isoformat(),
            "to_ts": (now + timedelta(days=7)).isoformat(),
            "service_id": 1,
        },
    )
    assert avail.status_code == 200
    slots = (avail.json() or {}).get("slots") or []
    assert slots
    slot = slots[0]

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(booking_router, "_finalize_confirmed_booking", boom)
    async def always_free(*args, **kwargs):
        return True

    monkeypatch.setattr(booking_router, "staff_is_free", always_free)
    response = client.post(
        f"/api/booking/links/{token}/book",
        json={
            "link_token": token,
            "staff_id": slot["staff_id"],
            "start_utc": slot["start_utc"],
            "customer_name": "Finalize Failure Test",
            "customer_email": "finalize-failure@example.com",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["customer_calendar_added"] is False


def test_release_bookings_with_missing_google_events(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()
    staff = StaffMember(
        id=4,
        org_id=1,
        name="担当A",
        google_refresh_token=encrypt_secret("refresh-token", settings),
        google_calendar_id="primary",
    )
    booking = Booking(
        id=99,
        org_id=1,
        staff_id=4,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        google_event_id="evt-missing",
        manage_token="manage-token",
    )

    class DummySession:
        async def scalars(self, _query):
            class Result:
                def all(self_inner):
                    return [booking]

            return Result()

        async def flush(self):
            return None

    async def fake_event_status(*args, **kwargs):
        return False, None

    monkeypatch.setattr(booking_router, "get_calendar_event_status", fake_event_status)

    released = asyncio.run(
        _release_bookings_with_missing_google_events(
            DummySession(),
            settings,
            [staff],
            datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2030, 1, 2, 0, 0, tzinfo=timezone.utc),
        )
    )

    assert released == 1
    assert booking.status == "cancelled"
    assert booking.google_event_id is None


def test_link_availability_checks_missing_google_events_on_open(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()
    booking_router._PUBLIC_AVAILABILITY_CACHE.clear()
    captured: dict[str, object] = {}
    org = BookingOrg(id=1, name="Test Org", slug="test-org", availability_defaults_json={})
    link = PublicBookingLink(id=10, org_id=1, token="tok-1", title="初回相談", service_id=1, active=True)
    service = BookingService(id=1, org_id=1, name="初回相談", duration_minutes=30)
    staff = StaffMember(id=4, org_id=1, name="担当A", email="a@example.com", google_refresh_token="refresh")

    class DummyDb:
        def __init__(self) -> None:
            self.committed = False

        async def scalar(self, _query):
            return link

        async def get(self, model, _id):
            if model is BookingOrg:
                return org
            if model is BookingService:
                return service
            return None

        async def commit(self):
            self.committed = True

    async def fake_release(db, settings, staff_list, range_start, range_end):
        captured["staff_count"] = len(staff_list)
        captured["range_start"] = range_start
        captured["range_end"] = range_end
        return 1

    async def fake_resolve_valid_link_staff_ids(*args, **kwargs):
        return [staff.id]

    async def fake_eligible_staff(*args, **kwargs):
        return [staff]

    async def fake_load_google_busy_map(*args, **kwargs):
        return {}, {}

    async def fake_db_busy_map(*args, **kwargs):
        return {}

    async def fake_available_slots(*args, **kwargs):
        return [], 30, False, None

    monkeypatch.setattr(booking_router, "_release_bookings_with_missing_google_events", fake_release)
    monkeypatch.setattr(booking_router, "_resolve_valid_link_staff_ids", fake_resolve_valid_link_staff_ids)
    monkeypatch.setattr(booking_router, "eligible_staff", fake_eligible_staff)
    monkeypatch.setattr(booking_router, "_load_google_busy_map", fake_load_google_busy_map)
    monkeypatch.setattr(booking_router, "_db_booking_intervals_map_for_staff", fake_db_busy_map)
    monkeypatch.setattr(booking_router, "available_slots_for_link", fake_available_slots)

    now = datetime.now(timezone.utc)
    db = DummyDb()
    body = asyncio.run(
        booking_router.link_availability(
            "tok-1",
            db,
            settings,
            now,
            now + timedelta(days=7),
            1,
        )
    )

    assert body["slots"] == []
    assert captured["staff_count"] == 1
    assert captured["range_start"] is not None
    assert captured["range_end"] is not None
    assert db.committed is True


def test_link_availability_cache_hit_still_checks_missing_google_events(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()
    booking_router._PUBLIC_AVAILABILITY_CACHE.clear()
    captured: dict[str, object] = {}
    org = BookingOrg(id=1, name="Test Org", slug="test-org", availability_defaults_json={})
    link = PublicBookingLink(id=10, org_id=1, token="tok-1", title="初回相談", service_id=1, active=True)
    service = BookingService(id=1, org_id=1, name="初回相談", duration_minutes=30)
    staff = StaffMember(id=4, org_id=1, name="担当A", email="a@example.com", google_refresh_token="refresh")

    class DummyDb:
        def __init__(self) -> None:
            self.committed = False

        async def scalar(self, _query):
            return link

        async def get(self, model, _id):
            if model is BookingOrg:
                return org
            if model is BookingService:
                return service
            return None

        async def commit(self):
            self.committed = True

    async def fake_release(db, settings, staff_list, range_start, range_end):
        captured["staff_count"] = len(staff_list)
        return 0

    async def fake_resolve_valid_link_staff_ids(*args, **kwargs):
        return [staff.id]

    async def fake_eligible_staff(*args, **kwargs):
        return [staff]

    monkeypatch.setattr(booking_router, "_release_bookings_with_missing_google_events", fake_release)
    monkeypatch.setattr(booking_router, "_resolve_valid_link_staff_ids", fake_resolve_valid_link_staff_ids)
    monkeypatch.setattr(booking_router, "eligible_staff", fake_eligible_staff)

    now = datetime.now(timezone.utc)
    booking_router._set_cached_public_availability(
        "tok-1",
        now,
        now + timedelta(days=7),
        1,
        {"slots": [], "busy_intervals": [], "blocked_dates": [], "cached": False},
        settings,
    )
    db = DummyDb()
    body = asyncio.run(
        booking_router.link_availability(
            "tok-1",
            db,
            settings,
            now,
            now + timedelta(days=7),
            1,
        )
    )

    assert body["cached"] is True
    assert captured["staff_count"] == 1
    assert db.committed is False


def test_db_busy_intervals_ignore_pending_for_auto_confirm_org(client) -> None:
    from app.db import get_session_factory
    import secrets

    async def run() -> None:
        Session = get_session_factory()
        async with Session() as session:
            org = BookingOrg(
                name="Auto Org",
                slug=f"auto-org-pending-ignore-{secrets.token_hex(4)}",
                auto_confirm=True,
                availability_defaults_json={},
                cancel_policy_json={},
            )
            staff = StaffMember(org=org, name="担当A", email="a@example.com", active=True)
            session.add_all([org, staff])
            await session.flush()
            booking = Booking(
                org_id=org.id,
                staff_id=staff.id,
                service_id=None,
                start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
                end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
                status="pending",
                customer_name="Customer",
                customer_email="customer@example.com",
                manage_token="pending-ignore",
            )
            session.add(booking)
            await session.commit()

            intervals = await db_booking_busy_intervals_for_staff(
                session,
                staff.id,
                datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc),
                datetime(2030, 1, 2, 0, 0, tzinfo=timezone.utc),
            )
            assert intervals == []

    asyncio.run(run())


def test_release_unsynced_orphan_bookings(monkeypatch) -> None:
    settings = get_settings()
    staff = StaffMember(
        id=4,
        org_id=1,
        name="手塚眞司",
        google_refresh_token=encrypt_secret("refresh-token", settings),
        google_calendar_id="primary",
    )
    booking = Booking(
        id=101,
        org_id=1,
        staff_id=4,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 7, 30, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 8, 30, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        google_event_id=None,
        google_calendar_sync_error="sync failed",
        created_at=datetime(2025, 12, 31, 0, 0, tzinfo=timezone.utc),
        manage_token="orphan-token",
    )

    class DummySession:
        async def scalars(self, _query):
            class Result:
                def all(self_inner):
                    return [booking]

            return Result()

        async def flush(self):
            return None

    released = asyncio.run(
        _release_unsynced_orphan_bookings(
            DummySession(),
            settings,
            [staff],
            datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2030, 1, 2, 0, 0, tzinfo=timezone.utc),
            {4: []},
            {},
        )
    )

    assert released == 1
    assert booking.status == "cancelled"


def test_release_unsynced_orphan_bookings_without_error_text(monkeypatch) -> None:
    settings = get_settings()
    staff = StaffMember(
        id=5,
        org_id=1,
        name="手塚眞司",
        google_refresh_token=encrypt_secret("refresh-token", settings),
        google_calendar_id="primary",
    )
    booking = Booking(
        id=102,
        org_id=1,
        staff_id=5,
        service_id=1,
        start_utc=datetime(2030, 1, 2, 7, 30, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 2, 8, 30, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        google_event_id=None,
        google_calendar_synced_at=None,
        google_calendar_sync_error=None,
        created_at=datetime(2025, 12, 31, 0, 0, tzinfo=timezone.utc),
        manage_token="orphan-token-2",
    )

    class DummySession:
        async def scalars(self, _query):
            class Result:
                def all(self_inner):
                    return [booking]

            return Result()

        async def flush(self):
            return None

    released = asyncio.run(
        _release_unsynced_orphan_bookings(
            DummySession(),
            settings,
            [staff],
            datetime(2030, 1, 2, 0, 0, tzinfo=timezone.utc),
            datetime(2030, 1, 3, 0, 0, tzinfo=timezone.utc),
            {5: []},
            {},
        )
    )

    assert released == 1
    assert booking.status == "cancelled"
    assert "自動で解放しました" in (booking.google_calendar_sync_error or "")


def test_release_stale_synced_bookings_without_google_busy(monkeypatch) -> None:
    settings = get_settings()
    staff = StaffMember(
        id=6,
        org_id=1,
        name="手塚眞司",
        google_refresh_token=encrypt_secret("refresh-token", settings),
        google_calendar_id="primary",
    )
    booking = Booking(
        id=103,
        org_id=1,
        staff_id=6,
        service_id=1,
        start_utc=datetime(2030, 1, 3, 7, 30, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 3, 8, 30, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        google_event_id="evt-stale",
        google_calendar_synced_at=datetime(2025, 12, 31, 0, 0, tzinfo=timezone.utc),
        google_calendar_sync_error=None,
        created_at=datetime(2025, 12, 31, 0, 0, tzinfo=timezone.utc),
        manage_token="stale-synced-token",
    )

    class DummySession:
        async def scalars(self, _query):
            class Result:
                def all(self_inner):
                    return [booking]

            return Result()

        async def flush(self):
            return None

    released = asyncio.run(
        _release_stale_synced_bookings_without_google_busy(
            DummySession(),
            settings,
            [staff],
            datetime(2030, 1, 3, 0, 0, tzinfo=timezone.utc),
            datetime(2030, 1, 4, 0, 0, tzinfo=timezone.utc),
            {6: []},
            {},
        )
    )

    assert released == 1
    assert booking.status == "cancelled"
    assert booking.google_event_id is None
    assert "自動で解放" in (booking.google_calendar_sync_error or "")


def test_public_availability_survives_busy_union_failure(client, monkeypatch) -> None:
    import app.booking.router as booking_router

    health = client.get("/health")
    assert health.status_code == 200
    token = (health.json().get("booking_demo") or {}).get("token")
    assert token

    async def boom(*args, **kwargs):
        raise RuntimeError("busy-union boom")

    monkeypatch.setattr(booking_router, "busy_intervals_union_for_link", boom)

    now = datetime.now(timezone.utc)
    response = client.get(
        f"/api/booking/links/{token}/availability",
        params={
            "from_ts": now.isoformat(),
            "to_ts": (now + timedelta(days=7)).isoformat(),
            "service_id": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "busy_intervals" in body
    assert body.get("availability_error") in (None, "")


def test_public_availability_survives_single_slot_pick_failure(client, monkeypatch) -> None:
    import app.booking.routing_service as routing_service

    health = client.get("/health")
    assert health.status_code == 200
    token = (health.json().get("booking_demo") or {}).get("token")
    assert token

    original = routing_service.pick_staff_for_slot
    state = {"calls": 0}

    async def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("slot-eval boom")
        return await original(*args, **kwargs)

    monkeypatch.setattr(routing_service, "pick_staff_for_slot", flaky)

    now = datetime.now(timezone.utc)
    response = client.get(
        f"/api/booking/links/{token}/availability",
        params={
            "from_ts": now.isoformat(),
            "to_ts": (now + timedelta(days=7)).isoformat(),
            "service_id": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body.get("availability_error") in (None, "")
    assert isinstance(body.get("slots"), list)


def test_public_availability_survives_malformed_google_busy_interval(client, monkeypatch) -> None:
    import app.booking.routing_service as routing_service

    health = client.get("/health")
    assert health.status_code == 200
    token = (health.json().get("booking_demo") or {}).get("token")
    assert token

    async def bad_busy_map(*args, **kwargs):
        staff_list = args[0] if args else []
        if not staff_list:
            return {}
        return {staff_list[0].id: [("bad", "interval")]}

    monkeypatch.setattr(routing_service, "_load_google_busy_map", bad_busy_map)

    now = datetime.now(timezone.utc)
    response = client.get(
        f"/api/booking/links/{token}/availability",
        params={
            "from_ts": now.isoformat(),
            "to_ts": (now + timedelta(days=7)).isoformat(),
            "service_id": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body.get("availability_error") in (None, "")
    assert isinstance(body.get("slots"), list)


def test_public_availability_falls_back_to_open_hours_when_slots_empty(client, monkeypatch) -> None:
    import app.booking.router as booking_router
    import app.booking.routing_service as routing_service

    health = client.get("/health")
    assert health.status_code == 200
    token = (health.json().get("booking_demo") or {}).get("token")
    assert token

    async def fake_slots(*args, **kwargs):
        return [], 30, True, "slot_pick_failed"

    async def fake_busy(*args, **kwargs):
        return []

    monkeypatch.setattr(booking_router, "available_slots_for_link", fake_slots)
    monkeypatch.setattr(booking_router, "busy_intervals_union_for_link", fake_busy)

    now = datetime.now(timezone.utc)
    response = client.get(
        f"/api/booking/links/{token}/availability",
        params={
            "from_ts": now.isoformat(),
            "to_ts": (now + timedelta(days=7)).isoformat(),
            "service_id": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body.get("availability_error") in (None, "")
    assert body.get("slots")
    warning = (((body.get("calendar_integration") or {}).get("warning_ja")) or "")
    assert "受付時間ベース" in warning


def test_public_availability_fallback_respects_blocked_dates(client, monkeypatch) -> None:
    import app.booking.router as booking_router

    health = client.get("/health")
    assert health.status_code == 200
    token = (health.json().get("booking_demo") or {}).get("token")
    assert token

    async def fake_slots(*args, **kwargs):
        return [], 30, True, "slot_pick_failed"

    async def fake_busy(*args, **kwargs):
        return []

    monkeypatch.setattr(booking_router, "available_slots_for_link", fake_slots)
    monkeypatch.setattr(booking_router, "busy_intervals_union_for_link", fake_busy)

    response = client.get(
        f"/api/booking/links/{token}/availability",
        params={
            "from_ts": "2026-04-08T00:00:00+09:00",
            "to_ts": "2026-04-15T00:00:00+09:00",
            "service_id": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    blocked = set(body.get("blocked_dates") or [])
    slots = body.get("slots") or []
    jst = ZoneInfo("Asia/Tokyo")
    for slot in slots:
        start_utc = datetime.fromisoformat(str(slot.get("start_utc")))
        assert start_utc.astimezone(jst).date().isoformat() not in blocked


def test_load_google_busy_map_reports_per_staff_errors(monkeypatch) -> None:
    import app.booking.routing_service as routing_service

    settings = get_settings()
    staff = StaffMember(id=10, name="担当", google_refresh_token=encrypt_secret("rtok", settings))

    async def fake_freebusy(*args, **kwargs):
        raise RuntimeError("google boom")

    monkeypatch.setattr(routing_service, "freebusy_busy_intervals", fake_freebusy)

    gmap, errors = asyncio.run(
        routing_service._load_google_busy_map(
            [staff],
            datetime(2026, 4, 8, tzinfo=timezone.utc),
            datetime(2026, 4, 9, tzinfo=timezone.utc),
            settings,
        )
    )

    assert gmap == {10: []}
    assert errors == {10: "google boom"}


def test_finalize_confirmed_booking_creates_event_without_attendees(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()
    captured: dict[str, object] = {}

    class DummySession:
        async def flush(self) -> None:
            return None

    async def fake_create_event_for_booking(*args, **kwargs):
        captured["attendees_emails"] = kwargs.get("attendees_emails")
        captured["description"] = kwargs.get("description")
        captured["location"] = kwargs.get("location")
        return {"id": "evt-1"}

    async def fake_create_event_for_booking_detailed(*args, **kwargs):
        captured["attendees_emails"] = kwargs.get("attendees_emails")
        captured["description"] = kwargs.get("description")
        captured["location"] = kwargs.get("location")
        return {"id": "evt-1"}, None

    async def fake_send_booking_emails(*args, **kwargs):
        return {
            "customer": False,
            "staff": False,
            "customer_error": "SMTP temporary failure",
            "staff_error": None,
        }

    monkeypatch.setattr(booking_router, "create_event_for_booking_detailed", fake_create_event_for_booking_detailed)
    monkeypatch.setattr(booking_router, "send_booking_emails", fake_send_booking_emails)

    org = BookingOrg(name="Test Org", slug="test-org", availability_defaults_json={})
    staff = StaffMember(
        org_id=1,
        name="担当A",
        email="staff-a@example.com",
        google_refresh_token="refresh-token",
        zoom_meeting_url="https://zoom.example/staff-a",
    )
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        company_name="Acme Corp",
        form_answers_json={"customer_number": "AP12345"},
        meeting_provider="zoom",
        manage_token="manage-token",
    )

    customer_calendar_added = asyncio.run(
        _finalize_confirmed_booking(
            DummySession(),
            settings,
            booking,
            staff,
            org,
            "初回相談",
            booking_link_title="初回予約リンク",
            post_booking_message="開始5分前までにご準備ください。",
        )
    )

    assert customer_calendar_added is False
    assert booking.google_event_id == "evt-1"
    assert booking.google_calendar_sync_error is None
    assert booking.google_calendar_synced_at is not None
    assert captured["attendees_emails"] is None
    assert captured["location"] == "https://zoom.example/staff-a"
    assert "Zoom URL: https://zoom.example/staff-a" in str(captured["description"])
    assert "ご案内: 開始5分前までにご準備ください。" in str(captured["description"])
    assert "予約者: Customer" in str(captured["description"])
    assert "メール: customer@example.com" in str(captured["description"])
    assert "顧客番号: AP12345" in str(captured["description"])
    assert booking.customer_name == ""
    assert booking.customer_email == ""
    assert booking.company_name is None
    assert booking.form_answers_json == {}
    assert booking.customer_confirmation_email_sent_at is None
    assert booking.customer_confirmation_email_error == "SMTP temporary failure"
    assert booking.customer_confirmation_email_last_attempt_at is not None


def test_finalize_confirmed_booking_records_calendar_sync_error_when_unlinked(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()

    class DummySession:
        async def flush(self) -> None:
            return None

    async def fake_send_booking_emails(*args, **kwargs):
        return {"customer": True, "staff": True, "customer_error": None, "staff_error": None}

    monkeypatch.setattr(booking_router, "send_booking_emails", fake_send_booking_emails)

    org = BookingOrg(name="Test Org", slug="test-org", availability_defaults_json={})
    staff = StaffMember(
        org_id=1,
        name="担当A",
        email="staff-a@example.com",
        google_refresh_token=None,
    )
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        meeting_provider="none",
        manage_token="manage-token",
    )

    asyncio.run(
        _finalize_confirmed_booking(
            DummySession(),
            settings,
            booking,
            staff,
            org,
            "初回相談",
            booking_link_title="初回予約リンク",
        )
    )

    assert booking.google_event_id is None
    assert booking.google_calendar_synced_at is None
    assert booking.google_calendar_sync_error == "担当のGoogleカレンダー連携が未設定です"
    assert booking.customer_name == ""
    assert booking.customer_email == ""


def test_delete_staff_calendar_event_clears_google_event_id(monkeypatch) -> None:
    import app.booking.router as booking_router

    captured: dict[str, object] = {}

    async def fake_delete_event_for_booking(refresh_token, calendar_id, event_id, settings):
        captured["refresh_token"] = refresh_token
        captured["calendar_id"] = calendar_id
        captured["event_id"] = event_id
        return None

    monkeypatch.setattr(booking_router, "delete_event_for_booking", fake_delete_event_for_booking)

    staff = StaffMember(
        org_id=1,
        name="担当A",
        google_refresh_token="refresh-token",
        google_calendar_id="primary",
    )
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        google_event_id="evt-123",
    )

    deleted = asyncio.run(
        _delete_staff_calendar_event_if_present(
            booking,
            staff,
            get_settings(),
        )
    )

    assert deleted is True
    assert captured["event_id"] == "evt-123"
    assert booking.google_event_id is None


def test_manage_info_cancels_when_google_event_missing(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()
    staff = StaffMember(
        id=4,
        org_id=1,
        name="担当A",
        google_refresh_token=encrypt_secret("refresh-token", settings),
        google_calendar_id="primary",
    )
    booking = Booking(
        id=99,
        org_id=1,
        staff_id=4,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        google_event_id="evt-missing",
        manage_token="manage-token",
    )
    org = BookingOrg(id=1, name="Test Org", slug="test-org", availability_defaults_json={})

    class DummySession:
        def __init__(self) -> None:
            self.committed = False

        async def scalar(self, _query):
            return booking

        async def get(self, model, _id):
            if model is StaffMember:
                return staff
            if model is BookingOrg:
                return org
            return None

        async def commit(self):
            self.committed = True

    async def fake_event_status(*args, **kwargs):
        return False, None

    monkeypatch.setattr(booking_router, "get_calendar_event_status", fake_event_status)

    body = asyncio.run(manage_info("manage-token", DummySession(), settings))

    assert body["booking"]["status"] == "cancelled"
    assert booking.google_event_id is None


def test_public_booking_response_includes_post_booking_message() -> None:
    settings = get_settings()
    org = BookingOrg(name="Test Org", slug="test-org", availability_defaults_json={})
    staff = StaffMember(org_id=1, name="担当A", email="staff-a@example.com")
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        meeting_url="https://zoom.example/staff-a",
        manage_token="manage-token",
    )

    response = _public_booking_response(
        settings,
        org,
        booking,
        staff,
        "初回相談",
        booking_link_title="初回予約リンク",
        customer_calendar_added=False,
        post_booking_message="開始5分前までにご準備ください。",
    )

    assert response["post_booking_message"] == "開始5分前までにご準備ください。"
    assert "開始5分前までにご準備ください。" in unquote(response["google_calendar_add_url"])


def test_booking_confirmation_email_body_includes_post_booking_message() -> None:
    settings = get_settings()
    org = BookingOrg(name="Test Org", slug="test-org", availability_defaults_json={})
    staff = StaffMember(org_id=1, name="担当A", email="staff-a@example.com")
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        meeting_url="https://zoom.example/staff-a",
        manage_token="manage-token",
    )

    _subject, body = build_booking_confirmation_email_body(
        settings,
        org,
        booking,
        staff,
        "初回予約リンク",
        manage_url="https://example.com/app/manage/manage-token",
        email_settings={},
        post_booking_message="開始5分前までにご準備ください。",
    )

    assert "開始5分前までにご準備ください。" in body


def test_finalize_confirmed_booking_handles_encrypted_customer_fields(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()
    captured: dict[str, object] = {}

    class DummySession:
        async def flush(self) -> None:
            return None

    async def fake_create_event_for_booking_detailed(*args, **kwargs):
        captured["description"] = kwargs.get("description")
        return {"id": "evt-enc"}, None

    async def fake_send_booking_emails(*args, **kwargs):
        return {"customer": True, "staff": True, "customer_error": None, "staff_error": None}

    monkeypatch.setattr(booking_router, "create_event_for_booking_detailed", fake_create_event_for_booking_detailed)
    monkeypatch.setattr(booking_router, "send_booking_emails", fake_send_booking_emails)

    org = BookingOrg(name="Test Org", slug="test-org", availability_defaults_json={})
    staff = StaffMember(
        org_id=1,
        name="担当A",
        email="staff-a@example.com",
        google_refresh_token="refresh-token",
        zoom_meeting_url=encrypt_secret("https://zoom.example/staff-a", settings),
    )
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name=encrypt_secret("Encrypted Customer", settings),
        customer_email=encrypt_secret("encrypted@example.com", settings),
        meeting_provider="zoom",
        manage_token="manage-token",
    )

    asyncio.run(
        _finalize_confirmed_booking(
            DummySession(),
            settings,
            booking,
            staff,
            org,
            "初回相談",
            booking_link_title="初回予約リンク",
        )
    )

    assert "予約者: Encrypted Customer" in str(captured["description"])
    assert "メール: encrypted@example.com" in str(captured["description"])
    assert booking.customer_name == ""
    assert booking.customer_email == ""


def test_finalize_confirmed_booking_scrubs_even_when_email_send_raises(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()

    class DummySession:
        async def flush(self) -> None:
            return None

    async def fake_create_event_for_booking_detailed(*args, **kwargs):
        return {"id": "evt-exc"}, None

    async def fake_send_booking_emails(*args, **kwargs):
        raise RuntimeError("mail exploded")

    monkeypatch.setattr(booking_router, "create_event_for_booking_detailed", fake_create_event_for_booking_detailed)
    monkeypatch.setattr(booking_router, "send_booking_emails", fake_send_booking_emails)

    org = BookingOrg(name="Test Org", slug="test-org", availability_defaults_json={})
    staff = StaffMember(
        org_id=1,
        name="担当A",
        email="staff-a@example.com",
        google_refresh_token="refresh-token",
    )
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        company_name="Acme Corp",
        form_answers_json={"customer_number": "AP12345"},
        manage_token="manage-token",
    )

    with pytest.raises(RuntimeError):
        asyncio.run(
            _finalize_confirmed_booking(
                DummySession(),
                settings,
                booking,
                staff,
                org,
                "初回相談",
                booking_link_title="初回予約リンク",
            )
        )

    assert booking.customer_name == ""
    assert booking.customer_email == ""
    assert booking.company_name is None
    assert booking.form_answers_json == {}


def test_sync_booking_to_staff_calendar_recreates_event(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()
    captured: dict[str, object] = {}

    class DummySession:
        async def flush(self) -> None:
            return None

    async def fake_delete_event_for_booking(*args, **kwargs):
        captured["deleted_event_id"] = args[2]
        return None

    async def fake_create_event_for_booking_detailed(*args, **kwargs):
        captured["description"] = kwargs.get("description")
        return {"id": "evt-resync"}, None

    monkeypatch.setattr(booking_router, "delete_event_for_booking", fake_delete_event_for_booking)
    monkeypatch.setattr(booking_router, "create_event_for_booking_detailed", fake_create_event_for_booking_detailed)

    org = BookingOrg(name="Test Org", slug="test-org", availability_defaults_json={})
    staff = StaffMember(
        org_id=1,
        name="担当A",
        email="staff-a@example.com",
        google_refresh_token="refresh-token",
        zoom_meeting_url=encrypt_secret("https://zoom.example/staff-a", settings),
    )
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        meeting_provider="zoom",
        google_event_id="evt-old",
        manage_token="manage-token",
    )

    ok = asyncio.run(
        _sync_booking_to_staff_calendar(
            DummySession(),
            settings,
            booking,
            staff,
            org,
            service_name="初回相談",
            booking_link_title="初回予約リンク",
        )
    )

    assert ok is True
    assert captured["deleted_event_id"] == "evt-old"
    assert booking.google_event_id == "evt-resync"
    assert booking.google_calendar_sync_error is None
    assert booking.google_calendar_synced_at is not None


def test_sync_booking_to_staff_calendar_records_error(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()

    class DummySession:
        async def flush(self) -> None:
            return None

    async def fake_create_event_for_booking_detailed(*args, **kwargs):
        return None, "sync failed"

    monkeypatch.setattr(booking_router, "create_event_for_booking_detailed", fake_create_event_for_booking_detailed)

    org = BookingOrg(name="Test Org", slug="test-org", availability_defaults_json={})
    staff = StaffMember(org_id=1, name="担当A", email="staff-a@example.com", google_refresh_token="refresh-token")
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        meeting_provider="none",
        manage_token="manage-token",
    )

    ok = asyncio.run(
        _sync_booking_to_staff_calendar(
            DummySession(),
            settings,
            booking,
            staff,
            org,
            service_name="初回相談",
            booking_link_title="初回予約リンク",
        )
    )

    assert ok is False
    assert booking.google_event_id is None
    assert booking.google_calendar_synced_at is None
    assert booking.google_calendar_sync_error == "sync failed"


def test_sync_booking_to_staff_calendar_retries_once(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()
    calls = {"count": 0}

    class DummySession:
        async def flush(self) -> None:
            return None

    async def fake_create_event_for_booking_detailed(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return None, "temporary sync failure"
        return {"id": "evt-retry"}, None

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(booking_router, "create_event_for_booking_detailed", fake_create_event_for_booking_detailed)
    monkeypatch.setattr(booking_router.asyncio, "sleep", fake_sleep)

    org = BookingOrg(name="Test Org", slug="test-org", availability_defaults_json={})
    staff = StaffMember(org_id=1, name="担当A", email="staff-a@example.com", google_refresh_token="refresh-token")
    booking = Booking(
        org_id=1,
        staff_id=1,
        service_id=1,
        start_utc=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        meeting_provider="none",
        manage_token="manage-token",
    )

    ok = asyncio.run(
        _sync_booking_to_staff_calendar(
            DummySession(),
            settings,
            booking,
            staff,
            org,
            service_name="初回相談",
            booking_link_title="初回予約リンク",
        )
    )

    assert ok is True
    assert calls["count"] == 2
    assert booking.google_event_id == "evt-retry"
    assert booking.google_calendar_sync_error is None
