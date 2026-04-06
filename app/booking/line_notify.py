from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


async def send_line_push(settings: Settings, line_user_id: str, text: str) -> dict[str, Any]:
    token = (settings.line_messaging_channel_access_token or "").strip()
    if not token or not line_user_id.strip():
        return {"ok": False, "skipped": True}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "to": line_user_id.strip(),
        "messages": [{"type": "text", "text": text[:5000]}],
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(LINE_PUSH_URL, json=body, headers=headers)
        if r.status_code >= 400:
            logger.warning("LINE push failed: %s %s", r.status_code, r.text)
            return {"ok": False, "status": r.status_code, "body": r.text}
        return {"ok": True}
    except Exception:
        logger.exception("LINE push error")
        return {"ok": False, "error": "exception"}


async def notify_staff_line_booking(
    settings: Settings,
    *,
    staff_line_user_id: str | None,
    customer_name: str,
    start_iso: str,
    manage_hint: str,
) -> dict[str, Any]:
    if not staff_line_user_id:
        return {"ok": False, "skipped": True}
    text = f"新規予約: {customer_name}\n{start_iso}\n{manage_hint}"
    return await send_line_push(settings, staff_line_user_id, text)
