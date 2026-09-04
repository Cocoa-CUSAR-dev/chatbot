import uuid
from unittest.mock import AsyncMock, patch

from src.conversation import reuse, service
from src.conversation.constants import AnswerSource
from src.forms.schemas import FormDetail


def _form_with(*question_dicts: dict[str, object]) -> list[service.Question]:
    return service.questions_from_form(
        FormDetail(task_form_id="tf-reuse", sections=[{"questions": list(question_dicts)}])
    )


def _varchar(field_name: str, *, mandatory: bool = True, sort_order: int = 0) -> dict[str, object]:
    return {
        "question_id": str(uuid.uuid4()),
        "label": field_name,
        "field_name": field_name,
        "input_type": "VARCHAR",
        "is_mandatory": mandatory,
        "sort_order": sort_order,
    }


def _option(field_name: str, choice_ids: list[str], *, sort_order: int = 0) -> dict[str, object]:
    return {
        "question_id": str(uuid.uuid4()),
        "label": field_name,
        "field_name": field_name,
        "input_type": "OPTION",
        "is_mandatory": True,
        "sort_order": sort_order,
        "choices": [{"id": cid, "name": f"label-{cid}"} for cid in choice_ids],
    }


def _boolean(field_name: str, *, sort_order: int = 0) -> dict[str, object]:
    return {
        "question_id": str(uuid.uuid4()),
        "label": field_name,
        "field_name": field_name,
        "input_type": "BOOLEAN",
        "is_mandatory": True,
        "sort_order": sort_order,
    }


class TestSanitizeForAutofill:
    """The actual filtering rules (drop task_id/stale-parent-ids, drop a
    stale OPTION/BOOLEAN value) now live in Go -- mobile-backend's
    internal/validation/autofill_sanitizer.go (#105, US2-5) -- and are
    tested there. This module's own job is just: build the right request
    payload, and return whatever Go sends back. fetch_sanitized_autofill is
    mocked throughout so these tests don't need a real Go backend.
    """

    async def test_returns_whatever_go_returns(self) -> None:
        questions = _form_with(_varchar("note"))
        go_response = {"note": "hello"}

        with patch(
            "src.conversation.reuse.fetch_sanitized_autofill",
            new=AsyncMock(return_value=go_response),
        ):
            sanitized = await reuse.sanitize_for_autofill({"note": "hello"}, questions)

        assert sanitized == go_response

    async def test_payload_carries_every_question_with_field_and_input_type(self) -> None:
        questions = _form_with(
            _varchar("note", sort_order=0), _option("fertilizer_id", [], sort_order=1)
        )
        fetch_mock = AsyncMock(return_value={})

        with patch("src.conversation.reuse.fetch_sanitized_autofill", new=fetch_mock):
            await reuse.sanitize_for_autofill({}, questions)

        sent_questions = fetch_mock.call_args.kwargs["questions"]
        assert {q["fieldName"] for q in sent_questions} == {"note", "fertilizer_id"}
        by_field = {q["fieldName"]: q for q in sent_questions}
        assert by_field["note"]["inputType"] == "VARCHAR"
        assert by_field["fertilizer_id"]["inputType"] == "OPTION"

    async def test_payload_includes_real_choices_for_a_constrained_question(self) -> None:
        choice_id = str(uuid.uuid4())
        questions = _form_with(_option("fertilizer_id", [choice_id]))
        fetch_mock = AsyncMock(return_value={})

        with patch("src.conversation.reuse.fetch_sanitized_autofill", new=fetch_mock):
            await reuse.sanitize_for_autofill({}, questions)

        sent_questions = fetch_mock.call_args.kwargs["questions"]
        assert sent_questions[0]["choices"] == [{"id": choice_id, "name": f"label-{choice_id}"}]

    async def test_payload_strips_pause_and_skip_sentinels_from_choices(self) -> None:
        # Question.choices always has pause (and skip, for non-mandatory)
        # prepended -- see service.py's _choices_for. Purely a LINE UI
        # concept, so Go should never see "__pause__"/"__skip__" as if they
        # were real domain choices.
        choice_id = str(uuid.uuid4())
        questions = _form_with(_option("fertilizer_id", [choice_id]))
        fetch_mock = AsyncMock(return_value={})

        with patch("src.conversation.reuse.fetch_sanitized_autofill", new=fetch_mock):
            await reuse.sanitize_for_autofill({}, questions)

        sent_choice_ids = {c["id"] for c in fetch_mock.call_args.kwargs["questions"][0]["choices"]}
        assert sent_choice_ids == {choice_id}

    async def test_payload_includes_synthesized_choices_for_a_boolean_question(self) -> None:
        # Kotlin never sends `choices` for BOOLEAN -- service.py's
        # _constrained_choices_for synthesizes true/false locally, and
        # _real_choices picks that up like any other constrained question.
        questions = _form_with(_boolean("is_quality_damage"))
        fetch_mock = AsyncMock(return_value={})

        with patch("src.conversation.reuse.fetch_sanitized_autofill", new=fetch_mock):
            await reuse.sanitize_for_autofill({}, questions)

        sent_choice_ids = {c["id"] for c in fetch_mock.call_args.kwargs["questions"][0]["choices"]}
        assert sent_choice_ids == {"true", "false"}

    async def test_unconstrained_question_gets_an_empty_choices_list(self) -> None:
        questions = _form_with(_varchar("note"))
        fetch_mock = AsyncMock(return_value={})

        with patch("src.conversation.reuse.fetch_sanitized_autofill", new=fetch_mock):
            await reuse.sanitize_for_autofill({}, questions)

        assert fetch_mock.call_args.kwargs["questions"][0]["choices"] == []

    async def test_raw_answer_passed_through_unchanged_as_the_answer_kwarg(self) -> None:
        questions = _form_with(_varchar("note"))
        raw = {"note": "hello", "task_id": "t-1"}
        fetch_mock = AsyncMock(return_value={})

        with patch("src.conversation.reuse.fetch_sanitized_autofill", new=fetch_mock):
            await reuse.sanitize_for_autofill(raw, questions)

        # Go, not this module, decides what to drop -- the raw dict goes
        # over the wire exactly as received.
        assert fetch_mock.call_args.kwargs["answer"] == raw


class TestBuildAnswerRows:
    def test_free_text_field_becomes_a_text_only_answer(self) -> None:
        questions = _form_with(_varchar("note"))
        conversation_id = uuid.uuid4()

        rows = reuse.build_answer_rows(conversation_id, {"note": "hello"}, questions)

        assert len(rows) == 1
        row = rows[0]
        assert row.conversation_id == conversation_id
        assert row.question_id == questions[0].question_id
        assert row.answer == {"text": "hello"}
        assert row.source == AnswerSource.GUIDED_FLOW

    def test_option_field_resolves_id_to_its_current_label(self) -> None:
        choice_id = str(uuid.uuid4())
        questions = _form_with(_option("fertilizer_id", [choice_id]))

        rows = reuse.build_answer_rows(uuid.uuid4(), {"fertilizer_id": choice_id}, questions)

        assert len(rows) == 1
        assert rows[0].answer == {"text": f"label-{choice_id}", "value": choice_id}

    def test_field_with_no_matching_question_is_skipped(self) -> None:
        questions = _form_with(_varchar("note"))

        rows = reuse.build_answer_rows(uuid.uuid4(), {"retired_field": "x"}, questions)

        assert rows == []

    def test_multiple_fields_each_get_their_own_row(self) -> None:
        choice_id = str(uuid.uuid4())
        questions = _form_with(
            _varchar("note", sort_order=0),
            _option("fertilizer_id", [choice_id], sort_order=1),
        )

        rows = reuse.build_answer_rows(
            uuid.uuid4(), {"note": "ok", "fertilizer_id": choice_id}, questions
        )

        assert len(rows) == 2
        question_ids = {row.question_id for row in rows}
        assert question_ids == {q.question_id for q in questions}


class TestSanitizeThenBuildIntegration:
    async def test_full_pipeline_from_go_response_to_answer_rows(self) -> None:
        keep_option_id = str(uuid.uuid4())
        questions = _form_with(
            _varchar("note", sort_order=0),
            _option("fertilizer_id", [keep_option_id], sort_order=1),
        )
        # What Go would actually return after dropping task_id/farm_activity_id
        # and the stale fertilizer_id itself -- this test's job is just
        # confirming build_answer_rows handles that shape correctly, not
        # re-testing Go's own filtering rules.
        go_response = {"note": "sprayed at dawn"}

        with patch(
            "src.conversation.reuse.fetch_sanitized_autofill",
            new=AsyncMock(return_value=go_response),
        ):
            sanitized = await reuse.sanitize_for_autofill(
                {
                    "task_id": str(uuid.uuid4()),
                    "farm_activity_id": str(uuid.uuid4()),
                    "note": "sprayed at dawn",
                    "fertilizer_id": "stale",
                },
                questions,
            )
        rows = reuse.build_answer_rows(uuid.uuid4(), sanitized, questions)

        assert len(rows) == 1
        assert rows[0].answer == {"text": "sprayed at dawn"}
