from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import delete, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_app_user, require_session_admin_only
from app.auth.models import AppUser, PasswordResetToken
from app.auth.passwords import hash_password, verify_password
from app.auth.rate_limit import (
    check_login_rate_limit,
    check_password_reset_rate_limit,
    clear_login_failures,
    record_login_failure,
    record_password_reset_attempt,
)
from app.auth.schemas import (
    AdminSetPasswordBody,
    AdminUserCreate,
    AdminUserOrgPatch,
    ForgotPasswordBody,
    LoginBody,
    ResetPasswordBody,
    UserPreferencesPatch,
)
from app.booking.initial_setup import (
    default_org_availability_defaults,
    default_org_cancel_policy,
    ensure_org_initial_setup,
)
from app.booking.db_models import BookingAuditLog, BookingOrg
from app.booking.email_booking import send_simple_mail
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.security.audit import write_audit_log

router = APIRouter(tags=["auth"])
_ORG_SLUG_SANITIZE_RE = re.compile(r"[^a-z0-9]+")


async def _session_db() -> AsyncSession:
    factory = get_session_factory()
    async with factory() as session:
        yield session


AuthDb = Annotated[AsyncSession, Depends(_session_db)]


async def _materialize_org_assignment(
    db: AsyncSession,
    org_slug_in: str | None,
    org_name_in: str | None,
) -> str | None:
    """操作中の組織として使う slug を返す。紐付けなしは None。既存組織があれば表示名が渡されていれば更新。"""
    if not org_slug_in:
        if org_name_in and org_name_in.strip():
            raise HTTPException(400, "組織 slug を入力してください。")
        return None
    slug = org_slug_in
    name_stripped = (org_name_in or "").strip() or None
    existing = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
    if existing:
        if name_stripped is not None and name_stripped != existing.name:
            existing.name = name_stripped
        await ensure_org_initial_setup(db, existing)
        return slug
    if not name_stripped:
        raise HTTPException(
            400,
            "指定の組織 slug はまだありません。新規に作成する場合は「組織の表示名」も入力してください。",
        )
    new_org = BookingOrg(
        name=name_stripped,
        slug=slug,
        routing_mode="priority",
        cancel_policy_json=default_org_cancel_policy(),
        availability_defaults_json=default_org_availability_defaults(),
    )
    db.add(new_org)
    try:
        await db.flush()
        await ensure_org_initial_setup(db, new_org)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, "この組織 slug は既に使われています")
    return slug


async def _default_org_assignment_for_user(
    db: AsyncSession,
    username: str,
    display_name: str | None,
) -> str:
    base_slug = _ORG_SLUG_SANITIZE_RE.sub("-", (username or "").strip().lower()).strip("-")
    if not base_slug:
        base_slug = "team"
    slug = base_slug
    seq = 2
    while await db.scalar(select(BookingOrg.id).where(BookingOrg.slug == slug)):
        slug = f"{base_slug}-{seq}"
        seq += 1
    org_name = (display_name or "").strip() or f"{username} 用"
    out = await _materialize_org_assignment(db, slug, org_name)
    if not out:
        raise HTTPException(500, "default org assignment failed")
    return out


@router.post("/api/auth/login")
async def login(
    request: Request,
    body: LoginBody,
    db: AuthDb,
    settings: Settings = Depends(get_settings),
) -> dict:
    username = body.username.strip()
    check_login_rate_limit(
        request,
        username,
        max_attempts=max(1, int(settings.auth_rate_limit_max_attempts or 10)),
        window_sec=max(60, int(settings.auth_rate_limit_window_sec or 900)),
    )
    u = await db.scalar(select(AppUser).where(AppUser.username == username))
    if not u or not u.is_active:
        record_login_failure(
            request,
            username,
            window_sec=max(60, int(settings.auth_rate_limit_window_sec or 900)),
        )
        raise HTTPException(401, "ユーザーIDまたはパスワードが正しくありません")
    if not verify_password(body.password, u.password_hash):
        record_login_failure(
            request,
            username,
            window_sec=max(60, int(settings.auth_rate_limit_window_sec or 900)),
        )
        raise HTTPException(401, "ユーザーIDまたはパスワードが正しくありません")
    clear_login_failures(request, username)
    request.session["user_id"] = u.id
    return {
        "ok": True,
        "user": {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "display_name": u.display_name or "",
            "default_org_slug": u.default_org_slug,
        },
    }


@router.post("/api/auth/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@router.get("/api/auth/me")
async def me(request: Request, db: AuthDb) -> dict:
    u = await get_current_app_user(request, db)
    if not u:
        return {"authenticated": False}
    default_org_name: str | None = None
    if u.default_org_slug:
        org_row = await db.scalar(select(BookingOrg).where(BookingOrg.slug == u.default_org_slug))
        if org_row is not None:
            default_org_name = (org_row.name or "").strip() or None
    return {
        "authenticated": True,
        "user": {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "display_name": u.display_name or "",
            "email": u.email,
            "default_org_slug": u.default_org_slug,
            "default_org_name": default_org_name,
        },
    }


@router.patch("/api/auth/me/preferences")
async def patch_me_preferences(
    request: Request,
    body: UserPreferencesPatch,
    db: AuthDb,
) -> dict:
    u = await get_current_app_user(request, db)
    if not u:
        raise HTTPException(401, "ログインが必要です")
    if body.default_org_slug is not None:
        raw = (body.default_org_slug or "").strip()
        if not raw:
            u.default_org_slug = None
        else:
            slug = raw.lower()
            org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
            if not org:
                raise HTTPException(400, "指定の組織が見つかりません")
            u.default_org_slug = slug
    await db.commit()
    await db.refresh(u)
    return {"ok": True, "default_org_slug": u.default_org_slug}


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@router.post("/api/auth/forgot-password")
async def forgot_password(
    request: Request,
    body: ForgotPasswordBody,
    db: AuthDb,
    settings: Settings = Depends(get_settings),
) -> dict:
    identifier = f"{body.username.strip()}::{str(body.email).strip().lower()}"
    check_password_reset_rate_limit(
        request,
        identifier,
        max_attempts=max(1, int(settings.password_reset_rate_limit_max_attempts or 5)),
        window_sec=max(60, int(settings.password_reset_rate_limit_window_sec or 3600)),
    )
    record_password_reset_attempt(
        request,
        identifier,
        window_sec=max(60, int(settings.password_reset_rate_limit_window_sec or 3600)),
    )
    u = await db.scalar(select(AppUser).where(AppUser.username == body.username.strip()))
    if not u or not u.is_active:
        return {"ok": True, "message": "該当する場合はメールを送信しました"}
    em = (u.email or "").strip().lower()
    if not em or em != str(body.email).strip().lower():
        return {"ok": True, "message": "該当する場合はメールを送信しました"}
    if not settings.smtp_host:
        raise HTTPException(
            503,
            "メール送信が未設定です。管理者にパスワード再設定を依頼するか、SMTP を .env に設定してください。",
        )
    raw = secrets.token_urlsafe(32)
    th = _token_hash(raw)
    exp = datetime.now(timezone.utc) + timedelta(hours=2)
    await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == u.id))
    db.add(PasswordResetToken(user_id=u.id, token_hash=th, expires_at=exp))
    await db.commit()
    base = settings.public_base_url_value()
    link = f"{base}/app/reset-password?token={raw}"
    text = (
        f"{u.username} 様\n\n"
        "パスワード再設定のリクエストを受け付けました。次のリンクから新しいパスワードを設定してください（2時間有効）。\n\n"
        f"{link}\n\n"
        "心当たりがない場合はこのメールを無視してください。"
    )
    ok = await send_simple_mail(
        settings,
        [em],
        "[予約管理] パスワード再設定",
        text,
        dry_run=settings.actions_dry_run,
    )
    if not ok and not settings.actions_dry_run:
        raise HTTPException(500, "メール送信に失敗しました")
    await write_audit_log(
        db,
        request,
        action="auth.password_reset_requested",
        target_type="app_user",
        target_id=u.id,
        detail={"username": u.username},
    )
    await db.commit()
    return {"ok": True, "message": "該当する場合はメールを送信しました"}


@router.post("/api/auth/reset-password")
async def reset_password_ep(request: Request, body: ResetPasswordBody, db: AuthDb) -> dict:
    th = _token_hash(body.token.strip())
    now = datetime.now(timezone.utc)
    row = await db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == th,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
    )
    if not row:
        raise HTTPException(400, "リンクが無効か期限切れです。再度お手続きください。")
    user = await db.get(AppUser, row.user_id)
    if not user or not user.is_active:
        raise HTTPException(400, "アカウントが無効です")
    user.password_hash = hash_password(body.new_password)
    row.used_at = now
    await write_audit_log(
        db,
        request,
        action="auth.password_reset_completed",
        target_type="app_user",
        target_id=user.id,
        detail={"username": user.username},
    )
    await db.commit()
    return {"ok": True}


@router.get("/api/auth/admin/users")
async def admin_list_users(
    request: Request,
    db: AuthDb,
    settings: Settings = Depends(get_settings),
    x_admin_secret: str | None = Header(None),
) -> dict:
    await require_session_admin_only(request, settings, db, x_admin_secret)
    rows = (await db.scalars(select(AppUser).order_by(AppUser.id))).all()
    slugs = [s for s in (u.default_org_slug for u in rows) if s]
    org_names: dict[str, str] = {}
    if slugs:
        org_rows = (await db.scalars(select(BookingOrg).where(BookingOrg.slug.in_(slugs)))).all()
        for o in org_rows:
            org_names[o.slug] = o.name
    return {
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "is_active": u.is_active,
                "email": u.email,
                "display_name": u.display_name,
                "default_org_slug": u.default_org_slug,
                "org_name": org_names.get(u.default_org_slug) if u.default_org_slug else None,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in rows
        ]
    }


@router.post("/api/auth/admin/users")
async def admin_create_user(
    request: Request,
    body: AdminUserCreate,
    db: AuthDb,
    settings: Settings = Depends(get_settings),
    x_admin_secret: str | None = Header(None),
) -> dict:
    await require_session_admin_only(request, settings, db, x_admin_secret)
    un = body.username.strip()
    if not un:
        raise HTTPException(400, "ユーザーIDを入力してください")
    exists = await db.scalar(select(AppUser.id).where(AppUser.username == un))
    if exists:
        raise HTTPException(400, "このユーザーIDは既に使われています")
    role = (body.role or "user").strip().lower()
    if role not in ("admin", "user"):
        raise HTTPException(400, "role は admin または user です")
    n = await db.scalar(select(func.count()).select_from(AppUser))
    if (n or 0) == 0:
        role = "admin"

    default_org_slug = await _materialize_org_assignment(db, body.org_slug, body.org_name)
    if not default_org_slug:
        default_org_slug = await _default_org_assignment_for_user(
            db,
            un,
            body.display_name or body.org_name,
        )

    u = AppUser(
        username=un,
        password_hash=hash_password(body.password),
        role=role,
        email=(body.email or "").strip() or None,
        display_name=(body.display_name or "").strip(),
        default_org_slug=default_org_slug,
    )
    db.add(u)
    await db.flush()
    await write_audit_log(
        db,
        request,
        action="auth.user_created",
        org_slug=default_org_slug,
        target_type="app_user",
        target_id=u.id,
        detail={"username": u.username, "role": u.role},
    )
    await db.commit()
    await db.refresh(u)
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "default_org_slug": u.default_org_slug,
    }


@router.patch("/api/auth/admin/users/{user_id}/org")
async def admin_patch_user_org(
    user_id: int,
    request: Request,
    body: AdminUserOrgPatch,
    db: AuthDb,
    settings: Settings = Depends(get_settings),
    x_admin_secret: str | None = Header(None),
) -> dict[str, Any]:
    await require_session_admin_only(request, settings, db, x_admin_secret)
    user = await db.get(AppUser, user_id)
    if not user:
        raise HTTPException(404, "user not found")
    slug = await _materialize_org_assignment(db, body.org_slug, body.org_name)
    user.default_org_slug = slug
    await write_audit_log(
        db,
        request,
        action="auth.user_org_updated",
        org_slug=slug,
        target_type="app_user",
        target_id=user.id,
        detail={"username": user.username},
    )
    await db.commit()
    await db.refresh(user)
    org_name_out: str | None = None
    if slug:
        org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == slug))
        org_name_out = org.name if org else None
    return {"ok": True, "default_org_slug": user.default_org_slug, "org_name": org_name_out}


@router.delete("/api/auth/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    request: Request,
    db: AuthDb,
    settings: Settings = Depends(get_settings),
    x_admin_secret: str | None = Header(None),
) -> dict:
    actor = await require_session_admin_only(request, settings, db, x_admin_secret)
    if user_id == actor.id:
        raise HTTPException(400, "自分自身は削除できません")
    u = await db.get(AppUser, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    admin_cnt = await db.scalar(
        select(func.count()).select_from(AppUser).where(AppUser.role == "admin", AppUser.is_active.is_(True))
    )
    if u.role == "admin" and admin_cnt is not None and int(admin_cnt) <= 1:
        raise HTTPException(400, "最後の管理者は削除できません")
    await write_audit_log(
        db,
        request,
        action="auth.user_deleted",
        org_slug=u.default_org_slug,
        target_type="app_user",
        target_id=u.id,
        detail={"username": u.username, "role": u.role},
    )
    await db.execute(delete(AppUser).where(AppUser.id == user_id))
    await db.commit()
    return {"ok": True}


@router.post("/api/auth/admin/users/{user_id}/password")
async def admin_set_user_password(
    user_id: int,
    request: Request,
    body: AdminSetPasswordBody,
    db: AuthDb,
    settings: Settings = Depends(get_settings),
    x_admin_secret: str | None = Header(None),
) -> dict:
    await require_session_admin_only(request, settings, db, x_admin_secret)
    u = await db.get(AppUser, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    u.password_hash = hash_password(body.new_password)
    await write_audit_log(
        db,
        request,
        action="auth.user_password_updated",
        org_slug=u.default_org_slug,
        target_type="app_user",
        target_id=u.id,
        detail={"username": u.username},
    )
    await db.commit()
    return {"ok": True}


@router.get("/api/auth/admin/audit-logs")
async def admin_list_audit_logs(
    request: Request,
    db: AuthDb,
    settings: Settings = Depends(get_settings),
    x_admin_secret: str | None = Header(None),
) -> dict[str, Any]:
    await require_session_admin_only(request, settings, db, x_admin_secret)
    rows = (
        await db.scalars(
            select(BookingAuditLog).order_by(desc(BookingAuditLog.created_at), desc(BookingAuditLog.id)).limit(200)
        )
    ).all()
    return {
        "logs": [
            {
                "id": row.id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "action": row.action,
                "org_slug": row.org_slug,
                "actor_username": row.actor_username,
                "actor_role": row.actor_role,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "ip_address": row.ip_address,
                "detail": row.detail_json or {},
            }
            for row in rows
        ]
    }
