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
from sqlalchemy.ext.asyncio import AsyncSession

from src.conversation import intent, reuse, service, text_parsing
from src.conversation.constants import ActiveSubstate, ConversationStatus
from src.conversation.exceptions import ConversationNotFound
from src.conversation.intent import Intent
from src.conversation.models import Conversation
from src.database import async_session_maker
from src.diary.client import generate_diary
from src.exceptions import UpstreamServiceError
from src.forms.client import get_form
from src.line import identity, messages, parent_picker, temp_task_picker
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


async def _generate_and_push_diary(user_id: str, line_user_id: str) -> None:
    """US2-6 (docs-and-plan#130, #132, #133): awaited directly from both
    postback handlers below (single-submit confirm, and finish_multi), not
    fired as a background task -- confirmed live 2026-09-30: this service
    runs on Vercel, which freezes a function the moment it returns its
    response, so a detached asyncio.create_task never got a chance to run
    (Gemini showed zero usage; the diary card never arrived). Same root
    cause the reminders/pause-sweep jobs hit before (see git log for "fix:
    trigger reminder/pause jobs externally instead of relying on
    APScheduler") -- background work after responding doesn't survive on
    this host. The quick ack/finish reply still reaches the farmer
    immediately regardless, since those are their own calls to LINE's API,
    not the webhook's own HTTP response -- only the webhook request itself
    now stays open until the diary card is pushed. Errors are logged and
    swallowed -- confirm_conversation's own submit_task has already
    succeeded by the time this runs, so a diary that fails to generate
    means no follow-up card, not a failed submission.

    push_flex, not reply_flex: the reply token from the original postback
    is long gone by the time the LLM polish pass finishes.
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


# docs-and-plan#189: the confirmation summary's buttons are postback-only, so
# a farmer who types instead of tapping used to get "ไม่พบบทสนทนานี้แล้ว".
# These are the typed equivalents of the three buttons, matched exactly after
# strip().lower() plus trailing-particle stripping ("ยืนยันครับ" -> "ยืนยัน")
# -- NOT sent through the intent classifier: nothing mid-form ever is (see
# _reply_for_intent's docstring / the US2-11 design doc §3.1).
_CONFIRM_WORDS = frozenset({"ยืนยัน", "ตกลง", "ใช่", "ok", "โอเค", "เค", "ส่ง"})
_CANCEL_WORDS = frozenset({"ยกเลิก"})
_EDIT_WORDS = frozenset({"แก้ไข"})


def _awaiting_confirmation(conversation: Conversation) -> bool:
    """True when an ACTIVE conversation has no open question left -- i.e. it
    is sitting on the confirmation summary.

    Mirrors handle_answer's own two checks, in the same order: the
    parent-picker step also leaves current_question_id NULL (see
    start_conversation), and that step is still genuinely awaiting a typed
    answer, so it must not be mistaken for the confirmation step.
    """
    if (conversation.parent_answer or {}).get("pending_kind") is not None:
        return False
    return conversation.current_question_id is None


async def _confirm_and_reply(reply_token: str, line_user_id: str, conversation_id: UUID) -> None:
    """The "ยืนยัน" path, shared by the confirm postback and the typed
    yes-words at the confirmation step (docs-and-plan#189) -- one
    implementation so the typed route can never drift from the tapped one
    (it also carries the multi-submit prompt and the awaited diary, both
    easy to forget in a second copy).
    """
    async with async_session_maker() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            await reply_text(reply_token, "ไม่พบบทสนทนานี้แล้ว")
            return
        form = await get_form(str(conversation.task_form_id))
        reply = await service.confirm_conversation(
            session, conversation_id=conversation.conversation_id, form=form
        )
        user_id = str(conversation.user_id)

    if reply.submission_failed:
        # Re-attach the confirm button so tapping it again retries --
        # the conversation is still awaiting confirmation, not completed.
        await reply_confirm_prompt(reply_token, reply.text, reply.conversation_id)
        return
    if reply.offer_another:
        # Saved, but this task wants more rows (multi-submit) -- offer
        # the next one instead of closing out.
        await reply_add_another_prompt(reply_token, reply.text, reply.conversation_id)
        return
    # Not _reply(): confirm_conversation's reply still carries substate
    # AWAITING_CONFIRMATION on its terminal "thanks" message (the
    # conversation is COMPLETED by this point, not awaiting anything),
    # so routing it through _reply() would attach a confirm button
    # pointing at an already-completed conversation.
    #
    # US2-6: sent via reply_flex, not reply_text, so this ack and the
    # diary card pushed once generation finishes read as the same kind
    # of message rather than plain text followed by a Flex card.
    await reply_flex(reply_token, reply.text, build_quick_ack_flex(reply.text))
    await _generate_and_push_diary(user_id, line_user_id)


async def _cancel_and_reply(reply_token: str, conversation_id: UUID) -> None:
    """The "ยกเลิก" path, shared with the typed word (docs-and-plan#189)."""
    async with async_session_maker() as session:
        try:
            reply = await service.cancel_conversation(session, conversation_id=conversation_id)
        except ConversationNotFound:
            await reply_text(reply_token, "ไม่พบบทสนทนานี้แล้ว")
            return
    # Same reasoning as confirm above: terminal message, no button.
    await reply_text(reply_token, reply.text)


async def _edit_and_reply(reply_token: str, conversation_id: UUID) -> None:
    """The "แก้ไข" path, shared with the typed word (docs-and-plan#189).

    US2-6: shows a picker of every already-answered (or skipped) question
    rather than asking which field by name -- same "buttons, not free text"
    reasoning as everywhere else in this flow (elderly farmers are the
    primary users).
    """
    async with async_session_maker() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            await reply_text(reply_token, "ไม่พบบทสนทนานี้แล้ว")
            return
        form = await get_form(str(conversation.task_form_id))
        questions = await service.editable_questions(
            session, conversation_id=conversation.conversation_id, form=form
        )
    if not questions:
        # Shouldn't happen in practice -- reaching AWAITING_CONFIRMATION
        # requires every required question to have an answer row -- but
        # an honest message beats a Quick Reply with zero buttons.
        await reply_text(reply_token, "ยังไม่มีคำตอบให้แก้ไขในตอนนี้")
        return
    await reply_edit_picker(
        reply_token,
        conversation_id=conversation_id,
        questions=questions[:_QUICK_REPLY_LIMIT],
    )


async def _handle_confirmation_step_text(
    event: MessageEvent, *, conversation_id: UUID, raw_text: str
) -> None:
    """Typed text while the confirmation summary is showing (#189).

    Found in manual testing: a farmer typed "เค" at the summary and got
    "ไม่พบบทสนทนานี้แล้ว", because handle_answer raises ConversationNotFound
    when there is no open question. Anything recognisable now runs the same
    code path as the matching button; anything else re-shows the summary
    with its buttons instead of dead-ending.
    """
    word = text_parsing.strip_trailing_particles(raw_text.strip().lower())

    if word in _CONFIRM_WORDS:
        await _confirm_and_reply(event.reply_token, event.source.user_id, conversation_id)
        return
    if word in _CANCEL_WORDS:
        await _cancel_and_reply(event.reply_token, conversation_id)
        return
    if word in _EDIT_WORDS:
        await _edit_and_reply(event.reply_token, conversation_id)
        return

    async with async_session_maker() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            await reply_text(event.reply_token, "ไม่พบบทสนทนานี้แล้ว")
            return
        form = await get_form(str(conversation.task_form_id))
        reply = await service.confirmation_prompt(
            session, conversation_id=conversation_id, form=form
        )
    await reply_confirm_prompt(
        event.reply_token,
        f"{messages.PRESS_A_BUTTON}\n\n{reply.text}",
        reply.conversation_id,
    )


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
        await _handle_follow(event)
    elif isinstance(event, PostbackEvent):
        await _handle_postback(event)
    else:
        logger.info("unhandled event type: %s", type(event).__name__)


async def _reply_task_list(
    reply_token: str,
    session: AsyncSession,
    user_id: UUID,
    *,
    only_title: str | None = None,
) -> None:
    """The one implementation of "show this farmer their tasks", shared by the
    exact start keyword and by a classified SHOW_TASKS (US2-11 #184).

    Shared on purpose: the keyword path pauses whatever was ACTIVE first
    (US2-3), and a second copy of this would sooner or later forget to. Here
    nothing can be active -- the caller only reaches the classifier when there
    is no ACTIVE conversation -- so the pause is a no-op on that path, which
    is exactly why it is safe to keep identical.

    only_title narrows the list to the single task the farmer named
    ("อยากกรอกเก็บเกี่ยว"). It is a filter over the same query, never a
    different one -- and if it somehow matches nothing, the farmer gets the
    full list rather than an empty one.
    """
    await service.pause_active_conversation(session, user_id=user_id)

    # TEMPORARY (see src/line/temp_task_picker.py): Boom's real LIFF to-do
    # list is Sprint 5. Until then, pending tasks come back as Quick Reply
    # buttons instead of leaving the farmer stuck.
    tasks = await temp_task_picker.list_pending_tasks(session, user_id)
    if only_title is not None:
        tasks = [task for task in tasks if task.title == only_title] or tasks
    if not tasks:
        await reply_text(reply_token, messages.NO_PENDING_TASKS)
        return
    await reply_task_choices(reply_token, "เลือกงานที่ต้องการทำ:", tasks)


async def _reply_for_intent(
    reply_token: str,
    session: AsyncSession,
    user_id: UUID,
    result: intent.IntentResult,
    task_titles: list[str],
) -> None:
    """Turns a classified intent into one of the flows that already exists
    (US2-11 #184).

    Two things this deliberately never does. It never starts a form:
    SHOW_TASKS ends at the same Quick Reply buttons the keyword produces, so
    the worst a misclassification can cost is one unwanted list, never a
    conversation the farmer didn't ask for. And it never sends text the model
    wrote -- every non-task branch is a fixed string from src/line/messages.py,
    chosen by the intent, so a farmer can't be told something nobody on the
    team has read (no weather, no prices, no agronomy advice).

    UNKNOWN is also what every classifier failure returns, so this last branch
    is the bot's pre-US2-11 behaviour, unchanged.
    """
    if result.intent is Intent.SHOW_TASKS:
        await _reply_task_list(
            reply_token,
            session,
            user_id,
            only_title=intent.match_task_hint(result.task_hint, task_titles),
        )
        return

    text = {
        Intent.GREETING: messages.WELCOME_BACK,
        Intent.HELP: messages.HELP,
        Intent.OFF_TOPIC: messages.OFF_TOPIC,
    }.get(result.intent, messages.START_HINT)
    await reply_text(reply_token, text, quick_reply=messages.START_QUICK_REPLY)


async def _handle_follow(event: FollowEvent) -> None:
    """A farmer just added the OA as a friend (US2-11 / docs-and-plan#185).

    Previously this only logged, so the farmer's first ever interaction with
    the bot was silence, and nothing told them what it is for or what to type.
    A reply is used rather than a push: the follow event carries a reply
    token, and replies don't spend the monthly push quota.

    An unlinked farmer is pointed at a human on purpose. HOW a LINE account
    gets linked is ADR 0002, still undecided -- inventing a procedure here
    would be a promise this service can't keep.
    """
    user_id = await _resolve_user_id(event.source.user_id)
    if user_id is None:
        await reply_text(event.reply_token, messages.WELCOME_NOT_LINKED)
        return
    await reply_text(
        event.reply_token,
        messages.WELCOME_NEW_FRIEND,
        quick_reply=messages.START_QUICK_REPLY,
    )


async def _reply_unsupported_message_type(event: MessageEvent) -> None:
    """#186 item 2: every non-text message type used to be logged and
    silently dropped, so a farmer who sent a sticker, a photo or their
    location got no reply whatsoever and no way to tell the bot from a dead
    one. Mid-form the farmer is being asked something specific, so they are
    told to type the answer rather than pointed back at the task list.
    """
    user_id = await _resolve_user_id(event.source.user_id)
    if user_id is None:
        await reply_text(event.reply_token, messages.NOT_LINKED)
        return

    async with async_session_maker() as session:
        result = await session.execute(
            select(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.status == ConversationStatus.ACTIVE,
            )
        )
        in_form = result.scalars().first() is not None

    if in_form:
        await reply_text(event.reply_token, messages.UNSUPPORTED_MESSAGE_TYPE_IN_FORM)
        return
    await reply_text(
        event.reply_token,
        messages.UNSUPPORTED_MESSAGE_TYPE,
        quick_reply=messages.START_QUICK_REPLY,
    )


async def _handle_message(event: MessageEvent) -> None:
    message = event.message

    if isinstance(message, TextMessageContent):
        user_id = await _resolve_user_id(event.source.user_id)
        if user_id is None:
            await reply_text(event.reply_token, messages.NOT_LINKED)
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
                #
                # US2-11: the exact keyword keeps its own branch ahead of the
                # classifier -- free, instant, and never at the mercy of a
                # provider being up.
                await _reply_task_list(event.reply_token, session, user_id)
                return

            result = await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.status == ConversationStatus.ACTIVE,
                )
            )
            conversation = result.scalars().first()
            if conversation is None:
                # US2-11 (#184): the only branch this story replaces. Every
                # OTHER path above stays exactly as it was -- in particular
                # an ACTIVE conversation never reaches the classifier, so a
                # real answer like "เริ่มเก็บเกี่ยววันนี้" is still an answer.
                tasks = await temp_task_picker.list_pending_tasks(session, user_id)
                classified = await intent.classify(message.text, [task.title for task in tasks])
                await _reply_for_intent(
                    event.reply_token,
                    session,
                    user_id,
                    classified,
                    [task.title for task in tasks],
                )
                return

            # docs-and-plan#189. The pause label is deliberately excluded:
            # handle_answer treats pausing as valid from ANY step, including
            # this one, and that must keep working.
            if _awaiting_confirmation(conversation) and message.text.strip() != service.PAUSE_LABEL:
                await _handle_confirmation_step_text(
                    event,
                    conversation_id=conversation.conversation_id,
                    raw_text=message.text,
                )
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
        await _reply_unsupported_message_type(event)
    elif isinstance(message, ImageMessageContent):
        # TODO: hand off to src.conversation -- photo evidence for a task.
        logger.info("image message, id=%s", message.id)
        await _reply_unsupported_message_type(event)
    else:
        # Stickers, audio, video, files -- previously logged and dropped, so
        # the farmer got no reply at all (#186 item 2).
        logger.info("unhandled message content type: %s", type(message).__name__)
        await _reply_unsupported_message_type(event)


async def _handle_postback(event: PostbackEvent) -> None:
    action, args = _parse_postback_data(event.postback.data)

    if action == "start":
        task_id, task_form_id, handler = args
        user_id = await _resolve_user_id(event.source.user_id)
        if user_id is None:
            await reply_text(event.reply_token, messages.NOT_LINKED)
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
            await reply_text(event.reply_token, messages.NOT_LINKED)
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
        await _confirm_and_reply(event.reply_token, event.source.user_id, UUID(conversation_id))
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
            await _generate_and_push_diary(str(conversation.user_id), event.source.user_id)
    elif action == "edit":
        (conversation_id,) = args
        await _edit_and_reply(event.reply_token, UUID(conversation_id))
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
        await _cancel_and_reply(event.reply_token, UUID(conversation_id))
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
