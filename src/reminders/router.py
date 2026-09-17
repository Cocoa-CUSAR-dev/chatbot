"""HTTP surface for creating a notify.reminder_schedule row -- the write
side of reminders that isn't APScheduler-triggered (see scheduler.py/jobs.py
for the delivery side). The only caller is web-backend's ChatbotClient, from
FormService.createForm -- reminder settings are set once, at form-creation
time (web-app's create-form page), not managed as a standalone resource.
There is deliberately no list/update/delete endpoint here: is_active only
ever flips off automatically (jobs.py, once nobody owes the task anymore),
never through this API.

Gated by the same X-Service-Key trust model as src/notifications (reusing
its dependency directly rather than a second copy of the same check).
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_session
from src.notifications.dependencies import require_service_key
from src.reminders.queries import create_reminder_schedule
from src.reminders.schemas import ReminderScheduleCreate, ReminderScheduleResponse

router = APIRouter(
    prefix="/service/reminders",
    tags=["reminders"],
    dependencies=[Depends(require_service_key)],
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


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
    )
    return ReminderScheduleResponse(
        schedule_id=schedule_id,
        task_id=body.task_id,
        cadence="DAILY",
        time_of_day=body.time_of_day,
        is_active=True,
        created_by=body.created_by,
    )
