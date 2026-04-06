"""availability_defaults の数値が空文字などでも例外にならないこと。"""

from app.booking.routing_service import availability_defaults_positive_int


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
