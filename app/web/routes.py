from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.deps import get_current_app_user
from app.booking.db_models import BookingOrg
from app.booking.demo_seed import get_demo_booking_info
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.version import __version__
from sqlalchemy import select

logger = logging.getLogger(__name__)
_TEMPLATES = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES))

router = APIRouter(tags=["web-app"])


def _html(template_name: str, request: Request, settings: Settings, **extra: Any) -> HTMLResponse:
    tpl = templates.env.get_template(template_name)
    return HTMLResponse(content=tpl.render(**_ctx(request, settings, **extra)))


def _ctx(request: Request, settings: Settings, **extra: Any) -> dict[str, Any]:
    base = settings.public_base_url_value()
    return {
        "request": request,
        "public_base": base,
        "version": __version__,
        "demo": get_demo_booking_info(),
        **extra,
    }


def _viewer_payload(user: Any) -> dict[str, Any]:
    if not user:
        return {"authenticated": False, "user": None}
    return {
        "authenticated": True,
        "user": {
            "id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "display_name": getattr(user, "display_name", None),
            "role": getattr(user, "role", None),
            "default_org_slug": getattr(user, "default_org_slug", None),
            "default_org_name": getattr(user, "_default_org_name", None),
        },
    }


async def _session_user(request: Request):
    factory = get_session_factory()
    async with factory() as db:
        try:
            return await get_current_app_user(request, db)
        except Exception:
            logger.exception("Failed to resolve session user for path %s", getattr(request.url, "path", ""))
            request.session.clear()
            return None


async def _load_session_user_with_org(request: Request) -> Any:
    factory = get_session_factory()
    async with factory() as db:
        try:
            user = await get_current_app_user(request, db)
        except Exception:
            logger.exception("Failed to resolve session user for path %s", getattr(request.url, "path", ""))
            request.session.clear()
            return None
        if not user or not getattr(user, "default_org_slug", None):
            return user
        try:
            org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == user.default_org_slug))
            setattr(user, "_default_org_name", org.name if org else None)
        except Exception:
            logger.exception("Failed to resolve default org for user %s", getattr(user, "id", None))
            setattr(user, "_default_org_name", None)
        return user


async def _attach_default_org_name(user: Any) -> Any:
    if not user or not getattr(user, "default_org_slug", None):
        return user
    factory = get_session_factory()
    async with factory() as db:
        try:
            org = await db.scalar(select(BookingOrg).where(BookingOrg.slug == user.default_org_slug))
            setattr(user, "_default_org_name", org.name if org else None)
        except Exception:
            logger.exception("Failed to resolve default org for user %s", getattr(user, "id", None))
            setattr(user, "_default_org_name", None)
    return user


async def _viewer_is_admin(request: Request) -> bool:
    u = await _session_user(request)
    return bool(u and u.role == "admin")


async def _require_admin_html(request: Request, next_path: str) -> RedirectResponse | None:
    u = await _session_user(request)
    if not u:
        return RedirectResponse(url="/app/login?next=" + quote(next_path, safe=""), status_code=302)
    if u.role != "admin":
        return RedirectResponse(url="/app?notice=admin_only", status_code=302)
    return None


async def _require_login_html(request: Request, next_path: str) -> RedirectResponse | None:
    u = await _session_user(request)
    if not u:
        return RedirectResponse(url="/app/login?next=" + quote(next_path, safe=""), status_code=302)
    return None


@router.get("/app", response_class=HTMLResponse)
async def app_home(request: Request) -> Any:
    settings = get_settings()
    u = await _load_session_user_with_org(request)
    if not u:
        return RedirectResponse(url="/app/login", status_code=302)
    return _html("home.html", request, settings, app_viewer=_viewer_payload(u))


@router.get("/app/login", response_class=HTMLResponse)
async def app_login(request: Request) -> Any:
    settings = get_settings()
    nxt = (request.query_params.get("next") or "").strip() or "/app"
    return _html("login.html", request, settings, login_next=nxt, app_viewer=_viewer_payload(await _load_session_user_with_org(request)))


@router.get("/app/forgot-password", response_class=HTMLResponse)
async def app_forgot_password(request: Request) -> Any:
    settings = get_settings()
    return _html("forgot_password.html", request, settings, app_viewer=_viewer_payload(await _load_session_user_with_org(request)))


@router.get("/app/reset-password", response_class=HTMLResponse)
async def app_reset_password(request: Request) -> Any:
    settings = get_settings()
    tok = (request.query_params.get("token") or "").strip()
    return _html("reset_password.html", request, settings, reset_token=tok, app_viewer=_viewer_payload(await _load_session_user_with_org(request)))


@router.get("/app/accounts", response_class=HTMLResponse)
async def app_accounts(request: Request) -> Any:
    settings = get_settings()
    u = await _load_session_user_with_org(request)
    if not u:
        return RedirectResponse(url="/app/login?next=" + quote("/app/accounts", safe=""), status_code=302)
    return _html("accounts.html", request, settings, viewer_is_admin=bool(u and u.role == "admin"), app_viewer=_viewer_payload(u))


@router.get("/app/booking/{token}", response_class=HTMLResponse)
async def app_booking(request: Request, token: str) -> Any:
    settings = get_settings()
    return _html("booking.html", request, settings, link_token=token, app_viewer=_viewer_payload(await _load_session_user_with_org(request)))


@router.get("/app/manage/{manage_token}", response_class=HTMLResponse)
async def app_booking_manage(request: Request, manage_token: str) -> Any:
    settings = get_settings()
    return _html("manage_booking.html", request, settings, manage_token=manage_token, app_viewer=_viewer_payload(await _load_session_user_with_org(request)))


@router.get("/app/admin", response_class=HTMLResponse)
async def app_admin(request: Request) -> Any:
    settings = get_settings()
    u = await _load_session_user_with_org(request)
    if not u:
        return RedirectResponse(url="/app/login?next=" + quote("/app/admin", safe=""), status_code=302)
    return _html("admin.html", request, settings, viewer_is_admin=bool(u and u.role == "admin"), app_viewer=_viewer_payload(u))


@router.get("/app/campaigns", response_class=HTMLResponse)
async def app_campaigns_alias(request: Request) -> Any:
    settings = get_settings()
    u = await _load_session_user_with_org(request)
    if not u:
        return RedirectResponse(url="/app/login?next=" + quote("/app/campaigns", safe=""), status_code=302)
    return _html("admin.html", request, settings, viewer_is_admin=bool(u and u.role == "admin"), app_viewer=_viewer_payload(u))


@router.get("/app/settings", response_class=HTMLResponse)
async def app_settings(request: Request) -> Any:
    settings = get_settings()
    u = await _load_session_user_with_org(request)
    if not u:
        return RedirectResponse(url="/app/login?next=" + quote("/app/settings", safe=""), status_code=302)
    return _html("settings.html", request, settings, viewer_is_admin=bool(u and u.role == "admin"), app_viewer=_viewer_payload(u))


@router.get("/app/calendar", response_class=HTMLResponse)
async def app_calendar(request: Request) -> Any:
    settings = get_settings()
    u = await _load_session_user_with_org(request)
    if not u:
        return RedirectResponse(url="/app/login?next=" + quote("/app/calendar", safe=""), status_code=302)
    return _html("calendar.html", request, settings, viewer_is_admin=bool(u and u.role == "admin"), app_viewer=_viewer_payload(u))
