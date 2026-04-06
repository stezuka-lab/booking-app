"""カレンダー予定件名テンプレート。"""

from unittest.mock import MagicMock

from app.booking.calendar_title import format_calendar_event_title, DEFAULT_CALENDAR_TITLE_TEMPLATE


def test_default_template_joins_service_and_name() -> None:
    org = MagicMock()
    org.availability_defaults_json = {}
    b = MagicMock()
    b.customer_name = "山田"
    b.customer_phone = None
    b.company_name = None
    b.calendar_title_note = None
    b.form_answers_json = {}
    out = format_calendar_event_title(org, "30分相談", b)
    assert "30分相談" in out
    assert "山田" in out


def test_custom_template_with_company_and_note() -> None:
    org = MagicMock()
    org.availability_defaults_json = {
        "calendar_title_template": "【{note}】{name}（{company}）{service}",
    }
    b = MagicMock()
    b.customer_name = "佐藤"
    b.customer_phone = "03-0000"
    b.company_name = "株式会社テスト"
    b.calendar_title_note = "初回"
    b.form_answers_json = {}
    out = format_calendar_event_title(org, "相談", b)
    assert "【初回】" in out
    assert "佐藤" in out
    assert "株式会社テスト" in out
    assert "相談" in out


def test_customer_number_in_form_answers_maps_to_note() -> None:
    org = MagicMock()
    org.availability_defaults_json = {"calendar_title_template": "{customer_number}-{name}"}
    b = MagicMock()
    b.customer_name = "A"
    b.customer_phone = None
    b.company_name = None
    b.calendar_title_note = None
    b.form_answers_json = {"customer_number": "C-001"}
    out = format_calendar_event_title(org, "相談", b)
    assert "C-001" in out
    assert "A" in out


def test_default_constant() -> None:
    assert "{service}" in DEFAULT_CALENDAR_TITLE_TEMPLATE
    assert "{name}" in DEFAULT_CALENDAR_TITLE_TEMPLATE
