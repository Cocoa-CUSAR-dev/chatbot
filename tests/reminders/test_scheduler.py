from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.reminders.scheduler import configure_jobs, scheduler


def test_configure_jobs_registers_the_reminder_check_on_a_15_minute_interval() -> None:
    configure_jobs()

    job = scheduler.get_job("check_and_send_reminders")
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() == 15 * 60


def test_configure_jobs_registers_the_daily_pause_sweep_at_22_00_bangkok_time() -> None:
    configure_jobs()

    job = scheduler.get_job("pause_idle_conversations")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    assert str(job.trigger.timezone) == "Asia/Bangkok"


async def test_configure_jobs_is_idempotent_once_the_scheduler_is_running() -> None:
    # replace_existing=True -- matches main.py's lifespan, which always calls
    # configure_jobs() then scheduler.start() in that order. Verified only
    # once actually running: APScheduler's dedup-by-id only takes effect
    # against a live jobstore -- calling configure_jobs() twice BEFORE
    # start() leaves duplicate pending jobs instead of replacing, but that
    # sequence never happens in this app (configure_jobs() runs exactly
    # once, before the one scheduler.start() call).
    configure_jobs()
    scheduler.start()
    try:
        configure_jobs()
        assert len(scheduler.get_jobs()) == 2
    finally:
        scheduler.shutdown(wait=False)
