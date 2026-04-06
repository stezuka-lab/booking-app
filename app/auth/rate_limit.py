from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request

_login_failures: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or "unknown"


def _bucket_key(request: Request, username: str) -> str:
    return f"{_client_ip(request)}::{(username or '').strip().lower()}"


def check_login_rate_limit(
    request: Request,
    username: str,
    *,
    max_attempts: int = 10,
    window_sec: int = 900,
) -> None:
    key = _bucket_key(request, username)
    now = time.time()
    bucket = _login_failures[key]
    cutoff = now - window_sec
    bucket[:] = [t for t in bucket if t >= cutoff]
    if len(bucket) >= max_attempts:
        raise HTTPException(
            429,
            "ログイン試行が多すぎます。しばらく時間をおいて再度お試しください。",
        )


def record_login_failure(
    request: Request,
    username: str,
    *,
    window_sec: int = 900,
) -> None:
    key = _bucket_key(request, username)
    now = time.time()
    bucket = _login_failures[key]
    cutoff = now - window_sec
    bucket[:] = [t for t in bucket if t >= cutoff]
    bucket.append(now)


def clear_login_failures(request: Request, username: str) -> None:
    _login_failures.pop(_bucket_key(request, username), None)
