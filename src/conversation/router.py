"""Dev-only test surface for the GuidedFlow engine -- lets a developer drive
src.conversation.service directly, without LINE or a real LLM (Sprint 4).

Just request/response wiring -- all direct-database reads live in
dev_queries.py (see that file's docstring for why they're dev-only).

Not registered when ENVIRONMENT.is_deployed -- see src/main.py.
"""

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.conversation import dev_queries, service
from src.conversation.schemas import (
    ConfirmRequest,
    ConversationReplyResponse,
    FormSummary,
    MessageRequest,
    StartConversationRequest,
)
from src.database import get_session

router = APIRouter(prefix="/conversation/test", tags=["conversation-test"])

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "chat_test.html"


@router.get("/forms", response_model=list[FormSummary])
async def list_mock_forms(session: AsyncSession = Depends(get_session)) -> list[FormSummary]:
    return await dev_queries.list_testable_forms(session)


@router.post("/start", response_model=ConversationReplyResponse)
async def start(
    body: StartConversationRequest, session: AsyncSession = Depends(get_session)
) -> ConversationReplyResponse:
    user_id = await dev_queries.resolve_test_user_id(session)
    form = await dev_queries.load_form_detail(session, body.task_form_id)
    reply = await service.start_conversation(
        session,
        user_id=user_id,
        task_id=body.task_id,
        task_form_id=body.task_form_id,
        form=form,
    )
    return ConversationReplyResponse(
        conversation_id=reply.conversation_id, substate=reply.substate.value, text=reply.text
    )


@router.post("/message", response_model=ConversationReplyResponse)
async def message(
    body: MessageRequest, session: AsyncSession = Depends(get_session)
) -> ConversationReplyResponse:
    form = await dev_queries.load_form_detail(session, body.task_form_id)
    reply = await service.handle_answer(
        session, conversation_id=body.conversation_id, raw_text=body.text, form=form
    )
    return ConversationReplyResponse(
        conversation_id=reply.conversation_id, substate=reply.substate.value, text=reply.text
    )


@router.post("/confirm", response_model=ConversationReplyResponse)
async def confirm(
    body: ConfirmRequest, session: AsyncSession = Depends(get_session)
) -> ConversationReplyResponse:
    form = await dev_queries.load_form_detail(session, body.task_form_id)
    reply = await service.confirm_conversation(
        session, conversation_id=body.conversation_id, form=form
    )
    return ConversationReplyResponse(
        conversation_id=reply.conversation_id, substate=reply.substate.value, text=reply.text
    )


@router.get("/ui", response_class=HTMLResponse)
async def ui() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")
