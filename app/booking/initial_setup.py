from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.db_models import BookingFormDefinition, BookingOrg, BookingService


def default_org_cancel_policy() -> dict[str, object]:
    return {"change_until_hours_before": 24, "same_day_phone_only": True}


def default_org_availability_defaults() -> dict[str, object]:
    return {
        "timezone": "Asia/Tokyo",
        "start": "08:00",
        "end": "22:00",
        "slot_minutes": 30,
        "buffer_minutes": 0,
        "block_saturday": False,
        "block_sunday": False,
        "block_weekends": False,
        "block_holidays": False,
        "calendar_title_template": "{service} — {name}",
    }


def default_form_fields() -> list[dict[str, object]]:
    return [
        {
            "id": "customer_number",
            "type": "text",
            "label": "顧客番号（AP/EP）",
            "placeholder": "例: AP123456",
        }
    ]


async def ensure_org_initial_setup(session: AsyncSession, org: BookingOrg) -> None:
    first_service = await session.scalar(
        select(BookingService)
        .where(BookingService.org_id == org.id)
        .where(BookingService.active.is_(True))
        .order_by(BookingService.id)
        .limit(1)
    )
    if first_service is None:
        session.add(
            BookingService(
                org_id=org.id,
                name="初回相談",
                duration_minutes=30,
                active=True,
            )
        )

    first_form = await session.scalar(
        select(BookingFormDefinition)
        .where(BookingFormDefinition.org_id == org.id)
        .where(BookingFormDefinition.active.is_(True))
        .order_by(BookingFormDefinition.id)
        .limit(1)
    )
    if first_form is None:
        session.add(
            BookingFormDefinition(
                org_id=org.id,
                name="デフォルト",
                fields_json=default_form_fields(),
                active=True,
            )
        )
