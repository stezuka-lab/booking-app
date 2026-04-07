"""起動時またはスクリプトから呼び出すデモデータ投入（冪等）。"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.db_models import (
    BookingFormDefinition,
    BookingOrg,
    BookingService,
    PublicBookingLink,
    StaffMember,
)
from app.config import Settings

logger = logging.getLogger(__name__)

DEMO_SLUG = "demo-shop"

_demo_info: dict[str, Any] | None = None


def get_demo_booking_info() -> dict[str, Any] | None:
    """直近の ensure で記録したデモ URL など。"""
    return _demo_info


async def ensure_demo_booking_data(session: AsyncSession, settings: Settings) -> dict[str, Any] | None:
    """
    demo-shop が無ければ店舗・担当・予約区分・フォーム・公開リンクを作成。
    既にあればトークンを読み取り、リンクが無ければ追加。
    戻り値: public_url 等（ログ・/health 用）
    """
    global _demo_info
    slug = DEMO_SLUG
    base = settings.public_base_url_value()

    org = await session.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    token: str

    if org:
        adv = dict(org.availability_defaults_json or {})
        if not adv.get("timezone"):
            adv["timezone"] = "Asia/Tokyo"
            org.availability_defaults_json = adv
            await session.commit()
        demo_links = (
            await session.scalars(
                select(PublicBookingLink)
                .where(PublicBookingLink.org_id == org.id)
                .where(PublicBookingLink.title == "デモ予約")
                .order_by(PublicBookingLink.id)
            )
        ).all()
        if len(demo_links) > 1:
            keep = demo_links[0]
            for extra in demo_links[1:]:
                await session.execute(delete(PublicBookingLink).where(PublicBookingLink.id == extra.id))
            await session.commit()
            link = keep
        elif demo_links:
            link = demo_links[0]
        else:
            link = await session.scalar(
                select(PublicBookingLink)
                .where(PublicBookingLink.org_id == org.id)
                .order_by(PublicBookingLink.id)
                .limit(1)
            )
        if link:
            token = link.token
            if link.service_id is None:
                svc_fix = await session.scalar(
                    select(BookingService)
                    .where(BookingService.org_id == org.id, BookingService.active.is_(True))
                    .order_by(BookingService.id)
                    .limit(1)
                )
                if svc_fix:
                    link.service_id = svc_fix.id
                    await session.commit()
        else:
            token = secrets.token_urlsafe(16)
            svc_new = await session.scalar(
                select(BookingService)
                .where(BookingService.org_id == org.id)
                .where(BookingService.active.is_(True))
                .order_by(BookingService.id)
                .limit(1)
            )
            session.add(
                PublicBookingLink(
                    org_id=org.id,
                    token=token,
                    title="デモ予約",
                    staff_ids_json=[],
                    service_id=svc_new.id if svc_new else None,
                )
            )
            await session.commit()
    else:
        org = BookingOrg(
            name="デモ店舗",
            slug=slug,
            routing_mode="priority",
            auto_confirm=False,
            cancel_policy_json={"change_until_hours_before": 24, "same_day_phone_only": True},
            availability_defaults_json={
                "timezone": "Asia/Tokyo",
                "start": "08:00",
                "end": "22:00",
                "slot_minutes": 30,
                "block_saturday": False,
                "block_sunday": False,
                "block_weekends": False,
                "block_holidays": False,
            },
        )
        session.add(org)
        await session.flush()

        session.add(
            StaffMember(
                org_id=org.id,
                name="デモ担当",
                email="staff@example.com",
                priority_rank=10,
            )
        )
        demo_svc = BookingService(org_id=org.id, name="30分相談", duration_minutes=30)
        session.add(demo_svc)
        fields = [
            {"id": "customer_number", "type": "text", "label": "顧客番号", "placeholder": "任意"},
        ]
        session.add(
            BookingFormDefinition(org_id=org.id, name="デフォルト", fields_json=fields, active=True)
        )
        await session.flush()
        token = secrets.token_urlsafe(16)
        session.add(
            PublicBookingLink(
                org_id=org.id,
                token=token,
                title="デモ予約",
                staff_ids_json=[],
                service_id=demo_svc.id,
            )
        )
        await session.commit()

    booking_path = f"/app/booking/{token}"
    public_url = f"{base}{booking_path}"
    _demo_info = {
        "slug": slug,
        "token": token,
        "booking_path": booking_path,
        "public_url": public_url,
        "docs_url": f"{base}/docs",
        "admin_summary_path": f"/api/booking/admin/orgs/{slug}/summary",
    }
    logger.info("Booking demo: %s (same-host path: %s)", public_url, booking_path)
    return _demo_info


async def run_demo_seed_if_enabled(settings: Settings) -> dict[str, Any] | None:
    """設定で有効なときだけ DB セッションを開いて投入。"""
    if not settings.booking_seed_demo:
        return None
    from app.db import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            return await ensure_demo_booking_data(session, settings)
        except Exception:
            logger.exception("Booking demo seed failed")
            return None
