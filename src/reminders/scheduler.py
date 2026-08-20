"""One AsyncIOScheduler instance for every job this service needs
(ADR 0003/0006): the reminder push check, the LLM-retry cron (src/llm's
late-extraction pass, ADR 0004), and the daily conversation auto-pause
sweep (US2-3). Started/stopped from main.py's lifespan.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.conversation.jobs import pause_idle_conversations
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
    scheduler.add_job(
        pause_idle_conversations,
        trigger=CronTrigger(hour=22, minute=0, timezone="Asia/Bangkok"),
        id="pause_idle_conversations",
        replace_existing=True,
    )
