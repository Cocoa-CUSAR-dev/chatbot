"""Sets dummy env vars BEFORE any `src` import happens -- every config.py in
this project instantiates its Settings object at module import time, so real
values (or at least placeholders) must exist before `import src.main` runs
anywhere, including in CI with no real .env file.
"""

import os
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("KOTLIN_BACKEND_URL", "http://localhost:3001/api/v1")
os.environ.setdefault("GO_BACKEND_URL", "http://localhost:8080")
os.environ.setdefault("GO_SERVICE_KEY", "test-service-key")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test-secret")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-token")

from src.main import app  # noqa: E402 -- must come after the env vars above


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
