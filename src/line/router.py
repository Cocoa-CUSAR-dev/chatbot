from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from linebot.v3.webhooks import Event, MessageEvent, TextMessageContent

from src.line.dependencies import parse_line_events
from src.line.service import reply_text

router = APIRouter(prefix="/line", tags=["line"])


async def _handle_event(event: Event) -> None:
    """Dispatches one parsed webhook event.

    This is deliberately a thin dispatcher -- see src/conversation for the
    actual slot-filling/state-machine logic (target-architecture.md #4).
    """
    if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
        # TODO: hand off to src.conversation's engine instead of echoing.
        await reply_text(event.reply_token, f"got: {event.message.text}")


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
