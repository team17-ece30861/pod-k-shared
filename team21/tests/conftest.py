import os
from pathlib import Path

# Use the token supplied by the environment when one exists.
# GitHub Actions currently supplies API_TOKEN=dev-token.
# Local tests fall back to test-token.
API_TOKEN = os.environ.get("API_TOKEN", "test-token")

os.environ["API_TOKEN"] = API_TOKEN
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///./phase3_test.db",
)

import pytest
from fastapi.testclient import TestClient

from app.main import app


HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "X-Actor": "pytest",
}

AUTH_ONLY = {
    "Authorization": f"Bearer {API_TOKEN}",
}


@pytest.fixture(scope="session", autouse=True)
def cleanup_db():
    yield
    Path("phase3_test.db").unlink(
        missing_ok=True
    )


@pytest.fixture()
def client():
    c = TestClient(app)

    c.post(
        "/test/reset",
        headers=AUTH_ONLY,
    )

    yield c

    c.post(
        "/test/reset",
        headers=AUTH_ONLY,
    )