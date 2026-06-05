"""Google OAuth 用: 担当者連携の署名付き URL（ワンクリックで Google へリダイレクト）。"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from app.config import Settings


def sign_staff_oauth_link(staff_id: int, ts: int, admin_secret: str) -> str:
    msg = f"{staff_id}:{ts}".encode()
    return hmac.new(admin_secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_staff_oauth_link(
    staff_id: int,
    ts: int,
    sig: str,
    admin_secret: str,
    *,
    max_age_sec: int = 7200,
) -> bool:
    if not admin_secret.strip():
        return False
    if abs(int(time.time()) - int(ts)) > max_age_sec:
        return False
    expected = sign_staff_oauth_link(staff_id, ts, admin_secret.strip())
    return hmac.compare_digest(expected, sig)


def _oauth_state_secret(settings: Settings) -> str:
    return (
        (settings.booking_session_secret or "").strip()
        or (settings.booking_admin_secret or "").strip()
        or (settings.booking_data_encryption_key or "").strip()
    )


def sign_google_oauth_state(staff_id: int, settings: Settings, *, ts: int | None = None) -> str:
    """Signed OAuth state. Keeps callback from trusting a bare staff_id."""
    secret = _oauth_state_secret(settings)
    if not secret:
        raise RuntimeError("OAuth state signing secret is not configured")
    issued_at = int(ts if ts is not None else time.time())
    msg = f"{int(staff_id)}:{issued_at}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{int(staff_id)}:{issued_at}:{sig}"


def verify_google_oauth_state(
    state: str,
    settings: Settings,
    *,
    max_age_sec: int = 7200,
) -> int | None:
    secret = _oauth_state_secret(settings)
    if not secret:
        return None
    parts = (state or "").split(":")
    if len(parts) != 3:
        return None
    sid_raw, ts_raw, sig = parts
    try:
        staff_id = int(sid_raw)
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return None
    if abs(int(time.time()) - ts) > max_age_sec:
        return None
    expected = sign_google_oauth_state(staff_id, settings, ts=ts).split(":", 2)[2]
    if not hmac.compare_digest(expected, sig):
        return None
    return staff_id


def google_calendar_authorization_url(staff_id: int, settings: Settings) -> str:
    from app.booking.calendar_google import GOOGLE_CALENDAR_SCOPES

    redirect_uri = settings.google_oauth_redirect_uri_value()
    if not settings.google_oauth_client_id or not redirect_uri:
        raise RuntimeError("Google OAuth not configured")
    scope = " ".join(GOOGLE_CALENDAR_SCOPES)
    q = {
        "client_id": settings.google_oauth_client_id.strip(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        # consent: refresh_token 再発行 / select_account: 誤アカウント切り替えしやすくする
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
        "state": sign_google_oauth_state(staff_id, settings),
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(q)
