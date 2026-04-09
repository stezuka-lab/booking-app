"""HTTP スモークテスト（DB 初期化・デモ投入を含む lifespan を通す）。"""

import pytest
from fastapi.testclient import TestClient
from pathlib import Path

from app.config import get_settings


def test_settings_defines_actions_dry_run() -> None:
    """予約確定時のメール等で参照する。未定義だと AttributeError で 500 になる。"""
    s = get_settings()
    assert isinstance(s.actions_dry_run, bool)
    assert isinstance(s.booking_jobs_embedded, bool)
    assert isinstance(s.smtp_use_ssl, bool)
    assert isinstance(s.smtp_starttls, bool)


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
    assert data.get("service") == "booking"
    booking = data.get("booking") or {}
    assert "public_base_url" in booking
    assert "admin_api_enabled" in booking
    assert "google_oauth_ready" in booking


def test_root(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data.get("service")
    links = data.get("links", {})
    assert links.get("app") == "/app"
    assert links.get("settings") == "/app/settings"
    assert links.get("oauth_google_status") == "/api/booking/oauth/google/status"
    assert "docs" in links


def test_version(client: TestClient) -> None:
    r = client.get("/version")
    assert r.status_code == 200
    assert "version" in r.json()


def test_booking_link_invalid_404(client: TestClient) -> None:
    r = client.get("/api/booking/links/invalid-token-xyz/meta")
    assert r.status_code == 404


def test_openapi_docs_available(client: TestClient) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "openapi" in r.json()


def test_web_app_home(client: TestClient) -> None:
    r = client.get("/app")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_web_app_settings_page(client: TestClient) -> None:
    r = client.get("/app/settings", follow_redirects=False)
    assert r.status_code == 302
    assert "/app/login" in (r.headers.get("location") or "")


def test_admin_template_defines_esc_helper() -> None:
    tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "function esc(" in tpl


def test_accounts_template_has_audit_logs_section() -> None:
    tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "accounts.html").read_text(encoding="utf-8")
    assert "監査ログ" in tpl
    assert "audit-tbody" in tpl
    assert "監査ログを読み込んでいます" in tpl


def test_backup_assets_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "docs" / "BACKUP.md").exists()
    assert (root / "scripts" / "backup_postgres.ps1").exists()
    assert (root / "scripts" / "restore_postgres.ps1").exists()


def test_calendar_page(client: TestClient) -> None:
    r = client.get("/app/calendar", follow_redirects=False)
    assert r.status_code == 302
    assert "/app/login" in (r.headers.get("location") or "")


def test_public_booking_page_no_store(client: TestClient) -> None:
    r = client.get("/app/booking/sample-token")
    assert r.status_code == 200
    assert "no-store" in (r.headers.get("cache-control") or "")


def test_manage_page_no_store(client: TestClient) -> None:
    r = client.get("/app/manage/sample-token")
    assert r.status_code == 200
    assert "no-store" in (r.headers.get("cache-control") or "")


def test_legacy_booking_redirect(client: TestClient) -> None:
    r = client.get("/booking/public/test-token-abc", follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers.get("location") or ""
    assert "/app/booking/test-token-abc" in loc


def test_oauth_google_status_public(client: TestClient) -> None:
    r = client.get("/api/booking/oauth/google/status")
    assert r.status_code == 200
    data = r.json()
    assert "google_oauth_ready" in data
    assert "redirect_uri" in data
    assert "client_id" in data
    assert "has_client_secret" in data
    assert "missing" in data and isinstance(data["missing"], list)
    assert "env_snippet" in data
    assert "suggested_redirect_uri" in data
    assert "display_redirect_uri" in data
    assert "console_credentials_url" in data


def test_link_availability_includes_busy_and_meta(client: TestClient) -> None:
    r = client.get("/health")
    demo = r.json().get("booking_demo") or {}
    token = demo.get("token")
    if not token:
        pytest.skip("booking demo token not available")
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    r2 = client.get(
        f"/api/booking/links/{token}/availability",
        params={
            "from_ts": now.isoformat(),
            "to_ts": (now + timedelta(days=7)).isoformat(),
            "service_id": 1,
        },
    )
    assert r2.status_code == 200
    body = r2.json()
    assert "slots" in body
    assert "busy_intervals" in body
    assert "slot_minutes" in body
    assert "service_duration_minutes" in body
    assert "buffer_minutes" in body
    assert isinstance(body["buffer_minutes"], int)
    assert "eligible_staff_count" in body
    assert isinstance(body["eligible_staff_count"], int)
    assert "calendar_integration" in body
    ci = body["calendar_integration"]
    assert "oauth_configured" in ci
    assert "google_linked_staff_count" in ci
    assert "unlinked_fallback_active" in ci
    assert "scheduling_hints" in body
    sh = body["scheduling_hints"]
    assert "min_gap_minutes_for_booking" in sh
    assert "note_ja" in sh
    assert "eligible_staff_count" in sh
    assert isinstance(sh["eligible_staff_count"], int)
    for slot in body.get("slots") or []:
        assert "staff_id" in slot
        assert isinstance(slot["staff_id"], int)


def test_link_meta_includes_availability_defaults(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    demo = r.json().get("booking_demo") or {}
    token = demo.get("token")
    if not token:
        pytest.skip("booking demo token not available")
    r2 = client.get(f"/api/booking/links/{token}/meta")
    assert r2.status_code == 200
    body = r2.json()
    assert "availability_defaults" in body
    assert isinstance(body["availability_defaults"], dict)
    link = body.get("link") or {}
    assert "bookable_until_date" in link
    assert "pre_booking_notice" in link
    assert "post_booking_message" in link


def test_oauth_callback_redirects_when_missing_code(client: TestClient) -> None:
    r = client.get("/api/booking/oauth/google/callback", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers.get("location") or ""
    assert "/app/calendar" in loc
    assert "google_oauth=err" in loc


def test_oauth_authorize_rejects_invalid_signature(client: TestClient) -> None:
    r = client.get(
        "/api/booking/oauth/google/authorize?staff_id=1&ts=0&sig=invalid",
        follow_redirects=False,
    )
    # 503: BOOKING_ADMIN_SECRET 未設定の環境。401: 署名検証失敗。
    assert r.status_code in (401, 503)
    assert r.status_code != 303


def test_admin_patch_org_demo_shop_when_secret_configured(client: TestClient) -> None:
    secret = (get_settings().booking_admin_secret or "").strip()
    if not secret:
        pytest.skip("BOOKING_ADMIN_SECRET が空のため管理 API をスキップ")
    h = {"X-Admin-Secret": secret}
    s = client.get("/api/booking/admin/orgs/demo-shop/summary", headers=h)
    assert s.status_code == 200
    org = s.json().get("org") or {}
    orig_mode = org.get("routing_mode") or "round_robin"
    orig_confirm = bool(org.get("auto_confirm"))
    r = client.patch(
        "/api/booking/admin/orgs/demo-shop",
        json={"routing_mode": "priority", "auto_confirm": False},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json().get("ok") is True
    r2 = client.patch(
        "/api/booking/admin/orgs/demo-shop",
        json={"routing_mode": orig_mode, "auto_confirm": orig_confirm},
        headers=h,
    )
    assert r2.status_code == 200
