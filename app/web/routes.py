from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.deps import get_current_app_user
from app.booking.demo_seed import get_demo_booking_info
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.version import __version__

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


async def _session_user(request: Request):
    factory = get_session_factory()
    async with factory() as db:
        return await get_current_app_user(request, db)


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
    u = await _session_user(request)
    if not u:
        return RedirectResponse(url="/app/login", status_code=302)
    return _html("home.html", request, settings)


@router.get("/app/login", response_class=HTMLResponse)
async def app_login(request: Request) -> Any:
    settings = get_settings()
    nxt = (request.query_params.get("next") or "").strip() or "/app"
    return _html("login.html", request, settings, login_next=nxt)


@router.get("/app/forgot-password", response_class=HTMLResponse)
async def app_forgot_password(request: Request) -> Any:
    settings = get_settings()
    return _html("forgot_password.html", request, settings)


@router.get("/app/reset-password", response_class=HTMLResponse)
async def app_reset_password(request: Request) -> Any:
    settings = get_settings()
    tok = (request.query_params.get("token") or "").strip()
    return _html("reset_password.html", request, settings, reset_token=tok)


@router.get("/app/accounts", response_class=HTMLResponse)
async def app_accounts(request: Request) -> Any:
    settings = get_settings()
    redir = await _require_login_html(request, "/app/accounts")
    if redir:
        return redir
    return _html("accounts.html", request, settings, viewer_is_admin=await _viewer_is_admin(request))


@router.get("/app/booking/{token}", response_class=HTMLResponse)
async def app_booking(request: Request, token: str) -> Any:
    settings = get_settings()
    return _html("booking.html", request, settings, link_token=token)


@router.get("/app/manage/{manage_token}", response_class=HTMLResponse)
async def app_booking_manage(request: Request, manage_token: str) -> Any:
    settings = get_settings()
    return _html("manage_booking.html", request, settings, manage_token=manage_token)


@router.get("/app/admin", response_class=HTMLResponse)
async def app_admin(request: Request) -> Any:
    settings = get_settings()
    redir = await _require_login_html(request, "/app/admin")
    if redir:
        return redir
    return _html("admin.html", request, settings, viewer_is_admin=await _viewer_is_admin(request))


@router.get("/app/campaigns", response_class=HTMLResponse)
async def app_campaigns_alias(request: Request) -> Any:
    settings = get_settings()
    redir = await _require_login_html(request, "/app/campaigns")
    if redir:
        return redir
    return _html("admin.html", request, settings, viewer_is_admin=await _viewer_is_admin(request))


@router.get("/app/settings", response_class=HTMLResponse)
async def app_settings(request: Request) -> Any:
    settings = get_settings()
    redir = await _require_login_html(request, "/app/settings")
    if redir:
        return redir
    return _html("settings.html", request, settings, viewer_is_admin=await _viewer_is_admin(request))


@router.get("/app/calendar", response_class=HTMLResponse)
async def app_calendar(request: Request) -> Any:
    settings = get_settings()
    redir = await _require_login_html(request, "/app/calendar")
    if redir:
        return redir
    return _html("calendar.html", request, settings, viewer_is_admin=await _viewer_is_admin(request))
