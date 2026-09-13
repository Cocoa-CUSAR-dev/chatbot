import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    FlexContainer,
    FlexMessage,
    MessageAction,
    MulticastRequest,
    PostbackAction,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
)

from src.line.config import line_settings
from src.line.schemas import QuickReplyOption

if TYPE_CHECKING:
    from src.conversation.service import Question
    from src.line.temp_task_picker import PendingTask

logger = logging.getLogger(__name__)

# LINE's multicast endpoint accepts at most 500 recipients per call.
_MULTICAST_LIMIT = 500

_configuration = Configuration(access_token=line_settings.LINE_CHANNEL_ACCESS_TOKEN)

_QUICK_REPLY_LABEL_MAX = 20  # LINE's own platform limit on a button's label


def _build_quick_reply(options: list[QuickReplyOption]) -> QuickReply:
    return QuickReply(
        items=[
            QuickReplyItem(action=MessageAction(label=opt.label, text=opt.text)) for opt in options
        ]
    )


async def reply_text(
    reply_token: str, text: str, quick_reply: list[QuickReplyOption] | None = None
) -> None:
    """Reply within the ~60s reply-token window -- free, not a push."""
    message = TextMessage(
        text=text,
        quickReply=_build_quick_reply(quick_reply) if quick_reply else None,
    )
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).reply_message(
            ReplyMessageRequest(replyToken=reply_token, messages=[message])
        )


async def reply_task_choices(reply_token: str, text: str, tasks: list["PendingTask"]) -> None:
    """Quick Reply where each tap fires a Postback instead of re-sending its
    label as text -- used for the TEMPORARY task-picker keyword flow (see
    src/line/temp_task_picker.py; Sprint 5's real LIFF to-do list replaces
    this). Each button's data is "start:<task_id>:<task_form_id>:<handler>" --
    handler rides along so _handle_postback can warn the farmer before
    starting a conversation for a form Go can't save yet, without a second
    DB round-trip (well under LINE's 300-char limit on postback data).
    """
    quick_reply = QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(
                    # US2-3: a short "🔄 " marker on the button itself
                    # (label is truncated to 20 chars, so a longer prefix
                    # would eat into an already-tight budget) -- the
                    # unclipped displayText spells it out in full instead,
                    # since that's what shows in the chat history.
                    label=(f"🔄 {task.title}" if task.has_conversation else task.title)[
                        :_QUICK_REPLY_LABEL_MAX
                    ],
                    data=f"start:{task.task_id}:{task.task_form_id}:{task.handler}",
                    displayText=(f"ทำต่อ: {task.title}" if task.has_conversation else task.title),
                )
            )
            for task in tasks
        ]
    )
    message = TextMessage(text=text, quickReply=quick_reply)
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).reply_message(
            ReplyMessageRequest(replyToken=reply_token, messages=[message])
        )


async def reply_confirm_prompt(reply_token: str, text: str, conversation_id: UUID) -> None:
    """Three Quick Reply buttons: "confirm", "edit", and "cancel" Postbacks.

    Without this, AWAITING_CONFIRMATION has no way for a farmer to actually
    confirm via real LINE -- typing free text at that point raises
    ConversationNotFound in handle_answer (no open question left to answer
    against). Same Postback convention reply_task_choices already
    established for "start"; router.py's _handle_postback already knows
    how to handle "confirm:<conversation_id>", "edit:<conversation_id>",
    and "cancel:<conversation_id>".

    Edit (US2-6) exists so a farmer who spots a mistake in the summary can
    fix just that one field instead of cancelling and starting the whole
    form over.

    Cancel exists because this same prompt is re-shown on a failed
    submission (CB-1) -- for a handler Go can't save yet, retrying can
    never succeed, and a farmer needs a way out other than retrying
    forever (live-reported 2026-08-09: 3 retries in a row, all the same
    honest failure, no escape).
    """
    quick_reply = QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(
                    label="ยืนยัน",
                    data=f"confirm:{conversation_id}",
                    displayText="ยืนยัน",
                )
            ),
            QuickReplyItem(
                action=PostbackAction(
                    label="แก้ไข",
                    data=f"edit:{conversation_id}",
                    displayText="แก้ไข",
                )
            ),
            QuickReplyItem(
                action=PostbackAction(
                    label="ยกเลิก",
                    data=f"cancel:{conversation_id}",
                    displayText="ยกเลิก",
                )
            ),
        ]
    )
    message = TextMessage(text=text, quickReply=quick_reply)
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).reply_message(
            ReplyMessageRequest(replyToken=reply_token, messages=[message])
        )


async def reply_autofill_offer(
    reply_token: str, *, task_id: str, task_form_id: str, handler: str, preview: str = ""
) -> None:
    """US2-4: offers reusing the farmer's last COMPLETED submission for this
    handler, before a fresh Conversation row exists. Two Postback buttons,
    not a guided-flow question -- there's no current_question_id yet for
    handle_answer to resolve a typed "ใช่"/"ไม่" against, since the
    conversation itself doesn't exist until the farmer answers this. Same
    encode-everything-in-postback-data convention as reply_task_choices/
    reply_confirm_prompt: no extra state is persisted between this offer and
    the tap -- router.py's "start_autofill" branch re-fetches (and
    re-sanitizes) the last answer itself on "yes" rather than trusting
    whatever was true when this was sent; `preview` is display-only,
    computed by that same router.py call site purely to show here.

    `preview` (from reuse.format_autofill_preview) is shown before the
    yes/no question -- live-reported feedback: a blind "reuse old data?"
    prompt with no content until AFTER agreeing left a farmer unable to
    make an informed choice.
    """
    quick_reply = QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(
                    label="ใช้ข้อมูลเดิม",
                    data=f"start_autofill:yes:{task_id}:{task_form_id}:{handler}",
                    displayText="ใช้ข้อมูลเดิม (แก้ไขเพิ่มเติมได้ทีหลัง)",
                )
            ),
            QuickReplyItem(
                action=PostbackAction(
                    label="กรอกใหม่",
                    data=f"start_autofill:no:{task_id}:{task_form_id}:{handler}",
                    displayText="กรอกใหม่",
                )
            ),
        ]
    )
    prefix = f"พบข้อมูลที่เคยกรอกไว้ก่อนหน้านี้:\n{preview}\n\n" if preview else "พบข้อมูลที่เคยกรอกไว้ก่อนหน้านี้ "
    message = TextMessage(
        text=(
            f"{prefix}ต้องการนำมาใช้กรอกให้อัตโนมัติหรือไม่? "
            '(เลือก "ใช้ข้อมูลเดิม" แล้วยังกลับมาแก้ไขทีละข้อได้ทีหลัง ผ่านปุ่ม "แก้ไข" ตอนสรุปคำตอบ)'
        ),
        quickReply=quick_reply,
    )
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).reply_message(
            ReplyMessageRequest(replyToken=reply_token, messages=[message])
        )


async def reply_edit_picker(
    reply_token: str, *, conversation_id: UUID, questions: list["Question"]
) -> None:
    """US2-6: lists the questions a farmer can currently revisit (from
    service.editable_questions -- already answered or skipped), one
    Postback button each. Same "no state persisted between the message and
    the tap" convention as reply_task_choices/reply_autofill_offer: the
    button's own data carries everything router.py's "edit_pick" branch
    needs (conversation_id + question_id) to re-open that exact question.
    """
    quick_reply = QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(
                    label=q.label[:_QUICK_REPLY_LABEL_MAX],
                    data=f"edit_pick:{conversation_id}:{q.question_id}",
                    displayText=q.label,
                )
            )
            for q in questions
        ]
    )
    message = TextMessage(text="เลือกข้อที่ต้องการแก้ไข:", quickReply=quick_reply)
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).reply_message(
            ReplyMessageRequest(replyToken=reply_token, messages=[message])
        )


async def reply_flex(reply_token: str, alt_text: str, contents: dict[str, Any]) -> None:
    """Flex Message reply -- e.g. a confirmation summary (AwaitingConfirmation,
    target-architecture.md #4) with real layout instead of a wall of text.
    `contents` is a raw Flex JSON dict (bubble or carousel) -- see LINE's Flex
    Message docs / simulator for building one; FlexContainer.from_dict parses
    it into the SDK's own model.
    """
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[
                    FlexMessage(altText=alt_text, contents=FlexContainer.from_dict(contents))
                ],
            )
        )


async def push_text(to: str, text: str) -> None:
    """Proactive send outside the reply-token window -- this is a COSTED send.

    Used for things like reminders (ADR 0006) and late-extraction follow-ups
    (ADR 0004) -- never call this when a reply-token is still valid.
    """
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).push_message(
            PushMessageRequest(to=to, messages=[TextMessage(text=text)])
        )


async def push_flex(to: str, alt_text: str, contents: dict[str, Any]) -> None:
    flex_message = FlexMessage(altText=alt_text, contents=FlexContainer.from_dict(contents))
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).push_message(
            PushMessageRequest(to=to, messages=[flex_message])
        )


async def multicast_text(to: list[str], text: str) -> None:
    """Same message to many farmers at once -- e.g. a reminder batch
    (ADR 0006) -- distinct from looping push_text per-recipient, and billed
    differently by LINE. Callers with more than LINE's 500-recipient cap
    should use multicast_text_batched instead of chunking themselves.
    """
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).multicast(
            MulticastRequest(to=to, messages=[TextMessage(text=text)])
        )


async def multicast_text_batched(to: list[str], text: str) -> tuple[list[str], list[str]]:
    """Chunks `to` into LINE's 500-recipient multicast limit and sends each
    chunk independently, returning (succeeded, failed) LINE user ids.

    The two previous call sites (src/reminders/jobs.py, src/notifications/
    service.py) each hand-rolled this same chunking loop wrapped in one big
    try/except around the whole thing -- so a later chunk failing marked
    every recipient as failed, including ones from an earlier chunk that had
    already been delivered. Tracking success per chunk here, in one place,
    fixes both call sites at once and means a future change to LINE's limit
    (or to this retry behavior) only has to happen here.
    """
    succeeded: list[str] = []
    failed: list[str] = []
    for start in range(0, len(to), _MULTICAST_LIMIT):
        chunk = to[start : start + _MULTICAST_LIMIT]
        try:
            await multicast_text(chunk, text)
        except Exception:
            logger.exception("multicast chunk of %d recipient(s) failed", len(chunk))
            failed.extend(chunk)
        else:
            succeeded.extend(chunk)
    return succeeded, failed
