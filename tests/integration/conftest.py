"""DB-gated fixtures for integration tests -- kept in tests/integration/ only
so files outside this directory never pull in a real Postgres dependency.
"""

import os
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database import engine


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Real AsyncSession, rolled back after the test via a restarted
    SAVEPOINT -- a plain outer-transaction rollback wouldn't survive the
    code under test calling session.commit() itself. Skips unless
    RUN_DB_TESTS is set (see tests/resources/schema.sql, ci.yml).
    """
    if not os.getenv("RUN_DB_TESTS"):
        pytest.skip("RUN_DB_TESTS not set -- no local Postgres to test against")

    async with engine.connect() as connection:
        await connection.begin()
        await connection.begin_nested()

        session_maker = async_sessionmaker(bind=connection, expire_on_commit=False)
        session = session_maker()

        def restart_savepoint(sync_session: Any, transaction: Any) -> None:
            if transaction.nested and not transaction._parent.nested:
                sync_session.begin_nested()

        event.listen(session.sync_session, "after_transaction_end", restart_savepoint)

        try:
            yield session
        finally:
            event.remove(session.sync_session, "after_transaction_end", restart_savepoint)
            await session.close()
            await connection.rollback()
