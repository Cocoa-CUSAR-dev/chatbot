"""Orchestrates a conversation's GuidedFlow lifecycle -- Sprint 1 scope only,
no LLM (see target-architecture.md #4). Pure decision logic lives in
state_machine.py; this module is the I/O layer around it: loads/persists
Conversation + ConversationAnswer rows and decides what to say back.

Deliberately takes the form's question script (`FormDetail`) as a parameter
rather than fetching it itself -- lets the same logic run against either
`forms.client.get_form()` (real Kotlin call, production) or a direct DB query
(this sprint's test router, see src/conversation/router.py) without this
module knowing or caring which. Confirmed against Kotlin's actual DTOs
(web-backend's FormRepository/Question.kt): each question dict carries
question_id/label/field_name/is_mandatory/sort_order, and OPTION-type
questions additionally carry choices: [{id, name}].

OPTION-question answers: a farmer picks by the choice's label (matches a
LINE Quick Reply MessageAction, which sends its own label back as the
message text -- see src/line/service.py's QuickReplyOption). handle_answer
resolves that label against the open question's own choice list and stores
the resolved id as answer["value"] (falling back to answer["text"] alone for
non-OPTION questions, which have no choices to resolve against). If the
farmer's text doesn't match any listed choice, the question is re-asked
rather than silently accepting text that can't be stored as a real domain
value -- this is exactly what caused a real failed submission before choices
existed here (see this task's own history: guided-flow text answers for
OPTION questions failed with `invalid input syntax for type uuid`).
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
class Choice:
    id: str
    label: str


# BOOLEAN questions target a real `boolean` column (e.g.
# agriculture.farm_pest_disease_record.is_quality_damage) -- free text like
# "ไม่"/"aaa" fails with `invalid input syntax for type boolean`, the exact
# same failure OPTION questions had before choices existed. Kotlin never
# sends a `choices` list for BOOLEAN (its own filter is INPUT_TYPE ==
# OPTION only), so these two are synthesized here instead of looked up --
# same choices/resolution/re-ask mechanism either way, just a fixed pair
# instead of a database-backed list.
_BOOLEAN_CHOICES = [Choice(id="true", label="ใช่"), Choice(id="false", label="ไม่")]


@dataclass(frozen=True)
class Question:
    question_id: UUID
    label: str
    field_name: str
    is_mandatory: bool
    sort_order: int
    choices: list[Choice] | None = None


@dataclass(frozen=True)
class ConversationReply:
    conversation_id: UUID
    substate: ActiveSubstate
    text: str
    choices: list[Choice] | None = None


def _choices_for(q: dict[str, Any]) -> list[Choice] | None:
    if q.get("choices"):
        return [Choice(id=str(c["id"]), label=str(c.get("name") or "")) for c in q["choices"]]
    if q.get("input_type") == "BOOLEAN":
        return _BOOLEAN_CHOICES
    return None


def questions_from_form(form: FormDetail) -> list[Question]:
    """Flattens a FormDetail's sections into a sort_order-ordered list."""
    questions = [
        Question(
            question_id=UUID(str(q["question_id"])),
            label=str(q.get("label") or ""),
            field_name=str(q.get("field_name") or ""),
            is_mandatory=bool(q.get("is_mandatory", False)),
            sort_order=int(q.get("sort_order", 0)),
            choices=_choices_for(q),
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


async def _answered_rows(session: AsyncSession, conversation_id: UUID) -> list[ConversationAnswer]:
    result = await session.execute(
        select(ConversationAnswer).where(ConversationAnswer.conversation_id == conversation_id)
    )
    return list(result.scalars().all())


def _format_confirmation_summary(
    questions: list[Question], answers: list[ConversationAnswer]
) -> str:
    label_by_id = {q.question_id: q.label for q in questions}
    sort_order_by_id = {q.question_id: q.sort_order for q in questions}
    ordered = sorted(answers, key=lambda a: sort_order_by_id.get(a.question_id, 0))
    # Always the human-readable text, even for resolved OPTION answers --
    # farmers review labels ("พ่นยา"), not the underlying UUID.
    lines = [f"- {label_by_id.get(a.question_id, '?')}: {a.answer.get('text')}" for a in ordered]
    return "สรุปคำตอบของคุณ:\n" + "\n".join(lines) + "\n\nยืนยันการส่งข้อมูลหรือไม่?"


def _reply_for_question(conversation_id: UUID, question: Question) -> ConversationReply:
    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=question.label,
        choices=question.choices,
    )


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

    return _reply_for_question(conversation.conversation_id, first_question)


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

    questions = questions_from_form(form)
    question_by_id = {q.question_id: q for q in questions}
    current_question = question_by_id.get(conversation.current_question_id)

    resolved_value: str | None = None
    if current_question is not None and current_question.choices:
        matched = next((c for c in current_question.choices if c.label == raw_text), None)
        if matched is None:
            # Doesn't match any listed choice -- re-ask rather than store
            # text that can't resolve to a real domain value later. Keeps
            # the same question open, same choices offered again.
            return _reply_for_question(conversation_id, current_question)
        resolved_value = matched.id

    answer: dict[str, Any] = {"text": raw_text}
    if resolved_value is not None:
        answer["value"] = resolved_value

    session.add(
        ConversationAnswer(
            conversation_id=conversation_id,
            question_id=conversation.current_question_id,
            answer=answer,
            source=AnswerSource.GUIDED_FLOW,
        )
    )
    await session.flush()

    answer_rows = await _answered_rows(session, conversation_id)
    answered = {row.question_id for row in answer_rows}
    transition = on_guided_answer(all_slots_filled=_all_required_answered(questions, answered))

    if transition.next_state == ActiveSubstate.AWAITING_CONFIRMATION:
        conversation.current_question_id = None
        await session.commit()
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text=_format_confirmation_summary(questions, answer_rows),
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
    return _reply_for_question(conversation_id, next_question)


async def confirm_conversation(
    session: AsyncSession, *, conversation_id: UUID, form: FormDetail
) -> ConversationReply:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFound()

    field_name_by_question_id = {q.question_id: q.field_name for q in questions_from_form(form)}
    answer_rows = await _answered_rows(session, conversation_id)
    answer_payload: dict[str, Any] = {
        field_name_by_question_id.get(row.question_id, str(row.question_id)): (
            row.answer.get("value") or row.answer.get("text")
        )
        for row in answer_rows
    }

    try:
        await submit_task(
            TaskSubmission(
                user_id=str(conversation.user_id),
                task_id=str(conversation.task_id),
                answer=answer_payload,
            )
        )
    except Exception:
        # Go's dissection logic is real now (not a stub -- see tasks/client.py's
        # docstring), so a failure here means an actual problem worth reading
        # the exception message for (bad service key, no matching
        # chat.conversation, unsupported handler, etc.) -- not an expected gap.
        # Still swallowed rather than raised: the chatbot's own side (this
        # conversation, its answers) is already durably saved regardless of
        # whether Go's write succeeded, so there's nothing to roll back.
        logger.exception(
            "submit_task failed for conversation_id=%s -- chatbot-side data is still saved",
            conversation_id,
        )

    conversation.status = ConversationStatus.COMPLETED
    await session.commit()

    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.AWAITING_CONFIRMATION,
        text="บันทึกข้อมูลเรียบร้อยแล้ว ขอบคุณครับ",
    )
