import uuid

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
    def test_drops_task_id_and_stale_parent_fields_unconditionally(self) -> None:
        questions = _form_with(_varchar("note"))
        raw = {
            "task_id": str(uuid.uuid4()),
            "farm_activity_id": str(uuid.uuid4()),
            "harvest_id": str(uuid.uuid4()),
            "batch_id": str(uuid.uuid4()),
            "note": "hello",
        }

        sanitized = reuse.sanitize_for_autofill(raw, questions)

        assert sanitized == {"note": "hello"}

    def test_free_text_field_passes_through_unchanged(self) -> None:
        questions = _form_with(_varchar("note"))

        sanitized = reuse.sanitize_for_autofill({"note": "ปุ๋ยอินทรีย์"}, questions)

        assert sanitized == {"note": "ปุ๋ยอินทรีย์"}

    def test_option_value_still_present_in_current_form_is_kept(self) -> None:
        keep_id = str(uuid.uuid4())
        questions = _form_with(_option("fertilizer_id", [keep_id]))

        sanitized = reuse.sanitize_for_autofill({"fertilizer_id": keep_id}, questions)

        assert sanitized == {"fertilizer_id": keep_id}

    def test_option_value_no_longer_in_current_form_is_dropped(self) -> None:
        # e.g. that fertilizer was deleted/renamed since the last submission --
        # its old id no longer resolves to any real choice on this form.
        stale_id = str(uuid.uuid4())
        current_id = str(uuid.uuid4())
        questions = _form_with(_option("fertilizer_id", [current_id]))

        sanitized = reuse.sanitize_for_autofill({"fertilizer_id": stale_id}, questions)

        assert sanitized == {}

    def test_boolean_value_matching_synthesized_choice_is_kept(self) -> None:
        questions = _form_with(_boolean("is_quality_damage"))

        sanitized = reuse.sanitize_for_autofill({"is_quality_damage": "true"}, questions)

        assert sanitized == {"is_quality_damage": "true"}

    def test_field_name_not_on_current_form_at_all_passes_through(self) -> None:
        # sanitize_for_autofill only knows to reject OPTION/BOOLEAN mismatches
        # -- an unrecognized free-text-shaped field name is left for
        # build_answer_rows to drop, since that's where the current form's
        # question set is actually looked up by field_name.
        questions = _form_with(_varchar("note"))

        sanitized = reuse.sanitize_for_autofill({"retired_field": "x"}, questions)

        assert sanitized == {"retired_field": "x"}

    def test_mixed_answer_only_drops_what_it_should(self) -> None:
        keep_option_id = str(uuid.uuid4())
        stale_option_id = str(uuid.uuid4())
        questions = _form_with(
            _varchar("note", sort_order=0),
            _option("fertilizer_id", [keep_option_id], sort_order=1),
        )
        raw = {
            "task_id": str(uuid.uuid4()),
            "batch_id": str(uuid.uuid4()),
            "note": "ok",
            "fertilizer_id": stale_option_id,
        }

        sanitized = reuse.sanitize_for_autofill(raw, questions)

        assert sanitized == {"note": "ok"}


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
    def test_full_pipeline_drops_stale_option_and_keeps_the_rest(self) -> None:
        keep_option_id = str(uuid.uuid4())
        stale_option_id = str(uuid.uuid4())
        questions = _form_with(
            _varchar("note", sort_order=0),
            _option("fertilizer_id", [keep_option_id], sort_order=1),
        )
        raw = {
            "task_id": str(uuid.uuid4()),
            "farm_activity_id": str(uuid.uuid4()),
            "note": "sprayed at dawn",
            "fertilizer_id": stale_option_id,
        }

        sanitized = reuse.sanitize_for_autofill(raw, questions)
        rows = reuse.build_answer_rows(uuid.uuid4(), sanitized, questions)

        assert len(rows) == 1
        assert rows[0].answer == {"text": "sprayed at dawn"}
