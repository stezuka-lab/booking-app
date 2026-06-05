"""pytest 共通設定。"""

from collections.abc import Generator
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault(
    "BOOKING_DATA_ENCRYPTION_KEY",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
)
os.environ["ACTIONS_DRY_RUN"] = "false"

from app.main import app


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
