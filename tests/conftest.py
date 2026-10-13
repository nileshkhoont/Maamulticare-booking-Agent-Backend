import os

os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGODB_DB_NAME", "ai_calling_agent_test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")
os.environ.setdefault("AGENT_TOOL_SECRET", "test-tool-secret")
os.environ.setdefault("EDESY_WEBHOOK_SECRET", "test-webhook-secret")

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.security import hash_password
from app.db.mongodb import close_db, connect_db, get_database
from app.models.admin import Admin
from app.models.business_config import BusinessConfig, WorkingHours


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await connect_db()
    db = get_database()
    yield
    # Wipe every collection between tests so they stay independent.
    for name in await db.list_collection_names():
        await db.drop_collection(name)
    await close_db()


@pytest_asyncio.fixture
async def business_config() -> BusinessConfig:
    config = BusinessConfig(
        working_days=["monday", "tuesday", "wednesday", "thursday", "friday"],
        working_hours=[WorkingHours(start="09:00", end="17:00")],
        slot_duration_minutes=30,
        buffer_minutes=0,
        holidays=[],
        max_advance_booking_days=30,
        timezone="UTC",
    )
    await config.insert()
    return config


@pytest_asyncio.fixture
async def admin() -> Admin:
    admin = Admin(
        name="Test Admin",
        email="admin@example.com",
        password_hash=hash_password("test-password-123"),
        role="super_admin",
    )
    await admin.insert()
    return admin


@pytest_asyncio.fixture
async def client():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient, admin: Admin) -> dict:
    response = await client.post(
        "/api/v1/auth/login", json={"email": admin.email, "password": "test-password-123"}
    )
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
