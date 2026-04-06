"""
空き枠・重なり判定の共通ロジック。

- Google Calendar API からは FreeBusy の busy 区間のみを利用（イベントタイトルは取得しない）。
- 既存 DB 予約: routing_service.staff_is_free では Google busy とマージし、google_calendar_allows_booking で判定。
- バッファ（分）が 0 のときは区間の重なりのみ。0 より大きいときは隣接予定のあいだにギャップ条件を適用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def to_utc_aware(dt: datetime) -> datetime:
    """DB や SQLite 経由で naive に戻る場合があるため、比較・演算前に UTC aware に揃える。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def buffer_td(minutes: int) -> timedelta:
    return timedelta(minutes=max(0, minutes))


def merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """区間を結合（実際に重なるもののみ）。端点だけで接する区間は結合しない。"""
    if not intervals:
        return []
    intervals = [(to_utc_aware(a), to_utc_aware(b)) for a, b in intervals]
    intervals = sorted(intervals, key=lambda x: x[0])
    merged: list[tuple[datetime, datetime]] = [intervals[0]]
    for s, e in intervals[1:]:
        ls, le = merged[-1]
        if s < le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def expand_interval(
    start: datetime, end: datetime, buf: timedelta
) -> tuple[datetime, datetime]:
    """[start,end] を前後 buf だけ広げた区間。"""
    start = to_utc_aware(start)
    end = to_utc_aware(end)
    return start - buf, end + buf


def intervals_overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    """半開区間ではなく閉区間の重なり（端点接続は重ならない）。"""
    a0, a1, b0, b1 = (to_utc_aware(x) for x in (a0, a1, b0, b1))
    return a0 < b1 and b0 < a1


def candidate_blocks_existing(
    cand_start: datetime,
    cand_end: datetime,
    buf: timedelta,
    existing_start: datetime,
    existing_end: datetime,
) -> bool:
    """候補（前後にバッファを足した帯）が、既存予約の実区間と重なるか。

    既存側にバッファを重ねると、前後の予定のあいだ（例: 17:00〜18:00）まで潰れて
    取れる枠が無くなるため、既存は拡張しない。
    """
    c0, c1 = expand_interval(cand_start, cand_end, buf)
    e0 = to_utc_aware(existing_start)
    e1 = to_utc_aware(existing_end)
    return intervals_overlap(c0, c1, e0, e1)


def candidate_hits_google_busy(
    cand_start: datetime,
    cand_end: datetime,
    buf: timedelta,
    busy_intervals: list[tuple[datetime, datetime]] | None,
) -> bool:
    """互換用: Google がブロックするか（google_calendar_allows_booking の否定）。"""
    buf_min = int(buf.total_seconds() // 60) if buf else 0
    return not google_calendar_allows_booking(
        cand_start, cand_end, buf_min, busy_intervals if busy_intervals else []
    )


def google_calendar_allows_booking(
    cand_start: datetime,
    cand_end: datetime,
    buf_minutes: int,
    busy_intervals: list[tuple[datetime, datetime]],
) -> bool:
    """Google FreeBusy の busy のいずれとも重ならず、かつバッファ分の余裕を保てるなら True。

    バッファ b>0 のとき、隣接する busy のあいだ [prev_end, next_start] においては
    prev_end + b 以降に開始し、next_start - b 以前に終了する必要がある（1 時間のあいだに 60 分枠が入るのは b=0 のとき）。
    """
    cs = to_utc_aware(cand_start)
    ce = to_utc_aware(cand_end)
    if cs >= ce:
        return False
    if not busy_intervals:
        return True
    merged = merge_intervals(busy_intervals)
    buf = buffer_td(buf_minutes)
    for bs, be in merged:
        if cs < be and ce > bs:
            return False
    if buf.total_seconds() == 0:
        return True
    prev_be: datetime | None = None
    for bs, be in merged:
        if prev_be is None:
            if ce <= bs - buf:
                return True
        else:
            if cs >= prev_be + buf and ce <= bs - buf:
                return True
        prev_be = be
    if prev_be is not None and cs >= prev_be + buf:
        return True
    return False
