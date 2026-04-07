from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.calendar_policy import day_is_blocked_for_booking
from app.booking.availability import (
    google_calendar_allows_booking,
    merge_intervals,
    to_utc_aware,
)
from app.booking.calendar_google import freebusy_busy_intervals
from app.booking.db_models import Booking, BookingOrg, BookingService, PublicBookingLink, StaffMember
from app.config import Settings
from app.security.crypto import decrypt_secret

# 予約開始時刻の既定刻み（分）。実際の刻みは min(この値, 所要時間) とし、所要より粗い刻みで枠を落とさない。
BOOKING_SLOT_STEP_MINUTES = 30

# GET /availability の from_ts〜to_ts と枠終了時刻の比較で、クライアントの to_ts が 23:59:59.999 等のときに落ちるのを防ぐ
AVAILABILITY_RANGE_END_SLACK = timedelta(seconds=2)


def json_object_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def json_list_or_empty(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def availability_zone(defaults: dict) -> ZoneInfo:
    """availability_defaults の timezone（未設定は Asia/Tokyo）。"""
    defaults = json_object_or_empty(defaults)
    name = (defaults.get("timezone") or "Asia/Tokyo").strip() or "Asia/Tokyo"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Tokyo")


def org_local_date_for_utc_instant(instant: datetime, org: BookingOrg) -> date:
    """予約開始時刻を組織タイムゾーンの暦日に変換（土日祝ブロック判定用）。"""
    defaults = json_object_or_empty(org.availability_defaults_json)
    loc_tz = availability_zone(defaults)
    t = instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)
    return t.astimezone(loc_tz).date()


def org_calendar_day_bounds_utc(
    instant: datetime,
    org: BookingOrg,
) -> tuple[datetime, datetime]:
    """予約時刻を含むカレンダー日（組織タイムゾーン）の [00:00, 翌00:00) を UTC で返す。FreeBusy 窓用。"""
    defaults = json_object_or_empty(org.availability_defaults_json)
    loc_tz = availability_zone(defaults)
    t = instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)
    local = t.astimezone(loc_tz)
    d = local.date()
    day0 = datetime.combine(d, time.min, tzinfo=loc_tz)
    day1 = day0 + timedelta(days=1)
    return day0.astimezone(timezone.utc), day1.astimezone(timezone.utc)


def availability_defaults_positive_int(
    defaults: dict[str, Any],
    key: str,
    fallback: int,
) -> int:
    """JSON 保存の slot_minutes / duration 等。空文字・None・不正値で int() が落ちないようにする。"""
    raw = defaults.get(key)
    if raw is None:
        return fallback
    if isinstance(raw, str) and not raw.strip():
        return fallback
    try:
        v = int(raw)
        return v if v > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def org_buffer_minutes(org: BookingOrg, settings: Settings) -> int:
    """予約前後の余白（分）。組織の availability_defaults.buffer_minutes が優先、未設定は .env の BOOKING_BUFFER_MINUTES（既定 0）。

    0 のときは区間の重なりのみで判定（隙間が枠の長さ以上あれば予約可）。0 より大きいときのみギャップ条件を適用。
    """
    defaults = json_object_or_empty(org.availability_defaults_json)
    raw = defaults.get("buffer_minutes")
    if raw is not None and raw != "":
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return max(0, int(getattr(settings, "booking_buffer_minutes", 0) or 0))


def link_buffer_minutes(
    link: PublicBookingLink | None,
    org: BookingOrg,
    settings: Settings,
) -> int:
    raw = getattr(link, "buffer_minutes", None) if link is not None else None
    if raw is not None and raw != "":
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return org_buffer_minutes(org, settings)


def link_max_advance_booking_days(
    link: PublicBookingLink | None,
    org: BookingOrg,
) -> int:
    raw = getattr(link, "max_advance_booking_days", None) if link is not None else None
    if raw is not None and raw != "":
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    defaults = json_object_or_empty(org.availability_defaults_json)
    try:
        return max(0, int(defaults.get("max_advance_booking_days") or 0))
    except (TypeError, ValueError):
        return 0


def link_bookable_until_date(
    link: PublicBookingLink | None,
) -> date | None:
    raw = (getattr(link, "bookable_until_date", None) or "").strip() if link is not None else ""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def union_intervals(
    lists: list[list[tuple[datetime, datetime]]],
) -> list[tuple[datetime, datetime]]:
    """複数スタッフの区間の和集合。"""
    flat: list[tuple[datetime, datetime]] = []
    for lst in lists:
        flat.extend(lst)
    return merge_intervals(flat)


def expand_intervals_by_buffer_minutes(
    intervals: list[tuple[datetime, datetime]],
    buf_min: int,
) -> list[tuple[datetime, datetime]]:
    """予約前後余白（分）を表示用に反映した区間（各 [start,end] を前後に広げてマージ）。"""
    if not intervals or buf_min <= 0:
        return intervals
    td = timedelta(minutes=buf_min)
    expanded: list[tuple[datetime, datetime]] = []
    for a, b in intervals:
        aa = to_utc_aware(a) - td
        bb = to_utc_aware(b) + td
        expanded.append((aa, bb))
    return merge_intervals(expanded)


def _parse_slot_iso_to_utc(s: str) -> datetime:
    t = str(s).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    return to_utc_aware(datetime.fromisoformat(t))


def slot_interval_overlaps_busy(
    start_utc: datetime,
    end_utc: datetime,
    busy_merged: list[tuple[datetime, datetime]],
) -> bool:
    """候補 [start,end) が busy のいずれかと重なるか。"""
    s0 = to_utc_aware(start_utc)
    s1 = to_utc_aware(end_utc)
    for a, b in busy_merged:
        a = to_utc_aware(a)
        b = to_utc_aware(b)
        if s0 < b and s1 > a:
            return True
    return False


def filter_slots_not_overlapping_busy(
    slots: list[dict],
    busy: list[tuple[datetime, datetime]],
) -> list[dict]:
    """busy 区間（マージ済み）と重なるスロットを除外。表示と API の不整合を防ぐ。"""
    if not busy or not slots:
        return slots
    merged = merge_intervals([(to_utc_aware(a), to_utc_aware(b)) for a, b in busy])
    if not merged:
        return slots
    out: list[dict] = []
    for s in slots:
        raw_start = s.get("start_utc")
        raw_end = s.get("end_utc")
        if raw_start is None or raw_end is None:
            continue
        try:
            su = _parse_slot_iso_to_utc(str(raw_start))
            eu = _parse_slot_iso_to_utc(str(raw_end))
        except (TypeError, ValueError):
            out.append(s)
            continue
        if not slot_interval_overlaps_busy(su, eu, merged):
            out.append(s)
    return out


def intersect_two_merged_interval_lists(
    a: list[tuple[datetime, datetime]],
    b: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """ふたつの merge 済み区間列の交差（重なり部分のみ）。"""
    out: list[tuple[datetime, datetime]] = []
    for s1, e1 in a:
        for s2, e2 in b:
            ss = max(s1, s2)
            ee = min(e1, e2)
            if ss < ee:
                out.append((ss, ee))
    return merge_intervals(out)


def intersect_all_staff_busy_intervals(
    per_staff: list[list[tuple[datetime, datetime]]],
) -> list[tuple[datetime, datetime]]:
    """各担当の「埋まり」を重ねたとき、全員が同時に埋まっている時間帯（表示用）。"""
    if not per_staff:
        return []
    acc = per_staff[0]
    for lst in per_staff[1:]:
        acc = intersect_two_merged_interval_lists(acc, lst)
    return acc


def blocked_iso_dates_in_range(
    org: BookingOrg,
    range_start: datetime,
    range_end: datetime,
) -> list[str]:
    """指定期間に重なる暦日のうち、店舗設定で予約不可の日付（YYYY-MM-DD、組織 TZ）。"""
    defaults = json_object_or_empty(org.availability_defaults_json)
    loc_tz = availability_zone(defaults)
    rs = range_start if range_start.tzinfo else range_start.replace(tzinfo=timezone.utc)
    re = range_end if range_end.tzinfo else range_end.replace(tzinfo=timezone.utc)
    rs_l = rs.astimezone(loc_tz).date()
    re_l = re.astimezone(loc_tz).date()
    out: list[str] = []
    d = rs_l
    step = timedelta(days=1)
    while d <= re_l:
        if day_is_blocked_for_booking(d, defaults):
            out.append(d.isoformat())
        d = d + step
    return out


def link_lead_blocked_dates(org: BookingOrg, link: PublicBookingLink) -> set[date]:
    """リンク設定「直近 N 日」を組織 TZ の今日から N 暦日（当日含む）ブロックする日集合。"""
    n = max(0, min(366, int(getattr(link, "block_next_days", 0) or 0)))
    if n == 0:
        return set()
    defaults = json_object_or_empty(org.availability_defaults_json)
    loc_tz = availability_zone(defaults)
    today = datetime.now(loc_tz).date()
    return {today + timedelta(days=i) for i in range(n)}


def blocked_iso_dates_in_range_for_link(
    org: BookingOrg,
    link: PublicBookingLink,
    range_start: datetime,
    range_end: datetime,
) -> list[str]:
    """店舗の土日祝ブロックに加え、リンクの直近 N 日ブロックをマージ（YYYY-MM-DD、組織 TZ）。"""
    base = blocked_iso_dates_in_range(org, range_start, range_end)
    extra = link_lead_blocked_dates(org, link)
    defaults = json_object_or_empty(org.availability_defaults_json)
    loc_tz = availability_zone(defaults)
    rs = range_start if range_start.tzinfo else range_start.replace(tzinfo=timezone.utc)
    re = range_end if range_end.tzinfo else range_end.replace(tzinfo=timezone.utc)
    rs_l = rs.astimezone(loc_tz).date()
    re_l = re.astimezone(loc_tz).date()
    step = timedelta(days=1)
    merged = set(base)
    for d in extra:
        if rs_l <= d <= re_l:
            merged.add(d.isoformat())
    cutoff = link_bookable_until_date(link)
    if cutoff is not None:
        d = max(rs_l, cutoff + step)
        while d <= re_l:
            merged.add(d.isoformat())
            d = d + step
    return sorted(merged)


async def db_booking_busy_intervals_for_staff(
    session: AsyncSession,
    staff_id: int,
    range_start: datetime,
    range_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """担当の確定・保留予約を [start,end) 区間として返す。"""
    q = select(Booking).where(
        Booking.staff_id == staff_id,
        Booking.status.in_(("pending", "confirmed")),
        Booking.start_utc < range_end,
        Booking.end_utc > range_start,
    )
    rows = list((await session.scalars(q)).all())
    out: list[tuple[datetime, datetime]] = []
    for b in rows:
        out.append((b.start_utc, b.end_utc))
    return merge_intervals(out)


async def busy_intervals_union_for_link(
    session: AsyncSession,
    staff_list: list[StaffMember],
    range_start: datetime,
    range_end: datetime,
    gmap: dict[int, list[tuple[datetime, datetime]]],
) -> list[tuple[datetime, datetime]]:
    """各担当の Google+DB の埋まりの和集合（いずれかが埋まっている時間）。

    以前は「全員が同時に埋まっている区間（交差）」を返していたが、
    担当間で予定がずれると交差が空になり斜線が消えたり、
    足し合わせで隙間に見えても誰も連続枠を取れない場合と表示がずれたりするため、
    カレンダー上の「予定あり」は和集合で示す（予約枠の有無は slots を正とする）。"""
    per_staff: list[list[tuple[datetime, datetime]]] = []
    for s in staff_list:
        gbusy = list(gmap.get(s.id) or [])
        dbb = await db_booking_busy_intervals_for_staff(session, s.id, range_start, range_end)
        per_staff.append(merge_intervals(gbusy + dbb))
    if not per_staff:
        return []
    if len(per_staff) == 1:
        return per_staff[0]
    return union_intervals(per_staff)


async def eligible_staff(
    session: AsyncSession,
    org: BookingOrg,
    link_staff_ids: list[int],
    service: BookingService | None,
    settings: Settings,
) -> list[StaffMember]:
    q = select(StaffMember).where(
        StaffMember.org_id == org.id,
        StaffMember.active.is_(True),
    )
    if link_staff_ids:
        q = q.where(StaffMember.id.in_(link_staff_ids))
    rows = (await session.scalars(q)).all()
    rows = list(rows)
    # Google OAuth が有効なときは原則、連携済み（refresh_token あり）の担当のみ。
    # ただし誰も未連携のときに rows を空にすると予約枠が常に 0 件になる（初期導入で多発）ため、
    # その場合だけ全員を対象に戻す（FreeBusy は未連携は [] ＝ Google 上は空き扱い）。
    if settings.is_google_oauth_configured():
        linked = [s for s in rows if (decrypt_secret(s.google_refresh_token, settings) or "").strip()]
        rows = linked if linked else rows
    return rows


def link_priority_rank_for_staff(
    staff: StaffMember,
    link_priority_overrides: dict[str, int] | None = None,
) -> int:
    raw = None
    if isinstance(link_priority_overrides, dict):
        raw = link_priority_overrides.get(str(staff.id))
        if raw is None:
            raw = link_priority_overrides.get(staff.id)
    try:
        if raw is not None:
            return max(0, int(raw))
    except (TypeError, ValueError):
        pass
    return max(0, int(getattr(staff, "priority_rank", 100) or 100))


async def _load_google_busy_map(
    staff_list: list[StaffMember],
    window_start: datetime,
    window_end: datetime,
    settings: Settings,
) -> dict[int, list[tuple[datetime, datetime]]]:
    """担当 id ごとに必ずキーを返す。トークンが無い担当は []（未連携＝Google 上は空き扱い）。"""
    tmin = window_start.isoformat()
    tmax = window_end.isoformat()

    async def load_for_staff(s: StaffMember) -> tuple[int, list[tuple[datetime, datetime]]]:
        refresh_token = decrypt_secret(s.google_refresh_token, settings)
        if not refresh_token:
            return s.id, []
        intervals = await freebusy_busy_intervals(
            refresh_token,
            s.google_calendar_id,
            tmin,
            tmax,
            settings,
        )
        return s.id, list(intervals) if intervals else []

    pairs = await asyncio.gather(*(load_for_staff(s) for s in staff_list))
    return {staff_id: intervals for staff_id, intervals in pairs}


async def _db_booking_intervals_for_staff(
    session: AsyncSession,
    staff_id: int,
    *,
    exclude_booking_id: int | None = None,
) -> list[tuple[datetime, datetime]]:
    """担当の保留・確定予約を区間列として返す（マージ前）。staff_is_free の統合判定用。"""
    q = select(Booking).where(
        Booking.staff_id == staff_id,
        Booking.status.in_(("pending", "confirmed")),
    )
    if exclude_booking_id is not None:
        q = q.where(Booking.id != exclude_booking_id)
    rows = list((await session.scalars(q)).all())
    return [(to_utc_aware(b.start_utc), to_utc_aware(b.end_utc)) for b in rows]


async def staff_is_free(
    session: AsyncSession,
    staff: StaffMember,
    start: datetime,
    end: datetime,
    settings: Settings,
    google_busy_map: dict[int, list[tuple[datetime, datetime]]],
    *,
    exclude_booking_id: int | None = None,
    buffer_minutes: int | None = None,
) -> bool:
    buf_min = max(
        0,
        int(buffer_minutes) if buffer_minutes is not None else 0,
    )
    # DB と Google をマージし、google_calendar_allows_booking で一括判定（buf=0 は重なりのみ）。
    db_ivs = await _db_booking_intervals_for_staff(
        session,
        staff.id,
        exclude_booking_id=exclude_booking_id,
    )
    gbusy = google_busy_map.get(staff.id) or []
    g_list = [(to_utc_aware(a), to_utc_aware(b)) for a, b in gbusy]
    merged = merge_intervals(db_ivs + g_list)
    return google_calendar_allows_booking(start, end, buf_min, merged)


async def pick_staff_round_robin(
    session: AsyncSession,
    candidates: list[StaffMember],
) -> StaffMember | None:
    if not candidates:
        return None
    candidates.sort(key=lambda s: (s.round_robin_counter, s.id))
    chosen = candidates[0]
    chosen.round_robin_counter += 1
    await session.flush()
    return chosen


async def pick_staff_priority(
    session: AsyncSession,
    candidates: list[StaffMember],
    link_priority_overrides: dict[str, int] | None = None,
) -> StaffMember | None:
    if not candidates:
        return None
    candidates.sort(key=lambda s: (link_priority_rank_for_staff(s, link_priority_overrides), s.id))
    return candidates[0]


async def pick_staff_for_slot(
    session: AsyncSession,
    org: BookingOrg,
    link_staff_ids: list[int],
    service: BookingService | None,
    start: datetime,
    end: datetime,
    settings: Settings,
    *,
    link_priority_overrides: dict[str, int] | None = None,
    buffer_minutes_override: int | None = None,
    google_busy_map: dict[int, list[tuple[datetime, datetime]]] | None = None,
    dry_run: bool = False,
) -> StaffMember | None:
    """担当のうち、この枠が Google+DB 的に取れる人を列挙し、優先度またはラウンドロビンで1名を返す。"""
    staff_list = await eligible_staff(session, org, link_staff_ids, service, settings)
    if not staff_list:
        return None
    buf_min = (
        max(0, int(buffer_minutes_override))
        if buffer_minutes_override is not None
        else org_buffer_minutes(org, settings)
    )
    if google_busy_map is not None:
        gmap = google_busy_map
    else:
        ws, we = org_calendar_day_bounds_utc(start, org)
        pad = timedelta(days=1)
        gmap = await _load_google_busy_map(staff_list, ws - pad, we + pad, settings)
    free: list[StaffMember] = []
    for s in staff_list:
        if await staff_is_free(session, s, start, end, settings, gmap, buffer_minutes=buf_min):
            free.append(s)
    if not free:
        return None
    free.sort(key=lambda s: (link_priority_rank_for_staff(s, link_priority_overrides), s.id))
    # 複数担当が空いていても calendar には1名分のみ。優先度（小さいほど先）で決定。
    if org.routing_mode == "round_robin":
        best_rank = link_priority_rank_for_staff(free[0], link_priority_overrides)
        tier = [s for s in free if link_priority_rank_for_staff(s, link_priority_overrides) == best_rank]
        if dry_run:
            tier_sorted = sorted(tier, key=lambda s: (s.round_robin_counter, s.id))
            return tier_sorted[0]
        return await pick_staff_round_robin(session, tier)
    return await pick_staff_priority(session, free, link_priority_overrides)


def scheduling_hints_json(
    service_duration_minutes: int,
    buffer_minutes: int,
    *,
    eligible_staff_count: int = 0,
) -> dict[str, Any]:
    """GET /availability に載せる説明用メタ（buffer_minutes=0 のときは重なりのみ）。"""
    d = max(1, int(service_duration_minutes))
    b = max(0, int(buffer_minutes))
    gap = d + 2 * b
    esc = max(0, int(eligible_staff_count))
    if b == 0:
        note_ja = (
            f"緑の予約枠は、Google カレンダーおよびこのシステム上の予約と時間が重ならない枠です（所要 {d} 分）。"
            "前後余白は 0 分のため、隙間が枠の長さ以上あれば予約できます。"
        )
    else:
        note_ja = (
            "余白（バッファ）がある場合は、隣接する予定のあいだに「前の終了＋余白以降に開始し、次の開始－余白までに終了」"
            f"できる必要があります（所要 {d} 分・余白 {b} 分/側のとき、あいだは概ね {gap} 分以上が目安）。"
        )
    return {
        "buffer_minutes": b,
        "service_duration_minutes": d,
        "min_gap_minutes_for_booking": gap,
        "eligible_staff_count": esc,
        "note_ja": note_ja,
        "multi_staff_note_ja": (
            "担当が複数いる場合、時間帯が担当同士で細かく分かれていると、"
            "カレンダーでは隙間に見えても「どの担当も連続した空き」がなく、緑の予約枠が出ないことがあります。"
            if esc > 1
            else ""
        ),
        "busy_overlay_note_ja": (
            "斜線の「予定あり」は、いずれかの担当に予定がある時間をまとめて表示しています。"
            "予約できるかどうかは緑の「予約可能」の有無で判断してください。"
        ),
    }


def booking_conflict_detail_json(
    code: str,
    message_ja: str,
    *,
    duration_minutes: int | None = None,
    buffer_minutes: int = 0,
) -> dict[str, Any]:
    """POST /book の 409 用。クライアントが理由と数値を表示できるようにする。"""
    b = max(0, int(buffer_minutes))
    out: dict[str, Any] = {
        "code": code,
        "message": message_ja,
        "buffer_minutes": b,
    }
    if duration_minutes is not None:
        d = max(1, int(duration_minutes))
        out["duration_minutes"] = d
        out["min_gap_minutes_for_booking"] = d + 2 * b
        if b == 0:
            out["hint_ja"] = "この枠は他の予定と時間が重なっています。緑の「予約可能」から選び直してください。"
        else:
            out["hint_ja"] = (
                f"余白が{b}分/側のとき、隣り合う予定のあいだは最低{d + 2 * b}分（所要{d}分+余白×2）必要です。"
            )
    return out


async def available_slots_for_link(
    session: AsyncSession,
    org: BookingOrg,
    link_staff_ids: list[int],
    service: BookingService | None,
    range_start: datetime,
    range_end: datetime,
    settings: Settings,
    slot_minutes: int | None = None,
    *,
    staff_list: list[StaffMember] | None = None,
    google_busy_map: dict[int, list[tuple[datetime, datetime]]] | None = None,
    extra_blocked_dates: set[date] | None = None,
    link_priority_overrides: dict[str, int] | None = None,
    buffer_minutes_override: int | None = None,
    max_advance_days_override: int | None = None,
    bookable_until_date_override: date | None = None,
) -> tuple[list[dict], int]:
    defaults = json_object_or_empty(org.availability_defaults_json)
    if service:
        dur_minutes = max(1, int(service.duration_minutes))
    else:
        sm = slot_minutes
        if sm is None:
            sm = availability_defaults_positive_int(defaults, "slot_minutes", BOOKING_SLOT_STEP_MINUTES)
        dur_minutes = availability_defaults_positive_int(defaults, "duration", sm)
    duration = timedelta(minutes=dur_minutes)
    # 60分枠など、所要より粗い30分刻みだけだと理論上問題ないが、所要<30分では 30分刻みだと取りこぼすため細かくする
    step_minutes = max(1, min(BOOKING_SLOT_STEP_MINUTES, dur_minutes))

    loc_tz = availability_zone(defaults)
    rs_utc = range_start if range_start.tzinfo else range_start.replace(tzinfo=timezone.utc)
    re_utc = range_end if range_end.tzinfo else range_end.replace(tzinfo=timezone.utc)
    if staff_list is None:
        staff_list = await eligible_staff(session, org, link_staff_ids, service, settings)
    if google_busy_map is None:
        _gpad = timedelta(hours=2)
        gmap = await _load_google_busy_map(
            staff_list,
            rs_utc - _gpad,
            re_utc + _gpad,
            settings,
        )
    else:
        gmap = google_busy_map

    out: list[dict] = []
    rs_local = rs_utc.astimezone(loc_tz)
    re_local = re_utc.astimezone(loc_tz)
    cur_day = rs_local.date()
    end_day = re_local.date()
    if max_advance_days_override is not None:
        max_adv = max(0, int(max_advance_days_override))
    else:
        max_adv = max(0, int(defaults.get("max_advance_booking_days") or 0))
    today_org = datetime.now(loc_tz).date()
    last_bookable: date | None = (today_org + timedelta(days=max_adv)) if max_adv > 0 else None
    cutoff_date = bookable_until_date_override

    while cur_day <= end_day:
        if last_bookable is not None and cur_day > last_bookable:
            cur_day = cur_day + timedelta(days=1)
            continue
        if cutoff_date is not None and cur_day > cutoff_date:
            cur_day = cur_day + timedelta(days=1)
            continue
        if day_is_blocked_for_booking(cur_day, defaults) or (
            extra_blocked_dates and cur_day in extra_blocked_dates
        ):
            cur_day = cur_day + timedelta(days=1)
            continue
        sh, sm, eh, em = 8, 0, 22, 0
        start_s = defaults.get("start", "08:00")
        end_s = defaults.get("end", "22:00")
        try:
            sh, sm = map(int, str(start_s).split(":")[:2])
            eh, em = map(int, str(end_s).split(":")[:2])
        except ValueError:
            pass
        day_start = datetime.combine(cur_day, time(hour=sh, minute=sm), tzinfo=loc_tz)
        day_end_bound = datetime.combine(cur_day, time(hour=eh, minute=em), tzinfo=loc_tz)

        step = timedelta(minutes=step_minutes)
        cur = day_start
        while cur + duration <= day_end_bound:
            seg_end = cur + duration
            cur_u = cur.astimezone(timezone.utc)
            seg_u = seg_end.astimezone(timezone.utc)
            if cur_u >= rs_utc and seg_u <= re_utc + AVAILABILITY_RANGE_END_SLACK:
                picked = await pick_staff_for_slot(
                    session,
                    org,
                    link_staff_ids,
                    service,
                    cur_u,
                    seg_u,
                    settings,
                    link_priority_overrides=link_priority_overrides,
                    buffer_minutes_override=buffer_minutes_override,
                    google_busy_map=gmap,
                    dry_run=True,
                )
                if picked:
                    out.append(
                        {
                            "start_utc": cur_u.isoformat(),
                            "end_utc": seg_u.isoformat(),
                            "slot_minutes": step_minutes,
                            # POST /book で同じ担当に固定（一覧と確定の割当ズレ防止）
                            "staff_id": picked.id,
                            "staff_name": (picked.name or "").strip() or None,
                        }
                    )
            cur += step
        cur_day = cur_day + timedelta(days=1)

    return out, step_minutes
