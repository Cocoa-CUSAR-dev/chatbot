from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from src.reminders.jobs import _run_reminder_check
from src.reminders.queries import DueReminder

_NOW = datetime(2026, 9, 7, 17, 5)  # 17:05, past a 17:00 time_of_day


async def _run(*, due, owing, done, id_map, multicast_batched=None):
    """multicast_batched, if given, should be an AsyncMock whose return
    value is (succeeded, failed) LINE user ids -- matching
    multicast_text_batched's real contract, not a side_effect that raises
    (the real function never lets a delivery error escape).
    """
    session = AsyncMock()
    multicast_batched = multicast_batched or AsyncMock(side_effect=lambda to, _msg: (to, []))
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
        patch("src.reminders.jobs.multicast_text_batched", new=multicast_batched),
        patch("src.reminders.jobs.record_reminders", new=AsyncMock()) as record,
    ):
        await _run_reminder_check(session, _NOW)
    return record, multicast_batched


def _due(task_id):
    return [DueReminder(schedule_id=uuid4(), task_id=task_id, task_title="เก็บเกี่ยว")]


async def test_pushes_to_users_who_still_owe_and_logs_them():
    a, b = uuid4(), uuid4()
    tid = uuid4()
    record, multicast_batched = await _run(
        due=_due(tid), owing=[a, b], done=set(), id_map={a: "U1", b: "U2"}
    )
    multicast_batched.assert_awaited_once()
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
    record, multicast_batched = await _run(due=[], owing=[], done=set(), id_map={})
    record.assert_not_awaited()
    multicast_batched.assert_not_awaited()


async def test_everyone_owing_already_reminded_sends_nothing():
    a = uuid4()
    tid = uuid4()
    record, multicast_batched = await _run(due=_due(tid), owing=[a], done={a}, id_map={})
    multicast_batched.assert_not_awaited()
    record.assert_not_awaited()


async def test_nobody_owes_the_task_sends_nothing():
    tid = uuid4()
    record, multicast_batched = await _run(due=_due(tid), owing=[], done=set(), id_map={})
    multicast_batched.assert_not_awaited()
    record.assert_not_awaited()


async def test_line_failure_still_logs_but_with_failed_status():
    a = uuid4()
    tid = uuid4()
    all_failed = AsyncMock(return_value=([], ["U1"]))
    record, _ = await _run(
        due=_due(tid), owing=[a], done=set(), id_map={a: "U1"}, multicast_batched=all_failed
    )
    assert record.await_args.kwargs["status"] == "failed"
    assert record.await_args.kwargs["user_ids"] == [a]


async def test_a_partial_failure_logs_succeeded_and_failed_users_separately():
    """The bug the review found: a later chunk failing used to mark the
    WHOLE batch (including recipients an earlier chunk already reached) as
    failed in notify.reminder_log. Now each recipient gets logged by what
    actually happened to them, in two separate calls.
    """
    a, b = uuid4(), uuid4()
    tid = uuid4()
    partial = AsyncMock(return_value=(["U1"], ["U2"]))
    record, _ = await _run(
        due=_due(tid),
        owing=[a, b],
        done=set(),
        id_map={a: "U1", b: "U2"},
        multicast_batched=partial,
    )
    assert record.await_count == 2
    calls = {c.kwargs["status"]: c.kwargs["user_ids"] for c in record.await_args_list}
    assert calls == {"sent": [a], "failed": [b]}
