"""予約可能日: 土日祝ブロックなど。"""

from __future__ import annotations

from datetime import date
from typing import Any

try:
    import holidays as holidays_lib

    _HOLIDAYS_AVAILABLE = True
except Exception:  # pragma: no cover
    holidays_lib = None  # type: ignore[assignment]
    _HOLIDAYS_AVAILABLE = False

# 年ごとに生成しないと、将来日・過去年の祝日判定が漏れることがある（holidays の挙動依存）
_jp_by_year: dict[int, Any] = {}


def _jp_holidays_for_year(year: int) -> Any | None:
    if not _HOLIDAYS_AVAILABLE or holidays_lib is None:
        return None
    if year not in _jp_by_year:
        _jp_by_year[year] = holidays_lib.country_holidays("JP", years=year)
    return _jp_by_year[year]


def day_is_blocked_for_booking(d: date, availability_defaults: dict[str, Any] | None) -> bool:
    """availability_defaults の曜日・祝日ブロックに基づく。

    - block_saturday / block_sunday: 個別（日曜も設定で制御）
    - block_weekends: 互換用。True のとき土日ともブロック（上記が未指定のとき）
    """
    defaults = availability_defaults or {}
    wd = d.weekday()  # 0=月 … 5=土 6=日

    has_weekend_keys = any(k in defaults for k in ("block_saturday", "block_sunday"))
    if has_weekend_keys:
        if wd == 5 and defaults.get("block_saturday"):
            return True
        if wd == 6 and defaults.get("block_sunday"):
            return True
    elif defaults.get("block_weekends") and wd >= 5:
        return True

    if defaults.get("block_holidays"):
        try:
            jp = _jp_holidays_for_year(d.year)
            if jp is not None and d in jp:
                return True
        except Exception:
            # holidays ライブラリやデータ不整合で落ちないようにする
            pass
    return False
