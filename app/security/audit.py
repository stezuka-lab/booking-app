from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AppUser
from app.booking.db_models import BookingAuditLog


async def current_actor_from_request(request: Request | None, db: AsyncSession) -> AppUser | None:
    if request is None:
        return None
    uid = request.session.get("user_id")
    if uid is None:
        return None
    user = await db.get(AppUser, int(uid))
    if not user or not user.is_active:
        return None
    return user


async def write_audit_log(
    db: AsyncSession,
    request: Request | None,
    *,
    action: str,
    org_slug: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    actor = await current_actor_from_request(request, db)
    ip = ((request.client.host if request and request.client else "") or None)
    ua = ((request.headers.get("user-agent") if request else "") or "").strip() or None
    if ua and len(ua) > 512:
        ua = ua[:512]
    log = BookingAuditLog(
        actor_user_id=actor.id if actor else None,
        actor_username=actor.username if actor else ("api-secret" if request and request.headers.get("x-admin-secret") else None),
        actor_role=actor.role if actor else ("secret" if request and request.headers.get("x-admin-secret") else None),
        org_slug=(org_slug or "").strip() or None,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        ip_address=ip,
        user_agent=ua,
        detail_json=detail or {},
    )
    db.add(log)
