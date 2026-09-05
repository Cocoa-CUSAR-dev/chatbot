import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.line.parent_picker import FIELD_NAME, PROMPT, ParentOption, choices_for, kind_for_handler


def test_kind_for_handler_maps_the_5_previously_blocked_handlers() -> None:
    assert kind_for_handler("farm_activity_fertilizer") == "farm_activity"
    assert kind_for_handler("farm_activity_chemical") == "farm_activity"
    assert kind_for_handler("harvest_grade_detail") == "harvest"
    assert kind_for_handler("fermentation_batch") == "batch"
    assert kind_for_handler("drying_batch") == "batch"


def test_kind_for_handler_returns_none_for_an_unrelated_handler() -> None:
    assert kind_for_handler("processing_record") is None


def test_every_kind_has_a_field_name_and_prompt() -> None:
    for kind in ("farm_activity", "harvest", "batch"):
        assert kind in FIELD_NAME
        assert kind in PROMPT


def _mock_session(rows: list) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(return_value=rows)
    return session


async def test_choices_for_farm_activity_builds_labelled_options() -> None:
    rows = [
        SimpleNamespace(
            id=uuid.uuid4(), created_at=datetime(2026, 8, 1), farm_name="สวนทดสอบ", type_name="ใส่ปุ๋ย"
        )
    ]
    session = _mock_session(rows)

    result = await choices_for(session, "farm_activity", uuid.uuid4())

    assert result == [ParentOption(id=str(rows[0].id), label="ใส่ปุ๋ย 01/08/2026 — สวนทดสอบ")]


async def test_choices_for_batch_builds_labelled_options() -> None:
    rows = [SimpleNamespace(id=uuid.uuid4(), created_at=datetime(2026, 8, 1), place="สถานี A")]
    session = _mock_session(rows)

    result = await choices_for(session, "batch", uuid.uuid4())

    assert result == [ParentOption(id=str(rows[0].id), label="แบทช์ 01/08/2026 — สถานี A")]
