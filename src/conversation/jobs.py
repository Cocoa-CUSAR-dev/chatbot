"""Scheduled job bodies for conversation lifecycle (US2-3) -- kept separate
from src/reminders/jobs.py since this is conversation-state management, not
reminder delivery, even though both run on the same AsyncIOScheduler
instance (src/reminders/scheduler.py).
"""

import logging
from typing import Any, cast

from sqlalchemy import CursorResult, update

from src.conversation.constants import ConversationStatus
from src.conversation.models import Conversation
from src.database import async_session_maker

logger = logging.getLogger(__name__)


async def pause_idle_conversations() -> None:
    """Runs once daily (see scheduler.py) -- a fixed wall-clock cutoff, not
    a per-conversation idle timer: anything still ACTIVE by the time this
    runs is treated as "the farmer will pick this up another day" and gets
    paused. Doesn't touch PAUSED/COMPLETED/CANCELLED rows.

    This is the backstop for "went quiet and never came back" -- the
    same-turn "farmer is deliberately switching tasks right now" case is
    handled immediately instead, by service.pause_active_conversation
    whenever เริ่ม is typed. Deliberately silent (no proactive LINE push):
    that's the separate Reminders/Notif. epic's job, not this one's.
    """
    async with async_session_maker() as session:
        raw_result = await session.execute(
            update(Conversation)
            .where(Conversation.status == ConversationStatus.ACTIVE)
            .values(status=ConversationStatus.PAUSED)
        )
        # session.execute()'s declared return type is the general Result[Any]
        # -- an UPDATE always actually returns the CursorResult subtype that
        # carries rowcount, this just tells mypy what we already know.
        result = cast(CursorResult[Any], raw_result)
        await session.commit()
    logger.info("pause_idle_conversations: paused %d conversation(s)", result.rowcount)
