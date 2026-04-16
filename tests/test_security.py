from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth.rate_limit import (
    check_login_rate_limit,
    check_password_reset_rate_limit,
    clear_login_failures,
    record_login_failure,
    record_password_reset_attempt,
)
from app.booking.db_models import Booking
from app.booking.router import _scrub_booking_personal_data
from app.config import Settings
from app.main import _is_same_origin
from app.security.crypto import decrypt_secret, encrypt_secret


def _dummy_request(ip: str = "127.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def test_settings_trusted_hosts_include_public_base_host() -> None:
    s = Settings(
        public_base_url="https://reserve.example.com",
        security_trusted_hosts="booking.example.com, reserve.example.com",
    )
    hosts = s.trusted_hosts()
    assert "reserve.example.com" in hosts
    assert "booking.example.com" in hosts
    assert "localhost" in hosts


def test_public_deployment_hides_demo_info() -> None:
    s = Settings(public_base_url="https://reserve.example.com")
    assert s.should_expose_demo_info() is False


def test_platform_url_overrides_localhost_defaults() -> None:
    s = Settings(
        public_base_url="http://127.0.0.1:8000",
        google_oauth_redirect_uri="http://127.0.0.1:8000/api/booking/oauth/google/callback",
        render_external_url="https://booking-test.onrender.com",
    )
    assert s.public_base_url_value() == "https://booking-test.onrender.com"
    assert (
        s.google_oauth_redirect_uri_value()
        == "https://booking-test.onrender.com/api/booking/oauth/google/callback"
    )
    assert "booking-test.onrender.com" in s.trusted_hosts()


def test_login_rate_limit_blocks_after_repeated_failures() -> None:
    request = _dummy_request("10.0.0.8")
    username = "tester"
    clear_login_failures(request, username)
    for _ in range(2):
        record_login_failure(request, username, window_sec=3600)
    with pytest.raises(HTTPException) as exc:
        check_login_rate_limit(request, username, max_attempts=2, window_sec=3600)
    assert exc.value.status_code == 429
    clear_login_failures(request, username)


def test_password_reset_rate_limit_blocks_after_repeated_attempts() -> None:
    request = _dummy_request("10.0.0.9")
    ident = "tester::user@example.com"
    for _ in range(2):
        record_password_reset_attempt(request, ident, window_sec=3600)
    with pytest.raises(HTTPException) as exc:
        check_password_reset_rate_limit(request, ident, max_attempts=2, window_sec=3600)
    assert exc.value.status_code == 429


def test_security_headers_present(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("content-security-policy")


def test_same_origin_allows_localhost_and_loopback_aliases() -> None:
    assert _is_same_origin("http://localhost:8000/app/login", "http://127.0.0.1:8000")
    assert _is_same_origin("http://127.0.0.1:8000/app/login", "http://localhost:8000")
    assert not _is_same_origin("http://localhost:8001/app/login", "http://127.0.0.1:8000")


def test_secret_encryption_roundtrip() -> None:
    settings = Settings(booking_session_secret="test-session-secret")
    encrypted = encrypt_secret("refresh-token", settings)
    assert encrypted
    assert encrypted != "refresh-token"
    assert decrypt_secret(encrypted, settings) == "refresh-token"


def test_secret_decrypt_accepts_legacy_plaintext() -> None:
    settings = Settings()
    assert decrypt_secret("legacy-plain-token", settings) == "legacy-plain-token"


def test_scrub_booking_personal_data_clears_pii_fields() -> None:
    booking = Booking(
        customer_name="Customer Name",
        customer_email="customer@example.com",
        customer_phone="090-0000-0000",
        company_name="Acme",
        calendar_title_note="VIP",
        form_answers_json={"customer_number": "C-001"},
        utm_source="google",
        utm_medium="cpc",
        utm_campaign="spring",
        referrer="https://example.com/?email=customer@example.com",
        ga_client_id="ga.123",
    )

    _scrub_booking_personal_data(booking)

    assert booking.customer_name == ""
    assert booking.customer_email == ""
    assert booking.customer_phone is None
    assert booking.company_name is None
    assert booking.calendar_title_note is None
    assert booking.form_answers_json == {}
    assert booking.utm_source is None
    assert booking.utm_medium is None
    assert booking.utm_campaign is None
    assert booking.referrer is None
    assert booking.ga_client_id is None
