"""Orchestrates a conversation's GuidedFlow lifecycle -- Sprint 1 scope only,
no LLM (see target-architecture.md #4). Pure decision logic lives in
state_machine.py; this module is the I/O layer around it: loads/persists
Conversation + ConversationAnswer rows and decides what to say back.

Deliberately takes the form's question script (`FormDetail`) as a parameter
rather than fetching it itself -- lets the same logic run against either
`forms.client.get_form()` (real Kotlin call, production) or a direct DB query
(this sprint's test router, see src/conversation/router.py) without this
module knowing or caring which. See forms/schemas.py's own docstring: the
real Kotlin response shape isn't pinned down yet, so the `sections` parsing
below (question_id/label/field_name/is_mandatory/sort_order per question)
needs confirming against a real response before it's trusted end-to-end.
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.conversation.constants import ActiveSubstate, AnswerSource, ConversationStatus
from src.conversation.exceptions import ConversationNotFound
from src.conversation.models import Conversation, ConversationAnswer
from src.conversation.state_machine import on_guided_answer
from src.forms.schemas import FormDetail
from src.tasks.client import submit_task
from src.tasks.schemas import TaskSubmission

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Question:
    question_id: UUID
    label: str
    field_name: str
    is_mandatory: bool
    sort_order: int


@dataclass(frozen=True)
class ConversationReply:
    conversation_id: UUID
    substate: ActiveSubstate
    text: str


def questions_from_form(form: FormDetail) -> list[Question]:
    """Flattens a FormDetail's sections into a sort_order-ordered list."""
    questions = [
        Question(
            question_id=UUID(str(q["question_id"])),
            label=str(q.get("label") or ""),
            field_name=str(q.get("field_name") or ""),
            is_mandatory=bool(q.get("is_mandatory", False)),
            sort_order=int(q.get("sort_order", 0)),
        )
        for section in form.sections
        for q in section.get("questions", [])
    ]
    return sorted(questions, key=lambda q: q.sort_order)


def _next_unanswered_required(questions: list[Question], answered: set[UUID]) -> Question | None:
    for question in questions:
        if question.is_mandatory and question.question_id not in answered:
            return question
    return None


def _all_required_answered(questions: list[Question], answered: set[UUID]) -> bool:
    return _next_unanswered_required(questions, answered) is None


async def _answered_question_ids(session: AsyncSession, conversation_id: UUID) -> set[UUID]:
    result = await session.execute(
        select(ConversationAnswer.question_id).where(
            ConversationAnswer.conversation_id == conversation_id
        )
    )
    return set(result.scalars().all())


async def start_conversation(
    session: AsyncSession,
    *,
    user_id: UUID,
    task_id: UUID,
    task_form_id: UUID,
    form: FormDetail,
) -> ConversationReply:
    questions = questions_from_form(form)
    first_question = _next_unanswered_required(questions, answered=set())

    conversation = Conversation(
        user_id=user_id,
        task_id=task_id,
        task_form_id=task_form_id,
        status=ConversationStatus.ACTIVE,
        current_question_id=first_question.question_id if first_question else None,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)

    if first_question is None:
        # No mandatory questions on this form -- nothing to ask, straight to
        # confirmation. A real shape in this sprint's mock data (every
        # processing_record question is is_mandatory=false -- see
        # database/seed/mock_forms.sql's note), not a bug in this engine.
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="ไม่มีคำถามที่จำเป็นต้องตอบ ยืนยันการส่งข้อมูลหรือไม่?",
        )

    return ConversationReply(
        conversation_id=conversation.conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=first_question.label,
    )


async def handle_answer(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    raw_text: str,
    form: FormDetail,
) -> ConversationReply:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFound()
    if conversation.current_question_id is None:
        raise ConversationNotFound("Conversation has no open question to answer")

    session.add(
        ConversationAnswer(
            conversation_id=conversation_id,
            question_id=conversation.current_question_id,
            answer={"text": raw_text},
            source=AnswerSource.GUIDED_FLOW,
        )
    )
    await session.flush()

    questions = questions_from_form(form)
    answered = await _answered_question_ids(session, conversation_id)
    transition = on_guided_answer(all_slots_filled=_all_required_answered(questions, answered))

    if transition.next_state == ActiveSubstate.AWAITING_CONFIRMATION:
        conversation.current_question_id = None
        await session.commit()
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="ได้รับข้อมูลครบแล้ว ยืนยันการส่งข้อมูลหรือไม่?",
        )

    next_question = _next_unanswered_required(questions, answered)
    if next_question is None:
        # on_guided_answer's contract (constants.py) says this can't happen --
        # "slots remain" and "no unanswered required question found" are
        # contradictory. Fail loudly rather than silently going quiet on the
        # farmer.
        raise RuntimeError(
            f"on_guided_answer reported slots remaining for conversation_id={conversation_id} "
            "but no unanswered mandatory question was found"
        )

    conversation.current_question_id = next_question.question_id
    await session.commit()
    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=next_question.label,
    )


async def confirm_conversation(
    session: AsyncSession, *, conversation_id: UUID, form: FormDetail
) -> ConversationReply:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFound()

    field_name_by_question_id = {q.question_id: q.field_name for q in questions_from_form(form)}
    result = await session.execute(
        select(ConversationAnswer).where(ConversationAnswer.conversation_id == conversation_id)
    )
    answer_payload: dict[str, Any] = {
        field_name_by_question_id.get(row.question_id, str(row.question_id)): row.answer.get(
            "text"
        )
        for row in result.scalars().all()
    }

    try:
        await submit_task(TaskSubmission(task_id=str(conversation.task_id), answer=answer_payload))
    except Exception:
        logger.exception(
            "submit_task failed for conversation_id=%s -- note Go's SubmitTask "
            "dissection logic is still a stub (ADR 0001), so even a 200 here "
            "would not create a real domain row yet",
            conversation_id,
        )

    conversation.status = ConversationStatus.COMPLETED
    await session.commit()

    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.AWAITING_CONFIRMATION,
        text="บันทึกข้อมูลเรียบร้อยแล้ว ขอบคุณครับ",
    )
