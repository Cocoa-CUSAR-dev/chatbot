from datetime import time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class RecipientRule(BaseModel):
    """Who a reminder is aimed at: every holder of a role ("ROLE", id is a
    role_id) or exactly one person ("USER", id is a user_id).
    """

    type: Literal["ROLE", "USER"]
    id: UUID


class ReminderScheduleCreate(BaseModel):
    """cadence is deliberately not a field -- see create_reminder_schedule's
    own docstring in queries.py. This only ever represents "remind daily at
    this time for this task". No recipients = remind everyone who owes it.
    """

    task_id: UUID
    time_of_day: time
    created_by: UUID
    recipients: list[RecipientRule] = Field(default_factory=list)


class ReminderScheduleUpdate(BaseModel):
    """Every field optional: only what is sent changes. `recipients`, when
    sent, REPLACES the whole list (an empty list = back to "everyone").
    """

    time_of_day: time | None = None
    is_active: bool | None = None
    recipients: list[RecipientRule] | None = None


class ReminderScheduleResponse(BaseModel):
    schedule_id: UUID
    task_id: UUID
    cadence: str
    time_of_day: time
    is_active: bool
    created_by: UUID
    recipients: list[RecipientRule] = Field(default_factory=list)
