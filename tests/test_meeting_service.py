"""会議 URL 解決（担当別 Zoom）。"""

from unittest.mock import MagicMock

from app.booking.meeting_service import build_meeting_url, resolve_meeting_provider_for_staff
from app.config import Settings


def test_zoom_prefers_staff_url_over_default() -> None:
    staff = MagicMock()
    staff.zoom_meeting_url = "https://zoom.example/jp/staff-a"
    settings = Settings(zoom_default_meeting_url="https://zoom.example/global")
    url, prov = build_meeting_url("zoom", settings, staff)
    assert prov == "zoom"
    assert url == "https://zoom.example/jp/staff-a"


def test_zoom_falls_back_to_settings() -> None:
    staff = MagicMock()
    staff.zoom_meeting_url = None
    settings = Settings(zoom_default_meeting_url="https://zoom.example/global")
    url, prov = build_meeting_url("zoom", settings, staff)
    assert url == "https://zoom.example/global"


def test_resolve_meeting_provider_prefers_staff_zoom_over_google() -> None:
    staff = MagicMock()
    staff.zoom_meeting_url = "https://zoom.example/jp/staff-a"
    staff.google_refresh_token = "refresh-token"
    settings = Settings()
    assert resolve_meeting_provider_for_staff(staff, settings) == "zoom"


def test_resolve_meeting_provider_does_not_fallback_to_google_meet() -> None:
    staff = MagicMock()
    staff.zoom_meeting_url = None
    staff.google_refresh_token = "refresh-token"
    settings = Settings()
    assert resolve_meeting_provider_for_staff(staff, settings) == "none"
