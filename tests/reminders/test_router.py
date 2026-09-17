from datetime import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from httpx import AsyncClient

# matches tests/conftest.py's CHATBOT_SERVICE_KEY
_KEY = "test-service-key"


async def test_missing_service_key_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/service/reminders",
        json={"task_id": str(uuid4()), "time_of_day": "17:00", "created_by": str(uuid4())},
    )
    assert resp.status_code == 401


async def test_wrong_service_key_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/service/reminders",
        json={"task_id": str(uuid4()), "time_of_day": "17:00", "created_by": str(uuid4())},
        headers={"X-Service-Key": "not-the-key"},
    )
    assert resp.status_code == 401


async def test_valid_request_creates_a_daily_active_schedule(client: AsyncClient) -> None:
    task_id, created_by = uuid4(), uuid4()
    schedule_id = uuid4()
    with patch(
        "src.reminders.router.create_reminder_schedule",
        new=AsyncMock(return_value=schedule_id),
    ) as create:
        resp = await client.post(
            "/service/reminders",
            json={"task_id": str(task_id), "time_of_day": "17:00", "created_by": str(created_by)},
            headers={"X-Service-Key": _KEY},
        )

    assert resp.status_code == 200
    create.assert_awaited_once()
    assert create.await_args.kwargs["task_id"] == task_id
    assert create.await_args.kwargs["time_of_day"] == time(17, 0)
    assert create.await_args.kwargs["created_by"] == created_by

    body = resp.json()
    assert body == {
        "schedule_id": str(schedule_id),
        "task_id": str(task_id),
        "cadence": "DAILY",
        "time_of_day": "17:00:00",
        "is_active": True,
        "created_by": str(created_by),
    }


async def test_request_has_no_way_to_specify_a_cadence(client: AsyncClient) -> None:
    """There is deliberately no `cadence` field to send -- see
    ReminderScheduleCreate's own docstring. Sending one anyway is just
    ignored (Pydantic drops unknown fields by default), not an error.
    """
    task_id, created_by = uuid4(), uuid4()
    with patch(
        "src.reminders.router.create_reminder_schedule",
        new=AsyncMock(return_value=uuid4()),
    ) as create:
        resp = await client.post(
            "/service/reminders",
            json={
                "task_id": str(task_id),
                "time_of_day": "17:00",
                "created_by": str(created_by),
                "cadence": "WEEKLY",
            },
            headers={"X-Service-Key": _KEY},
        )

    assert resp.status_code == 200
    assert resp.json()["cadence"] == "DAILY"
    create.assert_awaited_once()
    assert "cadence" not in create.await_args.kwargs
