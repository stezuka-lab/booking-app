"""空き枠・バッファ重なり判定のユニットテスト。"""

from datetime import datetime, timedelta, timezone

from app.booking.availability import (
    buffer_td,
    candidate_blocks_existing,
    candidate_hits_google_busy,
    google_calendar_allows_booking,
    intervals_overlap,
    merge_intervals,
)
from app.booking.db_models import BookingOrg
from app.booking.routing_service import (
    expand_intervals_by_buffer_minutes,
    filter_slots_not_overlapping_busy,
    org_calendar_day_bounds_utc,
    union_intervals,
)


def test_intervals_overlap():
    a = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert intervals_overlap(a, a + timedelta(hours=1), a + timedelta(minutes=30), a + timedelta(hours=2))
    assert not intervals_overlap(a, a + timedelta(hours=1), a + timedelta(hours=1), a + timedelta(hours=2))


def test_buffer_blocks_back_to_back():
    buf = buffer_td(15)
    existing_start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    existing_end = existing_start + timedelta(minutes=30)
    cand_start = existing_end
    cand_end = cand_start + timedelta(minutes=30)
    assert candidate_blocks_existing(cand_start, cand_end, buf, existing_start, existing_end)


def test_gap_after_existing_not_blocked_when_candidate_starts_after_buffer():
    """前の予定終了〜次の予定までの隙間で、候補＋バッファが既存の実区間と端だけ接する場合は不可にならない。"""
    buf = buffer_td(15)
    existing_start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    existing_end = datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)
    cand_start = datetime(2026, 1, 1, 17, 15, tzinfo=timezone.utc)
    cand_end = datetime(2026, 1, 1, 17, 45, tzinfo=timezone.utc)
    assert not candidate_blocks_existing(cand_start, cand_end, buf, existing_start, existing_end)


def test_google_busy_with_buffer():
    buf = buffer_td(15)
    start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    busy = [(datetime(2026, 1, 1, 9, 50, tzinfo=timezone.utc), datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc))]
    assert candidate_hits_google_busy(start, end, buf, busy)


def test_merged_db_and_google_intervals_use_same_gap_rule_as_google_only():
    """staff_is_free 相当: DB 由来と Google 由来を merge し、隙間の 60 分枠が buf=0 で許可される。"""
    t17 = datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)
    t18 = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)
    db_half = [(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), t17)]
    g_half = [(t18, datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc))]
    merged = merge_intervals(db_half + g_half)
    assert google_calendar_allows_booking(t17, t18, 0, merged)


def test_google_busy_one_hour_gap_allows_sixty_minute_booking_when_no_buffer():
    """17:00まで・18:00から予定、あいだ1時間。余白0なら60分枠は予約可能（端点は重ならない）。"""
    buf = buffer_td(0)
    t17 = datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)
    t18 = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)
    busy = [
        (datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), t17),
        (t18, datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)),
    ]
    assert google_calendar_allows_booking(t17, t18, 0, busy)
    assert not candidate_hits_google_busy(t17, t18, buf, busy)


def test_google_busy_one_hour_gap_blocks_sixty_minute_when_buffer_exceeds_gap():
    """1時間の隙間に60分枠・余白15分はギャップ条件を満たさない（17:15開始・17:45終了までしか不可）。"""
    buf = buffer_td(15)
    t17 = datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)
    t18 = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)
    busy = [
        (datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), t17),
        (t18, datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)),
    ]
    assert not google_calendar_allows_booking(t17, t18, 15, busy)
    assert candidate_hits_google_busy(t17, t18, buf, busy)


def test_org_calendar_day_bounds_utc_uses_org_timezone_date():
    """UTC の日付と組織 TZ の「営業日」がずれるときも FreeBusy 窓が正しい日を覆う。"""
    org = BookingOrg(availability_defaults_json={"timezone": "Asia/Tokyo"})
    inst = datetime(2026, 4, 3, 23, 0, tzinfo=timezone.utc)
    ws, we = org_calendar_day_bounds_utc(inst, org)
    assert ws == datetime(2026, 4, 3, 15, 0, tzinfo=timezone.utc)
    assert we == datetime(2026, 4, 4, 15, 0, tzinfo=timezone.utc)


def test_expand_intervals_by_buffer_minutes():
    a = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    b = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    out = expand_intervals_by_buffer_minutes([(a, b)], 15)
    assert len(out) == 1
    assert out[0][0] == a - timedelta(minutes=15)
    assert out[0][1] == b + timedelta(minutes=15)


def test_union_intervals_covers_if_any_staff_busy():
    """表示用 busy は和集合。担当Aが10–12、担当Bが12–14なら隙間なく覆う（接点のみの区間はマージしない）。"""
    a = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    b = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    c = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    out = union_intervals([[(a, b)], [(b, c)]])
    assert len(out) == 2
    assert out[0] == (a, b)
    assert out[1] == (b, c)


def test_merge_intervals_does_not_bridge_touching_only():
    """バッファ拡張で端点が接しただけの区間を 1 本にまとめない（隙間のスロットを消さない）。"""
    a = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
    b = datetime(2026, 1, 1, 11, 30, tzinfo=timezone.utc)
    c = datetime(2026, 1, 1, 11, 30, tzinfo=timezone.utc)
    d = datetime(2026, 1, 1, 13, 30, tzinfo=timezone.utc)
    out = merge_intervals([(a, b), (c, d)])
    assert len(out) == 2


def test_filter_slots_not_overlapping_busy_drops_overlap():
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)
    busy = [(t0, t1)]
    slots = [
        {"start_utc": t0.isoformat(), "end_utc": t1.isoformat()},
        {"start_utc": (t1 + timedelta(hours=1)).isoformat(), "end_utc": (t1 + timedelta(hours=1, minutes=30)).isoformat()},
    ]
    kept = filter_slots_not_overlapping_busy(slots, busy)
    assert len(kept) == 1


def test_filter_slots_with_buffer_expanded_busy_would_false_positive():
    """表示用にバッファ拡張した busy で除外すると、直後の空き枠まで落ちる回帰防止。"""
    busy_end = datetime(2026, 4, 8, 8, 0, tzinfo=timezone.utc)
    busy_raw = [(datetime(2026, 4, 8, 1, 0, tzinfo=timezone.utc), busy_end)]
    busy_expanded = expand_intervals_by_buffer_minutes(busy_raw, 15)
    slot_ok = {
        "start_utc": busy_end.isoformat(),
        "end_utc": (busy_end + timedelta(minutes=30)).isoformat(),
    }
    kept_raw = filter_slots_not_overlapping_busy([slot_ok], busy_raw)
    kept_expanded = filter_slots_not_overlapping_busy([slot_ok], busy_expanded)
    assert len(kept_raw) == 1
    assert len(kept_expanded) == 0
    assert busy_expanded[0][1] > busy_end
