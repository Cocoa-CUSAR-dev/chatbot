"""Async DB engine/session for this service's OWN schemas only.

Per ADR 0001 and ADR 0005: this service reads/writes `chat.*`, `auth.line_identity`,
`auth.line_link_code`, and `notify.*` directly. It never touches the existing
`form.*`/`agriculture.*`/etc. domain tables directly -- those go through the
existing backends (see src/forms and src/tasks). It also never runs migrations
itself -- the `database` repo (Flyway) is the sole schema owner.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.config import settings

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Shared declarative base for this service's own ORM models."""


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
