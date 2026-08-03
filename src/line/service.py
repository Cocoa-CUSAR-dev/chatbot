from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)

from src.line.config import line_settings

_configuration = Configuration(access_token=line_settings.LINE_CHANNEL_ACCESS_TOKEN)


async def reply_text(reply_token: str, text: str) -> None:
    """Reply within the ~60s reply-token window -- free, not a push."""
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).reply_message(
            ReplyMessageRequest(replyToken=reply_token, messages=[TextMessage(text=text)])
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
