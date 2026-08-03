"""ORM models for notify.reminder_schedule / notify.reminder_log -- schema
owned by the `database` repo's Flyway migrations (ADR 0005), not created here.
"""

import uuid
from datetime import datetime, time

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base
from src.models import uuid_pk


class ReminderSchedule(Base):
    __tablename__ = "reminder_schedule"
    __table_args__ = {"schema": "notify"}

    schedule_id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("form.task.task_id"))
    cadence: Mapped[str] = mapped_column(String)
    time_of_day: Mapped[time] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth.user_account.user_id"))


class ReminderLog(Base):
    """The idempotency source of truth for the periodic reminder check --
    NOT APScheduler's in-memory job state, which doesn't survive a restart
    (ADR 0006).
    """

    __tablename__ = "reminder_log"
    __table_args__ = {"schema": "notify"}

    log_id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth.user_account.user_id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("form.task.task_id"))
    sent_at: Mapped[datetime] = mapped_column()
    channel: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
