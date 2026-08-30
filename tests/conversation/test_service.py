import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.conversation import service
from src.conversation.constants import ActiveSubstate, AnswerSource, ConversationStatus
from src.conversation.exceptions import ConversationNotFound
from src.conversation.models import Conversation, ConversationAnswer
from src.exceptions import UpstreamServiceError
from src.forms.schemas import FormDetail
from src.line import parent_picker
from src.tasks.exceptions import HandlerNotSupported


def _form(*, mandatory_flags: list[bool]) -> tuple[FormDetail, list[uuid.UUID]]:
    question_ids = [uuid.uuid4() for _ in mandatory_flags]
    questions = [
        {
            "question_id": str(qid),
            "label": f"question {i}",
            "field_name": f"field_{i}",
            "input_type": "VARCHAR",
            "is_mandatory": mandatory,
            "sort_order": i,
        }
        for i, (qid, mandatory) in enumerate(zip(question_ids, mandatory_flags, strict=True))
    ]
    return FormDetail(task_form_id="tf-1", sections=[{"questions": questions}]), question_ids


def _validated_field_form(
    validation_rule: dict[str, object] | None,
) -> tuple[FormDetail, uuid.UUID]:
    """A single mandatory free-text question carrying `validation_rule`
    directly, the same shape _question_from_dict reads off a real question
    dict post-forms/client.py-conversion -- lets tests drive the Validate
    Answer step without needing a live Kotlin response.
    """
    question_id = uuid.uuid4()
    questions = [
        {
            "question_id": str(question_id),
            "label": "จำนวนพัดลม",
            "field_name": "fan_count",
            "input_type": "VARCHAR",
            "is_mandatory": True,
            "sort_order": 0,
            "validation_rule": validation_rule,
        }
    ]
    return FormDetail(task_form_id="tf-validated", sections=[{"questions": questions}]), question_id


def _boolean_form() -> tuple[FormDetail, uuid.UUID]:
    """A single mandatory BOOLEAN question -- matches the real
    farm_pest_disease_record.is_quality_damage shape that actually failed
    with `invalid input syntax for type boolean` before this existed.
    """
    question_id = uuid.uuid4()
    questions = [
        {
            "question_id": str(question_id),
            "label": "ทำให้ผลโกโก้เสียคุณภาพหรือไม่",
            "field_name": "is_quality_damage",
            "input_type": "BOOLEAN",
            "is_mandatory": True,
            "sort_order": 0,
        }
    ]
    return FormDetail(task_form_id="tf-bool", sections=[{"questions": questions}]), question_id


def _mandatory_option_form(choice_count: int) -> tuple[FormDetail, uuid.UUID]:
    """A single mandatory OPTION question with `choice_count` real choices --
    matches farm_id (backed by ref.farm_constant) on the harvest/farm_activity
    forms, the field a reviewer caught actually hitting this at 13 real rows.
    """
    question_id = uuid.uuid4()
    questions = [
        {
            "question_id": str(question_id),
            "label": "เลือกฟาร์ม",
            "field_name": "farm_id",
            "input_type": "OPTION",
            "is_mandatory": True,
            "sort_order": 0,
            "choices": [
                {"id": str(uuid.uuid4()), "name": f"farm {i}"} for i in range(choice_count)
            ],
        }
    ]
    return FormDetail(task_form_id="tf-option", sections=[{"questions": questions}]), question_id


def _answer(question_id: uuid.UUID, text: str = "some answer") -> ConversationAnswer:
    return ConversationAnswer(
        conversation_id=uuid.uuid4(),
        question_id=question_id,
        answer={"text": text},
        source=AnswerSource.GUIDED_FLOW,
    )


def _mock_session(
    *, answers: list[ConversationAnswer], conversation: Conversation | None = None
) -> MagicMock:
    """A DB session double -- no real Postgres, matching this repo's existing
    no-real-DB test convention (see tests/line/test_webhook.py).

    session.execute is used for several different queries across this
    module: the locked conversation lookup (.scalar_one_or_none()), the
    answered-rows lookup (.scalars().all()), and the pause/resume lookups
    (.scalars().first()) -- all rigged on the same result double since each
    call site only ever touches the method chain it cares about.
    """
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(
        side_effect=lambda obj: setattr(obj, "conversation_id", uuid.uuid4())
    )

    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = answers
    execute_result.scalars.return_value.first.return_value = conversation
    execute_result.scalar_one_or_none.return_value = conversation
    session.execute = AsyncMock(return_value=execute_result)
    return session


def test_questions_from_form_orders_by_sort_order() -> None:
    form, question_ids = _form(mandatory_flags=[True, True])
    form.sections[0]["questions"].reverse()  # shuffle -- confirm sorting actually happens

    questions = service.questions_from_form(form)

    assert [q.question_id for q in questions] == question_ids


def test_boolean_question_gets_synthesized_yes_no_choices() -> None:
    """Kotlin never sends `choices` for BOOLEAN (its own filter is
    INPUT_TYPE == OPTION only), so these have to be synthesized locally.
    """
    form, question_id = _boolean_form()

    questions = service.questions_from_form(form)

    assert len(questions) == 1
    assert questions[0].choices == [
        service.Choice(id="__pause__", label="⏸️ พักไว้ก่อน"),
        service.Choice(id="true", label="ใช่"),
        service.Choice(id="false", label="ไม่"),
    ]


def test_mandatory_option_at_quota_leaves_room_for_pause() -> None:
    """Regression caught in review (PR #25): the mandatory branch of
    _choices_for used to return `[_PAUSE_CHOICE, *constrained]` completely
    unsliced, on the assumption a mandatory question's real choices already
    fit LINE's 13-item Quick Reply cap. Adding pause broke that -- a
    mandatory OPTION field with exactly 13 real choices (farm_id, backed by
    ref.farm_constant, real production data) became 14 total, and router.py's
    own send-time `choices[:13]` slice silently dropped the LAST real
    choice -- a farm a farmer could no longer actually select via button.
    """
    form, _ = _mandatory_option_form(choice_count=service._QUICK_REPLY_LIMIT)

    questions = service.questions_from_form(form)

    assert len(questions) == 1
    choices = questions[0].choices
    assert choices is not None
    assert len(choices) == service._QUICK_REPLY_LIMIT  # pause + 12 real, not 14
    assert choices[0] == service.Choice(id="__pause__", label="⏸️ พักไว้ก่อน")
    expected_labels = [f"farm {i}" for i in range(service._QUICK_REPLY_LIMIT - 1)]
    assert [c.label for c in choices[1:]] == expected_labels


def test_mandatory_option_well_under_quota_keeps_every_real_choice() -> None:
    """The fix must not over-truncate the common case -- only the
    near-the-cap case actually needs anything dropped."""
    form, _ = _mandatory_option_form(choice_count=3)

    questions = service.questions_from_form(form)

    choices = questions[0].choices
    assert choices is not None
    assert len(choices) == 4  # pause + all 3 real choices, nothing dropped
    assert [c.label for c in choices[1:]] == ["farm 0", "farm 1", "farm 2"]


class TestStartConversation:
    async def test_asks_first_unanswered_question_regardless_of_mandatory(self) -> None:
        """Every question gets asked, not just mandatory ones -- an optional
        question gets a skip button instead of being silently omitted (see
        _choices_for's skip-button logic). _next_unanswered_required
        filtering on is_mandatory again would make skip buttons unreachable
        dead code, since only questions it selects as "next" ever reach the
        farmer.
        """
        form, (q1, q2, q3) = _form(mandatory_flags=[False, True, True])
        session = _mock_session(answers=[])

        reply = await service.start_conversation(
            session,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            form=form,
        )

        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert reply.text == "question 0"  # q1, sort_order 0 -- first regardless of mandatory
        assert reply.choices == [
            service.Choice(id="__skip__", label="⏭️ ข้าม"),
            service.Choice(id="__pause__", label="⏸️ พักไว้ก่อน"),
        ]

    async def test_all_optional_form_still_asks_every_question(self) -> None:
        """An all-optional form (matches database/seed/mock_forms.sql's
        processing_record shape) still asks each question with a skip
        option, rather than silently deciding none of them are worth
        asking and jumping straight to confirmation.
        """
        form, _ = _form(mandatory_flags=[False, False])
        session = _mock_session(answers=[])

        reply = await service.start_conversation(
            session,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            form=form,
        )

        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert reply.text == "question 0"


class TestStartConversationWithAutofill:
    """US2-4: seeding a brand-new conversation from a sanitized reuse
    answer (already filtered by src.conversation.reuse.sanitize_for_autofill
    before it reaches here -- these tests pass already-clean dicts, the
    same way router.py will after calling that function itself).
    """

    async def test_every_question_reused_goes_straight_to_confirmation(self) -> None:
        form, (q1, q2) = _form(mandatory_flags=[True, True])
        session = _mock_session(answers=[])

        reply = await service.start_conversation_with_autofill(
            session,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            form=form,
            sanitized_answer={"field_0": "คำตอบที่ 1", "field_1": "คำตอบที่ 2"},
        )

        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION
        assert "สรุปคำตอบของคุณ" in reply.text
        assert "คำตอบที่ 1" in reply.text
        assert "คำตอบที่ 2" in reply.text

    async def test_partial_reuse_recaps_then_asks_the_remaining_question(self) -> None:
        form, (q1, q2) = _form(mandatory_flags=[True, True])
        session = _mock_session(answers=[])

        reply = await service.start_conversation_with_autofill(
            session,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            form=form,
            sanitized_answer={"field_0": "คำตอบที่ 1"},
        )

        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert "question 0" in reply.text  # recap of the reused answer
        assert "คำตอบที่ 1" in reply.text
        assert reply.text.endswith("question 1")  # the one still unanswered, asked fresh

    async def test_nothing_reused_behaves_like_a_fresh_start_with_no_recap(self) -> None:
        form, (q1,) = _form(mandatory_flags=[True])
        session = _mock_session(answers=[])

        reply = await service.start_conversation_with_autofill(
            session,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            form=form,
            sanitized_answer={},
        )

        assert reply.text == "question 0"  # no recap prefix -- nothing was reused

    async def test_reused_option_value_shows_its_label_not_its_raw_id(self) -> None:
        form, question_id = _mandatory_option_form(choice_count=3)
        farm_choice_id = form.sections[0]["questions"][0]["choices"][1]["id"]
        session = _mock_session(answers=[])

        reply = await service.start_conversation_with_autofill(
            session,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            form=form,
            sanitized_answer={"farm_id": farm_choice_id},
        )

        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION
        assert "farm 1" in reply.text  # the label, not the raw uuid
        assert farm_choice_id not in reply.text


class TestStartConversationWithParentPicker:
    """The 5 previously-blocked handlers (docs/plans/chatbot-child-handler-
    design.md) start on a synthetic parent-picker question instead of the
    form's real first question -- router.py has already resolved
    parent_choices and confirmed it's non-empty before calling this.
    """

    async def test_asks_parent_picker_question_before_any_real_question(self) -> None:
        form, _ = _form(mandatory_flags=[True])  # a real question exists but must NOT be asked yet
        session = _mock_session(answers=[])
        choices = [parent_picker.ParentOption(id="pa-1", label="กิจกรรม 01/08/2026 — ใส่ปุ๋ย")]

        reply = await service.start_conversation(
            session,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            form=form,
            parent_kind="farm_activity",
            parent_choices=choices,
        )

        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert reply.text == parent_picker.PROMPT["farm_activity"]
        assert reply.choices == [
            service.Choice(id="__pause__", label="⏸️ พักไว้ก่อน"),
            service.Choice(id="pa-1", label="กิจกรรม 01/08/2026 — ใส่ปุ๋ย"),
        ]
        added_conversation = session.add.call_args.args[0]
        # current_question_id has a real FK to form.question -- there's no
        # such row for this synthetic step, so it must stay NULL, not some
        # made-up placeholder (see the ForeignKeyViolationError this
        # replaced). Which picker is pending lives in parent_answer instead.
        assert added_conversation.current_question_id is None
        assert added_conversation.parent_answer == {"pending_kind": "farm_activity"}


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
        # q2 still unanswered
        session = _mock_session(answers=[_answer(q1)], conversation=conversation)
        # confirm_conversation uses plain session.get, unlike handle_answer's
        # locked select -- _mock_session only rigs session.execute.
        session.get = AsyncMock(return_value=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="answer 1", form=form
        )

        assert reply is not None
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
        session = _mock_session(answers=[_answer(q1)], conversation=conversation)
        # confirm_conversation uses plain session.get, unlike handle_answer's
        # locked select -- _mock_session only rigs session.execute.
        session.get = AsyncMock(return_value=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="answer 1", form=form
        )

        assert reply is not None
        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION
        assert conversation.current_question_id is None

    async def test_confirmation_summary_lists_every_answer(self) -> None:
        """The actual "submission confirmation" deliverable -- a farmer
        should see what they answered before confirming, not a generic
        "ready?" line.
        """
        form, (q1, q2) = _form(mandatory_flags=[True, True])
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=q2,
        )
        session = _mock_session(
            answers=[_answer(q1, text="คำตอบที่ 1"), _answer(q2, text="คำตอบที่ 2")],
            conversation=conversation,
        )

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="คำตอบที่ 2", form=form
        )

        assert reply is not None
        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION
        assert "question 0" in reply.text
        assert "คำตอบที่ 1" in reply.text
        assert "question 1" in reply.text
        assert "คำตอบที่ 2" in reply.text
        # question 0's label should appear before question 1's -- sort_order,
        # not answer-insertion order.
        assert reply.text.index("question 0") < reply.text.index("question 1")


class TestHandleAnswerWithChoices:
    """Covers the resolution logic BOOLEAN and OPTION questions both share --
    real bug this closes: `agriculture.farm_pest_disease_record` rejected
    free-text answers like "ไม่"/"aaa" for its real `boolean` column with
    `invalid input syntax for type boolean` until this resolved a tapped/typed
    label to a real "true"/"false" value first.
    """

    async def test_matching_choice_resolves_to_true_false_value(self) -> None:
        form, question_id = _boolean_form()
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=question_id,
        )
        session = _mock_session(
            answers=[_answer(question_id, text="ใช่")], conversation=conversation
        )

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="ใช่", form=form
        )

        assert reply is not None
        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION
        added_answer = session.add.call_args.args[0]
        assert added_answer.answer == {"text": "ใช่", "value": "true"}

    async def test_non_matching_answer_reasks_same_question(self) -> None:
        form, question_id = _boolean_form()
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=question_id,
        )
        session = _mock_session(answers=[], conversation=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="aaa", form=form
        )

        assert reply is not None
        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert reply.choices == [
            service.Choice(id="__pause__", label="⏸️ พักไว้ก่อน"),
            service.Choice(id="true", label="ใช่"),
            service.Choice(id="false", label="ไม่"),
        ]
        assert "กรุณาเลือกคำตอบจากตัวเลือกที่กำหนดเท่านั้น" in reply.text
        session.add.assert_not_called()  # bad answer never gets persisted
        assert conversation.current_question_id == question_id  # unchanged, still open


_FAN_COUNT_RULE = {
    "type": "INT",
    "min": 0,
    "max": 50,
    "integer_only": True,
    "error_message": "กรุณากรอกจำนวนพัดลมเป็นจำนวนเต็ม 0-50",
}


class TestHandleAnswerValidation:
    """The (New) Validate Answer step: a free-text answer that fails its
    field's format rule must be rejected -- re-ask the same question with
    the rule's own error_message, never persisted.
    """

    async def test_invalid_answer_reasks_with_error_message(self) -> None:
        form, question_id = _validated_field_form(_FAN_COUNT_RULE)
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=question_id,
        )
        session = _mock_session(answers=[], conversation=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="999", form=form
        )

        assert reply is not None
        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert "กรุณากรอกจำนวนพัดลมเป็นจำนวนเต็ม 0-50" in reply.text
        assert "จำนวนพัดลม" in reply.text  # question re-asked, not dropped
        session.add.assert_not_called()  # invalid answer never gets persisted
        assert conversation.current_question_id == question_id  # unchanged, still open

    async def test_valid_answer_is_stored_and_advances(self) -> None:
        form, question_id = _validated_field_form(_FAN_COUNT_RULE)
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=question_id,
        )
        # answers=[...] simulates the DB state the post-add re-query will see --
        # the mock can't react to session.add() dynamically (see _mock_session's
        # own docstring / test_reaches_confirmation_once_all_required_answered).
        session = _mock_session(answers=[_answer(question_id, text="5")], conversation=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="5", form=form
        )

        assert reply is not None
        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION
        added_answer = session.add.call_args.args[0]
        assert added_answer.answer == {"text": "5"}

    async def test_field_with_no_rule_skips_validation_entirely(self) -> None:
        """Existing behavior for untracked fields (e.g. field_0/field_1 used
        throughout this file's other fixtures) must be unaffected.
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
        session = _mock_session(
            answers=[_answer(q1, text="anything goes")], conversation=conversation
        )

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="anything goes", form=form
        )

        assert reply is not None
        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION
        session.add.assert_called_once()

    async def test_mandatory_question_with_no_rule_rejects_blank_answer(self) -> None:
        """A mandatory field with no validation_rule row at all must still
        reject a blank/whitespace-only answer -- validate_answer alone would
        pass it through unchecked (see test_field_with_no_rule_skips_
        validation_entirely), which is exactly the gap this guards against.
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
        session = _mock_session(answers=[], conversation=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="   ", form=form
        )

        assert reply is not None
        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert "เว้นว่าง" in reply.text
        session.add.assert_not_called()  # blank answer never gets persisted
        assert conversation.current_question_id == q1  # unchanged, still open

    async def test_mandatory_question_with_rule_rejects_blank_answer(self) -> None:
        """Same as above but for a field that DOES have a validation_rule --
        the blank check must fire before validate_answer's own type check,
        not rely on it (an empty string trivially satisfies VARCHAR's
        max_length-only check).
        """
        form, question_id = _validated_field_form(_FAN_COUNT_RULE)
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=question_id,
        )
        session = _mock_session(answers=[], conversation=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="", form=form
        )

        assert reply is not None
        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert "เว้นว่าง" in reply.text
        session.add.assert_not_called()
        assert conversation.current_question_id == question_id


class TestHandleAnswerParentPicker:
    """parent_answer holding a "pending_kind" key means this conversation is
    still on the synthetic parent-picker step, not a real form.question --
    handle_answer must route these through _handle_parent_answer rather than
    the normal ConversationAnswer path (that path would violate
    conversation_answer.question_id's real FK to form.question, since the
    picker step has no corresponding row there).
    """

    def _conversation_awaiting_parent_pick(self, conversation_id: uuid.UUID) -> Conversation:
        return Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=None,
            parent_answer={"pending_kind": "batch"},
        )

    async def test_matching_pick_stores_parent_answer_and_advances_to_real_question(self) -> None:
        form, (q1,) = _form(mandatory_flags=[True])
        conversation_id = uuid.uuid4()
        conversation = self._conversation_awaiting_parent_pick(conversation_id)
        session = _mock_session(answers=[], conversation=conversation)
        choices = [parent_picker.ParentOption(id="batch-1", label="แบทช์ 01/08/2026 — สถานี A")]

        with patch("src.line.parent_picker.choices_for", new=AsyncMock(return_value=choices)):
            reply = await service.handle_answer(
                session,
                conversation_id=conversation_id,
                raw_text="แบทช์ 01/08/2026 — สถานี A",
                form=form,
            )

        assert reply is not None
        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert reply.text == "question 0"  # the form's real first question, not re-asking
        assert conversation.current_question_id == q1
        assert conversation.parent_answer == {"field_name": "batch_id", "value": "batch-1"}
        session.add.assert_not_called()  # never written as a ConversationAnswer row

    async def test_non_matching_pick_reasks_with_fresh_choices(self) -> None:
        form, (q1,) = _form(mandatory_flags=[True])
        conversation_id = uuid.uuid4()
        conversation = self._conversation_awaiting_parent_pick(conversation_id)
        session = _mock_session(answers=[], conversation=conversation)
        choices = [parent_picker.ParentOption(id="batch-1", label="แบทช์ 01/08/2026 — สถานี A")]

        with patch("src.line.parent_picker.choices_for", new=AsyncMock(return_value=choices)):
            reply = await service.handle_answer(
                session, conversation_id=conversation_id, raw_text="ไม่ตรงกับตัวเลือกไหนเลย", form=form
            )

        assert reply is not None
        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert reply.choices == [
            service.Choice(id="__pause__", label="⏸️ พักไว้ก่อน"),
            service.Choice(id="batch-1", label="แบทช์ 01/08/2026 — สถานี A"),
        ]
        # still on the picker step, not advanced to the real form or resolved
        assert conversation.parent_answer == {"pending_kind": "batch"}
        assert conversation.current_question_id is None


class TestHandleAnswerPause:
    """US2-3: tapping the pause choice must never write a ConversationAnswer
    row or touch current_question_id -- pausing isn't answering. Checked
    against all three shapes handle_answer can be in when the pause label
    arrives: a real question, the parent-picker step, and no open question
    at all.
    """

    async def test_pauses_without_storing_an_answer(self) -> None:
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
        session = _mock_session(answers=[], conversation=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="⏸️ พักไว้ก่อน", form=form
        )

        assert reply is not None
        assert "พัก" in reply.text
        assert conversation.status == ConversationStatus.PAUSED
        assert conversation.current_question_id == q1  # unchanged -- not answered, just paused
        session.add.assert_not_called()

    async def test_pauses_during_parent_picker_step(self) -> None:
        form, _ = _form(mandatory_flags=[True])
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=None,
            parent_answer={"pending_kind": "batch"},
        )
        session = _mock_session(answers=[], conversation=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="⏸️ พักไว้ก่อน", form=form
        )

        assert reply is not None
        assert conversation.status == ConversationStatus.PAUSED
        # still pending -- pausing didn't resolve the parent pick either
        assert conversation.parent_answer == {"pending_kind": "batch"}

    async def test_pauses_with_no_open_question(self) -> None:
        """Awaiting confirmation has no pause button in the real UI (only
        confirm/cancel), but the underlying check is a plain raw-text match
        with no question-list dependency -- confirming it doesn't crash here
        (ConversationNotFound) is cheap insurance, not just theoretical.
        """
        form, (q1,) = _form(mandatory_flags=[True])
        conversation_id = uuid.uuid4()
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=None,
        )
        session = _mock_session(answers=[_answer(q1)], conversation=conversation)

        reply = await service.handle_answer(
            session, conversation_id=conversation_id, raw_text="⏸️ พักไว้ก่อน", form=form
        )

        assert reply is not None
        assert conversation.status == ConversationStatus.PAUSED


class TestPauseActiveConversation:
    async def test_pauses_the_active_conversation(self) -> None:
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=uuid.uuid4(),
        )
        session = _mock_session(answers=[], conversation=conversation)

        await service.pause_active_conversation(session, user_id=conversation.user_id)

        assert conversation.status == ConversationStatus.PAUSED

    async def test_no_op_when_nothing_active(self) -> None:
        session = _mock_session(answers=[], conversation=None)

        await service.pause_active_conversation(session, user_id=uuid.uuid4())

        session.commit.assert_not_called()


class TestFindResumableConversation:
    async def test_finds_a_paused_conversation(self) -> None:
        user_id, task_id = uuid.uuid4(), uuid.uuid4()
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            user_id=user_id,
            task_id=task_id,
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.PAUSED,
            current_question_id=uuid.uuid4(),
        )
        session = _mock_session(answers=[], conversation=conversation)

        found = await service.find_resumable_conversation(session, user_id=user_id, task_id=task_id)

        assert found is conversation

    async def test_none_when_nothing_open(self) -> None:
        session = _mock_session(answers=[], conversation=None)

        found = await service.find_resumable_conversation(
            session, user_id=uuid.uuid4(), task_id=uuid.uuid4()
        )

        assert found is None


class TestResumeConversation:
    async def test_mid_question_resume_recaps_answers_then_asks_next(self) -> None:
        form, (q1, q2) = _form(mandatory_flags=[True, True])
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.PAUSED,
            current_question_id=q2,
        )
        session = _mock_session(answers=[_answer(q1, text="คำตอบที่ 1")])

        reply = await service.resume_conversation(session, conversation=conversation, form=form)

        assert conversation.status == ConversationStatus.ACTIVE
        assert reply.substate == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
        assert "question 0" in reply.text  # recap of what was already answered
        assert "คำตอบที่ 1" in reply.text
        assert reply.text.endswith("question 1")  # the still-open question, asked fresh
        assert reply.choices == [
            service.Choice(id="__pause__", label="⏸️ พักไว้ก่อน"),
        ]

    async def test_resume_with_nothing_answered_yet_has_no_recap(self) -> None:
        form, (q1,) = _form(mandatory_flags=[True])
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.PAUSED,
            current_question_id=q1,
        )
        session = _mock_session(answers=[])

        reply = await service.resume_conversation(session, conversation=conversation, form=form)

        assert reply.text == "question 0"  # no recap prefix -- nothing answered yet

    async def test_resume_at_confirmation_step_shows_summary(self) -> None:
        form, (q1,) = _form(mandatory_flags=[True])
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.PAUSED,
            current_question_id=None,
        )
        session = _mock_session(answers=[_answer(q1, text="คำตอบที่ 1")])

        reply = await service.resume_conversation(session, conversation=conversation, form=form)

        assert conversation.status == ConversationStatus.ACTIVE
        assert reply.substate == ActiveSubstate.AWAITING_CONFIRMATION
        assert "สรุปคำตอบของคุณ" in reply.text
        assert "คำตอบที่ 1" in reply.text

    async def test_resume_during_parent_picker_step(self) -> None:
        form, _ = _form(mandatory_flags=[True])
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.PAUSED,
            current_question_id=None,
            parent_answer={"pending_kind": "batch"},
        )
        session = _mock_session(answers=[])
        choices = [parent_picker.ParentOption(id="batch-1", label="แบทช์ 01/08/2026 — สถานี A")]

        with patch("src.line.parent_picker.choices_for", new=AsyncMock(return_value=choices)):
            reply = await service.resume_conversation(session, conversation=conversation, form=form)

        assert conversation.status == ConversationStatus.ACTIVE
        assert reply.text == parent_picker.PROMPT["batch"]  # no recap -- nothing answered yet
        assert reply.choices == [
            service.Choice(id="__pause__", label="⏸️ พักไว้ก่อน"),
            service.Choice(id="batch-1", label="แบทช์ 01/08/2026 — สถานี A"),
        ]

    async def test_resume_raises_if_current_question_no_longer_in_form(self) -> None:
        """The form changed out from under a long-paused conversation (a
        question was removed) -- fail loudly rather than silently asking
        about a question that isn't there anymore.
        """
        form, _ = _form(mandatory_flags=[True])  # only question 0 exists
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.PAUSED,
            current_question_id=uuid.uuid4(),  # not in the form at all
        )
        session = _mock_session(answers=[])

        with pytest.raises(ConversationNotFound):
            await service.resume_conversation(session, conversation=conversation, form=form)


class TestConfirmConversation:
    """No coverage existed for this function at all before -- adding it
    while touching the HandlerNotSupported branch, since it's the function
    responsible for CB-1's honest-failure behavior (see its own docstring).
    """

    def _conversation(self, conversation_id: uuid.UUID) -> Conversation:
        return Conversation(
            conversation_id=conversation_id,
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_form_id=uuid.uuid4(),
            status=ConversationStatus.ACTIVE,
            current_question_id=None,
        )

    async def test_marks_completed_and_thanks_farmer_on_success(self) -> None:
        form, (q1,) = _form(mandatory_flags=[True])
        conversation_id = uuid.uuid4()
        conversation = self._conversation(conversation_id)
        session = _mock_session(answers=[_answer(q1)], conversation=conversation)
        # confirm_conversation uses plain session.get, unlike handle_answer's
        # locked select -- _mock_session only rigs session.execute.
        session.get = AsyncMock(return_value=conversation)

        with patch("src.conversation.service.submit_task", new=AsyncMock()):
            reply = await service.confirm_conversation(
                session, conversation_id=conversation_id, form=form
            )

        assert reply.submission_failed is False
        assert "บันทึกข้อมูลเรียบร้อยแล้ว" in reply.text
        assert conversation.status == ConversationStatus.COMPLETED

    async def test_stays_open_and_honest_on_generic_failure(self) -> None:
        form, (q1,) = _form(mandatory_flags=[True])
        conversation_id = uuid.uuid4()
        conversation = self._conversation(conversation_id)
        session = _mock_session(answers=[_answer(q1)], conversation=conversation)
        # confirm_conversation uses plain session.get, unlike handle_answer's
        # locked select -- _mock_session only rigs session.execute.
        session.get = AsyncMock(return_value=conversation)

        failing_submit = AsyncMock(side_effect=UpstreamServiceError("Go returned 500"))
        with patch("src.conversation.service.submit_task", new=failing_submit):
            reply = await service.confirm_conversation(
                session, conversation_id=conversation_id, form=form
            )

        assert reply.submission_failed is True
        assert "ลองใหม่อีกครั้ง" in reply.text  # "try again" -- worth retrying
        assert conversation.status == ConversationStatus.ACTIVE  # NOT completed -- nothing saved

    async def test_stays_open_with_distinct_message_when_handler_not_supported(self) -> None:
        """The regression this test guards against: before HandlerNotSupported
        existed, this case fell into the generic branch and told the farmer
        "ลองใหม่อีกครั้ง" (try again) for a failure retrying could never fix.
        """
        form, (q1,) = _form(mandatory_flags=[True])
        conversation_id = uuid.uuid4()
        conversation = self._conversation(conversation_id)
        session = _mock_session(answers=[_answer(q1)], conversation=conversation)
        # confirm_conversation uses plain session.get, unlike handle_answer's
        # locked select -- _mock_session only rigs session.execute.
        session.get = AsyncMock(return_value=conversation)

        failing_submit = AsyncMock(side_effect=HandlerNotSupported("Go returned 501"))
        with patch("src.conversation.service.submit_task", new=failing_submit):
            reply = await service.confirm_conversation(
                session, conversation_id=conversation_id, form=form
            )

        assert reply.submission_failed is True
        assert "ลองใหม่อีกครั้ง" not in reply.text  # must NOT suggest retrying
        assert "ยังไม่รองรับ" in reply.text  # "not supported yet" -- the honest reason
        assert conversation.status == ConversationStatus.ACTIVE  # still cancellable

    async def test_merges_parent_answer_into_submission_payload(self) -> None:
        """The parent-picker's pick (stored on conversation.parent_answer, not
        as a ConversationAnswer row -- see TestHandleAnswerParentPicker) must
        still land in Go's payload under its real column name, alongside the
        form's normal answers.
        """
        form, (q1,) = _form(mandatory_flags=[True])
        conversation_id = uuid.uuid4()
        conversation = self._conversation(conversation_id)
        conversation.parent_answer = {"field_name": "batch_id", "value": "batch-1"}
        session = _mock_session(answers=[_answer(q1, text="คำตอบ")], conversation=conversation)
        session.get = AsyncMock(return_value=conversation)

        submit_task_mock = AsyncMock()
        with patch("src.conversation.service.submit_task", new=submit_task_mock):
            reply = await service.confirm_conversation(
                session, conversation_id=conversation_id, form=form
            )

        assert reply.submission_failed is False
        submission = submit_task_mock.call_args.args[0]
        assert submission.answer["batch_id"] == "batch-1"
        assert submission.answer["field_0"] == "คำตอบ"
