from datetime import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from httpx import AsyncClient

from src.reminders.schemas import RecipientRule

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
    with (
        patch(
            "src.reminders.router.create_reminder_schedule",
            new=AsyncMock(return_value=schedule_id),
        ) as create,
        patch(
            "src.reminders.router.list_schedules_for_task",
            new=AsyncMock(
                return_value=[
                    _row(schedule_id, task_id, time_of_day=time(17, 0), created_by=created_by)
                ]
            ),
        ),
    ):
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
        "recipients": [],
    }


async def test_request_has_no_way_to_specify_a_cadence(client: AsyncClient) -> None:
    """There is deliberately no `cadence` field to send -- see
    ReminderScheduleCreate's own docstring. Sending one anyway is just
    ignored (Pydantic drops unknown fields by default), not an error.
    """
    task_id, created_by = uuid4(), uuid4()
    schedule_id = uuid4()
    with (
        patch(
            "src.reminders.router.create_reminder_schedule",
            new=AsyncMock(return_value=schedule_id),
        ) as create,
        patch(
            "src.reminders.router.list_schedules_for_task",
            new=AsyncMock(return_value=[_row(schedule_id, task_id, created_by=created_by)]),
        ),
    ):
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


async def test_create_passes_recipient_rules_through(client: AsyncClient) -> None:
    task_id, created_by, role_id, user_id = uuid4(), uuid4(), uuid4(), uuid4()
    schedule_id = uuid4()
    persisted_recipients = [
        RecipientRule(type="ROLE", id=role_id),
        RecipientRule(type="USER", id=user_id),
    ]
    with (
        patch(
            "src.reminders.router.create_reminder_schedule",
            new=AsyncMock(return_value=schedule_id),
        ) as create,
        patch(
            "src.reminders.router.list_schedules_for_task",
            new=AsyncMock(
                return_value=[
                    _row(
                        schedule_id,
                        task_id,
                        created_by=created_by,
                        recipients=persisted_recipients,
                    )
                ]
            ),
        ),
    ):
        resp = await client.post(
            "/service/reminders",
            json={
                "task_id": str(task_id),
                "time_of_day": "17:00",
                "created_by": str(created_by),
                "recipients": [
                    {"type": "ROLE", "id": str(role_id)},
                    {"type": "USER", "id": str(user_id)},
                ],
            },
            headers={"X-Service-Key": _KEY},
        )

    assert resp.status_code == 200
    rules = create.await_args.kwargs["recipients"]
    assert [(r.type, r.id) for r in rules] == [("ROLE", role_id), ("USER", user_id)]
    assert resp.json()["recipients"] == [
        {"type": "ROLE", "id": str(role_id)},
        {"type": "USER", "id": str(user_id)},
    ]


async def test_unknown_recipient_type_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/service/reminders",
        json={
            "task_id": str(uuid4()),
            "time_of_day": "17:00",
            "created_by": str(uuid4()),
            "recipients": [{"type": "EVERYONE", "id": str(uuid4())}],
        },
        headers={"X-Service-Key": _KEY},
    )
    assert resp.status_code == 422


async def test_list_and_update_require_the_service_key(client: AsyncClient) -> None:
    assert (
        await client.get("/service/reminders", params={"task_id": str(uuid4())})
    ).status_code == 401
    assert (await client.patch(f"/service/reminders/{uuid4()}", json={})).status_code == 401


def _row(schedule_id, task_id, **overrides):  # type: ignore[no-untyped-def]
    from src.reminders.queries import ScheduleRow

    fields = {
        "schedule_id": schedule_id,
        "task_id": task_id,
        "cadence": "DAILY",
        "time_of_day": time(9, 0),
        "is_active": True,
        "created_by": uuid4(),
        "recipients": [],
    }
    fields.update(overrides)
    return ScheduleRow(**fields)


async def test_list_returns_the_tasks_schedules(client: AsyncClient) -> None:
    schedule_id, task_id = uuid4(), uuid4()
    with patch(
        "src.reminders.router.list_schedules_for_task",
        new=AsyncMock(return_value=[_row(schedule_id, task_id)]),
    ) as lst:
        resp = await client.get(
            "/service/reminders", params={"task_id": str(task_id)}, headers={"X-Service-Key": _KEY}
        )

    assert resp.status_code == 200
    assert lst.await_args.args[1] == task_id
    assert resp.json()[0]["schedule_id"] == str(schedule_id)


async def test_patch_updates_only_the_fields_sent(client: AsyncClient) -> None:
    schedule_id, task_id = uuid4(), uuid4()
    with patch(
        "src.reminders.router.update_reminder_schedule",
        new=AsyncMock(return_value=_row(schedule_id, task_id, is_active=False)),
    ) as upd:
        resp = await client.patch(
            f"/service/reminders/{schedule_id}",
            json={"is_active": False},
            headers={"X-Service-Key": _KEY},
        )

    assert resp.status_code == 200
    assert upd.await_args.kwargs == {"time_of_day": None, "is_active": False, "recipients": None}
    assert resp.json()["is_active"] is False


async def test_patch_unknown_schedule_is_404(client: AsyncClient) -> None:
    from src.reminders.exceptions import ReminderNotFound

    with patch(
        "src.reminders.router.update_reminder_schedule",
        new=AsyncMock(side_effect=ReminderNotFound()),
    ):
        resp = await client.patch(
            f"/service/reminders/{uuid4()}", json={}, headers={"X-Service-Key": _KEY}
        )
    assert resp.status_code == 404


async def test_bad_reference_is_422(client: AsyncClient) -> None:
    from src.reminders.exceptions import InvalidReminderReference

    with patch(
        "src.reminders.router.create_reminder_schedule",
        new=AsyncMock(side_effect=InvalidReminderReference()),
    ):
        resp = await client.post(
            "/service/reminders",
            json={"task_id": str(uuid4()), "time_of_day": "17:00", "created_by": str(uuid4())},
            headers={"X-Service-Key": _KEY},
        )
    assert resp.status_code == 422
