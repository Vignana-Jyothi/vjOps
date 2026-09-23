import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")  # guaranteed-dead port
os.environ.setdefault("EMBED_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("BOOTSTRAP_ADMIN_PASSWORD", "test-admin-password")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "control-plane"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app, bootstrap_admin  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    init_db()
    bootstrap_admin()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_token(client):
    from app.config import settings

    r = client.post(
        "/api/auth/login",
        data={"username": settings.bootstrap_admin_email, "password": settings.bootstrap_admin_password},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}
