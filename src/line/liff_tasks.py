"""LIFF-authenticated routes for the farmer-facing to-do list screen
(docs-and-plan#176, US4-2) -- the real replacement for
src/line/temp_task_picker.py's keyword-triggered Quick Reply fallback (not
deleted yet; see that module's own docstring for why).

Both routes are reached from a plain HTTP call out of the LIFF webview, not
a LINE webhook event -- there is no reply_token here, which is why "start"
pushes its result (src.line.service.push_*) instead of replying, and is a
COSTED send unlike the free reply-token path router.py's "start" postback
uses for the same underlying logic.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.conversation import reuse, service
from src.conversation.constants import ActiveSubstate
from src.database import get_session
from src.exceptions import UpstreamServiceError
from src.forms.client import get_form
from src.line import identity, liff, parent_picker
from src.line.config import line_settings
from src.line.exceptions import LineAccountNotLinked
from src.line.liff import InvalidLiffToken
from src.line.schemas import QuickReplyOption
from src.line.service import push_autofill_offer, push_confirm_prompt, push_text
from src.tasks.client import fetch_last_answer, fetch_pending_tasks
from src.tasks.schemas import TaskListItem

router = APIRouter(prefix="/line/liff", tags=["liff"])
logger = logging.getLogger(__name__)

# Same local alias convention as src/notifications/router.py -- no shared
# export for this in src/database.py yet.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ResolvedLiffUser(BaseModel):
    user_id: UUID
    line_user_id: str


def _channel_id_from_liff_id(liff_id: str) -> str:
    """LINE documents a LIFF ID as "{channelId}-{randomId}" -- the same
    channel ID the OAuth2 verify endpoint's client_id parameter expects.
    Deriving it avoids a second, easy-to-desync env var just for this one
    value -- see LIFF_ID in src/line/config.py.
    """
    return liff_id.split("-", 1)[0]


async def resolve_liff_user(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> ResolvedLiffUser:
    """FastAPI dependency: Bearer LIFF ID token -> the internal user_id, via
    the same two steps _resolve_user_id/liff.verify_id_token already do
    elsewhere, just reachable from an HTTP call instead of a LINE event.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise InvalidLiffToken("Missing bearer LIFF ID token")
    id_token = authorization.removeprefix("Bearer ")

    if not line_settings.LIFF_ID:
        # Fail closed, same reasoning as ServiceAuthMiddleware's unset-key
        # case on the mobile-backend side: an unconfigured LIFF_ID must
        # never silently accept every token as if it were a real channel.
        raise InvalidLiffToken("LIFF_ID is not configured")

    channel_id = _channel_id_from_liff_id(line_settings.LIFF_ID)
    claims = await liff.verify_id_token(id_token, channel_id)
    line_user_id = claims.get("sub")
    if not line_user_id:
        raise InvalidLiffToken("LIFF token had no sub claim")

    user_id = await identity.lookup_user_id(session, line_user_id)
    if user_id is None:
        raise LineAccountNotLinked

    return ResolvedLiffUser(user_id=user_id, line_user_id=line_user_id)


LiffUserDep = Annotated[ResolvedLiffUser, Depends(resolve_liff_user)]


@router.get("/tasks")
async def list_pending_tasks(liff_user: LiffUserDep) -> list[TaskListItem]:
    """The to-do list itself. Filters out COMPLETED here, not in Go --
    fetch_pending_tasks/GetTasksForUser is a generic "list tasks for a
    user_id" building block (see that function's own docstring); "pending"
    is this caller's notion, same division of responsibility
    temp_task_picker.list_pending_tasks already had.
    """
    tasks = await fetch_pending_tasks(user_id=str(liff_user.user_id))
    return [task for task in tasks if task.status != "COMPLETED"]


class StartTaskRequest(BaseModel):
    """task_form_id and handler ride along from the same GET /tasks response
    this is acting on -- same "the caller already has everything, no extra
    lookup" convention router.py's "start" postback data encoding uses.
    """

    task_form_id: UUID
    handler: str


class StartTaskResponse(BaseModel):
    pushed: bool


@router.post("/tasks/{task_id}/start")
async def start_task(
    task_id: UUID,
    body: StartTaskRequest,
    liff_user: LiffUserDep,
    session: SessionDep,
) -> StartTaskResponse:
    """One-tap "open": resumes an existing conversation for this task, or
    starts a fresh one -- exactly router.py's _handle_postback "start"
    branch, ported to push instead of reply since there's no reply_token
    from a plain HTTP call. Mirrors that branch's structure closely on
    purpose, to keep the two easy to compare when one changes.

    Doesn't handle the autofill "yes/no" follow-up itself -- once
    push_autofill_offer is sent, the farmer answers by tapping its Postback
    button in the chat, which router.py's existing "start_autofill" branch
    already handles via its own fresh reply_token. Only the offer itself
    needed a push twin here.
    """
    user_id = liff_user.user_id
    to = liff_user.line_user_id

    existing = await service.find_resumable_conversation(session, user_id=user_id, task_id=task_id)
    if existing is not None:
        form = await get_form(str(existing.task_form_id))
        reply = await service.resume_conversation(session, conversation=existing, form=form)
        await _push(to, reply)
        return StartTaskResponse(pushed=True)

    form = await get_form(str(body.task_form_id))
    parent_kind = parent_picker.kind_for_handler(body.handler)
    parent_choices = None
    if parent_kind is not None:
        parent_choices = await parent_picker.choices_for(session, parent_kind, user_id)
        if not parent_choices:
            await push_text(to, parent_picker.EMPTY_PROMPT[parent_kind])
            return StartTaskResponse(pushed=True)
    else:
        try:
            last_answer = await fetch_last_answer(user_id=str(user_id), handler=body.handler)
        except UpstreamServiceError:
            logger.warning(
                "fetch_last_answer failed for handler=%s on LIFF start -- starting fresh, no offer",
                body.handler,
                exc_info=True,
            )
            last_answer = None
        if last_answer is not None:
            preview = ""
            try:
                offer_questions = service.questions_from_form(form)
                sanitized_preview = await reuse.sanitize_for_autofill(last_answer, offer_questions)
                preview = reuse.format_autofill_preview(sanitized_preview, offer_questions)
            except UpstreamServiceError:
                logger.warning(
                    "sanitize_for_autofill failed for handler=%s while building the LIFF "
                    "offer preview -- showing the offer without one",
                    body.handler,
                    exc_info=True,
                )
            await push_autofill_offer(
                to,
                task_id=str(task_id),
                task_form_id=str(body.task_form_id),
                handler=body.handler,
                preview=preview,
            )
            return StartTaskResponse(pushed=True)

    reply = await service.start_conversation(
        session,
        user_id=user_id,
        task_id=task_id,
        task_form_id=body.task_form_id,
        form=form,
        parent_kind=parent_kind,
        parent_choices=parent_choices,
    )
    await _push(to, reply)
    return StartTaskResponse(pushed=True)


async def _push(to: str, reply: service.ConversationReply) -> None:
    """Push equivalent of router.py's _reply -- same three-way branch
    (AWAITING_CONFIRMATION / has choices / plain text), just push_* instead
    of reply_*. See _reply's own docstring for why each branch exists.
    """
    if reply.substate == ActiveSubstate.AWAITING_CONFIRMATION:
        await push_confirm_prompt(to, reply.text, reply.conversation_id)
        return

    if not reply.choices:
        await push_text(to, reply.text)
        return

    quick_reply = [QuickReplyOption(label=c.label[:20], text=c.label) for c in reply.choices[:13]]
    await push_text(to, reply.text, quick_reply=quick_reply)
