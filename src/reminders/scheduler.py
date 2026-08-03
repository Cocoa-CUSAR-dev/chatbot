"""One AsyncIOScheduler instance for both jobs this service needs
(ADR 0003/0006): the reminder push check and the LLM-retry cron
(src/llm's late-extraction pass, ADR 0004). Started/stopped from main.py's
lifespan.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.reminders.jobs import check_and_send_reminders

scheduler = AsyncIOScheduler()


def configure_jobs() -> None:
    scheduler.add_job(
        check_and_send_reminders,
        trigger="interval",
        minutes=15,
        id="check_and_send_reminders",
        replace_existing=True,
    )
