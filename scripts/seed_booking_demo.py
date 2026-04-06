"""
CLI からデモデータを投入する（app.booking.demo_seed と同一ロジック）。

サーバー起動時も BOOKING_SEED_DEMO=true（既定）なら自動投入されます。

使い方（プロジェクトルートで）:
  python scripts/seed_booking_demo.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.booking.demo_seed import ensure_demo_booking_data  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session_factory, init_db  # noqa: E402


async def main() -> None:
    await init_db()
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        info = await ensure_demo_booking_data(session, settings)
    if info:
        print("--- デモ予約（CLI） ---")
        print("公開URL:", info["public_url"])
        print("API:", info["docs_url"])


if __name__ == "__main__":
    asyncio.run(main())
