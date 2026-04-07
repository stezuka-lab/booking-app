"""availability_defaults の数値や JSON 列が壊れていても例外にならないこと。"""

from app.booking.routing_service import (
    availability_defaults_positive_int,
    json_list_or_empty,
    json_object_or_empty,
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
