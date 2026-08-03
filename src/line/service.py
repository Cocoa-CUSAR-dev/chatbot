from typing import Any

from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    FlexContainer,
    FlexMessage,
    MessageAction,
    MulticastRequest,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
)

from src.line.config import line_settings
from src.line.schemas import QuickReplyOption

_configuration = Configuration(access_token=line_settings.LINE_CHANNEL_ACCESS_TOKEN)


def _build_quick_reply(options: list[QuickReplyOption]) -> QuickReply:
    return QuickReply(
        items=[
            QuickReplyItem(action=MessageAction(label=opt.label, text=opt.text))
            for opt in options
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
