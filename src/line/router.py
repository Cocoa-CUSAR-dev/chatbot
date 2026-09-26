import asyncio
import logging
from collections.abc import Coroutine
from typing import Annotated, Any
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

from src.conversation import reuse, service
from src.conversation.constants import ActiveSubstate, ConversationStatus
from src.conversation.exceptions import ConversationNotFound
from src.conversation.models import Conversation
from src.database import async_session_maker
from src.diary.client import generate_diary
from src.exceptions import UpstreamServiceError
from src.forms.client import get_form
from src.line import identity, parent_picker, temp_task_picker
from src.line.config import line_settings
from src.line.dependencies import parse_line_events
from src.line.flex_builders import build_diary_flex, build_quick_ack_flex
from src.line.schemas import QuickReplyOption
from src.line.service import (
    push_flex,
    reply_add_another_prompt,
    reply_autofill_offer,
    reply_confirm_prompt,
    reply_edit_picker,
    reply_flex,
    reply_task_choices,
    reply_text,
)
from src.sso.client import mint_sso_token
from src.tasks.client import fetch_last_answer

router = APIRouter(prefix="/line", tags=["line"])
logger = logging.getLogger(__name__)

_QUICK_REPLY_LIMIT = 13  # LINE's own cap on Quick Reply items per message

# US2-6 (docs-and-plan#130): asyncio.create_task's result has no strong
# reference by default, so Python is free to garbage-collect a fire-and-
# forget task mid-execution -- this set is exactly what asyncio's own docs
# recommend to prevent that, cleared via the done-callback once each task
# actually finishes.
_background_tasks: set[asyncio.Task[None]] = set()


def _fire_and_forget(coro: Coroutine[Any, None, None]) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _generate_and_push_diary(user_id: str, line_user_id: str) -> None:
    """US2-6 (docs-and-plan#130, #132, #133): fired without awaiting from the
    confirm postback handler below, so the quick ack (reply_flex) isn't
    delayed by web-backend's LLM polish pass. Errors are logged and
    swallowed -- confirm_conversation's own submit_task has already
    succeeded by the time this runs, so a diary that fails to generate
    means no follow-up card, not a failed submission.

    push_flex, not reply_flex: the reply token from the original confirm
    postback is long gone by the time the LLM polish pass finishes.
    """
    try:
        diary_text = await generate_diary(user_id)
    except Exception:
        logger.exception("diary generation failed for user_id=%s", user_id)
        return

    # SSO is best-effort, same as the LLM polish pass -- a farmer without a
    # working deep link should still get their diary card, just with a
    # link that lands on the login page instead of straight into /history.
    try:
        token = await mint_sso_token(user_id)
        history_url = f"{line_settings.WEB_APP_URL}/sso?token={token}"
    except Exception:
        logger.exception("SSO token mint failed for user_id=%s", user_id)
        history_url = f"{line_settings.WEB_APP_URL}/history"

    await push_flex(line_user_id, "ไดอารี่วันนี้", build_diary_flex(diary_text, history_url))


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
            is_start_keyword = message.text.strip().lower() in temp_task_picker.START_KEYWORDS

            if is_start_keyword:
                # US2-3: เริ่ม always shows every not-yet-done task (never
                # started, or a conversation still active/paused) -- not
                # gated on "is nothing currently active" anymore. Whatever
                # WAS active gets paused first so switching to look at the
                # list never silently loses it (replaces the old
                # abandoned-conversation-triggers-auto-cancel workaround:
                # that existed only to rescue เริ่ม from getting swallowed
                # as a literal answer, which pausing-instead-of-getting-
                # stuck no longer needs). Cancel itself is untouched, still
                # its own explicit action via the confirm prompt's button.
                await service.pause_active_conversation(session, user_id=user_id)

                # TEMPORARY (see src/line/temp_task_picker.py): Boom's real
                # LIFF to-do list is Sprint 5, ~2 months out as of when this
                # was written. Until then, a keyword lists pending tasks as
                # Quick Reply buttons instead of leaving the farmer stuck.
                tasks = await temp_task_picker.list_pending_tasks(session, user_id)
                if not tasks:
                    await reply_text(event.reply_token, "ไม่มีงานที่ต้องทำในตอนนี้")
                    return
                await reply_task_choices(event.reply_token, "เลือกงานที่ต้องการทำ:", tasks)
                return

            result = await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.status == ConversationStatus.ACTIVE,
                )
            )
            conversation = result.scalars().first()
            if conversation is None:
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
        task_id, task_form_id, handler = args
        user_id = await _resolve_user_id(event.source.user_id)
        if user_id is None:
            await reply_text(event.reply_token, "บัญชี LINE นี้ยังไม่ได้เชื่อมกับบัญชีในระบบ")
            return

        async with async_session_maker() as session:
            # US2-3/resume-conversation logic: this task might already have
            # a paused (or still-active) conversation -- resume it instead
            # of starting a duplicate one from scratch. Checked before
            # touching get_form/parent_picker at all, since a resumed
            # conversation doesn't need either of those precomputed again.
            existing = await service.find_resumable_conversation(
                session, user_id=user_id, task_id=UUID(task_id)
            )
            if existing is not None:
                form = await get_form(str(existing.task_form_id))
                reply = await service.resume_conversation(session, conversation=existing, form=form)
                await _reply(event.reply_token, reply)
                return

            form = await get_form(task_form_id)
            parent_kind = parent_picker.kind_for_handler(handler)
            parent_choices = None
            if parent_kind is not None:
                # One of the 5 previously-blocked handlers -- resolve the
                # farm/station-scoped picker options before starting the
                # conversation at all. Checked here (not left to
                # start_conversation) so an empty result never creates a
                # Conversation row that would have nothing to ask -- the
                # farmer is told to log the parent activity first instead.
                parent_choices = await parent_picker.choices_for(session, parent_kind, user_id)
                if not parent_choices:
                    await reply_text(event.reply_token, parent_picker.EMPTY_PROMPT[parent_kind])
                    return
            else:
                # US2-4: offer reusing the farmer's last COMPLETED
                # submission for this handler -- never for the 5
                # parent-picker handlers above (which farm/harvest/batch a
                # submission belongs to must always be picked fresh, never
                # reused). A Go hiccup here must not block starting a plain
                # conversation -- worst case, just no offer.
                try:
                    last_answer = await fetch_last_answer(user_id=str(user_id), handler=handler)
                except UpstreamServiceError:
                    logger.warning(
                        "fetch_last_answer failed for handler=%s -- starting fresh, no offer",
                        handler,
                        exc_info=True,
                    )
                    last_answer = None
                if last_answer is not None:
                    # Preview-only: router.py's own "start_autofill" branch
                    # re-fetches and re-sanitizes on "yes" rather than
                    # trusting this -- see reply_autofill_offer's docstring.
                    preview = ""
                    try:
                        offer_questions = service.questions_from_form(form)
                        sanitized_preview = await reuse.sanitize_for_autofill(
                            last_answer, offer_questions
                        )
                        preview = reuse.format_autofill_preview(sanitized_preview, offer_questions)
                    except UpstreamServiceError:
                        logger.warning(
                            "sanitize_for_autofill failed for handler=%s while building the "
                            "offer preview -- showing the offer without one",
                            handler,
                            exc_info=True,
                        )
                    await reply_autofill_offer(
                        event.reply_token,
                        task_id=task_id,
                        task_form_id=task_form_id,
                        handler=handler,
                        preview=preview,
                    )
                    return

            reply = await service.start_conversation(
                session,
                user_id=user_id,
                task_id=UUID(task_id),
                task_form_id=UUID(task_form_id),
                form=form,
                parent_kind=parent_kind,
                parent_choices=parent_choices,
            )
        await _reply(event.reply_token, reply)
    elif action == "start_autofill":
        decision, task_id, task_form_id, handler = args
        user_id = await _resolve_user_id(event.source.user_id)
        if user_id is None:
            await reply_text(event.reply_token, "บัญชี LINE นี้ยังไม่ได้เชื่อมกับบัญชีในระบบ")
            return

        form = await get_form(task_form_id)
        async with async_session_maker() as session:
            # Same double-tap/race guard as the "start" branch above --
            # this offer can only have been sent for a task with no
            # resumable conversation at the time, but time has passed
            # since then (the farmer had to read the offer and tap a
            # button), so re-check rather than trust that's still true.
            existing = await service.find_resumable_conversation(
                session, user_id=user_id, task_id=UUID(task_id)
            )
            if existing is not None:
                resume_form = await get_form(str(existing.task_form_id))
                reply = await service.resume_conversation(
                    session, conversation=existing, form=resume_form
                )
                await _reply(event.reply_token, reply)
                return

            if decision == "yes":
                # Re-fetched rather than trusting the offer's own check --
                # see reply_autofill_offer's docstring: no state is
                # persisted between the offer and this tap.
                try:
                    last_answer = await fetch_last_answer(user_id=str(user_id), handler=handler)
                except UpstreamServiceError:
                    logger.warning(
                        "fetch_last_answer failed for handler=%s on start_autofill -- "
                        "starting fresh instead",
                        handler,
                        exc_info=True,
                    )
                    last_answer = None
                if last_answer is not None:
                    questions = service.questions_from_form(form)
                    try:
                        sanitized = await reuse.sanitize_for_autofill(last_answer, questions)
                    except UpstreamServiceError:
                        # #105 (Go's /service/autofill/sanitize) hiccuped --
                        # same "never block a plain start" reasoning as the
                        # fetch_last_answer failure above. Nothing was
                        # created yet, so falling through to a plain start
                        # below is safe.
                        logger.warning(
                            "fetch_sanitized_autofill failed for handler=%s on "
                            "start_autofill -- starting fresh instead",
                            handler,
                            exc_info=True,
                        )
                    else:
                        reply = await service.start_conversation_with_autofill(
                            session,
                            user_id=user_id,
                            task_id=UUID(task_id),
                            task_form_id=UUID(task_form_id),
                            form=form,
                            sanitized_answer=sanitized,
                        )
                        await _reply(event.reply_token, reply)
                        return
                # The last answer disappeared between the offer and this
                # tap (race, or a second Go hiccup) -- fall through to a
                # plain start below rather than leaving the farmer stuck
                # on a "yes" that can no longer be honored.

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
        if reply.offer_another:
            # Saved, but this task wants more rows (multi-submit) -- offer
            # the next one instead of closing out.
            await reply_add_another_prompt(event.reply_token, reply.text, reply.conversation_id)
            return
        # Not _reply(): confirm_conversation's reply still carries substate
        # AWAITING_CONFIRMATION on its terminal "thanks" message (the
        # conversation is COMPLETED by this point, not awaiting anything),
        # so routing it through _reply() would attach a confirm button
        # pointing at an already-completed conversation.
        #
        # US2-6: sent via reply_flex, not reply_text, so this ack and the
        # diary card pushed a few seconds later (once generation finishes)
        # read as the same kind of message rather than plain text followed
        # by a Flex card.
        await reply_flex(event.reply_token, reply.text, build_quick_ack_flex(reply.text))
        _fire_and_forget(_generate_and_push_diary(str(conversation.user_id), event.source.user_id))
    elif action == "add_another":
        # Multi-submit loop: open the next submission on the same task and
        # form, with the parent selection carried forward so the farmer
        # isn't asked to re-pick the same harvest before every grade.
        (conversation_id,) = args
        async with async_session_maker() as session:
            conversation = await session.get(Conversation, UUID(conversation_id))
            if conversation is None:
                await reply_text(event.reply_token, "ไม่พบบทสนทนานี้แล้ว")
                return
            form = await get_form(str(conversation.task_form_id))
            try:
                reply = await service.start_next_submission(
                    session, conversation_id=conversation.conversation_id, form=form
                )
            except ConversationNotFound:
                await reply_text(event.reply_token, "ไม่พบบทสนทนานี้แล้ว")
                return
        await _reply(event.reply_token, reply)
    elif action == "finish_multi":
        # Phase 1 has nowhere to record "the farmer is done adding rows" --
        # that needs form.assignment, which is Phase 2. So this button only
        # acknowledges; the task itself stays listed until close_at. Named
        # as a known cost in the multi-submit design doc, not an oversight.
        #
        # The diary (US2-6) fires here rather than after every saved row: on
        # a multi-submit task this is the farmer actually saying they're
        # done, so it's the equivalent of the single-submit confirm above.
        (conversation_id,) = args
        async with async_session_maker() as session:
            conversation = await session.get(Conversation, UUID(conversation_id))
        await reply_text(event.reply_token, "บันทึกข้อมูลครบแล้ว ขอบคุณครับ")
        if conversation is not None:
            _fire_and_forget(
                _generate_and_push_diary(str(conversation.user_id), event.source.user_id)
            )
    elif action == "edit":
        # US2-6: shows a picker of every already-answered (or skipped)
        # question rather than asking which field by name -- same
        # "buttons, not free text" reasoning as everywhere else in this
        # flow (elderly farmers are the primary users).
        (conversation_id,) = args
        async with async_session_maker() as session:
            conversation = await session.get(Conversation, UUID(conversation_id))
            if conversation is None:
                await reply_text(event.reply_token, "ไม่พบบทสนทนานี้แล้ว")
                return
            form = await get_form(str(conversation.task_form_id))
            questions = await service.editable_questions(
                session, conversation_id=conversation.conversation_id, form=form
            )
        if not questions:
            # Shouldn't happen in practice -- reaching AWAITING_CONFIRMATION
            # requires every required question to have an answer row -- but
            # an honest message beats a Quick Reply with zero buttons.
            await reply_text(event.reply_token, "ยังไม่มีคำตอบให้แก้ไขในตอนนี้")
            return
        await reply_edit_picker(
            event.reply_token,
            conversation_id=conversation.conversation_id,
            questions=questions[:_QUICK_REPLY_LIMIT],
        )
    elif action == "edit_pick":
        conversation_id, question_id = args
        async with async_session_maker() as session:
            conversation = await session.get(Conversation, UUID(conversation_id))
            if conversation is None:
                await reply_text(event.reply_token, "ไม่พบบทสนทนานี้แล้ว")
                return
            form = await get_form(str(conversation.task_form_id))
            try:
                reply = await service.begin_edit(
                    session,
                    conversation_id=UUID(conversation_id),
                    question_id=UUID(question_id),
                    form=form,
                )
            except ConversationNotFound:
                await reply_text(event.reply_token, "ไม่พบคำถามนี้แล้ว")
                return
        # begin_edit's reply is substate=GUIDED_ASKING_FIXED_QUESTION with
        # the question's own choices -- _reply() already knows how to
        # render that as a Quick Reply, same as any normal guided-flow
        # question.
        await _reply(event.reply_token, reply)
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
