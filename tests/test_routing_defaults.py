"""availability_defaults の数値や JSON 列が壊れていても例外にならないこと。"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.booking.db_models import BookingOrg, BookingService, PublicBookingLink, StaffMember
from app.booking.routing_service import (
    availability_defaults_positive_int,
    json_list_or_empty,
    json_object_or_empty,
    link_daily_booking_limit_per_staff,
    link_round_robin_count,
    normalize_link_round_robin_counters,
    pick_staff_for_slot,
)


def test_positive_int_falls_back_on_empty_string() -> None:
    d = {"slot_minutes": "", "duration": ""}
    assert availability_defaults_positive_int(d, "slot_minutes", 30) == 30
    assert availability_defaults_positive_int(d, "duration", 45) == 45


def test_positive_int_falls_back_on_invalid_string() -> None:
    d = {"slot_minutes": "not-a-number"}
    assert availability_defaults_positive_int(d, "slot_minutes", 30) == 30


def test_positive_int_accepts_numeric_strings() -> None:
    d = {"slot_minutes": "15"}
    assert availability_defaults_positive_int(d, "slot_minutes", 30) == 15


def test_positive_int_zero_falls_back() -> None:
    d = {"duration": 0}
    assert availability_defaults_positive_int(d, "duration", 30) == 30


def test_json_object_or_empty_parses_stringified_json() -> None:
    assert json_object_or_empty('{"start":"08:00","end":"22:00"}') == {"start": "08:00", "end": "22:00"}


def test_json_object_or_empty_falls_back_on_invalid_value() -> None:
    assert json_object_or_empty("not-json") == {}
    assert json_object_or_empty(["bad"]) == {}


def test_json_list_or_empty_parses_stringified_json() -> None:
    assert json_list_or_empty("[1,2,3]") == [1, 2, 3]


def test_json_list_or_empty_falls_back_on_invalid_value() -> None:
    assert json_list_or_empty("not-json") == []
    assert json_list_or_empty({"bad": True}) == []


def test_link_round_robin_count_is_link_scoped() -> None:
    link = PublicBookingLink(round_robin_counters_json={"2": 4, "5": 1})
    assert link_round_robin_count(link, 2) == 4
    assert link_round_robin_count(link, 3) == 0


def test_normalize_link_round_robin_counters_drops_invalid_entries() -> None:
    assert normalize_link_round_robin_counters({"2": "3", "bad": "x", "4": -5}) == {"2": 3, "4": 0}


def test_link_daily_booking_limit_per_staff_normalizes_empty_values() -> None:
    assert link_daily_booking_limit_per_staff(PublicBookingLink(daily_booking_limit_per_staff=None)) is None
    assert link_daily_booking_limit_per_staff(PublicBookingLink(daily_booking_limit_per_staff=0)) is None
    assert link_daily_booking_limit_per_staff(PublicBookingLink(daily_booking_limit_per_staff=3)) == 3


def test_pick_staff_for_slot_hides_staff_when_daily_limit_reached() -> None:
    class DummySession:
        async def flush(self) -> None:
            return None

    org = BookingOrg(id=1, name="Test Org", slug="test-org", availability_defaults_json={})
    service = BookingService(id=1, org_id=1, name="初回相談", duration_minutes=30)
    staff = StaffMember(id=7, org_id=1, name="担当A", email="a@example.com")
    start = datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    local_day = start.astimezone(timezone.utc).date()

    chosen = asyncio.run(
        pick_staff_for_slot(
            DummySession(),
            org,
            [staff.id],
            service,
            start,
            end,
            settings=None,
            staff_list_override=[staff],
            db_busy_map={},
            merged_busy_map={},
            dry_run=True,
            daily_booking_counts_override={local_day: {staff.id: 3}},
            daily_booking_limit_per_staff_override=3,
        )
    )

    assert chosen is None
