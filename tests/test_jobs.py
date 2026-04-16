from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.booking.db_models import Booking
from app.booking.jobs import _repeat_outreach, _retry_staff_calendar_syncs, _send_reminders
from app.config import Settings


class DummySession:
    def __init__(self, bookings):
        self._bookings = bookings
        self._orgs = {}
        self._staff = {}
        self._services = {}

    async def scalars(self, _query):
        class Result:
            def __init__(self, items):
                self._items = items

            def all(self):
                return self._items

        return Result(self._bookings)

    async def get(self, _model, _id):
        name = getattr(_model, "__name__", "")
        if name == "BookingOrg":
            return self._orgs.get(_id)
        if name == "StaffMember":
            return self._staff.get(_id)
        if name == "BookingService":
            return self._services.get(_id)
        return None


def test_send_reminders_is_disabled() -> None:
    booking = Booking(
        org_id=1,
        staff_id=None,
        service_id=1,
        start_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        end_utc=datetime.now(timezone.utc) + timedelta(hours=3),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        manage_token="manage-token",
    )
    asyncio.run(_send_reminders(DummySession([booking]), Settings()))

    assert booking.customer_reminder_sent_at is None
    assert booking.customer_reminder_1h_sent_at is None


def test_repeat_outreach_is_disabled() -> None:
    asyncio.run(_repeat_outreach(DummySession([]), Settings()))


def test_retry_staff_calendar_syncs_retries_unsynced_confirmed_booking(monkeypatch) -> None:
    booking = Booking(
        id=10,
        org_id=1,
        staff_id=2,
        service_id=3,
        start_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        end_utc=datetime.now(timezone.utc) + timedelta(hours=3),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        booking_link_title_snapshot="初回予約リンク",
        manage_token="manage-token",
    )
    org = __import__("app.booking.db_models", fromlist=["BookingOrg"]).BookingOrg(name="Org", slug="org")
    staff = __import__("app.booking.db_models", fromlist=["StaffMember"]).StaffMember(
        id=2, org_id=1, name="担当A", email="staff@example.com", google_refresh_token="rt"
    )
    service = __import__("app.booking.db_models", fromlist=["BookingService"]).BookingService(
        id=3, org_id=1, name="初回相談", duration_minutes=60
    )
    session = DummySession([booking])
    session._orgs[1] = org
    session._staff[2] = staff
    session._services[3] = service
    captured = {}

    async def fake_load_google_busy_map(*args, **kwargs):
        return {2: []}, {}

    async def fake_sync(session_arg, settings_arg, booking_arg, staff_arg, org_arg, *, service_name, booking_link_title, post_booking_message=""):
        captured["booking_id"] = booking_arg.id
        captured["staff_id"] = staff_arg.id
        captured["service_name"] = service_name
        captured["booking_link_title"] = booking_link_title
        return True

    monkeypatch.setattr("app.booking.jobs._load_google_busy_map", fake_load_google_busy_map)
    monkeypatch.setattr("app.booking.router._sync_booking_to_staff_calendar", fake_sync)

    asyncio.run(_retry_staff_calendar_syncs(session, Settings()))

    assert captured["booking_id"] == 10
    assert captured["staff_id"] == 2
    assert captured["service_name"] == "初回相談"
    assert captured["booking_link_title"] == "初回予約リンク"


def test_retry_staff_calendar_syncs_cancels_missing_google_event(monkeypatch) -> None:
    booking = Booking(
        id=11,
        org_id=1,
        staff_id=2,
        service_id=3,
        start_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        end_utc=datetime.now(timezone.utc) + timedelta(hours=3),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        google_event_id="evt-missing",
        manage_token="manage-token",
    )
    org = __import__("app.booking.db_models", fromlist=["BookingOrg"]).BookingOrg(name="Org", slug="org", auto_confirm=True)
    staff = __import__("app.booking.db_models", fromlist=["StaffMember"]).StaffMember(
        id=2, org_id=1, name="担当A", email="staff@example.com", google_refresh_token="rt"
    )
    session = DummySession([booking])
    session._orgs[1] = org
    session._staff[2] = staff

    async def fake_load_google_busy_map(*args, **kwargs):
        return {2: []}, {}

    async def fake_get_status(*args, **kwargs):
        return False, None

    async def fake_sync(*args, **kwargs):
        raise AssertionError("should not resync when event is missing")

    monkeypatch.setattr("app.booking.jobs._load_google_busy_map", fake_load_google_busy_map)
    monkeypatch.setattr("app.booking.jobs.get_calendar_event_status", fake_get_status)
    monkeypatch.setattr("app.booking.router._sync_booking_to_staff_calendar", fake_sync)

    asyncio.run(_retry_staff_calendar_syncs(session, Settings()))

    assert booking.status == "cancelled"
    assert booking.google_event_id is None
    assert "自動で解放" in (booking.google_calendar_sync_error or "")


def test_retry_staff_calendar_syncs_cancels_old_unsynced_orphan(monkeypatch) -> None:
    booking = Booking(
        id=12,
        org_id=1,
        staff_id=2,
        service_id=3,
        start_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        end_utc=datetime.now(timezone.utc) + timedelta(hours=3),
        status="confirmed",
        customer_name="Customer",
        customer_email="customer@example.com",
        google_event_id=None,
        google_calendar_synced_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        manage_token="manage-token",
    )
    org = __import__("app.booking.db_models", fromlist=["BookingOrg"]).BookingOrg(name="Org", slug="org", auto_confirm=True)
    staff = __import__("app.booking.db_models", fromlist=["StaffMember"]).StaffMember(
        id=2, org_id=1, name="担当A", email="staff@example.com", google_refresh_token="rt"
    )
    session = DummySession([booking])
    session._orgs[1] = org
    session._staff[2] = staff

    async def fake_load_google_busy_map(*args, **kwargs):
        return {2: []}, {}

    async def fake_sync(*args, **kwargs):
        raise AssertionError("should not resync stale orphan booking")

    monkeypatch.setattr("app.booking.jobs._load_google_busy_map", fake_load_google_busy_map)
    monkeypatch.setattr("app.booking.router._sync_booking_to_staff_calendar", fake_sync)

    asyncio.run(_retry_staff_calendar_syncs(session, Settings()))

    assert booking.status == "cancelled"
    assert "自動で解放" in (booking.google_calendar_sync_error or "")
