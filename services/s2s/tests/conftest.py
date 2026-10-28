"""Tests run against a real Postgres: the *_test database (TEST_DATABASE_URL), emptied before each test."""
import base64
import json
import os

import pytest

URL = os.environ.get("TEST_DATABASE_URL", "")
os.environ["DATABASE_URL"] = URL
os.environ.setdefault("LIVEKIT_API_KEY", "test-key")
os.environ.setdefault("LIVEKIT_API_SECRET", "test-secret-test-secret-test-secret")
os.environ.setdefault("LIVEKIT_URL", "http://127.0.0.1:9")       # nothing listens: room cleanup is best effort
os.environ.setdefault("S2S_SPEAK_LANGUAGES", "en,hi")                # as on the shared models
os.environ.setdefault("S2S_HEAR_LANGUAGES", "en,hi")

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import db  # noqa: E402
from app.security import reset_rate_limits  # noqa: E402

needs_db = pytest.mark.skipif(not URL.rsplit("/", 1)[-1].endswith("_test"), reason="TEST_DATABASE_URL must name a *_test database")


@pytest.fixture
async def client():
    if not URL.rsplit("/", 1)[-1].endswith("_test"):
        pytest.skip("TEST_DATABASE_URL must name a *_test database")
    from app.web import app
    await db.migrate()
    async with db.engine().begin() as conn:
        tables = [r[0] for r in await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'"))]
        if tables:
            await conn.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE"))
    reset_rate_limits()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://meet.test", headers={"origin": "https://meet.test"}) as c:
        yield c
    await db.dispose()


def other(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """A second browser on the same app (its own cookies)."""
    return httpx.AsyncClient(transport=client._transport, base_url="https://meet.test", headers={"origin": "https://meet.test"})


async def signup(c, name, email, org=None, join_code=None, password="correct-horse-1"):
    body = {"name": name, "email": email, "password": password}
    body.update({"organization": org} if org else {"join_code": join_code})
    r = await c.post("/api/auth/signup", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
