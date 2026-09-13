import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.line.temp_task_picker import START_KEYWORDS, list_pending_tasks


def test_start_keywords_are_case_normalized_before_matching() -> None:
    # router.py matches via message.text.strip().lower() in START_KEYWORDS --
    # this only holds if every keyword here is already lowercase itself.
    assert all(keyword == keyword.lower() for keyword in START_KEYWORDS)
    assert "เริ่ม" in START_KEYWORDS
    assert "start" in START_KEYWORDS


async def test_list_pending_tasks_maps_rows_into_pending_task_dataclasses() -> None:
    task_id, task_form_id = uuid.uuid4(), uuid.uuid4()
    row = SimpleNamespace(
        _mapping={
            "task_id": task_id,
            "task_form_id": task_form_id,
            "title": "งานทดสอบ",
            "handler": "notes",
            "has_conversation": True,
        }
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=[row])

    tasks = await list_pending_tasks(session, uuid.uuid4())

    assert len(tasks) == 1
    assert tasks[0].task_id == task_id
    assert tasks[0].title == "งานทดสอบ"
    assert tasks[0].has_conversation is True
