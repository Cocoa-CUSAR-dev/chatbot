"""DB reads/writes the reminder check needs, kept out of jobs.py so each is a
plain, directly-testable async function.

Reading form.task / form.response directly here is a deliberate exception to
"the chatbot never touches form.* directly" (ADR 0001) -- the same shape of
exception src/line/temp_task_picker.py and src/line/parent_picker.py already
take. There is no service-side read API for "which users still owe task X",
and the reminder check needs exactly that. The write side (notify.*) this
service owns outright (ADR 0005).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class DueReminder:
    schedule_id: uuid.UUID
    task_id: uuid.UUID
    task_title: str | None


async def due_reminders(session: AsyncSession, now_time: time) -> list[DueReminder]:
    """Active schedules whose time_of_day has already passed for today.

    MVP: DAILY only. WEEKLY/other cadences are ignored -- notify.reminder_schedule
    has no day-of-week column to drive them, so adding those is a schema change
    (database repo) first, not just code here.

    "Already passed" + a 15-minute check interval means a reminder fires on the
    first check at or after time_of_day; the caller drops anyone already in
    notify.reminder_log for today, so later checks the same day are no-ops.
    """
    rows = await session.execute(
        text(
            """
            SELECT s.schedule_id, s.task_id, t.title AS task_title
            FROM notify.reminder_schedule s
            JOIN form.task t ON t.task_id = s.task_id
            WHERE s.is_active = true
              AND s.cadence = 'DAILY'
              AND s.time_of_day <= :now_time
            """
        ),
        {"now_time": now_time},
    )
    return [DueReminder(**dict(row._mapping)) for row in rows]


async def users_owing_task(
    session: AsyncSession, task_id: uuid.UUID, now: datetime
) -> list[uuid.UUID]:
    """LINE-reachable users who have no form.response for this task, while the
    task is inside its open/close window.

    Scoped to auth.line_identity on purpose: a LINE push is the only delivery
    channel in the MVP (ADR 0006), so users with no linked LINE account are
    simply out of scope for a reminder -- not an error to report.
    """
    rows = await session.execute(
        text(
            """
            SELECT li.user_id
            FROM auth.line_identity li
            JOIN form.task t ON t.task_id = :task_id
            LEFT JOIN form.response r
              ON r.task_log_id = t.task_id AND r.user_id = li.user_id
            WHERE r.response_id IS NULL
              AND (t.open_at IS NULL OR t.open_at <= :now)
              AND (t.close_at IS NULL OR t.close_at >= :now)
            """
        ),
        {"task_id": str(task_id), "now": now},
    )
    return [row.user_id for row in rows]


async def already_reminded_since(
    session: AsyncSession, task_id: uuid.UUID, since: datetime
) -> set[uuid.UUID]:
    """The idempotency check -- against the persisted notify.reminder_log, never
    APScheduler's in-memory state, so it survives a restart (ADR 0006).

    Compares `sent_at >= since` rather than `sent_at::date = today`: the caller
    writes sent_at and passes `since` in the *same* wall-clock frame (see
    record_reminders / jobs.py), so this holds regardless of what timezone the
    database session is in. The `::date` form silently broke near midnight
    Bangkok when the DB stored UTC -- sent_at's date and "today" disagreed by
    one day and every run re-sent.
    """
    rows = await session.execute(
        text(
            """
            SELECT user_id FROM notify.reminder_log
            WHERE task_id = :task_id
              AND channel = 'push'
              AND sent_at >= :since
            """
        ),
        {"task_id": str(task_id), "since": since},
    )
    return {row.user_id for row in rows}


async def record_reminders(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_ids: list[uuid.UUID],
    status: str,
    sent_at: datetime,
) -> None:
    """One notify.reminder_log row per user we attempted, written AFTER the
    push call returns. This is what already_reminded_since reads back.

    sent_at is written explicitly (not left to the column's `now()` default)
    so it lands in the same frame already_reminded_since compares against.
    """
    if not user_ids:
        return
    await session.execute(
        text(
            "INSERT INTO notify.reminder_log (user_id, task_id, sent_at, channel, status) "
            "VALUES (:user_id, :task_id, :sent_at, 'push', :status)"
        ),
        [
            {"user_id": str(uid), "task_id": str(task_id), "sent_at": sent_at, "status": status}
            for uid in user_ids
        ],
    )
