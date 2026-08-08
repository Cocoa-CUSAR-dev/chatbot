import uuid
from unittest.mock import AsyncMock, MagicMock

from src.conversation import service
from src.conversation.constants import ActiveSubstate, ConversationStatus
from src.conversation.models import Conversation
from src.forms.schemas import FormDetail


def _form(*, mandatory_flags: list[bool]) -> tuple[FormDetail, list[uuid.UUID]]:
    question_ids = [uuid.uuid4() for _ in mandatory_flags]
    questions = [
        {
            "question_id": str(qid),
            "label": f"question {i}",
            "field_name": f"field_{i}",
            "is_mandatory": mandatory,
            "sort_order": i,
        }
        for i, (qid, mandatory) in enumerate(zip(question_ids, mandatory_flags, strict=True))
    ]
    return FormDetail(task_form_id="tf-1", sections=[{"questions": questions}]), question_ids


def _mock_session(*, answered_ids: list[uuid.UUID]) -> MagicMock:
    """A DB session double -- no real Postgres, matching this repo's existing
    no-real-DB test convention (see tests/line/test_webhook.py).
    """
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "conversation_id", uuid.uuid4()))

    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = answered_ids
    session.execute = AsyncMock(return_value=execute_result)
    return session


def test_questions_from_form_orders_by_sort_order() -> None:
    form, question_ids = _form(mandatory_flags=[True, True])
    form.sections[0]["questions"].reverse()  # shuffle -- confirm sorting actually happens

    questions = service.questions_from_form(form)

    assert [q.question_id for q in questions] == question_ids


class TestStartConversation:
    async def test_asks_first_mandatory_question(self) -> None:
        form, (q1, q2, q3) = _form(mandatory_flags=[False, True, True])
        session = _mock_session(answered_ids=[])

        reply = await service.start_conversation(
            session, user_id=uuid.uuid4(), task_id=uuid.uuid4(), task_form_id=uuid.uuid4(), form=form
        )

        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert reply.text == "question 1"  # q2, the first mandatory one, not q1

    async def test_no_mandatory_questions_goes_straight_to_confirmation(self) -> None:
        """Matches database/seed/mock_forms.sql's processing_record shape --
        every question there is is_mandatory=false in the real data.
        """
        form, _ = _form(mandatory_flags=[False, False])
        session = _mock_session(answered_ids=[])

        reply = await service.start_conversation(
            session, user_id=uuid.uuid4(), task_id=uuid.uuid4(), task_form_id=uuid.uuid4(), form=form
        )

        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION


class TestHandleAnswer:
    async def test_advances_to_next_required_question_when_slots_remain(self) -> None:
        form, (q1, q2) = _form(mandatory_flags=[True, True])
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=q1,
        )
        session = _mock_session(answered_ids=[q1])  # q2 still unanswered
        session.get = AsyncMock(return_value=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="answer 1", form=form
        )

        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert conversation.current_question_id == q2

    async def test_reaches_confirmation_once_all_required_answered(self) -> None:
        """The exact case state_machine.py's own docstring calls out: "slots
        still missing" vs. "all filled" must route through on_guided_answer,
        not be reimplemented here.
        """
        form, (q1,) = _form(mandatory_flags=[True])
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=q1,
        )
        session = _mock_session(answered_ids=[q1])
        session.get = AsyncMock(return_value=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="answer 1", form=form
        )

        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION
        assert conversation.current_question_id is None
