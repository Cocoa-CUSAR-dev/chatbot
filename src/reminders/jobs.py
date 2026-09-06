"""The actual job bodies. Kept separate from scheduler.py so they're plain,
directly-testable async functions with no APScheduler-specific plumbing.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from src.database import async_session_maker
from src.line.identity import lookup_line_user_ids
from src.line.service import multicast_text
from src.reminders.queries import (
    already_reminded_today,
    due_reminders,
    record_reminders,
    users_owing_task,
)

logger = logging.getLogger(__name__)

_BANGKOK = ZoneInfo("Asia/Bangkok")
_MULTICAST_LIMIT = 500  # LINE's per-call recipient cap


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
    for reminder in due:
        owing = await users_owing_task(session, reminder.task_id, naive_now)
        if not owing:
            continue

        done_today = await already_reminded_today(session, reminder.task_id, now.date())
        targets = [uid for uid in owing if uid not in done_today]
        if not targets:
            continue

        id_map = await lookup_line_user_ids(session, targets)
        line_user_ids = [id_map[uid] for uid in targets if uid in id_map]

        status = "sent"
        try:
            for start in range(0, len(line_user_ids), _MULTICAST_LIMIT):
                await multicast_text(
                    line_user_ids[start : start + _MULTICAST_LIMIT],
                    _reminder_text(reminder.task_title),
                )
        except Exception:
            logger.exception("reminder push failed for task %s", reminder.task_id)
            status = "failed"

        await record_reminders(session, task_id=reminder.task_id, user_ids=targets, status=status)
        await session.commit()
        logger.info(
            "reminder task=%s recipients=%d status=%s",
            reminder.task_id,
            len(targets),
            status,
        )
