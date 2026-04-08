from functools import lru_cache
import hashlib
from base64 import urlsafe_b64encode
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    host: str = "0.0.0.0"
    port: int = 8000
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    render_external_url: str = Field(
        default="",
        validation_alias=AliasChoices("RENDER_EXTERNAL_URL"),
    )
    railway_public_domain: str = Field(
        default="",
        validation_alias=AliasChoices("RAILWAY_PUBLIC_DOMAIN"),
    )
    railway_static_url: str = Field(
        default="",
        validation_alias=AliasChoices("RAILWAY_STATIC_URL"),
    )

    # 予約: メール内リンク・OAuth 用の絶対 URL（末尾スラッシュなし）
    public_base_url: str = Field(
        default="http://127.0.0.1:8000",
        description=(
            "予約・管理メール内 URL のベース（例: https://reserve.example.com）。"
            "本番では実際にブラウザで開く HTTPS のオリジンに合わせる。"
        ),
    )
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""
    zoom_default_meeting_url: str = ""
    teams_default_meeting_url: str = ""
    booking_jobs_cron: str = "*/5 * * * *"
    booking_jobs_embedded: bool = True
    booking_reminder_hours_before: int = 24
    booking_reminder_second_hours_before: int = 1
    booking_staff_reminder_hours_before: int = 24
    # 組織で buffer_minutes 未設定のときの既定（0＝余白なし・重なりのみ）
    booking_buffer_minutes: int = 0
    booking_repeat_outreach_days: int = 30
    booking_admin_secret: str = Field(
        default="",
        description="管理者 API 用共有シークレット（推測困難なランダム文字列）。空でもログイン済み管理者セッションで API 可。",
    )
    # ブラウザセッション（ログイン）用。本番では固定の強いランダム文字列を .env に設定すること。
    booking_session_secret: str = Field(
        default="",
        description="Cookie セッション署名用。空のときは起動ごとにランダム（再起動でログアウト）。",
    )
    security_trusted_hosts: str = Field(
        default="",
        description="許可する Host ヘッダ。カンマ区切りで追加指定。空なら PUBLIC_BASE_URL から自動推定。",
    )
    security_force_https_redirect: bool = False
    security_hsts_seconds: int = 31536000
    auth_rate_limit_window_sec: int = 900
    auth_rate_limit_max_attempts: int = 10
    password_reset_rate_limit_window_sec: int = 3600
    password_reset_rate_limit_max_attempts: int = 5
    booking_public_rate_limit_window_sec: int = 3600
    booking_public_rate_limit_max_requests: int = 40
    booking_public_availability_cache_sec: int = 0
    api_docs_enabled: bool = True
    # DB にユーザーが 1 人もいないときだけ 1 回だけ作成（初期管理者）
    booking_bootstrap_admin_user: str = ""
    booking_bootstrap_admin_password: str = ""
    booking_seed_demo: bool = True
    line_messaging_channel_access_token: str = ""
    # メール・LINE 等の副作用を送らずログのみ（本番は False）
    actions_dry_run: bool = False

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_ssl: bool = False
    smtp_starttls: bool = True
    booking_data_encryption_key: str = ""

    @field_validator(
        "public_base_url",
        "google_oauth_client_id",
        "google_oauth_client_secret",
        "google_oauth_redirect_uri",
        "booking_session_secret",
        mode="before",
    )
    @classmethod
    def _strip_oauth_and_base(cls, v: object) -> object:
        """`.env` の前後空白・BOM で OAuth が不一致になるのを防ぐ。"""
        if isinstance(v, str):
            return v.strip().lstrip("\ufeff")
        return v

    def is_google_oauth_configured(self) -> bool:
        """Calendar OAuth: CLIENT_ID + SECRET + REDIRECT_URI が揃っているか。"""
        return bool(
            (self.google_oauth_client_id or "").strip()
            and (self.google_oauth_client_secret or "").strip()
            and self.google_oauth_redirect_uri_value()
        )

    @staticmethod
    def _is_local_origin(url: str) -> bool:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").strip().lower()
        return host in {"", "127.0.0.1", "localhost", "0.0.0.0"}

    def _platform_public_base_url(self) -> str:
        direct = (self.render_external_url or "").strip().rstrip("/")
        if direct:
            return direct
        railway_static = (self.railway_static_url or "").strip().rstrip("/")
        if railway_static:
            return railway_static
        railway_domain = (self.railway_public_domain or "").strip().strip("/")
        if railway_domain:
            return f"https://{railway_domain}"
        return ""

    def public_base_url_value(self) -> str:
        explicit = (self.public_base_url or "").strip().rstrip("/")
        platform = self._platform_public_base_url()
        if explicit and not self._is_local_origin(explicit):
            return explicit
        if platform:
            return platform
        if explicit:
            return explicit
        host = (self.host or "").strip() or "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return f"http://{host}:{int(self.port or 8000)}"

    def google_oauth_redirect_uri_value(self) -> str:
        explicit = (self.google_oauth_redirect_uri or "").strip().rstrip("/")
        public_base = self.public_base_url_value()
        derived = f"{public_base}/api/booking/oauth/google/callback"
        if explicit and not self._is_local_origin(explicit):
            return explicit
        if self._platform_public_base_url():
            return derived
        return explicit or derived

    def public_base_host(self) -> str:
        parsed = urlparse(self.public_base_url_value())
        return (parsed.hostname or "").strip().lower()

    def is_https_deployment(self) -> bool:
        parsed = urlparse(self.public_base_url_value())
        return parsed.scheme.lower() == "https"

    def is_public_deployment(self) -> bool:
        host = self.public_base_host()
        if not host:
            return False
        return host not in {"127.0.0.1", "localhost", "0.0.0.0"}

    def trusted_hosts(self) -> list[str]:
        hosts = {"localhost", "127.0.0.1", "testserver"}
        base_host = self.public_base_host()
        if base_host:
            hosts.add(base_host)
        raw = (self.security_trusted_hosts or "").strip()
        if raw:
            for item in raw.split(","):
                host = item.strip().lower()
                if host:
                    hosts.add(host)
        return sorted(hosts)

    def should_expose_demo_info(self) -> bool:
        return not self.is_public_deployment()

    def booking_data_encryption_key_value(self) -> str:
        explicit = (self.booking_data_encryption_key or "").strip()
        if explicit:
            return explicit
        basis = (
            (self.booking_session_secret or "").strip()
            or (self.booking_admin_secret or "").strip()
        )
        if not basis:
            return ""
        digest = hashlib.sha256(basis.encode("utf-8")).digest()
        return urlsafe_b64encode(digest).decode("ascii")


@lru_cache
def get_settings() -> Settings:
    return Settings()
