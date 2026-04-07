from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth.bootstrap import run_bootstrap_admin_if_needed
from app.auth.router import router as auth_router
from app.booking.demo_seed import get_demo_booking_info, run_demo_seed_if_enabled
from app.booking.jobs import setup_booking_scheduler, shutdown_booking_scheduler
from app.booking.router import router as booking_router
from app.config import Settings, get_settings
from app.db import get_session_factory, init_db
from app.web.routes import router as web_router
from app.version import __version__

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path("data").mkdir(parents=True, exist_ok=True)
    await init_db()
    settings = get_settings()
    if settings.is_public_deployment():
        if not (settings.booking_session_secret or "").strip():
            raise RuntimeError(
                "公開サーバーでは BOOKING_SESSION_SECRET を必ず設定してください。"
            )
        if settings.booking_seed_demo:
            raise RuntimeError(
                "公開サーバーでは BOOKING_SEED_DEMO=false にしてください。"
            )
    await run_bootstrap_admin_if_needed(settings)
    admin_on = bool(settings.booking_admin_secret.strip())
    logger.info(
        "Booking config: PUBLIC_BASE_URL=%s | legacy X-Admin-Secret %s",
        settings.public_base_url_value(),
        "on" if admin_on else "off (session login or bootstrap user)",
    )
    await run_demo_seed_if_enabled(settings)
    if settings.booking_jobs_embedded:
        setup_booking_scheduler()
    else:
        logger.info("Booking embedded scheduler disabled; run job runner separately.")
    yield
    if settings.booking_jobs_embedded:
        shutdown_booking_scheduler()


_settings_for_session = get_settings()
_session_key = (_settings_for_session.booking_session_secret or "").strip() or secrets.token_hex(32)
if not (_settings_for_session.booking_session_secret or "").strip():
    logger.warning(
        "BOOKING_SESSION_SECRET is empty; using ephemeral session key (all users logged out on restart). "
        "Set BOOKING_SESSION_SECRET in production."
    )

app = FastAPI(
    title="オンライン予約管理",
    description="空き枠・予約・Google カレンダー連携。API は `/docs`、Web は `/app`、状態は `/health`。解約検知 AI は別アプリ `churn-insight-app` を参照。",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if _settings_for_session.api_docs_enabled else None,
    redoc_url="/redoc" if _settings_for_session.api_docs_enabled else None,
    openapi_url="/openapi.json" if _settings_for_session.api_docs_enabled else None,
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_settings_for_session.trusted_hosts(),
)

if _settings_for_session.security_force_https_redirect:
    app.add_middleware(HTTPSRedirectMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_key,
    same_site="lax",
    max_age=14 * 24 * 3600,
    https_only=_settings_for_session.is_https_deployment(),
)


def _security_headers(settings: Settings) -> dict[str, str]:
    csp = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://accounts.google.com https://www.googletagmanager.com; "
        "connect-src 'self' https://accounts.google.com https://www.googleapis.com https://www.googletagmanager.com; "
        "font-src 'self' data:;"
    )
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": csp,
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if settings.is_https_deployment():
        headers["Strict-Transport-Security"] = (
            f"max-age={max(0, int(settings.security_hsts_seconds or 0))}; includeSubDomains"
        )
    return headers


def _is_same_origin(candidate: str, expected_origin: str) -> bool:
    try:
        parsed = urlparse(candidate)
        expected = urlparse(expected_origin)
        if not parsed.scheme or not parsed.netloc or not expected.scheme or not expected.netloc:
            return False
        actual_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        normalized_expected = f"{expected.scheme}://{expected.netloc}".lower()
        if actual_origin == normalized_expected:
            return True
        actual_host = (parsed.hostname or "").lower()
        expected_host = (expected.hostname or "").lower()
        actual_port = parsed.port
        expected_port = expected.port
        is_local_pair = {actual_host, expected_host} <= {"localhost", "127.0.0.1"}
        if is_local_pair and parsed.scheme.lower() == expected.scheme.lower() and actual_port == expected_port:
            return True
        return False
    except Exception:
        return False


def _should_enforce_same_origin(request_path: str, method: str) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    return (
        request_path.startswith("/api/auth/")
        or request_path.startswith("/api/booking/admin/")
        or request_path.endswith("/cancel")
        or request_path.endswith("/reschedule")
    )


def _should_disable_cache(request_path: str) -> bool:
    return (
        request_path.startswith("/app/booking/")
        or request_path.startswith("/app/manage/")
        or request_path.startswith("/api/booking/manage/")
    )


@app.middleware("http")
async def apply_http_security(request, call_next):
    settings = get_settings()
    if _should_enforce_same_origin(request.url.path, request.method):
        expected_origins = {
            settings.public_base_url_value().rstrip("/"),
            str(request.base_url).rstrip("/"),
        }
        origin = (request.headers.get("origin") or "").strip()
        referer = (request.headers.get("referer") or "").strip()
        if origin and not any(_is_same_origin(origin, expected) for expected in expected_origins):
            return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
        if not origin and referer and not any(
            _is_same_origin(referer, expected) for expected in expected_origins
        ):
            return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
    response = await call_next(request)
    for key, value in _security_headers(settings).items():
        response.headers.setdefault(key, value)
    if _should_disable_cache(request.url.path):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

app.include_router(auth_router)
app.include_router(booking_router)
app.include_router(web_router)

_app_dir = Path(__file__).resolve().parent
app.mount(
    "/assets/web",
    StaticFiles(directory=str(_app_dir / "web" / "static")),
    name="assets_web",
)


async def get_db() -> AsyncSession:
    factory = get_session_factory()
    async with factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@app.get("/")
async def root() -> dict[str, Any]:
    out = {
        "service": "オンライン予約管理",
        "version": __version__,
        "links": {
            "app": "/app",
            "login": "/app/login",
            "accounts": "/app/accounts",
            "settings": "/app/settings",
            "campaigns": "/app/campaigns",
            "booking_urls": "/app/campaigns",
            "calendar": "/app/calendar",
            "admin": "/app/admin",
            "health": "/health",
            "oauth_google_status": "/api/booking/oauth/google/status",
        },
        "hint": "booking-app/START.md を参照。解約検知 AI は churn-insight-app です。",
    }
    if _settings_for_session.api_docs_enabled:
        out["links"].update(
            {
                "docs": "/docs",
                "redoc": "/redoc",
                "openapi": "/openapi.json",
            }
        )
    return out


@app.get("/version")
async def version_info() -> dict[str, str]:
    return {"version": __version__}


@app.get("/health")
async def health(settings: SettingsDep) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "ok",
        "service": "booking",
        "booking": {
            "public_base_url": settings.public_base_url_value(),
            "admin_api_enabled": bool(settings.booking_admin_secret.strip()),
            "google_oauth_ready": settings.is_google_oauth_configured(),
        },
    }
    demo = get_demo_booking_info()
    if demo and settings.should_expose_demo_info():
        out["booking_demo"] = demo
    return out
