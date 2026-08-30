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
    from src.line.temp_task_picker import PendingTask

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
    """Two Quick Reply buttons: "confirm" and "cancel" Postbacks.

    Without this, AWAITING_CONFIRMATION has no way for a farmer to actually
    confirm via real LINE -- typing free text at that point raises
    ConversationNotFound in handle_answer (no open question left to answer
    against). Same Postback convention reply_task_choices already
    established for "start"; router.py's _handle_postback already knows
    how to handle "confirm:<conversation_id>" and "cancel:<conversation_id>".

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
    differently by LINE.
    """
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).multicast(
            MulticastRequest(to=to, messages=[TextMessage(text=text)])
        )
