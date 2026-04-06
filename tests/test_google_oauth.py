"""Google OAuth ユーティリティ（署名・認可 URL）のテスト。"""

import time

import pytest

from app.booking.oauth_util import (
    google_calendar_authorization_url,
    sign_staff_oauth_link,
    verify_staff_oauth_link,
)
from app.config import Settings


def test_sign_and_verify_staff_link() -> None:
    ts = int(time.time())
    sig = sign_staff_oauth_link(7, ts, "my-admin-secret")
    assert verify_staff_oauth_link(7, ts, sig, "my-admin-secret")
    assert not verify_staff_oauth_link(7, ts, sig + "bad", "my-admin-secret")
    assert not verify_staff_oauth_link(8, ts, sig, "my-admin-secret")


def test_verify_rejects_empty_secret() -> None:
    sig = sign_staff_oauth_link(1, 100, "s")
    assert not verify_staff_oauth_link(1, 100, sig, "")


def test_google_calendar_authorization_url_contains_scopes_and_state() -> None:
    s = Settings(
        google_oauth_client_id="test-id.apps.googleusercontent.com",
        google_oauth_client_secret="unused-here",
        google_oauth_redirect_uri="http://127.0.0.1:8000/api/booking/oauth/google/callback",
    )
    url = google_calendar_authorization_url(99, s)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "googleapis.com%2Fauth%2Fcalendar" in url or "calendar" in url
    assert "state=99" in url
    assert "access_type=offline" in url
    assert "prompt=" in url and "consent" in url


def test_google_calendar_authorization_url_uses_platform_redirect_uri() -> None:
    s = Settings(
        public_base_url="http://127.0.0.1:8000",
        render_external_url="https://booking-test.onrender.com",
        google_oauth_client_id="test-id.apps.googleusercontent.com",
        google_oauth_client_secret="unused-here",
        google_oauth_redirect_uri="http://127.0.0.1:8000/api/booking/oauth/google/callback",
    )
    url = google_calendar_authorization_url(5, s)
    assert (
        "redirect_uri=https%3A%2F%2Fbooking-test.onrender.com%2Fapi%2Fbooking%2Foauth%2Fgoogle%2Fcallback"
        in url
    )


def test_google_calendar_authorization_url_raises_when_not_configured() -> None:
    s = Settings(
        google_oauth_client_id="",
        google_oauth_client_secret="",
        google_oauth_redirect_uri="",
    )
    with pytest.raises(RuntimeError, match="Google OAuth not configured"):
        google_calendar_authorization_url(1, s)
