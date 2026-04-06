"""予約可能日ポリシー（土日祝ブロック）。"""

from datetime import date

from app.booking.calendar_policy import day_is_blocked_for_booking


def test_weekend_blocked_when_legacy_flag() -> None:
    sat = date(2026, 4, 4)  # Saturday
    assert day_is_blocked_for_booking(sat, {"block_weekends": True}) is True
    assert day_is_blocked_for_booking(sat, {"block_weekends": False}) is False


def test_weekday_not_blocked_by_weekend_flag() -> None:
    mon = date(2026, 4, 6)
    assert day_is_blocked_for_booking(mon, {"block_weekends": True}) is False


def test_saturday_and_sunday_individual() -> None:
    sat = date(2026, 4, 4)
    sun = date(2026, 4, 5)
    cfg = {"block_saturday": True, "block_sunday": False}
    assert day_is_blocked_for_booking(sat, cfg) is True
    assert day_is_blocked_for_booking(sun, cfg) is False
    cfg2 = {"block_saturday": False, "block_sunday": True}
    assert day_is_blocked_for_booking(sat, cfg2) is False
    assert day_is_blocked_for_booking(sun, cfg2) is True


def test_holiday_blocked_when_enabled() -> None:
    d = date(2026, 1, 12)
    assert day_is_blocked_for_booking(d, {"block_holidays": True}) is True
    assert day_is_blocked_for_booking(d, {"block_holidays": False}) is False


def test_holiday_year_scoped_jp() -> None:
    """年別の日本の祝日オブジェクトで将来年も判定できること。"""
    new_year = date(2027, 1, 1)
    assert day_is_blocked_for_booking(new_year, {"block_holidays": True}) is True
