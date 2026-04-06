from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from app.config import Settings

if TYPE_CHECKING:
    from app.booking.db_models import StaffMember


def resolve_meeting_provider_for_staff(staff: "StaffMember", settings: Settings) -> str:
    """顧客が会議手段を選ばない場合: 担当 Zoom → 既定 Zoom → Teams 既定 → なし。"""
    if (getattr(staff, "zoom_meeting_url", None) or "").strip():
        return "zoom"
    if (getattr(settings, "zoom_default_meeting_url", None) or "").strip():
        return "zoom"
    if (getattr(settings, "teams_default_meeting_url", None) or "").strip():
        return "teams"
    return "none"


def build_meeting_url(
    provider: str,
    settings: Settings,
    staff: "StaffMember | None" = None,
) -> tuple[str, str]:
    """会議 URL と保存用プロバイダ名を返す。Meet はカレンダー作成時に付与する前提でここでは none。"""
    p = (provider or "none").lower()
    staff_zoom = (getattr(staff, "zoom_meeting_url", None) or "").strip() if staff else ""
    if p == "zoom":
        if staff_zoom:
            return staff_zoom, "zoom"
        if settings.zoom_default_meeting_url.strip():
            return settings.zoom_default_meeting_url.strip(), "zoom"
    if p == "teams":
        if settings.teams_default_meeting_url.strip():
            return settings.teams_default_meeting_url.strip(), "teams"
    if p == "meet":
        # 実際の URL は Google Calendar insert の応答で埋める
        return "", "meet"
    return "", "none"


def meet_conference_request_id() -> str:
    return secrets.token_hex(8)
