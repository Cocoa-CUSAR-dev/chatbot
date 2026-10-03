"""Multi-plot answer: the pieces that need no database.

The fan-out tests here are the important ones. Writing N rows for one
conversation is the only part of this feature that can corrupt real farm data
rather than merely annoy a farmer: Go's SubmitTaskForUser deliberately allows
repeat submissions and dissectAnswer has no idempotency guard, so a retry that
re-sends an already-saved plot silently duplicates a record nobody will ever
notice. That is what `submitted` exists to prevent, and what these assert.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.conversation import service
from src.conversation.models import ConversationAnswer
from src.forms.schemas import FormDetail
from src.tasks.exceptions import HandlerNotSupported


def _form(*, field_names: list[str], is_multiple_submit: bool) -> FormDetail:
    questions = [
        {
            "question_id": str(uuid.uuid4()),
            "label": f"คำถาม {name}",
            "field_name": name,
            "input_type": "OPTION" if name.endswith("_id") else "VARCHAR",
            "is_mandatory": False,
            "sort_order": index,
        }
        for index, name in enumerate(field_names)
    ]
    return FormDetail(
        task_form_id="tf-1",
        sections=[{"questions": questions}],
        is_multiple_submit=is_multiple_submit,
    )


def _question(form: FormDetail, field_name: str) -> service.Question:
    return next(q for q in service.questions_from_form(form) if q.field_name == field_name)


class TestIsMultiPlotQuestion:
    def test_plot_question_on_a_multi_submit_form(self) -> None:
        form = _form(field_names=["plot_id", "description"], is_multiple_submit=True)
        assert service.is_multi_plot_question(_question(form, "plot_id"), form) is True

    def test_plot_question_on_an_ordinary_form_is_not(self) -> None:
        """Fanning out on a form the researchers did NOT mark multiple-submit
        would write exactly the duplicate rows they said they didn't want.
        """
        form = _form(field_names=["plot_id"], is_multiple_submit=False)
        assert service.is_multi_plot_question(_question(form, "plot_id"), form) is False

    def test_any_other_question_is_not(self) -> None:
        form = _form(field_names=["plot_id", "description"], is_multiple_submit=True)
        assert service.is_multi_plot_question(_question(form, "description"), form) is False


class TestPlotAnswerShape:
    def test_one_plot_is_stored_as_an_ordinary_option_answer(self) -> None:
        """So nothing downstream -- summary, payload, autofill, carry-forward
        -- has to know the picker exists for the common case.
        """
        assert service._plot_answer(["p1"], ["แปลง A"]) == {"text": "แปลง A", "value": "p1"}

    def test_several_plots_carry_the_fan_out_keys(self) -> None:
        answer = service._plot_answer(["p1", "p2"], ["แปลง A", "แปลง B"])
        assert answer == {
            "text": "แปลง A, แปลง B",
            "values": ["p1", "p2"],
            "labels": ["แปลง A", "แปลง B"],
            "multi": True,
            "submitted": [],
        }

    def test_selection_line_reads_back_what_was_picked(self) -> None:
        assert service._selection_text(["แปลง A", "แปลง C"]) == "เลือกแล้ว: แปลง A, แปลง C (2 แปลง)"
        assert service._selection_text([]) == "ยังไม่ได้เลือกแปลง"


class TestCarriedAnswer:
    def test_submitted_is_dropped(self) -> None:
        """Carrying it would make the next submission skip exactly the plots
        the previous one saved -- fewer rows than the farmer picked, silently.
        """
        carried = service._carried_answer(
            {
                "text": "แปลง A, แปลง B",
                "values": ["p1", "p2"],
                "labels": ["แปลง A", "แปลง B"],
                "multi": True,
                "submitted": ["p1", "p2"],
            }
        )
        assert "submitted" not in carried
        assert carried["values"] == ["p1", "p2"]

    def test_an_ordinary_answer_is_copied_unchanged(self) -> None:
        answer = {"text": "แปลง A", "value": "p1"}
        assert service._carried_answer(answer) == answer


def _row(answer: dict) -> ConversationAnswer:
    return ConversationAnswer(
        conversation_id=uuid.uuid4(),
        question_id=uuid.uuid4(),
        answer=answer,
        source="guided_flow",
    )


def _conversation() -> SimpleNamespace:
    return SimpleNamespace(conversation_id=uuid.uuid4(), user_id=uuid.uuid4(), task_id=uuid.uuid4())


def _multi_row(submitted: list[str] | None = None) -> ConversationAnswer:
    return _row(
        {
            "text": "แปลง A, แปลง B, แปลง C",
            "values": ["p1", "p2", "p3"],
            "labels": ["แปลง A", "แปลง B", "แปลง C"],
            "multi": True,
            "submitted": submitted or [],
        }
    )


class TestFanOut:
    async def test_one_submission_per_plot_identical_but_for_plot_id(self) -> None:
        session = MagicMock(commit=AsyncMock())
        submit = AsyncMock()
        row = _multi_row()

        with patch("src.conversation.service.submit_task", new=submit):
            result = await service._submit_per_plot(
                session,
                conversation=_conversation(),
                row=row,
                base_payload={"description": "พ่นยา"},
            )

        assert result is None  # nothing left to report -- every plot landed
        sent = [call.args[0].answer for call in submit.await_args_list]
        assert sent == [
            {"description": "พ่นยา", "plot_id": "p1"},
            {"description": "พ่นยา", "plot_id": "p2"},
            {"description": "พ่นยา", "plot_id": "p3"},
        ]
        assert row.answer["submitted"] == ["p1", "p2", "p3"]

    async def test_each_plot_is_committed_as_it_lands(self) -> None:
        """Not one commit at the end: a crash or a Vercel timeout between
        plots must not lose the record of what already reached Go.
        """
        session = MagicMock(commit=AsyncMock())
        with patch("src.conversation.service.submit_task", new=AsyncMock()):
            await service._submit_per_plot(
                session, conversation=_conversation(), row=_multi_row(), base_payload={}
            )
        assert session.commit.await_count == 3

    async def test_partial_failure_keeps_what_landed_and_says_so(self) -> None:
        session = MagicMock(commit=AsyncMock())
        submit = AsyncMock(side_effect=[None, RuntimeError("go is down"), None])
        row = _multi_row()

        with patch("src.conversation.service.submit_task", new=submit):
            result = await service._submit_per_plot(
                session, conversation=_conversation(), row=row, base_payload={}
            )

        assert result is not None
        assert result.submission_failed is True
        assert "บันทึกแล้ว 1 จาก 3 แปลง" in result.text
        assert "แปลง A" in result.text
        # It stopped at the failure rather than carrying on to plot 3 -- the
        # farmer is told what is missing, and a retry does the rest in order.
        assert submit.await_count == 2
        assert row.answer["submitted"] == ["p1"]

    async def test_retry_sends_only_the_plots_that_never_landed(self) -> None:
        """The duplicate-row guard, stated directly."""
        session = MagicMock(commit=AsyncMock())
        submit = AsyncMock()
        row = _multi_row(submitted=["p1"])

        with patch("src.conversation.service.submit_task", new=submit):
            result = await service._submit_per_plot(
                session, conversation=_conversation(), row=row, base_payload={}
            )

        assert result is None
        sent_plots = [call.args[0].answer["plot_id"] for call in submit.await_args_list]
        assert sent_plots == ["p2", "p3"]
        assert row.answer["submitted"] == ["p1", "p2", "p3"]

    async def test_failure_on_the_very_first_plot_reads_like_an_ordinary_failure(
        self,
    ) -> None:
        """ "บันทึกแล้ว 0 จาก 3" would be a strange way to say "nothing saved"."""
        session = MagicMock(commit=AsyncMock())
        submit = AsyncMock(side_effect=RuntimeError("go is down"))

        with patch("src.conversation.service.submit_task", new=submit):
            result = await service._submit_per_plot(
                session, conversation=_conversation(), row=_multi_row(), base_payload={}
            )

        assert result is not None
        assert result.submission_failed is True
        assert result.text == "เกิดข้อผิดพลาด ไม่สามารถบันทึกข้อมูลได้ กรุณาลองใหม่อีกครั้ง"

    async def test_unsupported_handler_propagates_to_the_honest_message(self) -> None:
        """It fails on the first plot and is permanent, so confirm_conversation's
        own branch reports it instead -- retrying could never help.
        """
        session = MagicMock(commit=AsyncMock())
        submit = AsyncMock(side_effect=HandlerNotSupported())
        row = _multi_row()

        with (
            patch("src.conversation.service.submit_task", new=submit),
            pytest.raises(HandlerNotSupported),
        ):
            await service._submit_per_plot(
                session, conversation=_conversation(), row=row, base_payload={}
            )

        assert row.answer["submitted"] == []


class TestConfirmationSummary:
    def test_multi_plot_answer_adds_the_row_count_line(self) -> None:
        form = _form(field_names=["plot_id"], is_multiple_submit=True)
        question = _question(form, "plot_id")
        row = _row(
            {
                "text": "แปลง A, แปลง C",
                "values": ["p1", "p3"],
                "labels": ["แปลง A", "แปลง C"],
                "multi": True,
                "submitted": [],
            }
        )
        row.question_id = question.question_id

        summary = service._format_confirmation_summary([question], [row])

        assert "จะบันทึกเป็น 2 รายการ (แยกตามแปลง): แปลง A, แปลง C" in summary

    def test_a_single_plot_answer_adds_nothing(self) -> None:
        form = _form(field_names=["plot_id"], is_multiple_submit=True)
        question = _question(form, "plot_id")
        row = _row({"text": "แปลง A", "value": "p1"})
        row.question_id = question.question_id

        summary = service._format_confirmation_summary([question], [row])

        assert "จะบันทึกเป็น" not in summary

    def test_an_unfinished_selection_is_not_listed_as_an_answer(self) -> None:
        form = _form(field_names=["plot_id"], is_multiple_submit=True)
        question = _question(form, "plot_id")
        row = _row({"selecting": True, "values": ["p1"], "labels": ["แปลง A"]})
        row.question_id = question.question_id

        assert service._format_answered_lines([question], [row]) == ""

    def test_an_unfinished_selection_does_not_count_as_answered(self) -> None:
        """Otherwise a farmer could tap one plot, walk away, and the next
        answer would carry them to the summary with the question half-made.
        """
        selecting = _row({"selecting": True, "values": ["p1"], "labels": ["แปลง A"]})
        done = _row({"text": "แปลง A", "value": "p1"})

        assert service._answered_question_ids([selecting, done]) == {done.question_id}
