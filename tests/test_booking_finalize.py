from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from app.booking.db_models import Booking, BookingOrg, StaffMember
from app.booking.router import (
    _delete_staff_calendar_event_if_present,
    _finalize_confirmed_booking,
    _public_booking_response,
    _sync_booking_to_staff_calendar,
)
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

    async def fake_upsert_customer(*args, **kwargs):
        return None

    async def fake_send_booking_emails(*args, **kwargs):
        return {
            "customer": False,
            "staff": False,
            "customer_error": "SMTP temporary failure",
            "staff_error": None,
        }

    async def fake_notify_staff_line_booking(*args, **kwargs):
        return {"ok": False, "skipped": True}

    monkeypatch.setattr(booking_router, "create_event_for_booking_detailed", fake_create_event_for_booking_detailed)
    monkeypatch.setattr(booking_router, "_upsert_customer", fake_upsert_customer)
    monkeypatch.setattr(booking_router, "send_booking_emails", fake_send_booking_emails)
    monkeypatch.setattr(booking_router, "notify_staff_line_booking", fake_notify_staff_line_booking)

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
    assert booking.customer_confirmation_email_sent_at is None
    assert booking.customer_confirmation_email_error == "SMTP temporary failure"
    assert booking.customer_confirmation_email_last_attempt_at is not None


def test_finalize_confirmed_booking_records_calendar_sync_error_when_unlinked(monkeypatch) -> None:
    import app.booking.router as booking_router

    settings = get_settings()

    class DummySession:
        async def flush(self) -> None:
            return None

    async def fake_upsert_customer(*args, **kwargs):
        return None

    async def fake_send_booking_emails(*args, **kwargs):
        return {"customer": True, "staff": True, "customer_error": None, "staff_error": None}

    async def fake_notify_staff_line_booking(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(booking_router, "_upsert_customer", fake_upsert_customer)
    monkeypatch.setattr(booking_router, "send_booking_emails", fake_send_booking_emails)
    monkeypatch.setattr(booking_router, "notify_staff_line_booking", fake_notify_staff_line_booking)

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

    async def fake_upsert_customer(*args, **kwargs):
        captured["upsert_args"] = args
        return None

    async def fake_send_booking_emails(*args, **kwargs):
        return {"customer": True, "staff": True, "customer_error": None, "staff_error": None}

    async def fake_notify_staff_line_booking(*args, **kwargs):
        captured["line_customer_name"] = kwargs.get("customer_name")
        return {"ok": True}

    monkeypatch.setattr(booking_router, "create_event_for_booking_detailed", fake_create_event_for_booking_detailed)
    monkeypatch.setattr(booking_router, "_upsert_customer", fake_upsert_customer)
    monkeypatch.setattr(booking_router, "send_booking_emails", fake_send_booking_emails)
    monkeypatch.setattr(booking_router, "notify_staff_line_booking", fake_notify_staff_line_booking)

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
    assert captured["line_customer_name"] == "Encrypted Customer"


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
