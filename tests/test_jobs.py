from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.booking.db_models import Booking
from app.booking.jobs import _send_reminders
from app.config import Settings
from app.security.crypto import encrypt_secret


class DummySession:
    def __init__(self, bookings):
        self._bookings = bookings

    async def scalars(self, _query):
        class Result:
            def __init__(self, items):
                self._items = items

            def all(self):
                return self._items

        return Result(self._bookings)

    async def get(self, _model, _id):
        return None


def test_send_reminders_marks_sent_only_on_success(monkeypatch) -> None:
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
    settings = Settings(
        smtp_host="smtp.example.com",
        booking_reminder_hours_before=24,
        booking_staff_reminder_hours_before=24,
        booking_reminder_second_hours_before=1,
    )

    async def fake_send_simple_mail(*args, **kwargs):
        return False

    monkeypatch.setattr("app.booking.jobs.send_simple_mail", fake_send_simple_mail)

    asyncio.run(_send_reminders(DummySession([booking]), settings))

    assert booking.customer_reminder_sent_at is None
    assert booking.customer_reminder_1h_sent_at is None


def test_send_reminders_uses_decrypted_customer_fields(monkeypatch) -> None:
    settings = Settings(
        smtp_host="smtp.example.com",
        booking_session_secret="test-session-secret",
        booking_reminder_hours_before=24,
        booking_staff_reminder_hours_before=24,
        booking_reminder_second_hours_before=1,
    )
    booking = Booking(
        org_id=1,
        staff_id=None,
        service_id=1,
        start_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        end_utc=datetime.now(timezone.utc) + timedelta(hours=3),
        status="confirmed",
        customer_name=encrypt_secret("Customer", settings),
        customer_email=encrypt_secret("customer@example.com", settings),
        manage_token="manage-token",
    )
    captured: dict[str, object] = {}

    async def fake_send_simple_mail(_settings, to_addrs, subject, body, *, dry_run):
        captured["to_addrs"] = to_addrs
        captured["subject"] = subject
        captured["body"] = body
        return True

    monkeypatch.setattr("app.booking.jobs.send_simple_mail", fake_send_simple_mail)

    asyncio.run(_send_reminders(DummySession([booking]), settings))

    assert captured["to_addrs"] == ["customer@example.com"]
