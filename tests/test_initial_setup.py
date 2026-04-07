from __future__ import annotations

import asyncio

from app.booking.db_models import BookingFormDefinition, BookingOrg, BookingService
from app.booking.initial_setup import (
    default_form_fields,
    default_org_availability_defaults,
    default_org_cancel_policy,
    ensure_org_initial_setup,
)
from app.auth.router import _materialize_org_assignment


def test_default_org_availability_defaults_match_expected() -> None:
    defaults = default_org_availability_defaults()
    assert defaults["timezone"] == "Asia/Tokyo"
    assert defaults["start"] == "08:00"
    assert defaults["end"] == "22:00"
    assert defaults["slot_minutes"] == 30
    assert defaults["buffer_minutes"] == 0
    assert defaults["calendar_title_template"] == "{service} — {name}"


def test_default_org_cancel_policy_match_expected() -> None:
    policy = default_org_cancel_policy()
    assert policy["change_until_hours_before"] == 24
    assert policy["same_day_phone_only"] is True


def test_default_form_fields_match_expected() -> None:
    fields = default_form_fields()
    assert fields == [
        {
            "id": "customer_number",
            "type": "text",
            "label": "顧客番号（AP/EP）",
            "placeholder": "例: AP123456",
        }
    ]


def test_ensure_org_initial_setup_adds_service_and_form_when_missing() -> None:
    added: list[object] = []

    class DummySession:
        def __init__(self) -> None:
            self.calls = 0

        async def scalar(self, _query):
            self.calls += 1
            return None

        def add(self, row):
            added.append(row)

    org = BookingOrg(id=10, name="Test Org", slug="test-org")

    asyncio.run(ensure_org_initial_setup(DummySession(), org))

    assert any(isinstance(row, BookingService) and row.name == "初回相談" for row in added)
    assert any(
        isinstance(row, BookingFormDefinition)
        and row.name == "デフォルト"
        and row.fields_json == default_form_fields()
        for row in added
    )


def test_materialize_org_assignment_backfills_existing_org(monkeypatch) -> None:
    org = BookingOrg(id=5, name="Existing Org", slug="existing-org")
    called: dict[str, object] = {}

    class DummySession:
        async def scalar(self, _query):
            return org

    async def fake_ensure(session, target_org):
        called["org_id"] = target_org.id

    monkeypatch.setattr("app.auth.router.ensure_org_initial_setup", fake_ensure)

    slug = asyncio.run(_materialize_org_assignment(DummySession(), "existing-org", None))

    assert slug == "existing-org"
    assert called["org_id"] == 5
