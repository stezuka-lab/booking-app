from app.db import database_url_for_alembic


def test_database_url_for_alembic_sqlite() -> None:
    raw = "sqlite+aiosqlite:///./data/app.db"
    assert database_url_for_alembic(raw) == "sqlite:///./data/app.db"


def test_database_url_for_alembic_postgres_asyncpg() -> None:
    raw = "postgresql+asyncpg://user:pass@example.com/dbname?sslmode=require"
    assert (
        database_url_for_alembic(raw)
        == "postgresql+psycopg://user:pass@example.com/dbname?sslmode=require"
    )


def test_database_url_for_alembic_passthrough() -> None:
    raw = "postgresql://user:pass@example.com/dbname"
    assert database_url_for_alembic(raw) == raw
