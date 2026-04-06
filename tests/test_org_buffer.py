"""組織・サーバー既定の予約前後バッファ（分）。既定 0。"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.booking.routing_service import (
    blocked_iso_dates_in_range_for_link,
    link_buffer_minutes,
    link_bookable_until_date,
    link_max_advance_booking_days,
    org_buffer_minutes,
)
from app.config import Settings


def test_org_buffer_overrides_settings() -> None:
    org = MagicMock()
    org.availability_defaults_json = {"buffer_minutes": 20}
    s = Settings(booking_buffer_minutes=5)
    assert org_buffer_minutes(org, s) == 20


def test_falls_back_to_settings() -> None:
    org = MagicMock()
    org.availability_defaults_json = {}
    s = Settings(booking_buffer_minutes=12)
    assert org_buffer_minutes(org, s) == 12


def test_defaults_to_zero_when_unset() -> None:
    org = MagicMock()
    org.availability_defaults_json = {}
    s = Settings()
    assert org_buffer_minutes(org, s) == 0


def test_zero_in_json_is_honored() -> None:
    org = MagicMock()
    org.availability_defaults_json = {"buffer_minutes": 0}
    s = Settings(booking_buffer_minutes=15)
    assert org_buffer_minutes(org, s) == 0


def test_link_buffer_overrides_org() -> None:
    org = MagicMock()
    org.availability_defaults_json = {"buffer_minutes": 5}
    link = MagicMock()
    link.buffer_minutes = 20
    s = Settings(booking_buffer_minutes=15)
    assert link_buffer_minutes(link, org, s) == 20


def test_link_buffer_falls_back_to_org_and_settings() -> None:
    org = MagicMock()
    org.availability_defaults_json = {}
    link = MagicMock()
    link.buffer_minutes = None
    s = Settings(booking_buffer_minutes=12)
    assert link_buffer_minutes(link, org, s) == 12


def test_link_max_advance_days_overrides_org() -> None:
    org = MagicMock()
    org.availability_defaults_json = {"max_advance_booking_days": 14}
    link = MagicMock()
    link.max_advance_booking_days = 30
    assert link_max_advance_booking_days(link, org) == 30


def test_link_max_advance_days_falls_back_to_org() -> None:
    org = MagicMock()
    org.availability_defaults_json = {"max_advance_booking_days": 14}
    link = MagicMock()
    link.max_advance_booking_days = None
    assert link_max_advance_booking_days(link, org) == 14


def test_link_bookable_until_date_parses_iso_date() -> None:
    link = MagicMock()
    link.bookable_until_date = "2026-04-30"
    assert link_bookable_until_date(link).isoformat() == "2026-04-30"


def test_link_bookable_until_date_invalid_returns_none() -> None:
    link = MagicMock()
    link.bookable_until_date = "2026-04-99"
    assert link_bookable_until_date(link) is None


def test_blocked_dates_include_days_after_bookable_until_date() -> None:
    org = MagicMock()
    org.availability_defaults_json = {"timezone": "Asia/Tokyo"}
    link = MagicMock()
    link.block_next_days = 0
    link.bookable_until_date = "2026-04-11"
    out = blocked_iso_dates_in_range_for_link(
        org,
        link,
        datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 13, 23, 59, tzinfo=timezone.utc),
    )
    assert "2026-04-12" in out
    assert "2026-04-13" in out
