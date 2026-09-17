"""DB-backed tests for the reminder check (src/reminders). Exercises the real
notify.*/form.* SQL in src/reminders/queries.py -- the unit tests in
tests/reminders/test_jobs.py mock all of that out. Skipped unless
RUN_DB_TESTS is set (see tests/integration/conftest.py).
"""

import uuid
from datetime import datetime, time
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.reminders.jobs import _run_reminder_check, check_and_send_reminders
from src.reminders.queries import create_reminder_schedule
from tests.integration.helpers import (
    seed_form_response,
    seed_task_form,
    seed_user_with_line_identity,
)

_BANGKOK = ZoneInfo("Asia/Bangkok")

# A check running at 17:00; schedules with time_of_day <= 17:00 are "due".
_NOW = datetime(2026, 9, 7, 17, 0)
# Tasks are seeded open well before _NOW so they're inside their window
# regardless of the wall-clock date the suite actually runs on.
_PAST = datetime(2026, 1, 1)


async def _seed_schedule(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    created_by: uuid.UUID,
    time_of_day: time = time(8, 0),
    cadence: str = "DAILY",
    is_active: bool = True,
) -> uuid.UUID:
    schedule_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO notify.reminder_schedule "
            "(schedule_id, task_id, cadence, time_of_day, is_active, created_by) "
            "VALUES (:sid, :task_id, :cadence, :tod, :active, :created_by)"
        ),
        {
            "sid": schedule_id,
            "task_id": task_id,
            "cadence": cadence,
            "tod": time_of_day,
            "active": is_active,
            "created_by": created_by,
        },
    )
    await session.commit()
    return schedule_id


def _all_succeed() -> AsyncMock:
    """A multicast_text_batched replacement where every recipient succeeds --
    matching its real (succeeded, failed) contract instead of the bare
    MagicMock a plain AsyncMock() would return (which jobs.py can't unpack).
    """
    return AsyncMock(side_effect=lambda to, _msg: (to, []))


async def _is_active(session: AsyncSession, schedule_id: uuid.UUID) -> bool:
    result = await session.execute(
        text("SELECT is_active FROM notify.reminder_schedule WHERE schedule_id = :sid"),
        {"sid": schedule_id},
    )
    return bool(result.scalar_one())


async def _log_rows(session: AsyncSession, task_id: uuid.UUID) -> list[tuple[str, str]]:
    result = await session.execute(
        text(
            "SELECT status, channel FROM notify.reminder_log WHERE task_id = :tid ORDER BY sent_at"
        ),
        {"tid": task_id},
    )
    return [(r.status, r.channel) for r in result]


async def test_reminds_a_user_who_still_owes_the_task(db_session: AsyncSession) -> None:
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder1")
    task_id, _ = await seed_task_form(db_session, title="บันทึกการเก็บเกี่ยว", open_at=_PAST)
    await _seed_schedule(db_session, task_id=task_id, created_by=user_id)

    with patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as multicast:
        await _run_reminder_check(db_session, _NOW)

    multicast.assert_awaited_once()
    assert multicast.await_args.args[0] == ["Ureminder1"]
    assert await _log_rows(db_session, task_id) == [("sent", "push")]


async def test_second_run_same_day_does_not_remind_again(db_session: AsyncSession) -> None:
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder2")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)
    await _seed_schedule(db_session, task_id=task_id, created_by=user_id)

    with patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as first:
        await _run_reminder_check(db_session, _NOW)
    with patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as second:
        await _run_reminder_check(db_session, _NOW)

    first.assert_awaited_once()
    second.assert_not_awaited()
    assert await _log_rows(db_session, task_id) == [("sent", "push")]


async def test_user_who_already_submitted_is_not_reminded(db_session: AsyncSession) -> None:
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder3")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)
    await seed_form_response(db_session, task_id=task_id, user_id=user_id)
    await _seed_schedule(db_session, task_id=task_id, created_by=user_id)

    with patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as multicast:
        await _run_reminder_check(db_session, _NOW)

    multicast.assert_not_awaited()
    assert await _log_rows(db_session, task_id) == []


async def test_schedule_time_not_yet_reached_is_not_due(db_session: AsyncSession) -> None:
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder4")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)
    await _seed_schedule(db_session, task_id=task_id, created_by=user_id, time_of_day=time(23, 0))

    with patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as multicast:
        await _run_reminder_check(db_session, _NOW)

    multicast.assert_not_awaited()
    assert await _log_rows(db_session, task_id) == []


async def test_inactive_schedule_is_skipped(db_session: AsyncSession) -> None:
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder5")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)
    await _seed_schedule(db_session, task_id=task_id, created_by=user_id, is_active=False)

    with patch("src.reminders.jobs.multicast_text_batched", new=AsyncMock()) as multicast:
        await _run_reminder_check(db_session, _NOW)

    multicast.assert_not_awaited()


async def test_a_failed_push_is_retried_on_the_next_run_the_same_day(
    db_session: AsyncSession,
) -> None:
    """The actual bug the review found: already_reminded_since used to
    match a task_id/channel/sent_at row regardless of status, so a
    status='failed' row (LINE down/rate-limited) permanently blocked a
    retry for the rest of the day. It should only block a retry once the
    push has genuinely status='sent'.
    """
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder6")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)
    await _seed_schedule(db_session, task_id=task_id, created_by=user_id)

    with patch(
        "src.reminders.jobs.multicast_text_batched",
        new=AsyncMock(return_value=([], ["Ureminder6"])),
    ) as first:
        await _run_reminder_check(db_session, _NOW)
    first.assert_awaited_once()
    assert await _log_rows(db_session, task_id) == [("failed", "push")]

    with patch(
        "src.reminders.jobs.multicast_text_batched",
        new=AsyncMock(return_value=(["Ureminder6"], [])),
    ) as second:
        await _run_reminder_check(db_session, _NOW)

    second.assert_awaited_once()
    assert await _log_rows(db_session, task_id) == [("failed", "push"), ("sent", "push")]


async def test_create_reminder_schedule_writes_a_real_daily_active_row(
    db_session: AsyncSession,
) -> None:
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder-create")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)

    schedule_id = await create_reminder_schedule(
        db_session, task_id=task_id, time_of_day=time(17, 0), created_by=user_id
    )

    row = await db_session.execute(
        text(
            "SELECT task_id, cadence, time_of_day, is_active, created_by "
            "FROM notify.reminder_schedule WHERE schedule_id = :sid"
        ),
        {"sid": schedule_id},
    )
    result = row.mappings().one()
    assert result["task_id"] == task_id
    assert result["cadence"] == "DAILY"
    assert result["time_of_day"] == time(17, 0)
    assert result["is_active"] is True
    assert result["created_by"] == user_id


async def test_a_freshly_created_schedule_is_immediately_pickable_by_the_job(
    db_session: AsyncSession,
) -> None:
    """End-to-end proof the write side (create_reminder_schedule, what
    web-backend's ChatbotClient calls) and the read side (due_reminders,
    users_owing_task) actually agree on the row shape -- not just each
    individually mocked/tested in isolation.
    """
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder-e2e")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)
    await create_reminder_schedule(
        db_session, task_id=task_id, time_of_day=time(8, 0), created_by=user_id
    )

    with patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as multicast:
        await _run_reminder_check(db_session, _NOW)

    multicast.assert_awaited_once()
    assert multicast.await_args.args[0] == ["Ureminder-e2e"]


async def test_schedule_deactivates_once_nobody_owes_the_task(db_session: AsyncSession) -> None:
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder-done")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)
    await seed_form_response(db_session, task_id=task_id, user_id=user_id)
    schedule_id = await _seed_schedule(db_session, task_id=task_id, created_by=user_id)

    with patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as multicast:
        await _run_reminder_check(db_session, _NOW)

    multicast.assert_not_awaited()
    assert await _is_active(db_session, schedule_id) is False


async def test_check_and_send_reminders_wrapper_reads_real_clock_and_own_session(
    db_session: AsyncSession,
) -> None:
    """Every test above calls _run_reminder_check directly on the SAME
    session the test seeded with, and hands it a hand-picked `now`. The real
    entry point -- check_and_send_reminders, what the scheduler actually
    calls every 15 minutes -- opens its OWN session via async_session_maker
    and reads the real wall clock via datetime.now(Asia/Bangkok). Neither of
    those was ever exercised by any existing test. Only "what time is it" is
    mocked here; the write happens on a genuinely separate DB connection,
    which is exactly why this test reads it back through db_session rather
    than trusting an in-process return value.
    """
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder7")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)
    await _seed_schedule(db_session, task_id=task_id, created_by=user_id, time_of_day=time(8, 0))

    fixed_now = datetime(2026, 9, 7, 17, 0, tzinfo=_BANGKOK)
    with (
        patch("src.reminders.jobs.datetime") as mock_datetime,
        patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as multicast,
    ):
        mock_datetime.now.return_value = fixed_now
        await check_and_send_reminders()

    multicast.assert_awaited_once()
    assert await _log_rows(db_session, task_id) == [("sent", "push")]


async def test_check_and_send_reminders_does_not_double_send_across_the_bangkok_midnight_gap(
    db_session: AsyncSession,
) -> None:
    """Regression for the bug fixed in aede632 (already_reminded_since):
    comparing sent_at::date to "today" broke between 00:00-07:00 Bangkok,
    because sent_at is stored in UTC and that window is still "yesterday" in
    UTC -- every 15-minute tick re-sent all night. Runs the real wrapper
    (real clock read via the mocked "now", real separate session) twice
    inside that window and asserts the second run sends nothing.
    """
    user_id = await seed_user_with_line_identity(db_session, line_user_id="Ureminder8")
    task_id, _ = await seed_task_form(db_session, open_at=_PAST)
    await _seed_schedule(db_session, task_id=task_id, created_by=user_id, time_of_day=time(0, 0))

    first_tick = datetime(2026, 9, 7, 1, 0, tzinfo=_BANGKOK)
    second_tick = datetime(2026, 9, 7, 1, 15, tzinfo=_BANGKOK)

    with (
        patch("src.reminders.jobs.datetime") as mock_datetime,
        patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as first,
    ):
        mock_datetime.now.return_value = first_tick
        await check_and_send_reminders()
    first.assert_awaited_once()

    with (
        patch("src.reminders.jobs.datetime") as mock_datetime,
        patch("src.reminders.jobs.multicast_text_batched", new=_all_succeed()) as second,
    ):
        mock_datetime.now.return_value = second_tick
        await check_and_send_reminders()
    second.assert_not_awaited()

    assert await _log_rows(db_session, task_id) == [("sent", "push")]
