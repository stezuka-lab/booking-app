from __future__ import annotations

import os

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    host = (os.getenv("HOST") or settings.host or "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("PORT") or settings.port or 8000)
    uvicorn.run("app.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
