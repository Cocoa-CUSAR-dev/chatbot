"""The actual job bodies. Kept separate from scheduler.py so they're plain,
directly-testable async functions with no APScheduler-specific plumbing.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from src.database import async_session_maker
from src.line.identity import lookup_line_user_ids
from src.line.service import multicast_text_batched
from src.reminders.queries import (
    already_reminded_since,
    due_reminders,
    record_reminders,
    users_owing_task,
)

logger = logging.getLogger(__name__)

_BANGKOK = ZoneInfo("Asia/Bangkok")


def _reminder_text(task_title: str | None) -> str:
    task = task_title or "งานที่ค้างอยู่"
    return f'⏰ อย่าลืมบันทึก "{task}" นะครับ\nพิมพ์ "เริ่ม" เพื่อดูงานที่ต้องทำ'


async def check_and_send_reminders() -> None:
    """Runs every 15 minutes (see scheduler.py). Idempotency is the persisted
    notify.reminder_log table, never in-memory state -- ADR 0006. Safe to run
    late, or twice: a user already logged for a task today is skipped.
    """
    async with async_session_maker() as session:
        await _run_reminder_check(session, datetime.now(_BANGKOK))


async def _run_reminder_check(session: AsyncSession, now: datetime) -> None:
    due = await due_reminders(session, now.time())
    if not due:
        return

    naive_now = now.replace(tzinfo=None)  # form.task timestamps are tz-naive
    # Dedup window: everything sent since midnight today, in the scheduler's own
    # timezone. record_reminders writes sent_at in this same frame, so the
    # comparison doesn't depend on the database session's timezone.
    day_start = naive_now.replace(hour=0, minute=0, second=0, microsecond=0)
    for reminder in due:
        owing = await users_owing_task(session, reminder.task_id, naive_now)
        if not owing:
            continue

        done_today = await already_reminded_since(session, reminder.task_id, day_start)
        targets = [uid for uid in owing if uid not in done_today]
        if not targets:
            continue

        id_map = await lookup_line_user_ids(session, targets)
        line_id_to_user = {id_map[uid]: uid for uid in targets if uid in id_map}
        line_user_ids = list(line_id_to_user)

        succeeded_line_ids, failed_line_ids = await multicast_text_batched(
            line_user_ids, _reminder_text(reminder.task_title)
        )
        succeeded_users = [line_id_to_user[lid] for lid in succeeded_line_ids]
        failed_users = [line_id_to_user[lid] for lid in failed_line_ids]

        # Two separate log calls, not one status for the whole batch --
        # a later chunk failing must not retroactively mark an earlier,
        # already-delivered chunk as failed too (or vice versa). Logging
        # failed_users as status='failed' (rather than skipping them) is
        # what lets already_reminded_since's status='sent' filter correctly
        # allow a retry on the next tick instead of silently giving up.
        if succeeded_users:
            await record_reminders(
                session,
                task_id=reminder.task_id,
                user_ids=succeeded_users,
                status="sent",
                sent_at=naive_now,
            )
        if failed_users:
            await record_reminders(
                session,
                task_id=reminder.task_id,
                user_ids=failed_users,
                status="failed",
                sent_at=naive_now,
            )
        await session.commit()
        logger.info(
            "reminder task=%s sent=%d failed=%d",
            reminder.task_id,
            len(succeeded_users),
            len(failed_users),
        )
