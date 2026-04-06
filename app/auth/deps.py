from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AppUser
from app.config import Settings


async def ensure_booking_admin(
    request: Request,
    settings: Settings,
    db: AsyncSession,
    x_admin_secret: str | None,
    *,
    org_slug: str | None = None,
) -> None:
    """X-Admin-Secret、または管理者セッション、または ``org_slug`` がログインユーザーの操作中の組織と一致するセッション。

    * ``org_slug`` が ``None`` の操作（例: 全組織一覧・新規組織作成）は **管理者** または **シークレット** のみ。
    * ``org_slug`` ありは、その組織に紐づく **一般ユーザー** も可（``default_org_slug`` が一致すること）。
    """
    sec = (settings.booking_admin_secret or "").strip()
    if sec and (x_admin_secret or "").strip() == sec:
        return
    user = await get_current_app_user(request, db)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="ログインするか、正しい X-Admin-Secret を指定してください",
        )
    if user.role == "admin":
        return
    if org_slug is not None:
        uslug = (user.default_org_slug or "").strip()
        if not uslug:
            raise HTTPException(
                status_code=403,
                detail="操作中の組織が未設定です。再読み込みするか管理者に連絡してください。",
            )
        if uslug == org_slug.strip():
            return
        raise HTTPException(status_code=403, detail="この組織を操作する権限がありません")
    raise HTTPException(status_code=403, detail="この操作には管理者権限が必要です")


async def get_current_app_user(request: Request, db: AsyncSession) -> AppUser | None:
    uid = request.session.get("user_id")
    if uid is None:
        return None
    user = await db.get(AppUser, int(uid))
    if not user or not user.is_active:
        return None
    return user


async def require_admin_user(request: Request, db: AsyncSession) -> AppUser:
    u = await get_current_app_user(request, db)
    if not u or u.role != "admin":
        raise HTTPException(status_code=403, detail="管理者権限が必要です")
    return u


async def require_session_admin_only(
    request: Request,
    settings: Settings,
    db: AsyncSession,
    x_admin_secret: str | None,
) -> AppUser:
    """アカウント管理 API: 共有シークレットだけでは不可。ブラウザで管理者ログイン必須。"""
    sec = (settings.booking_admin_secret or "").strip()
    if sec and (x_admin_secret or "").strip() == sec:
        raise HTTPException(
            403,
            "アカウント管理は、ブラウザで管理者としてログインしたうえで操作してください（X-Admin-Secret のみでは利用できません）。",
        )
    return await require_admin_user(request, db)


async def count_app_users(db: AsyncSession) -> int:
    from sqlalchemy import func

    r = await db.scalar(select(func.count()).select_from(AppUser))
    return int(r or 0)
