from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BookingOrg(Base):
    """テナント（店舗・部署など）。"""

    __tablename__ = "booking_orgs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # round_robin | priority（旧 skill は未使用扱い）
    routing_mode: Mapped[str] = mapped_column(String(32), default="round_robin")
    # キャンセル・変更ポリシー JSON: {"change_until_hours_before": 24, "same_day_phone_only": true}
    cancel_policy_json: Mapped[Any] = mapped_column(JSON, default=dict)
    # 営業時間など {"timezone":"Asia/Tokyo","start":"08:00","end":"22:00"}（開始刻みは API 定数、長さは予約区分の所要時間）
    availability_defaults_json: Mapped[Any] = mapped_column(JSON, default=dict)
    # True なら即時 confirmed。現在の運用では即時確定を既定とする。
    auto_confirm: Mapped[bool] = mapped_column(Boolean, default=True)
    # GA4 / 計測
    ga4_measurement_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 予約完了メール（顧客・担当）の ON/OFF と文面
    email_settings_json: Mapped[Any] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    staff: Mapped[list["StaffMember"]] = relationship(back_populates="org")
    services: Mapped[list["BookingService"]] = relationship(back_populates="org")
    links: Mapped[list["PublicBookingLink"]] = relationship(back_populates="org")
    forms: Mapped[list["BookingFormDefinition"]] = relationship(back_populates="org")


class StaffMember(Base):
    """担当者。Google カレンダーと紐づけ可能。"""

    __tablename__ = "booking_staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("booking_orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    # 数値が小さいほど優先（priority モード）
    priority_rank: Mapped[int] = mapped_column(Integer, default=100)
    round_robin_counter: Mapped[int] = mapped_column(Integer, default=0)
    google_calendar_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    google_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    # OAuth userinfo（連携した Google アカウント表示用）
    google_profile_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    google_profile_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    zoom_meeting_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 旧スキーマ互換・未使用時は空（JSON 配列文字列など）
    skill_tags: Mapped[str] = mapped_column(String(1024), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    org: Mapped["BookingOrg"] = relationship(back_populates="staff")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="staff")


class BookingService(Base):
    """予約区分（所要時間）。施策や管理用の区分として利用。"""

    __tablename__ = "booking_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("booking_orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    org: Mapped["BookingOrg"] = relationship(back_populates="services")


class PublicBookingLink(Base):
    """1 リンクで複数担当の空きを集約する公開 URL 用トークン。"""

    __tablename__ = "booking_public_links"
    __table_args__ = (UniqueConstraint("token", name="uq_booking_link_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("booking_orgs.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256), default="予約")
    # カテゴリ（予約区分）。紐づくと予約画面で区分選択を出さない
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("booking_services.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 表示する staff_id のリスト。空なら org 内の全 active 担当
    staff_ids_json: Mapped[Any] = mapped_column(JSON, default=list)
    routing_mode: Mapped[str] = mapped_column(String(32), default="priority")
    daily_booking_limit_per_staff: Mapped[int | None] = mapped_column(Integer, nullable=True)
    round_robin_counters_json: Mapped[Any] = mapped_column(JSON, default=dict)
    # リンクごとの担当優先度上書き。{"12": 10, "15": 30} のように staff_id -> priority_rank。
    staff_priority_overrides_json: Mapped[Any] = mapped_column(JSON, default=dict)
    # リンクごとの予約前後余白（分）。NULL のときは組織設定を使用。
    buffer_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # リンクごとの先行予約上限（日）。NULL のときは組織設定を使用。
    max_advance_booking_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # この日を超える予約は不可（組織タイムゾーン基準、YYYY-MM-DD）。
    bookable_until_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 予約ページ上部に表示するお知らせ。
    pre_booking_notice: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 予約完了時に表示する追加メッセージ。
    post_booking_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # False のとき公開 API は 403（予約不可）
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 組織 TZ の「今日」から数えて N 暦日（当日含む）を予約不可にする（0 で無効）
    block_next_days: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    org: Mapped["BookingOrg"] = relationship(back_populates="links")


class BookingFormDefinition(Base):
    """ヒアリングフォーム（選択・自由記述・ファイル添付の定義）。"""

    __tablename__ = "booking_form_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("booking_orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(256), default="デフォルト")
    # [{"id": "q1", "type": "select", "label": "...", "options": [...]}, ...]
    fields_json: Mapped[Any] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    org: Mapped["BookingOrg"] = relationship(back_populates="forms")


class Booking(Base):
    """予約本体。"""

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("booking_orgs.id", ondelete="CASCADE"), index=True)
    public_link_id: Mapped[int | None] = mapped_column(
        ForeignKey("booking_public_links.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("booking_staff.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 担当削除後も一覧表示用（作成時点の担当名）
    staff_display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("booking_services.id", ondelete="SET NULL"), nullable=True
    )
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # pending | confirmed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    customer_name: Mapped[str] = mapped_column(String(256), default="")
    customer_email: Mapped[str] = mapped_column(String(320), default="")
    booking_link_title_snapshot: Mapped[str | None] = mapped_column(String(256), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # カレンダー件名用の任意メモ（テンプレートの {note}）
    calendar_title_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    form_answers_json: Mapped[Any] = mapped_column(JSON, default=dict)
    # meet | zoom | teams | none
    meeting_provider: Mapped[str] = mapped_column(String(32), default="none")
    meeting_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_event_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    google_calendar_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    google_calendar_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    manage_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # UTM / 経路
    utm_source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(String(256), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(256), nullable=True)
    referrer: Mapped[str | None] = mapped_column(Text, nullable=True)
    ga_client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_confirmation_email_last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    customer_confirmation_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    customer_confirmation_email_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    staff_notification_email_last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    staff_notification_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    staff_notification_email_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    staff_reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    customer_reminder_1h_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    staff_reminder_1h_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # リピート促進ジョブ用
    last_outreach_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    staff: Mapped["StaffMember | None"] = relationship(back_populates="bookings")


class BookingAttachment(Base):
    """事前資料などのファイルメタデータ（実体はローカル data/uploads）。"""

    __tablename__ = "booking_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    field_id: Mapped[str] = mapped_column(String(64), default="")
    original_filename: Mapped[str] = mapped_column(String(512), default="")
    stored_path: Mapped[str] = mapped_column(String(1024), default="")

class BookingAuditLog(Base):
    """管理操作や認証系の重要イベントを残す監査ログ。"""

    __tablename__ = "booking_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    actor_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    org_slug: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    detail_json: Mapped[Any] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
