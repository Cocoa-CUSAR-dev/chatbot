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
from src.conversation.constants import ActiveSubstate, ConversationStatus
from src.conversation.exceptions import ConversationNotFound
from src.conversation.models import Conversation
from src.database import async_session_maker
from src.forms.client import get_form
from src.line import identity, temp_task_picker
from src.line.dependencies import parse_line_events
from src.line.schemas import QuickReplyOption
from src.line.service import reply_confirm_prompt, reply_task_choices, reply_text

router = APIRouter(prefix="/line", tags=["line"])
logger = logging.getLogger(__name__)

_QUICK_REPLY_LIMIT = 13  # LINE's own cap on Quick Reply items per message


async def _reply(reply_token: str, reply: service.ConversationReply) -> None:
    """Sends a ConversationReply back:
    - AWAITING_CONFIRMATION -> a single "confirm" Postback button, since
      there's no open question left to answer against (see
      reply_confirm_prompt's docstring for why this is required, not
      cosmetic).
    - current question is OPTION-type (reply.choices is set) -> Quick
      Reply buttons. Farmers pick by label, same MessageAction mechanism
      as QuickReplyOption already uses elsewhere -- handle_answer
      resolves the tapped label against the question's own choice list
      either way, so typing the exact label instead of tapping works
      identically.
    - otherwise -> plain text.
    """
    if reply.substate == ActiveSubstate.AWAITING_CONFIRMATION:
        await reply_confirm_prompt(reply_token, reply.text, reply.conversation_id)
        return

    if not reply.choices:
        await reply_text(reply_token, reply.text)
        return

    quick_reply = [
        QuickReplyOption(label=c.label[:20], text=c.label)
        for c in reply.choices[:_QUICK_REPLY_LIMIT]
    ]
    await reply_text(reply_token, reply.text, quick_reply=quick_reply)


async def _resolve_user_id(line_user_id: str) -> UUID | None:
    """LINE user_id -> auth.user_account.user_id, via auth.line_identity.

    Only the lookup half is implemented (src/line/identity.py) -- HOW a row
    gets into auth.line_identity in the first place (pairing code, OAuth,
    something else) is ADR 0002, still reopened/undecided by the team. A
    farmer with no linked row yet correctly gets None here; callers must
    handle that, not assume resolution always succeeds.
    """
    async with async_session_maker() as session:
        user_id = await identity.lookup_user_id(session, line_user_id)

    if user_id is None:
        logger.warning(
            "no auth.line_identity row for line_user_id=%s -- not linked yet",
            line_user_id,
        )
    return user_id


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
            await reply_text(event.reply_token, "บัญชี LINE นี้ยังไม่ได้เชื่อมกับบัญชีในระบบ")
            return

        async with async_session_maker() as session:
            result = await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.status == ConversationStatus.ACTIVE,
                )
            )
            conversation = result.scalars().first()
            is_start_keyword = message.text.strip().lower() in temp_task_picker.START_KEYWORDS

            # A conversation sitting ACTIVE with no open question is one
            # that reached AWAITING_CONFIRMATION and was then abandoned --
            # the farmer never tapped confirm or cancel (closed the app,
            # got distracted, etc.). Live-reported 2026-08-09: this
            # permanently blocked เริ่ม, since the keyword was only ever
            # checked when there was NO active conversation at all --
            # typing เริ่ม here got routed into handle_answer as if it were
            # an answer, which raises ConversationNotFound (no open
            # question), surfacing as an opaque "ไม่พบบทสนทนานี้แล้ว". เริ่ม
            # should always be a way to start over -- auto-cancel the
            # abandoned one first rather than leaving the farmer stuck.
            is_abandoned = conversation is not None and conversation.current_question_id is None
            if is_abandoned and is_start_keyword:
                assert conversation is not None  # narrows for mypy; is_abandoned implies this
                await service.cancel_conversation(
                    session, conversation_id=conversation.conversation_id
                )
                conversation = None

            if conversation is None:
                # TEMPORARY (see src/line/temp_task_picker.py): Boom's real
                # LIFF to-do list is Sprint 5, ~2 months out as of when this
                # was written. Until then, a keyword lists pending tasks as
                # Quick Reply buttons instead of leaving the farmer stuck.
                if is_start_keyword:
                    tasks = await temp_task_picker.list_pending_tasks(session, user_id)
                    if not tasks:
                        await reply_text(event.reply_token, "ไม่มีงานที่ต้องทำในตอนนี้")
                        return
                    await reply_task_choices(event.reply_token, "เลือกงานที่ต้องการทำ:", tasks)
                    return

                keyword = next(iter(temp_task_picker.START_KEYWORDS))
                await reply_text(event.reply_token, f'พิมพ์ "{keyword}" เพื่อดูงานที่ต้องทำ')
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

        if reply is None:
            # Another message for this conversation was already being
            # processed -- first message wins, this one is dropped silently
            # (no reply sent; see handle_answer's own docstring/CB-12).
            return
        await _reply(event.reply_token, reply)
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
            await reply_text(event.reply_token, "บัญชี LINE นี้ยังไม่ได้เชื่อมกับบัญชีในระบบ")
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
        await _reply(event.reply_token, reply)
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
        if reply.submission_failed:
            # Re-attach the confirm button so tapping it again retries --
            # the conversation is still awaiting confirmation, not completed.
            await reply_confirm_prompt(event.reply_token, reply.text, reply.conversation_id)
            return
        # Not _reply(): confirm_conversation's reply still carries substate
        # AWAITING_CONFIRMATION on its terminal "thanks" message (the
        # conversation is COMPLETED by this point, not awaiting anything),
        # so routing it through _reply() would attach a confirm button
        # pointing at an already-completed conversation.
        await reply_text(event.reply_token, reply.text)
    elif action == "cancel":
        (conversation_id,) = args
        async with async_session_maker() as session:
            try:
                reply = await service.cancel_conversation(
                    session, conversation_id=UUID(conversation_id)
                )
            except ConversationNotFound:
                await reply_text(event.reply_token, "ไม่พบบทสนทนานี้แล้ว")
                return
        # Same reasoning as confirm above: terminal message, no button.
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
