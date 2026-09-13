from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from src.database import Base, async_session_maker, get_session


def test_base_is_a_declarative_base() -> None:
    assert issubclass(Base, DeclarativeBase)


async def test_get_session_yields_a_real_async_session() -> None:
    generator = get_session()
    session = await anext(generator)
    try:
        assert isinstance(session, AsyncSession)
    finally:
        await session.close()


def test_async_session_maker_builds_async_sessions() -> None:
    assert async_session_maker.class_ is AsyncSession
