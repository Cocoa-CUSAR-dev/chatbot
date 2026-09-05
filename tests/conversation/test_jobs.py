import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.conversation.jobs import pause_idle_conversations


def _patched_session_maker(rowcount: int) -> patch:
    result = MagicMock(rowcount=rowcount)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return patch("src.conversation.jobs.async_session_maker", return_value=session)


async def test_pause_idle_conversations_commits_and_logs_the_row_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _patched_session_maker(rowcount=3), caplog.at_level(logging.INFO):
        await pause_idle_conversations()

    assert "paused 3 conversation(s)" in caplog.text


async def test_pause_idle_conversations_commits_the_session() -> None:
    result = MagicMock(rowcount=0)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch("src.conversation.jobs.async_session_maker", return_value=session):
        await pause_idle_conversations()

    session.commit.assert_awaited_once()
