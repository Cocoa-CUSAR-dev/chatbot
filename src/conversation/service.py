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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.conversation.constants import ActiveSubstate, AnswerSource, ConversationStatus
from src.conversation.exceptions import ConversationNotFound
from src.conversation.models import Conversation, ConversationAnswer
from src.conversation.state_machine import on_guided_answer
from src.conversation.validation import validate_answer
from src.forms.schemas import FormDetail
from src.line import parent_picker
from src.tasks.client import submit_task
from src.tasks.exceptions import HandlerNotSupported
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

# Offered only on non-mandatory questions, always first when real choices
# exist too -- elderly farmers are the primary users here, so "this one's
# optional" needs to be visually obvious, not just implied by phrasing.
# LINE Quick Reply buttons can't be recolored (all MessageAction pills share
# one style), so an icon prefix is the actual lever available -- id "__skip__"
# is a sentinel, never a real domain value.
_SKIP_CHOICE_ID = "__skip__"
_SKIP_CHOICE = Choice(id=_SKIP_CHOICE_ID, label="⏭️ ข้าม")

# LINE's own hard cap on Quick Reply buttons per message -- some OPTION
# questions already carry this many real choices, so prepending skip must
# make room for it rather than pushing the last real choice off the list.
_QUICK_REPLY_LIMIT = 13


@dataclass(frozen=True)
class Question:
    question_id: UUID
    label: str
    field_name: str
    input_type: str
    is_mandatory: bool
    sort_order: int
    choices: list[Choice] | None = None
    # True only for real constrained choices (OPTION/BOOLEAN) -- distinct
    # from "has a skip button but is otherwise free text", which must still
    # accept arbitrary typed text rather than re-asking on a non-match.
    has_constrained_choices: bool = False
    # From Kotlin's form.field_validation_rule, already joined onto this
    # question server-side (FormRepository.kt) -- null for OPTION/BOOLEAN/
    # upload (constrained another way already) or any field_name with no
    # rule row. See validation.py's own docstring for the key-casing wrinkle.
    validation_rule: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConversationReply:
    conversation_id: UUID
    substate: ActiveSubstate
    text: str
    choices: list[Choice] | None = None
    # True only when confirm_conversation's own submit_task call failed --
    # lets the caller re-attach the confirm button for a retry instead of
    # treating this as the terminal "saved" message.
    submission_failed: bool = False
    # Debug-only passthrough of the open question's own shape -- None
    # whenever this reply isn't "here's a fixed question to answer" (e.g.
    # confirmation/completed/cancelled replies have no single open question).
    # Not used by the real LINE webhook path; exists so the dev test UI can
    # show a developer what's actually being validated without guessing.
    input_type: str | None = None
    validation_rule: dict[str, Any] | None = None


def _constrained_choices_for(q: dict[str, Any]) -> list[Choice] | None:
    if q.get("choices"):
        return [Choice(id=str(c["id"]), label=str(c.get("name") or "")) for c in q["choices"]]
    if q.get("input_type") == "BOOLEAN":
        return _BOOLEAN_CHOICES
    return None


def _choices_for(q: dict[str, Any], is_mandatory: bool) -> tuple[list[Choice] | None, bool]:
    """Returns (choices to offer, whether they're a real constrained set).

    Mandatory questions are unchanged -- exactly the underlying OPTION/
    BOOLEAN choices, or None for free text. Non-mandatory questions always
    get a skip button prepended (first, even ahead of real choices); a
    non-mandatory free-text question gets a skip-only Quick Reply, but typing
    real text is still accepted (has_constrained_choices stays False).
    """
    constrained = _constrained_choices_for(q)
    if is_mandatory:
        return constrained, constrained is not None
    if constrained is not None:
        room_for_real_choices = _QUICK_REPLY_LIMIT - 1
        return [_SKIP_CHOICE, *constrained[:room_for_real_choices]], True
    return [_SKIP_CHOICE], False


# Only these are actually wired up in the chat flow today -- DATE, DATETIME,
# GEODATA, FLOAT, and INT are explicitly deferred (next sprints / LLM), and
# "upload" is a VARCHAR field_name convention (see form.question seed data)
# for photo attachments, which have nowhere to go yet either. Filtered out
# at the form level (not just skipped when picking the next question) so
# they never show up in the guided flow OR the confirmation summary.
_SUPPORTED_INPUT_TYPES = {"VARCHAR", "OPTION", "BOOLEAN"}


def _is_supported(q: dict[str, Any]) -> bool:
    if q.get("input_type") not in _SUPPORTED_INPUT_TYPES:
        return False
    return not (q.get("input_type") == "VARCHAR" and q.get("field_name") == "upload")


def _question_from_dict(q: dict[str, Any]) -> Question:
    is_mandatory = bool(q.get("is_mandatory", False))
    choices, has_constrained_choices = _choices_for(q, is_mandatory)
    return Question(
        question_id=UUID(str(q["question_id"])),
        label=str(q.get("label") or ""),
        field_name=str(q.get("field_name") or ""),
        input_type=str(q.get("input_type") or ""),
        is_mandatory=is_mandatory,
        sort_order=int(q.get("sort_order", 0)),
        choices=choices,
        has_constrained_choices=has_constrained_choices,
        validation_rule=q.get("validation_rule"),
    )


def questions_from_form(form: FormDetail) -> list[Question]:
    """Flattens a FormDetail's sections into a sort_order-ordered list."""
    questions = [
        _question_from_dict(q)
        for section in form.sections
        for q in section.get("questions", [])
        if _is_supported(q)
    ]
    return sorted(questions, key=lambda q: q.sort_order)


def _next_unanswered_required(questions: list[Question], answered: set[UUID]) -> Question | None:
    # Deliberately ignores is_mandatory -- every question gets asked, not
    # just required ones. Started as a workaround for the dev DB's
    # inconsistent is_mandatory data, but is now load-bearing: a
    # non-mandatory question only ever reaches the farmer (with its skip
    # button, see _choices_for) if this function selects it as "next".
    # Filtering on is_mandatory here would make skip buttons unreachable.
    for question in questions:
        if question.question_id not in answered:
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
    # Skipped fields were saved as nothing -- leave them out of the review
    # entirely rather than showing a confusing "skipped" line.
    answered = [a for a in answers if not a.answer.get("skipped")]
    ordered = sorted(answered, key=lambda a: sort_order_by_id.get(a.question_id, 0))
    # Always the human-readable text, even for resolved OPTION answers --
    # farmers review labels ("พ่นยา"), not the underlying UUID.
    lines = [f"- {label_by_id.get(a.question_id, '?')}: {a.answer.get('text')}" for a in ordered]
    return "สรุปคำตอบของคุณ:\n" + "\n".join(lines) + "\n\nยืนยันการส่งข้อมูลหรือไม่?"


def _reply_for_question(
    conversation_id: UUID, question: Question, *, error: str | None = None
) -> ConversationReply:
    text = f"{error}\n\n{question.label}" if error else question.label
    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=text,
        choices=question.choices,
        input_type=question.input_type,
        validation_rule=question.validation_rule,
    )


async def start_conversation(
    session: AsyncSession,
    *,
    user_id: UUID,
    task_id: UUID,
    task_form_id: UUID,
    form: FormDetail,
    parent_kind: str | None = None,
    parent_choices: list[parent_picker.ParentOption] | None = None,
) -> ConversationReply:
    # parent_kind set means this handler is one of the 5 that need a parent
    # row picked first (router.py already resolved parent_choices and
    # confirmed it's non-empty before calling this -- see
    # src/line/parent_picker.py). The conversation starts with NO open
    # question (current_question_id has a real FK to form.question -- there
    # is no such row for this synthetic step, so it can only ever be NULL or
    # a real question here, never a made-up placeholder). Which picker is
    # pending instead lives in parent_answer as {"pending_kind": ...};
    # handle_answer checks that before its normal current_question_id
    # handling and routes to _handle_parent_answer.
    if parent_kind is not None:
        assert parent_choices, "router.py must not call start_conversation with empty choices"
        conversation = Conversation(
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            status=ConversationStatus.ACTIVE,
            current_question_id=None,
            parent_answer={"pending_kind": parent_kind},
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
            text=parent_picker.PROMPT[parent_kind],
            choices=[Choice(id=o.id, label=o.label) for o in parent_choices],
        )

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


async def _handle_parent_answer(
    session: AsyncSession,
    *,
    conversation: Conversation,
    raw_text: str,
    parent_kind: str,
    form: FormDetail,
) -> ConversationReply:
    """Resolves an answer to the synthetic parent-picker step (see
    conversation.parent_answer's "pending_kind" shape in start_conversation).
    Recomputes choices fresh rather than trusting whatever was offered when
    the question was asked -- cheap (an indexed, capped-at-13 query) and
    avoids acting on a stale list if the farmer logged something new in
    between.

    On a match: stores the pick on conversation.parent_answer (NOT as a
    ConversationAnswer row -- there's no real form.question backing this
    step, and that table's question_id has a real FK, see
    src/conversation/models.py) and advances to the form's actual first
    question, exactly like start_conversation's non-parent path.
    """
    choices = await parent_picker.choices_for(session, parent_kind, conversation.user_id)
    matched = next((o for o in choices if o.label == raw_text), None)
    if matched is None:
        # Doesn't match any listed choice -- re-ask with a fresh list,
        # same "don't store what can't resolve to a real value" rule
        # handle_answer's own OPTION-question path already follows.
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
            text=parent_picker.PROMPT[parent_kind],
            choices=[Choice(id=o.id, label=o.label) for o in choices],
        )

    conversation.parent_answer = {
        "field_name": parent_picker.FIELD_NAME[parent_kind],
        "value": matched.id,
    }

    questions = questions_from_form(form)
    first_question = _next_unanswered_required(questions, answered=set())
    conversation.current_question_id = first_question.question_id if first_question else None
    await session.commit()

    if first_question is None:
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
) -> ConversationReply | None:
    # Locks the conversation row for the rest of this function -- serializes
    # concurrent webhook deliveries for the same conversation (LINE's own
    # retry-on-slow-response, or a farmer double-tapping/double-texting can
    # otherwise both read the same current_question_id and both write an
    # answer for it before either commits; live-caught 2026-08-09 during the
    # chatbot pathway audit). NOWAIT means a message that arrives while
    # another is still being processed for this conversation is dropped
    # immediately rather than queued -- first message wins, full stop.
    # Reconciling two rapid, genuinely different answers (e.g. a farmer
    # correcting a typo a second later) is deferred to the future LLM-driven
    # flow, which can actually decide what the farmer meant; this guided
    # flow doesn't try.
    try:
        conversation = (
            await session.execute(
                select(Conversation)
                .where(Conversation.conversation_id == conversation_id)
                .with_for_update(nowait=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        # 55P03 = lock_not_available (Postgres SQLSTATE) -- checked on the
        # code rather than the wrapped exception type, since SQLAlchemy's
        # asyncpg dialect re-wraps the driver error (AsyncAdapt_asyncpg_dbapi
        # .Error, not asyncpg's own LockNotAvailableError) but still proxies
        # .sqlstate through from the original.
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        logger.info(
            "conversation_id=%s already has an answer in flight -- dropping this message",
            conversation_id,
        )
        return None
    if conversation is None:
        raise ConversationNotFound()

    # Checked before the current_question_id==None check below: the picker
    # step deliberately leaves current_question_id NULL (see
    # start_conversation), so without this, a pending pick would be
    # misread as "no open question" instead of routed to the picker.
    pending_kind = (conversation.parent_answer or {}).get("pending_kind")
    if pending_kind is not None:
        return await _handle_parent_answer(
            session,
            conversation=conversation,
            raw_text=raw_text,
            parent_kind=pending_kind,
            form=form,
        )

    if conversation.current_question_id is None:
        raise ConversationNotFound("Conversation has no open question to answer")

    questions = questions_from_form(form)
    question_by_id = {q.question_id: q for q in questions}
    current_question = question_by_id.get(conversation.current_question_id)

    resolved_value: str | None = None
    is_skip = False
    if current_question is not None and current_question.choices:
        matched = next((c for c in current_question.choices if c.label == raw_text), None)
        if matched is not None and matched.id == _SKIP_CHOICE_ID:
            is_skip = True
        elif current_question.has_constrained_choices:
            if matched is None:
                # Doesn't match any listed choice -- re-ask rather than store
                # text that can't resolve to a real domain value later. Keeps
                # the same question open, same choices offered again.
                return _reply_for_question(
                    conversation_id,
                    current_question,
                    error="กรุณาเลือกคำตอบจากตัวเลือกที่กำหนดเท่านั้น",
                )
            resolved_value = matched.id
        # else: non-mandatory free-text question offering only a skip button
        # -- typed text that isn't the skip label is a real answer, not a
        # mismatch, so it falls through to the normal free-text path below.

    # Format validation (the Validate Answer step) only applies to genuine
    # free text -- a skip is an intentional absence (nothing to check), and
    # a resolved OPTION/BOOLEAN choice is already constrained to a
    # known-good value by construction (matched against current_question's
    # own choices above).
    if not is_skip and resolved_value is None and current_question is not None:
        error = validate_answer(current_question.validation_rule, raw_text)
        if error is not None:
            return _reply_for_question(conversation_id, current_question, error=error)

    if is_skip:
        answer: dict[str, Any] = {"skipped": True}
    else:
        answer = {"text": raw_text}
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
    # Skipped fields are saved as nothing -- omitted from the payload
    # entirely rather than sending an empty/null value for that column.
    answer_payload: dict[str, Any] = {
        field_name_by_question_id.get(row.question_id, str(row.question_id)): (
            row.answer.get("value") or row.answer.get("text")
        )
        for row in answer_rows
        if not row.answer.get("skipped")
    }
    if conversation.parent_answer is not None and "field_name" in conversation.parent_answer:
        parent_field_name = conversation.parent_answer["field_name"]
        answer_payload[parent_field_name] = conversation.parent_answer["value"]

    try:
        await submit_task(
            TaskSubmission(
                user_id=str(conversation.user_id),
                task_id=str(conversation.task_id),
                answer=answer_payload,
            )
        )
    except HandlerNotSupported:
        # A permanent failure, not a transient one -- Go is correctly saying
        # "not built yet" (see docs/plans/chatbot-child-handler-design.md),
        # not reporting a bug or outage. Retrying this exact submission will
        # never succeed, so the generic "ลองใหม่อีกครั้ง" (try again) message
        # below would be actively misleading here -- tell the farmer the
        # real reason instead. Still leaves the conversation awaiting
        # confirmation (not COMPLETED) so the farmer's own cancel button
        # works normally; this is a case not-yet-implemented, the pre-flight
        # check in src/line/router.py should catch known cases earlier than
        # this for the 5 handlers it knows about, but this is the honest
        # fallback for any handler Go itself reports unsupported.
        logger.info(
            "submit_task failed for conversation_id=%s -- handler not supported yet, "
            "telling the farmer honestly instead of suggesting a retry",
            conversation_id,
        )
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="ขออภัย แบบฟอร์มประเภทนี้ยังไม่รองรับการบันทึกอัตโนมัติ ทีมงานกำลังพัฒนาอยู่ คำตอบของคุณจะไม่ถูกบันทึก",
            submission_failed=True,
        )
    except Exception:
        # Go's dissection logic is real now (not a stub -- see tasks/client.py's
        # docstring), so a failure here means an actual problem worth reading
        # the exception message for (bad service key, no matching
        # chat.conversation, etc.) -- not an expected gap.
        #
        # Deliberately NOT marking the conversation COMPLETED here, and NOT
        # telling the farmer it saved -- it didn't. A prior version of this
        # code swallowed the failure and reported success anyway, which meant
        # total, undetectable data loss (confirmed live during the 2026-08-09
        # pathway audit: Go rejected the write, zero rows landed anywhere, and
        # the farmer was told "saved"). The conversation stays exactly where
        # it was (still awaiting confirmation) so tapping the confirm button
        # again just retries this same submit_task call.
        logger.exception(
            "submit_task failed for conversation_id=%s -- chatbot-side answers are still "
            "saved, but nothing was written to Go/the domain table; conversation left "
            "awaiting confirmation for retry",
            conversation_id,
        )
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="เกิดข้อผิดพลาด ไม่สามารถบันทึกข้อมูลได้ กรุณาลองใหม่อีกครั้ง",
            submission_failed=True,
        )

    conversation.status = ConversationStatus.COMPLETED
    await session.commit()

    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.AWAITING_CONFIRMATION,
        text="บันทึกข้อมูลเรียบร้อยแล้ว ขอบคุณครับ",
    )


async def cancel_conversation(session: AsyncSession, *, conversation_id: UUID) -> ConversationReply:
    """The escape hatch confirm/retry didn't have -- added after a farmer got
    stuck retrying a submission that could never succeed (an unsupported
    handler, CB-1's honest-failure message correctly refusing to lie about
    it, but with no way out other than retrying forever). Nothing to submit
    here -- just marks the conversation CANCELLED so `เริ่ม` can start a
    fresh one for the same task (the active-conversation lookup only
    matches status == ACTIVE).
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFound()

    conversation.status = ConversationStatus.CANCELLED
    conversation.current_question_id = None
    await session.commit()

    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.AWAITING_CONFIRMATION,
        text="ยกเลิกแล้ว ไม่ได้บันทึกข้อมูล",
    )
