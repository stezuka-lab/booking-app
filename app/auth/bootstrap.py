"""初回のみ: 環境変数で指定した管理者を 1 名だけ自動作成。"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.auth.models import AppUser
from app.auth.passwords import hash_password
from app.config import Settings
from app.db import get_session_factory

logger = logging.getLogger(__name__)


async def run_bootstrap_admin_if_needed(settings: Settings) -> None:
    u = (settings.booking_bootstrap_admin_user or "").strip()
    p = (settings.booking_bootstrap_admin_password or "").strip()
    if not u or not p:
        return
    factory = get_session_factory()
    async with factory() as db:
        n = await db.scalar(select(func.count()).select_from(AppUser))
        if (n or 0) > 0:
            return
        db.add(AppUser(username=u, password_hash=hash_password(p), role="admin", display_name="管理者"))
        await db.commit()
        logger.info("Bootstrap: created initial admin user %r", u)
