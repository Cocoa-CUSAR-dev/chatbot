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
from src.line.service import multicast_text_batched, push_text
from src.notifications.schemas import FailedRecipient, SendNotificationResponse

logger = logging.getLogger(__name__)


def _dedupe(user_ids: Sequence[UUID]) -> list[UUID]:
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for uid in user_ids:
        if uid not in seen:
            seen.add(uid)
            ordered.append(uid)
    return ordered


async def _deliver(line_user_ids: list[str], message: str) -> tuple[list[str], list[str]]:
    """Returns (succeeded, failed) LINE user ids -- never all-or-nothing, so
    one bad chunk of a large multicast doesn't get reported as a failure for
    recipients an earlier chunk already reached. multicast_text_batched
    handles the actual chunking/retry-tracking (shared with src/reminders/
    jobs.py, which had this same loop duplicated before).
    """
    if len(line_user_ids) == 1:
        try:
            await push_text(line_user_ids[0], message)
        except Exception:
            logger.exception("push to 1 recipient failed")
            return [], line_user_ids
        return line_user_ids, []
    return await multicast_text_batched(line_user_ids, message)


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

    line_id_to_user = {id_map[uid]: uid for uid in resolved}
    succeeded_line_ids, failed_line_ids = await _deliver(list(line_id_to_user), message)

    sent = [line_id_to_user[lid] for lid in succeeded_line_ids]
    failed.extend(
        FailedRecipient(user_id=line_id_to_user[lid], reason="line_api_error")
        for lid in failed_line_ids
    )
    return SendNotificationResponse(sent=sent, failed=failed)
