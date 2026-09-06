"""Ad-hoc proactive push: given first-party-supplied user_ids, map each to
its linked LINE account and deliver `message`. Not for reminders -- those
are scheduled (src/reminders). This is the "tell these specific people this
thing right now" path.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.line.identity import lookup_line_user_ids
from src.line.service import multicast_text, push_text
from src.notifications.schemas import FailedRecipient, SendNotificationResponse

logger = logging.getLogger(__name__)

# LINE's multicast endpoint accepts at most 500 recipients per call.
_MULTICAST_LIMIT = 500


def _dedupe(user_ids: Sequence[UUID]) -> list[UUID]:
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for uid in user_ids:
        if uid not in seen:
            seen.add(uid)
            ordered.append(uid)
    return ordered


async def _deliver(line_user_ids: list[str], message: str) -> None:
    if len(line_user_ids) == 1:
        await push_text(line_user_ids[0], message)
        return
    for start in range(0, len(line_user_ids), _MULTICAST_LIMIT):
        await multicast_text(line_user_ids[start : start + _MULTICAST_LIMIT], message)


async def send_notification(
    session: AsyncSession, user_ids: Sequence[UUID], message: str
) -> SendNotificationResponse:
    ordered = _dedupe(user_ids)
    id_map = await lookup_line_user_ids(session, ordered)

    resolved = [uid for uid in ordered if uid in id_map]
    failed = [
        FailedRecipient(user_id=uid, reason="no_line_identity")
        for uid in ordered
        if uid not in id_map
    ]

    if not resolved:
        return SendNotificationResponse(sent=[], failed=failed)

    try:
        await _deliver([id_map[uid] for uid in resolved], message)
    except Exception:
        # One bad delivery shouldn't 500 the caller -- report it as failed
        # recipients instead. LINE errors here are transient (rate limit,
        # upstream) far more often than they're the caller's fault.
        logger.exception("LINE delivery failed for %d recipient(s)", len(resolved))
        failed.extend(FailedRecipient(user_id=uid, reason="line_api_error") for uid in resolved)
        return SendNotificationResponse(sent=[], failed=failed)

    return SendNotificationResponse(sent=resolved, failed=failed)
