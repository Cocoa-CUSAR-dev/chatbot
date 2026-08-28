"""DB-gated fixtures for integration tests -- kept in tests/integration/ only
so files outside this directory never pull in a real Postgres dependency.
"""

import os
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import async_session_maker

_CLEANUP_TABLES = (
    "chat.conversation_answer",
    "chat.conversation",
    "auth.line_identity",
    "form.response",
    "form.question",
    "form.task_form",
    "form.task",
    "auth.user_account",
)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """The app's own async_session_maker, not a session pinned to one
    connection+SAVEPOINT -- webhook-level tests need the code under test to
    see committed rows from a different connection. Cleaned up by deleting
    every row afterward instead of rolling back. Skips unless RUN_DB_TESTS
    is set.
    """
    if not os.getenv("RUN_DB_TESTS"):
        pytest.skip("RUN_DB_TESTS not set -- no local Postgres to test against")

    async with async_session_maker() as session:
        try:
            yield session
        finally:
            for table in _CLEANUP_TABLES:
                await session.execute(text(f"DELETE FROM {table}"))
            await session.commit()
