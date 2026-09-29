"""HTTP-triggered entry points for the job bodies in src/reminders and
src/conversation/jobs.

Why this exists: src/reminders/scheduler.py's AsyncIOScheduler only fires its
interval/cron triggers while a process stays alive continuously between
runs. That's true on a persistent host (Render, Fly, a VM, `uvicorn` run
locally), but NOT on Vercel (or any serverless platform) -- a serverless
function is frozen the instant it finishes handling a request and only
resumes on the next one, so APScheduler's own timers never get a chance to
fire on schedule; live-caught 2026-09: Vercel's logs showed nothing but
"Run time of job ... was missed by ..." warnings, never an actual on-time
run, because every wake-up happened to arrive well after the job's
next_run_time had already passed.

These endpoints sidestep that entirely: something OUTSIDE the process sends
a request on a real schedule, and that request IS the trigger -- no
in-process timer required. Both endpoints call the exact same job bodies
scheduler.py uses, so behavior (idempotency, Bangkok-time handling, etc.)
is identical either way. On a persistent host, scheduler.py's own
scheduling still works fine and these are just unused extra entry points --
harmless either way.

What actually calls these here: cron-job.org, sending GET requests with an
`Authorization: Bearer <CRON_SECRET>` header (configured on cron-job.org
itself, not in this repo). .github/workflows/cron-reminders.yml and
cron-pause-idle-conversations.yml no longer have a `schedule:` -- they are
kept as `workflow_dispatch` only, to trigger the same endpoints by hand.
Not Vercel Cron -- Vercel's own Cron Jobs are plan-gated to at most once a
day on the Hobby tier (a deployment with a tighter vercel.json `crons`
entry flat-out fails to deploy), which is too coarse for the reminders
check. Vercel Cron becomes a viable *alternative* to cron-job.org only on a
paid plan; either way still points at these same two endpoints, so nothing
here would need to change.
"""

from fastapi import APIRouter, Depends

from src.conversation.jobs import pause_idle_conversations
from src.internal.dependencies import require_cron_secret
from src.reminders.jobs import check_and_send_reminders

router = APIRouter(
    prefix="/internal/cron",
    tags=["internal"],
    dependencies=[Depends(require_cron_secret)],
)


@router.get("/reminders")
async def run_reminders() -> dict[str, str]:
    """Wire to a schedule that runs at least as often as the tightest
    reminder_schedule.time_of_day granularity you need -- the job itself
    only actually pushes when something is due AND not already logged
    today (src/reminders/queries.py), so calling this more often than
    necessary is harmless, just a few no-op DB reads.
    """
    await check_and_send_reminders()
    return {"status": "ok"}


@router.get("/pause-idle-conversations")
async def run_pause_idle_conversations() -> dict[str, str]:
    """Wire to fire once daily at 22:00 Asia/Bangkok = 15:00 UTC -- the
    external scheduler is what decides *when*, so its job must be set to
    22:00 Asia/Bangkok (or 15:00 if its timezone is UTC); this endpoint
    itself does no time-of-day check of its own.
    """
    await pause_idle_conversations()
    return {"status": "ok"}
