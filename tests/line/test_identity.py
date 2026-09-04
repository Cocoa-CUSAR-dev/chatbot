import uuid
from unittest.mock import AsyncMock, MagicMock

from src.line.identity import LineIdentity, lookup_user_id


def _mock_session(return_value: uuid.UUID | None) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = return_value
    session.execute = AsyncMock(return_value=result)
    return session


async def test_lookup_user_id_returns_the_linked_user() -> None:
    user_id = uuid.uuid4()
    session = _mock_session(user_id)

    result = await lookup_user_id(session, "Uline123")

    assert result == user_id


async def test_lookup_user_id_returns_none_when_unlinked() -> None:
    session = _mock_session(None)

    result = await lookup_user_id(session, "Uunknown")

    assert result is None


def test_line_identity_table_schema() -> None:
    table = LineIdentity.__table__
    assert table.schema == "auth"
    assert table.name == "line_identity"
    assert table.c.display_name.nullable is True
