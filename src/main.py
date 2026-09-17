import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.config import settings
from src.exceptions import ServiceException
from src.internal.router import router as internal_router
from src.line.router import router as line_router
from src.notifications.router import router as notifications_router
from src.reminders.router import router as reminders_router
from src.reminders.scheduler import configure_jobs, scheduler

if not settings.ENVIRONMENT.is_deployed:
    from src.conversation.router import router as conversation_test_router

# Vercel sets this env var on every deployment automatically. A serverless
# function there is frozen between requests -- APScheduler's own in-process
# timers never get a chance to fire on schedule (live-caught 2026-09: nothing
# but "Run time of job ... was missed by ..." warnings, no on-time runs
# ever). Starting it there anyway is pure noise: it can't work, and the
# misfire warnings are misleading. src/internal/router.py's endpoints are
# the real trigger path on Vercel (wired to Vercel Cron / an external
# scheduler instead); on a persistent host (Render, a VM, local `uvicorn`)
# this is unset and the scheduler starts exactly as before.
_IS_SERVERLESS = bool(os.environ.get("VERCEL"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if not _IS_SERVERLESS:
        configure_jobs()
        scheduler.start()
    yield
    if not _IS_SERVERLESS:
        scheduler.shutdown()


app = FastAPI(
    title="Cocoa Chatbot Service",
    version=settings.APP_VERSION,
    lifespan=lifespan,
    openapi_url="/openapi.json" if not settings.ENVIRONMENT.is_deployed else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ServiceException)
async def service_exception_handler(request: Request, exc: ServiceException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": settings.APP_VERSION}


app.include_router(line_router)
app.include_router(notifications_router)
app.include_router(reminders_router)
app.include_router(internal_router)

if not settings.ENVIRONMENT.is_deployed:
    app.include_router(conversation_test_router)
