"""Google カレンダーに載せる予定の件名（タイトル）を組み立てる。"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.booking.db_models import Booking, BookingOrg

logger = logging.getLogger(__name__)

# プレースホルダ: {service} {name} {company} {phone} {note}
DEFAULT_CALENDAR_TITLE_TEMPLATE = "{service} — {name}"

_MAX_TITLE_LEN = 1024


def format_calendar_event_title(
    org: "BookingOrg",
    service_name: str,
    booking: "Booking",
) -> str:
    """availability_defaults.calendar_title_template に従い件名を生成。未設定は従来形式に近いデフォルト。"""
    defaults = org.availability_defaults_json or {}
    raw = (defaults.get("calendar_title_template") or "").strip()
    tpl = raw or DEFAULT_CALENDAR_TITLE_TEMPLATE

    fa = getattr(booking, "form_answers_json", None)
    fa_dict = fa if isinstance(fa, dict) else {}
    cust_no = str(fa_dict.get("customer_number") or "").strip()
    note_legacy = (getattr(booking, "calendar_title_note", None) or "").strip()
    mapping = {
        "service": (service_name or "").strip(),
        "name": (booking.customer_name or "").strip(),
        "company": (getattr(booking, "company_name", None) or "").strip(),
        "phone": (booking.customer_phone or "").strip(),
        "customer_number": cust_no,
        "note": note_legacy or cust_no,
    }

    class _Default(dict):
        def __missing__(self, key: str) -> str:
            return ""

    try:
        out = tpl.format_map(_Default(mapping))
    except Exception:
        logger.exception("calendar_title_template format failed; fallback")
        out = f"{mapping['service']} — {mapping['name']}"

    out = re.sub(r"\s+", " ", out.replace("\n", " ")).strip()
    if len(out) > _MAX_TITLE_LEN:
        out = out[: _MAX_TITLE_LEN - 1] + "…"
    return out or f"{mapping['service']} — {mapping['name']}"
