"""Dev-only test surface for the GuidedFlow engine -- lets a developer drive
src.conversation.service directly, without LINE or a real LLM (Sprint 4).

Just request/response wiring -- all direct-database reads live in
dev_queries.py (see that file's docstring for why they're dev-only).

Not registered when ENVIRONMENT.is_deployed -- see src/main.py.
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.conversation import dev_queries, service
from src.conversation.exceptions import AnswerInFlight
from src.conversation.schemas import (
    CancelRequest,
    ChoiceResponse,
    ConfirmRequest,
    ConversationReplyResponse,
    FormSummary,
    MessageRequest,
    StartConversationRequest,
)
from src.database import get_session

router = APIRouter(prefix="/conversation/test", tags=["conversation-test"])

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "chat_test.html"

# Annotated[..., Depends(...)] instead of `= Depends(...)` -- ruff's B008
# (flake8-bugbear) flags a function call in an argument default; this is
# also the FastAPI-recommended form for a reusable dependency type.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _to_response(reply: service.ConversationReply) -> ConversationReplyResponse:
    return ConversationReplyResponse(
        conversation_id=reply.conversation_id,
        substate=reply.substate.value,
        text=reply.text,
        choices=(
            [ChoiceResponse(id=c.id, label=c.label) for c in reply.choices]
            if reply.choices
            else None
        ),
    )


@router.get("/forms", response_model=list[FormSummary])
async def list_mock_forms(session: SessionDep) -> list[FormSummary]:
    return await dev_queries.list_testable_forms(session)


@router.post("/start", response_model=ConversationReplyResponse)
async def start(body: StartConversationRequest, session: SessionDep) -> ConversationReplyResponse:
    user_id = await dev_queries.resolve_test_user_id(session)
    form = await dev_queries.load_form_detail(session, body.task_form_id)
    reply = await service.start_conversation(
        session,
        user_id=user_id,
        task_id=body.task_id,
        task_form_id=body.task_form_id,
        form=form,
    )
    return _to_response(reply)


@router.post("/message", response_model=ConversationReplyResponse)
async def message(body: MessageRequest, session: SessionDep) -> ConversationReplyResponse:
    form = await dev_queries.load_form_detail(session, body.task_form_id)
    reply = await service.handle_answer(
        session, conversation_id=body.conversation_id, raw_text=body.text, form=form
    )
    if reply is None:
        raise AnswerInFlight()
    return _to_response(reply)


@router.post("/confirm", response_model=ConversationReplyResponse)
async def confirm(body: ConfirmRequest, session: SessionDep) -> ConversationReplyResponse:
    form = await dev_queries.load_form_detail(session, body.task_form_id)
    reply = await service.confirm_conversation(
        session, conversation_id=body.conversation_id, form=form
    )
    return _to_response(reply)


@router.post("/cancel", response_model=ConversationReplyResponse)
async def cancel(body: CancelRequest, session: SessionDep) -> ConversationReplyResponse:
    reply = await service.cancel_conversation(session, conversation_id=body.conversation_id)
    return _to_response(reply)


@router.get("/ui", response_class=HTMLResponse)
async def ui() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")
