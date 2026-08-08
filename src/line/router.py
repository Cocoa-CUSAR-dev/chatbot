import logging
from typing import Annotated
from uuid import UUID

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
from sqlalchemy import select

from src.conversation import service
from src.conversation.constants import ConversationStatus
from src.conversation.exceptions import ConversationNotFound
from src.conversation.models import Conversation
from src.database import async_session_maker
from src.forms.client import get_form
from src.line.dependencies import parse_line_events
from src.line.service import reply_text

router = APIRouter(prefix="/line", tags=["line"])
logger = logging.getLogger(__name__)


async def _resolve_user_id(line_user_id: str) -> UUID | None:
    """LINE user_id -> auth.user_account.user_id.

    Stubbed on purpose: ADR 0002 (LINE identity linking) is still open per
    the team's own discussion, and auth.line_identity has no ORM model or
    lookup helper in this repo yet. Returning None here (rather than
    guessing at a mapping) is deliberate -- callers must handle it, not
    assume identity resolution always succeeds.
    """
    logger.warning(
        "identity resolution not implemented yet (ADR 0002 pending) -- "
        "line_user_id=%s cannot be mapped to a user_id",
        line_user_id,
    )
    return None


def _parse_postback_data(data: str) -> tuple[str, list[str]]:
    """Placeholder convention: "action:arg1:arg2". The real encoding depends
    on whichever task-picker UX the team lands on (undecided, out of scope
    for this task) -- this only exists so the wiring below has something
    concrete to dispatch on.
    """
    action, *args = data.split(":")
    return action, args


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
        await _handle_postback(event)
    else:
        logger.info("unhandled event type: %s", type(event).__name__)


async def _handle_message(event: MessageEvent) -> None:
    message = event.message

    if isinstance(message, TextMessageContent):
        user_id = await _resolve_user_id(event.source.user_id)
        if user_id is None:
            await reply_text(event.reply_token, "ยังไม่รองรับการเชื่อมบัญชี LINE ในตอนนี้")
            return

        async with async_session_maker() as session:
            result = await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.status == ConversationStatus.ACTIVE,
                )
            )
            conversation = result.scalars().first()
            if conversation is None:
                # No task-picker UX exists yet (out of scope for this task) --
                # a farmer with no active conversation has no way to start
                # one from a bare text message.
                await reply_text(event.reply_token, "กรุณาเลือกงานที่ต้องการทำก่อนเริ่มสนทนา")
                return

            form = await get_form(str(conversation.task_form_id))
            try:
                reply = await service.handle_answer(
                    session,
                    conversation_id=conversation.conversation_id,
                    raw_text=message.text,
                    form=form,
                )
            except ConversationNotFound:
                await reply_text(event.reply_token, "ไม่พบบทสนทนานี้แล้ว")
                return

        await reply_text(event.reply_token, reply.text)
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


async def _handle_postback(event: PostbackEvent) -> None:
    action, args = _parse_postback_data(event.postback.data)

    if action == "start":
        task_id, task_form_id = args
        user_id = await _resolve_user_id(event.source.user_id)
        if user_id is None:
            await reply_text(event.reply_token, "ยังไม่รองรับการเชื่อมบัญชี LINE ในตอนนี้")
            return

        form = await get_form(task_form_id)
        async with async_session_maker() as session:
            reply = await service.start_conversation(
                session,
                user_id=user_id,
                task_id=UUID(task_id),
                task_form_id=UUID(task_form_id),
                form=form,
            )
        await reply_text(event.reply_token, reply.text)
    elif action == "confirm":
        (conversation_id,) = args
        async with async_session_maker() as session:
            conversation = await session.get(Conversation, UUID(conversation_id))
            if conversation is None:
                await reply_text(event.reply_token, "ไม่พบบทสนทนานี้แล้ว")
                return
            form = await get_form(str(conversation.task_form_id))
            reply = await service.confirm_conversation(
                session, conversation_id=conversation.conversation_id, form=form
            )
        await reply_text(event.reply_token, reply.text)
    else:
        logger.info("postback event, data=%s", event.postback.data)


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
