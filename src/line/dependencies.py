"""Webhook signature verification -- see ADR 0003 (line-bot-sdk-python chosen
specifically so this doesn't need hand-rolled HMAC verification).
"""

from typing import Annotated

from fastapi import Header, Request
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import Event

from src.line.config import line_settings
from src.line.exceptions import InvalidLineSignature

_parser = WebhookParser(line_settings.LINE_CHANNEL_SECRET)


async def parse_line_events(
    request: Request,
    x_line_signature: Annotated[str, Header()],
) -> list[Event]:
    body = (await request.body()).decode("utf-8")
    try:
        return _parser.parse(body, x_line_signature)
    except InvalidSignatureError as exc:
        raise InvalidLineSignature from exc
