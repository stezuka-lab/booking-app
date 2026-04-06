from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from app.booking.jobs import run_booking_reminders_and_crm
from app.db import init_db

logger = logging.getLogger(__name__)


async def _run_once() -> None:
    Path("data").mkdir(parents=True, exist_ok=True)
    await init_db()
    await run_booking_reminders_and_crm()


async def _run_loop(interval_seconds: int) -> None:
    while True:
        try:
            await _run_once()
        except Exception:
            logger.exception("booking job runner iteration failed")
        await asyncio.sleep(max(5, int(interval_seconds)))


def main() -> None:
    parser = argparse.ArgumentParser(description="booking job runner")
    parser.add_argument("mode", nargs="?", choices=["once", "loop"], default="once")
    parser.add_argument("--interval", type=int, default=60, help="loop mode interval seconds")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.mode == "loop":
        asyncio.run(_run_loop(args.interval))
    else:
        asyncio.run(_run_once())


if __name__ == "__main__":
    main()
