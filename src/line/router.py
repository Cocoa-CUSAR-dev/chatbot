import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from linebot.v3.webhooks import (
    Event,
    FollowEvent,
    ImageMessageContent,
    LocationMessageContent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

from src.line.dependencies import parse_line_events
from src.line.service import reply_text

router = APIRouter(prefix="/line", tags=["line"])
logger = logging.getLogger(__name__)


async def _handle_event(event: Event) -> None:
    """Dispatches one parsed webhook event by type.

    Deliberately thin -- the actual slot-filling/state-machine logic lives in
    src/conversation (target-architecture.md #4). This function's job is just
    routing each event type to the right place, not deciding what a farmer's
    answer means.
    """
    if isinstance(event, MessageEvent):
        await _handle_message(event)
    elif isinstance(event, FollowEvent):
        # TODO: hand off to src.conversation / whatever identity-linking
        # mechanism the team lands on for ADR 0002 -- this is a farmer
        # adding the OA as a friend for the first time.
        logger.info("follow event, user_id=%s", event.source.user_id)
    elif isinstance(event, PostbackEvent):
        # TODO: hand off to src.conversation -- fires from Quick Reply/Flex
        # Message button taps, e.g. confirming a summary (AwaitingConfirmation).
        logger.info("postback event, data=%s", event.postback.data)
    else:
        logger.info("unhandled event type: %s", type(event).__name__)


async def _handle_message(event: MessageEvent) -> None:
    message = event.message

    if isinstance(message, TextMessageContent):
        # TODO: hand off to src.conversation instead of echoing.
        await reply_text(event.reply_token, f"got: {message.text}")
    elif isinstance(message, LocationMessageContent):
        # TODO: hand off to src.conversation -- this is the direct LINE
        # equivalent of a GEODATA question (see the database review's
        # findings on GEODATA questions meaning "open a map picker" today).
        logger.info("location message: lat=%s lng=%s", message.latitude, message.longitude)
    elif isinstance(message, ImageMessageContent):
        # TODO: hand off to src.conversation -- photo evidence for a task.
        logger.info("image message, id=%s", message.id)
    else:
        logger.info("unhandled message content type: %s", type(message).__name__)


@router.post("/webhook", status_code=200)
async def webhook(
    background_tasks: BackgroundTasks,
    events: Annotated[list[Event], Depends(parse_line_events)],
) -> dict[str, str]:
    """Acknowledge the webhook immediately, process events in the background.

    LINE requires a fast response or it retries the delivery -- this is the
    FastAPI BackgroundTasks half of ADR 0003's async model.
    """
    for event in events:
        background_tasks.add_task(_handle_event, event)
    return {"status": "ok"}
