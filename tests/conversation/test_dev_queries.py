import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.conversation.dev_queries import (
    _load_choices,
    list_testable_forms,
    load_form_detail,
    resolve_test_user_id,
)
from src.conversation.exceptions import ConversationNotFound
from src.forms.exceptions import FormNotFound


def _rows(mappings: list[dict]) -> list[SimpleNamespace]:
    return [SimpleNamespace(_mapping=m) for m in mappings]


def _session(*execute_results) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute_results)
    return session


async def test_list_testable_forms_maps_rows_into_form_summaries() -> None:
    task_id, task_form_id = uuid.uuid4(), uuid.uuid4()
    session = _session(
        _rows(
            [
                {
                    "task_id": task_id,
                    "task_form_id": task_form_id,
                    "title": "งานทดสอบ",
                    "handler": "harvest",
                }
            ]
        )
    )

    forms = await list_testable_forms(session)

    assert len(forms) == 1
    assert forms[0].task_id == task_id
    assert forms[0].handler == "harvest"


async def test_load_choices_rejects_a_field_name_not_shaped_like_an_id() -> None:
    session = _session()  # never queried -- rejected before any SQL runs

    result = await _load_choices(session, "not_an_id_field")

    assert result == []
    session.execute.assert_not_called()


async def test_load_choices_builds_id_name_pairs_for_a_valid_field_name() -> None:
    # _load_choices iterates rows via .id/.name directly (a raw SQL result
    # object), NOT ._mapping like the other queries in this module.
    session = _session([SimpleNamespace(id="f-1", name="สวนทดสอบ")])

    result = await _load_choices(session, "farm_id")

    assert result == [{"id": "f-1", "name": "สวนทดสอบ"}]


async def test_load_form_detail_raises_form_not_found_when_no_questions_exist() -> None:
    task_form_id = uuid.uuid4()
    session = _session(_rows([]))

    with pytest.raises(FormNotFound):
        await load_form_detail(session, task_form_id)


async def test_load_form_detail_attaches_choices_to_option_questions() -> None:
    task_form_id = uuid.uuid4()
    question_id = uuid.uuid4()
    session = _session(
        _rows(
            [
                {
                    "question_id": question_id,
                    "label": "ฟาร์ม",
                    "field_name": "farm_id",
                    "input_type": "OPTION",
                    "is_mandatory": True,
                    "sort_order": 0,
                    "validation_rule": None,
                }
            ]
        ),
        [SimpleNamespace(id="f-1", name="สวนทดสอบ")],  # the nested _load_choices call
    )

    form = await load_form_detail(session, task_form_id)

    question = form.sections[0]["questions"][0]
    assert question["choices"] == [{"id": "f-1", "name": "สวนทดสอบ"}]


async def test_load_form_detail_converts_a_jsonb_validation_rule_to_snake_case() -> None:
    """SQLAlchemy's asyncpg dialect normally auto-decodes jsonb to a dict --
    this is that path (see load_form_detail's own comment on the dict case).
    """
    task_form_id = uuid.uuid4()
    session = _session(
        _rows(
            [
                {
                    "question_id": uuid.uuid4(),
                    "label": "จำนวนพัดลม",
                    "field_name": "fan_count",
                    "input_type": "VARCHAR",
                    "is_mandatory": True,
                    "sort_order": 0,
                    "validation_rule": {"type": "INT", "maxLength": 50, "errorMessage": "0-50"},
                }
            ]
        )
    )

    form = await load_form_detail(session, task_form_id)

    rule = form.sections[0]["questions"][0]["validation_rule"]
    assert rule == {"type": "INT", "max_length": 50, "error_message": "0-50"}


async def test_load_form_detail_parses_a_raw_json_string_validation_rule() -> None:
    """Fallback path if jsonb ever arrives as raw text instead of an
    already-decoded dict (see load_form_detail's own comment on this case).
    """
    task_form_id = uuid.uuid4()
    session = _session(
        _rows(
            [
                {
                    "question_id": uuid.uuid4(),
                    "label": "จำนวนพัดลม",
                    "field_name": "fan_count",
                    "input_type": "VARCHAR",
                    "is_mandatory": True,
                    "sort_order": 0,
                    "validation_rule": json.dumps({"type": "INT", "maxLength": 50}),
                }
            ]
        )
    )

    form = await load_form_detail(session, task_form_id)

    rule = form.sections[0]["questions"][0]["validation_rule"]
    assert rule == {"type": "INT", "max_length": 50}


async def test_resolve_test_user_id_returns_the_seed_users_id() -> None:
    user_id = uuid.uuid4()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user_id
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    assert await resolve_test_user_id(session) == user_id


async def test_resolve_test_user_id_raises_when_seed_user_is_missing() -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    with pytest.raises(ConversationNotFound):
        await resolve_test_user_id(session)
