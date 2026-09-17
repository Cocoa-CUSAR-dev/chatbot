from datetime import time
from uuid import UUID

from pydantic import BaseModel


class ReminderScheduleCreate(BaseModel):
    """cadence is deliberately not a field -- see create_reminder_schedule's
    own docstring in queries.py. The only caller today is web-backend's
    ChatbotClient, itself only ever called from FormService.createForm, so
    this only ever represents "remind daily at this time for this task."
    """

    task_id: UUID
    time_of_day: time
    created_by: UUID


class ReminderScheduleResponse(BaseModel):
    schedule_id: UUID
    task_id: UUID
    cadence: str
    time_of_day: time
    is_active: bool
    created_by: UUID
