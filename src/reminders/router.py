"""HTTP surface for managing notify.reminder_schedule rows and who they are
aimed at (notify.reminder_recipient) -- the write side of reminders that
isn't APScheduler-triggered (see scheduler.py/jobs.py for the delivery
side). The only caller is web-backend (create-form page + the per-task
reminder settings), never web-app directly.

Deliberately no DELETE: a schedule is switched off with PATCH is_active=false
(it also turns itself off once nobody owes the task, see jobs.py) so its
history in notify.reminder_log keeps a schedule to point at.

Gated by the same X-Service-Key trust model as src/notifications (reusing
its dependency directly rather than a second copy of the same check).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_session
from src.notifications.dependencies import require_service_key
from src.reminders.queries import (
    ScheduleRow,
    create_reminder_schedule,
    list_schedules_for_task,
    update_reminder_schedule,
)
from src.reminders.schemas import (
    ReminderScheduleCreate,
    ReminderScheduleResponse,
    ReminderScheduleUpdate,
)

router = APIRouter(
    prefix="/service/reminders",
    tags=["reminders"],
    dependencies=[Depends(require_service_key)],
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _to_response(row: ScheduleRow) -> ReminderScheduleResponse:
    return ReminderScheduleResponse(
        schedule_id=row.schedule_id,
        task_id=row.task_id,
        cadence=row.cadence,
        time_of_day=row.time_of_day,
        is_active=row.is_active,
        created_by=row.created_by,
        recipients=row.recipients,
    )


@router.post("", response_model=ReminderScheduleResponse)
async def create_reminder(
    body: ReminderScheduleCreate,
    session: SessionDep,
) -> ReminderScheduleResponse:
    schedule_id = await create_reminder_schedule(
        session,
        task_id=body.task_id,
        time_of_day=body.time_of_day,
        created_by=body.created_by,
        recipients=body.recipients,
    )
    return ReminderScheduleResponse(
        schedule_id=schedule_id,
        task_id=body.task_id,
        cadence="DAILY",
        time_of_day=body.time_of_day,
        is_active=True,
        created_by=body.created_by,
        recipients=body.recipients,
    )


@router.get("", response_model=list[ReminderScheduleResponse])
async def list_reminders(task_id: UUID, session: SessionDep) -> list[ReminderScheduleResponse]:
    return [_to_response(row) for row in await list_schedules_for_task(session, task_id)]


@router.patch("/{schedule_id}", response_model=ReminderScheduleResponse)
async def update_reminder(
    schedule_id: UUID,
    body: ReminderScheduleUpdate,
    session: SessionDep,
) -> ReminderScheduleResponse:
    row = await update_reminder_schedule(
        session,
        schedule_id,
        time_of_day=body.time_of_day,
        is_active=body.is_active,
        recipients=body.recipients,
    )
    return _to_response(row)
