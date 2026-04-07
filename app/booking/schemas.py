from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator

_ORG_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_org_slug_value(v: str) -> str:
    t = v.strip().lower()
    if not _ORG_SLUG_RE.match(t):
        raise ValueError(
            "組織 slug は URL 用の英小文字・数字・ハイフンのみです（例: demo-team, admin-team）。"
            "日本語名は「組織の表示名」に入力してください。"
        )
    if len(t) > 128:
        raise ValueError("slug が長すぎます")
    return t


class CancelPolicy(BaseModel):
    """変更・キャンセル可能までの時間（時間単位）。当日は電話のみなど。"""

    change_until_hours_before: int = 24
    same_day_phone_only: bool = True


class AvailabilityQuery(BaseModel):
    link_token: str
    service_id: int | None = None
    from_date: datetime
    to_date: datetime


class BookingCreate(BaseModel):
    link_token: str
    # リンクに service_id が無い場合のみ必須（従来互換）
    service_id: int | None = None
    staff_id: int | None = None
    start_utc: datetime
    customer_name: str = Field(min_length=1, max_length=256)
    customer_email: EmailStr
    customer_phone: str | None = None
    company_name: str | None = Field(None, max_length=256)
    calendar_title_note: str | None = Field(None, max_length=512)
    form_answers: dict[str, Any] = Field(default_factory=dict)
    meeting_provider: str | None = None  # 未指定時はサーバーが担当に応じて自動決定
    # 予約画面で Google Identity（OAuth トークン）を取得した場合のみ。サーバーで一度きりカレンダー登録に使用し保存しない。
    customer_google_access_token: str | None = Field(None, max_length=8192)
    # 経路
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    referrer: str | None = None
    ga_client_id: str | None = None
    # 直前の GET /availability と同一の FreeBusy 窓（送った場合、予約時も同じ gmap を使う）
    availability_from_ts: datetime | None = None
    availability_to_ts: datetime | None = None


class BookingManageAction(BaseModel):
    manage_token: str
    action: str  # cancel | reschedule
    new_start_utc: datetime | None = None


class StaffCreate(BaseModel):
    name: str = ""
    email: str = ""
    priority_rank: int = 100
    google_calendar_id: str | None = None
    zoom_meeting_url: str | None = None
    line_user_id: str | None = None


class OrgCreate(BaseModel):
    name: str
    slug: str
    routing_mode: str = "priority"

    @field_validator("slug")
    @classmethod
    def slug_ok(cls, v: str) -> str:
        return validate_org_slug_value(v)

    @field_validator("name")
    @classmethod
    def name_ok(cls, v: str) -> str:
        t = v.strip()
        if not t:
            raise ValueError("name は空にできません")
        return t


class OrgPatch(BaseModel):
    name: str | None = None
    slug: str | None = None
    auto_confirm: bool | None = None
    routing_mode: str | None = None
    ga4_measurement_id: str | None = None
    cancel_policy: dict[str, Any] | None = None
    availability_defaults: dict[str, Any] | None = None
    email_settings: dict[str, Any] | None = None

    @field_validator("slug")
    @classmethod
    def slug_patch_ok(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_org_slug_value(v)

    @field_validator("name")
    @classmethod
    def name_patch_ok(cls, v: str | None) -> str | None:
        if v is None:
            return None
        t = v.strip()
        if not t:
            raise ValueError("name は空にできません")
        return t


class StaffPatch(BaseModel):
    name: str | None = None
    email: str | None = None
    priority_rank: int | None = None
    google_calendar_id: str | None = None
    zoom_meeting_url: str | None = None
    line_user_id: str | None = None
    active: bool | None = None
    clear_google_oauth: bool | None = None


class RescheduleBody(BaseModel):
    new_start_utc: datetime


class ServiceCreate(BaseModel):
    name: str
    duration_minutes: int = 30


class ServicePatch(BaseModel):
    name: str | None = None
    duration_minutes: int | None = None
    active: bool | None = None


class FormDefinitionUpdate(BaseModel):
    name: str = "デフォルト"
    fields_json: list[dict[str, Any]] = Field(default_factory=list)


class PublicLinkCreate(BaseModel):
    """予約リンクごとに専用トークンの URL を発行（予約区分・担当を紐づけ）。"""

    title: str = "予約"
    service_id: int
    staff_ids: list[int] = Field(default_factory=list)
    staff_priority_overrides: dict[str, int] = Field(default_factory=dict)
    buffer_minutes: int | None = Field(None, ge=0, le=180)
    max_advance_booking_days: int | None = Field(None, ge=0, le=730)
    bookable_until_date: str | None = Field(None, max_length=10)
    pre_booking_notice: str | None = Field(None, max_length=4000)
    post_booking_message: str | None = Field(None, max_length=4000)
    block_next_days: int = Field(0, ge=0, le=366)


class PublicLinkPatch(BaseModel):
    title: str | None = None
    service_id: int | None = None
    staff_ids: list[int] | None = None
    staff_priority_overrides: dict[str, int] | None = None
    buffer_minutes: int | None = Field(None, ge=0, le=180)
    max_advance_booking_days: int | None = Field(None, ge=0, le=730)
    bookable_until_date: str | None = Field(None, max_length=10)
    pre_booking_notice: str | None = Field(None, max_length=4000)
    post_booking_message: str | None = Field(None, max_length=4000)
    active: bool | None = None
    block_next_days: int | None = Field(None, ge=0, le=366)


class OAuthLinkRequest(BaseModel):
    """管理者が署名付き Google 連携 URL を取得するためのリクエスト。"""

    staff_id: int
