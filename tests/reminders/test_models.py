from src.reminders.models import ReminderLog, ReminderSchedule


def test_reminder_schedule_table_schema() -> None:
    table = ReminderSchedule.__table__
    assert table.schema == "notify"
    assert table.name == "reminder_schedule"
    assert set(table.c.keys()) == {
        "schedule_id",
        "task_id",
        "cadence",
        "time_of_day",
        "is_active",
        "created_by",
    }
    assert table.c.is_active.default.arg is True


def test_reminder_log_table_schema() -> None:
    table = ReminderLog.__table__
    assert table.schema == "notify"
    assert table.name == "reminder_log"
    assert set(table.c.keys()) == {
        "log_id",
        "user_id",
        "task_id",
        "sent_at",
        "channel",
        "status",
    }
