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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.reminders.exceptions import InvalidReminderReference, ReminderNotFound
from src.reminders.schemas import RecipientRule


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
    session: AsyncSession,
    task_id: uuid.UUID,
    now: datetime,
    schedule_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """LINE-reachable users who have no form.response for this task, while the
    task is inside its open/close window.

    With a `schedule_id`, also narrowed to the people that schedule is aimed
    at (notify.reminder_recipient): anyone holding a targeted role, plus any
    individually targeted user. A schedule with no recipient rows -- every
    schedule created before recipients existed -- is aimed at everyone, so
    this behaves exactly as it always did for those.

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
              AND (
                CAST(:schedule_id AS uuid) IS NULL
                OR NOT EXISTS (
                  SELECT 1 FROM notify.reminder_recipient rr
                  WHERE rr.schedule_id = CAST(:schedule_id AS uuid)
                )
                OR EXISTS (
                  SELECT 1 FROM notify.reminder_recipient rr
                  WHERE rr.schedule_id = CAST(:schedule_id AS uuid)
                    AND (
                      rr.user_id = li.user_id
                      OR rr.role_id IN (
                        SELECT ur.role_id FROM auth.user_role ur
                        WHERE ur.user_id = li.user_id
                      )
                    )
                )
              )
            """
        ),
        {
            "task_id": str(task_id),
            "now": now,
            "schedule_id": str(schedule_id) if schedule_id else None,
        },
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

    Only counts status='sent' rows. A row logged 'failed' (LINE was down/
    rate-limited for that recipient, see jobs.py) means the push never
    actually reached them -- counting it here would permanently skip a real
    retry for the rest of the day over a transient delivery failure.
    """
    rows = await session.execute(
        text(
            """
            SELECT user_id FROM notify.reminder_log
            WHERE task_id = :task_id
              AND channel = 'push'
              AND status = 'sent'
              AND sent_at >= :since
            """
        ),
        {"task_id": str(task_id), "since": since},
    )
    return {row.user_id for row in rows}


async def create_reminder_schedule(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    time_of_day: time,
    created_by: uuid.UUID,
    recipients: list[RecipientRule] | None = None,
) -> uuid.UUID:
    """Inserts a new notify.reminder_schedule row (and its recipient rules,
    in the same transaction). cadence is hardcoded to
    'DAILY' here, not accepted as a parameter -- due_reminders above only
    ever recognizes that one value, and every other value would be a row
    the job silently never picks up, with no error anywhere to say so. This
    function structurally can't create that mismatch: nothing calling it
    can pass a different cadence, because there's nowhere to pass one.
    """
    schedule_id = uuid.uuid4()
    async with _invalid_reference_guard(session):
        await session.execute(
            text(
                "INSERT INTO notify.reminder_schedule "
                "(schedule_id, task_id, cadence, time_of_day, is_active, created_by) "
                "VALUES (:schedule_id, :task_id, 'DAILY', :time_of_day, true, :created_by)"
            ),
            {
                "schedule_id": schedule_id,
                "task_id": str(task_id),
                "time_of_day": time_of_day,
                "created_by": str(created_by),
            },
        )
        await _insert_recipients(session, schedule_id, recipients or [])
        await session.commit()
    return schedule_id


def _dedupe(recipients: list[RecipientRule]) -> list[RecipientRule]:
    # notify.reminder_recipient is UNIQUE per (schedule, role) and
    # (schedule, user) -- collapse repeats here instead of failing on them.
    return list({(r.type, r.id): r for r in recipients}.values())


async def _insert_recipients(
    session: AsyncSession, schedule_id: uuid.UUID, recipients: list[RecipientRule]
) -> None:
    rows = [
        {
            "schedule_id": str(schedule_id),
            "type": r.type,
            "role_id": str(r.id) if r.type == "ROLE" else None,
            "user_id": str(r.id) if r.type == "USER" else None,
        }
        for r in _dedupe(recipients)
    ]
    if not rows:
        return
    await session.execute(
        text(
            "INSERT INTO notify.reminder_recipient "
            "(schedule_id, recipient_type, role_id, user_id) "
            "VALUES (:schedule_id, :type, CAST(:role_id AS uuid), CAST(:user_id AS uuid))"
        ),
        rows,
    )


@asynccontextmanager
async def _invalid_reference_guard(session: AsyncSession) -> AsyncIterator[None]:
    """A foreign-key failure (a task/role/user id that doesn't exist) can
    surface at any INSERT or at commit -- either way, undo the whole write
    and report it as a bad request rather than a 500.
    """
    try:
        yield
    except IntegrityError as exc:
        await session.rollback()
        raise InvalidReminderReference from exc


@dataclass(frozen=True)
class ScheduleRow:
    schedule_id: uuid.UUID
    task_id: uuid.UUID
    cadence: str
    time_of_day: time
    is_active: bool
    created_by: uuid.UUID
    recipients: list[RecipientRule]


async def _recipients_for(
    session: AsyncSession, schedule_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[RecipientRule]]:
    if not schedule_ids:
        return {}
    rows = await session.execute(
        text(
            "SELECT schedule_id, recipient_type, role_id, user_id "
            "FROM notify.reminder_recipient "
            "WHERE schedule_id = ANY(CAST(:ids AS uuid[])) "
            "ORDER BY created_at, recipient_id"
        ),
        {"ids": [str(i) for i in schedule_ids]},
    )
    grouped: dict[uuid.UUID, list[RecipientRule]] = {}
    for row in rows:
        target = row.role_id if row.recipient_type == "ROLE" else row.user_id
        grouped.setdefault(row.schedule_id, []).append(
            RecipientRule(type=row.recipient_type, id=target)
        )
    return grouped


async def list_schedules_for_task(session: AsyncSession, task_id: uuid.UUID) -> list[ScheduleRow]:
    rows = await session.execute(
        text(
            "SELECT schedule_id, task_id, cadence, time_of_day, is_active, created_by "
            "FROM notify.reminder_schedule WHERE task_id = :task_id ORDER BY schedule_id"
        ),
        {"task_id": str(task_id)},
    )
    found = [dict(r._mapping) for r in rows]
    recipients = await _recipients_for(session, [f["schedule_id"] for f in found])
    return [ScheduleRow(**f, recipients=recipients.get(f["schedule_id"], [])) for f in found]


async def update_reminder_schedule(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    time_of_day: time | None = None,
    is_active: bool | None = None,
    recipients: list[RecipientRule] | None = None,
) -> ScheduleRow:
    """Changes only what is passed. `recipients` replaces the whole rule list
    (empty list = everyone again). Raises ReminderNotFound for an unknown id.
    """
    found = await session.execute(
        text("SELECT task_id FROM notify.reminder_schedule WHERE schedule_id = :id"),
        {"id": str(schedule_id)},
    )
    task_id = found.scalar_one_or_none()
    if task_id is None:
        raise ReminderNotFound

    async with _invalid_reference_guard(session):
        if time_of_day is not None:
            await session.execute(
                text(
                    "UPDATE notify.reminder_schedule SET time_of_day = :t WHERE schedule_id = :id"
                ),
                {"t": time_of_day, "id": str(schedule_id)},
            )
        if is_active is not None:
            await session.execute(
                text("UPDATE notify.reminder_schedule SET is_active = :a WHERE schedule_id = :id"),
                {"a": is_active, "id": str(schedule_id)},
            )
        if recipients is not None:
            await session.execute(
                text("DELETE FROM notify.reminder_recipient WHERE schedule_id = :id"),
                {"id": str(schedule_id)},
            )
            await _insert_recipients(session, schedule_id, recipients)
        await session.commit()

    schedules = await list_schedules_for_task(session, task_id)
    return next(s for s in schedules if s.schedule_id == schedule_id)


async def deactivate_schedule(session: AsyncSession, schedule_id: uuid.UUID) -> None:
    """Turns off a schedule -- used when nobody owes its task anymore (see
    jobs.py's _run_reminder_check): there's no point checking it again every
    day forever once every farmer who needed the reminder has submitted.
    Not a delete -- the row (and its history in notify.reminder_log) stays,
    only future checks stop considering it.
    """
    await session.execute(
        text("UPDATE notify.reminder_schedule SET is_active = false WHERE schedule_id = :id"),
        {"id": str(schedule_id)},
    )


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
