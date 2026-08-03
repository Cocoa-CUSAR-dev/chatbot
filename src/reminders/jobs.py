"""The actual job bodies. Kept separate from scheduler.py so they're plain,
directly-testable async functions with no APScheduler-specific plumbing.
"""

import logging

logger = logging.getLogger(__name__)


async def check_and_send_reminders() -> None:
    """Idempotency check MUST be against notify.reminder_log (a persisted
    row), never against in-memory state -- see ADR 0006.
    """
    # TODO: query notify.reminder_schedule for due reminders, cross-check
    # notify.reminder_log for ones already sent, push via src.line.service.
    logger.info("check_and_send_reminders: not yet implemented")
