"""BOOKING_ADMIN_SECRET 用のランダム文字列を標準出力に出す（.env に貼り付け用）。"""

from __future__ import annotations

import secrets

if __name__ == "__main__":
    print(secrets.token_urlsafe(32))
