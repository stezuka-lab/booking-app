"""シンプルなインメモリ・レート制限（プロセス単位。複数ワーカー間は共有されない）。"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request

# IP ごとのタイムスタンプ（秒）
_book_post_times: dict[str, list[float]] = defaultdict(list)


def check_public_booking_rate_limit(request: Request, *, max_requests: int = 40, window_sec: int = 3600) -> None:
    """予約 POST の乱用を抑止。同一 IP あたり window 秒で max 回まで。"""
    ip = (request.client.host if request.client else "") or "unknown"
    now = time.time()
    bucket = _book_post_times[ip]
    cutoff = now - window_sec
    bucket[:] = [t for t in bucket if t >= cutoff]
    if len(bucket) >= max_requests:
        raise HTTPException(
            429,
            "予約リクエストが多すぎます。しばらく時間をおいてから再度お試しください。",
        )
    bucket.append(now)
