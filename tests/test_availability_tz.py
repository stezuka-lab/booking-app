"""naive / aware 混在でも重なり判定が落ちないこと。"""

from datetime import datetime, timedelta, timezone

from app.booking.availability import (
    candidate_blocks_existing,
    intervals_overlap,
    to_utc_aware,
)


def test_to_utc_aware_naive_becomes_utc() -> None:
    n = datetime(2026, 4, 5, 10, 0, 0)
    u = to_utc_aware(n)
    assert u.tzinfo == timezone.utc
    assert u.hour == 10


def test_intervals_overlap_naive_and_aware() -> None:
    aware = datetime(2026, 4, 5, 10, 0, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 4, 5, 10, 30, 0)  # 10:30 UTC として正規化後に重なる
    assert intervals_overlap(aware, aware + timedelta(hours=1), naive, naive + timedelta(hours=1))


def test_candidate_blocks_existing_mixed_tz() -> None:
    buf = timedelta(minutes=0)
    cand_s = datetime(2026, 4, 5, 9, 0, 0, tzinfo=timezone.utc)
    cand_e = datetime(2026, 4, 5, 9, 30, 0, tzinfo=timezone.utc)
    ex_s = datetime(2026, 4, 5, 9, 15, 0)  # naive (DB 由来を想定)
    ex_e = datetime(2026, 4, 5, 9, 45, 0)
    assert candidate_blocks_existing(cand_s, cand_e, buf, ex_s, ex_e) is True
