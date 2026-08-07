"""TEMPORARY stand-in for the real farmer-facing task picker. Boom's
"FE - Create: pending to-do list UI" (Sprint 5, EPIC 4 "Reminders & Notif.")
is meant to replace this with a real LIFF screen -- until that ships, a
farmer types a keyword and gets their pending tasks back as LINE Quick Reply
buttons instead.

Deliberate, temporary exception to "the chatbot never touches form.*
directly" (ADR 0001) -- the same shortcut src/conversation/dev_queries.py
takes for dev testing, but this is production code reachable from real LINE
messages, not test scaffolding. Delete this file (and the keyword-handling
branch in src/line/router.py that calls it) once Sprint 5's real picker
ships.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Case-insensitive; message text is lowercased before matching.
START_KEYWORDS = {"เริ่ม", "start", "ทำแบบฟอร์ม"}

_QUICK_REPLY_LIMIT = 13  # LINE's own cap on Quick Reply items per message


@dataclass(frozen=True)
class PendingTask:
    task_id: UUID
    task_form_id: UUID
    title: str


async def list_pending_tasks(session: AsyncSession, user_id: UUID) -> list[PendingTask]:
    """A task is "pending" if this user has no form.response row for it yet --
    mirrors mobile-backend's real GetTasks logic (form_handler.go), rather
    than form.assignment, which turns out to be unpopulated in this DB
    despite existing in the schema.
    """
    rows = await session.execute(
        text(
            """
            SELECT t.task_id, tf.form_id AS task_form_id, t.title
            FROM form.task t
            JOIN form.task_form tf ON tf.task_id = t.task_id
            LEFT JOIN form.response r ON r.task_log_id = t.task_id AND r.user_id = :user_id
            WHERE r.response_id IS NULL
            ORDER BY t.open_at DESC
            LIMIT :limit
            """
        ),
        {"user_id": str(user_id), "limit": _QUICK_REPLY_LIMIT},
    )
    return [PendingTask(**dict(row._mapping)) for row in rows]
