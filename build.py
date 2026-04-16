from __future__ import annotations

from pathlib import Path
import shutil


def main() -> None:
    root = Path(__file__).resolve().parent
    src = root / "app" / "web" / "static"
    dst = root / "public" / "assets" / "web"
    dst.mkdir(parents=True, exist_ok=True)
    for path in src.glob("*"):
        if path.is_file():
            shutil.copy2(path, dst / path.name)


if __name__ == "__main__":
    main()

