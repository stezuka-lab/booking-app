from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.booking.db_models import Booking, BookingOrg


def _parse_policy(org: BookingOrg) -> dict:
    raw = org.cancel_policy_json
    if isinstance(raw, dict):
        return raw
    return {}


def can_change_or_cancel_online(
    org: BookingOrg, booking: Booking, now: datetime | None = None
) -> tuple[bool, str]:
    """オンラインでの変更・キャンセルが許可されるか。不可の場合は理由コード。"""
    now = now or datetime.now(timezone.utc)
    policy = _parse_policy(org)
    hours_before = int(policy.get("change_until_hours_before", 24))
    same_day_only_phone = bool(policy.get("same_day_phone_only", True))
    start = booking.start_utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    deadline = start - timedelta(hours=hours_before)
    if now > deadline:
        if same_day_only_phone:
            return False, "deadline_passed_phone_only"
        return False, "deadline_passed"
    return True, "ok"


def can_reschedule_online(
    org: BookingOrg, booking: Booking, now: datetime | None = None
) -> tuple[bool, str]:
    """変更（日時変更）はキャンセルと同じ締切ルールを適用。"""
    return can_change_or_cancel_online(org, booking, now=now)


def hours_until_start(booking: Booking, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    start = booking.start_utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return (start - now).total_seconds() / 3600.0
