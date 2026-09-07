import logging

import pytest

from src.reminders.jobs import check_and_send_reminders


async def test_check_and_send_reminders_runs_without_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Not implemented yet (ADR 0006's reminder push) -- this locks in the
    # current honest "not yet implemented" placeholder rather than silently
    # pretending it sends anything.
    with caplog.at_level(logging.INFO):
        await check_and_send_reminders()

    assert "not yet implemented" in caplog.text
