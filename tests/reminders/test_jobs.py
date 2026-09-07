from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from src.reminders.jobs import _run_reminder_check
from src.reminders.queries import DueReminder

_NOW = datetime(2026, 9, 7, 17, 5)  # 17:05, past a 17:00 time_of_day


async def _run(*, due, owing, done, id_map, multicast=None):
    session = AsyncMock()
    multicast = multicast or AsyncMock()
    with (
        patch("src.reminders.jobs.due_reminders", new=AsyncMock(return_value=due)),
        patch("src.reminders.jobs.users_owing_task", new=AsyncMock(return_value=owing)),
        patch(
            "src.reminders.jobs.already_reminded_since",
            new=AsyncMock(return_value=done),
        ),
        patch(
            "src.reminders.jobs.lookup_line_user_ids",
            new=AsyncMock(return_value=id_map),
        ),
        patch("src.reminders.jobs.multicast_text", new=multicast),
        patch("src.reminders.jobs.record_reminders", new=AsyncMock()) as record,
    ):
        await _run_reminder_check(session, _NOW)
    return record, multicast


def _due(task_id):
    return [DueReminder(schedule_id=uuid4(), task_id=task_id, task_title="เก็บเกี่ยว")]


async def test_pushes_to_users_who_still_owe_and_logs_them():
    a, b = uuid4(), uuid4()
    tid = uuid4()
    record, multicast = await _run(
        due=_due(tid), owing=[a, b], done=set(), id_map={a: "U1", b: "U2"}
    )
    multicast.assert_awaited_once()
    record.assert_awaited_once()
    kwargs = record.await_args.kwargs
    assert set(kwargs["user_ids"]) == {a, b}
    assert kwargs["status"] == "sent"


async def test_users_already_reminded_today_are_skipped():
    a, b = uuid4(), uuid4()
    tid = uuid4()
    record, _ = await _run(due=_due(tid), owing=[a, b], done={a}, id_map={b: "U2"})
    assert record.await_args.kwargs["user_ids"] == [b]


async def test_no_due_schedules_is_a_no_op():
    record, multicast = await _run(due=[], owing=[], done=set(), id_map={})
    record.assert_not_awaited()
    multicast.assert_not_awaited()


async def test_everyone_owing_already_reminded_sends_nothing():
    a = uuid4()
    tid = uuid4()
    record, multicast = await _run(due=_due(tid), owing=[a], done={a}, id_map={})
    multicast.assert_not_awaited()
    record.assert_not_awaited()


async def test_nobody_owes_the_task_sends_nothing():
    tid = uuid4()
    record, multicast = await _run(due=_due(tid), owing=[], done=set(), id_map={})
    multicast.assert_not_awaited()
    record.assert_not_awaited()


async def test_line_failure_still_logs_but_with_failed_status():
    a = uuid4()
    tid = uuid4()
    boom = AsyncMock(side_effect=RuntimeError("LINE down"))
    record, _ = await _run(due=_due(tid), owing=[a], done=set(), id_map={a: "U1"}, multicast=boom)
    assert record.await_args.kwargs["status"] == "failed"
