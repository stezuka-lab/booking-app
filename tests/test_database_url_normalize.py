from app.db import _normalize_database_url


def test_asyncpg_neon_url_removes_sslmode_and_channel_binding() -> None:
    url, connect_args = _normalize_database_url(
        "postgresql+asyncpg://user:pass@host/neondb?sslmode=require&channel_binding=require"
    )
    assert url == "postgresql+asyncpg://user:pass@host/neondb"
    assert connect_args == {"ssl": True}


def test_asyncpg_url_keeps_other_query_parameters() -> None:
    url, connect_args = _normalize_database_url(
        "postgresql+asyncpg://user:pass@host/neondb?sslmode=require&application_name=booking-app"
    )
    assert "application_name=booking-app" in url
    assert "sslmode" not in url
    assert connect_args == {"ssl": True}


def test_non_asyncpg_url_is_unchanged() -> None:
    url, connect_args = _normalize_database_url("sqlite+aiosqlite:///./data/app.db")
    assert url == "sqlite+aiosqlite:///./data/app.db"
    assert connect_args == {}
