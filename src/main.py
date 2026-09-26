from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.config import settings
from src.exceptions import ServiceException
from src.line.router import router as line_router
from src.notifications.router import router as notifications_router
from src.reminders.scheduler import configure_jobs, scheduler

if not settings.ENVIRONMENT.is_deployed:
    from src.conversation.router import router as conversation_test_router

# X-2d: error tracking. An empty SENTRY_DSN (the default) disables the SDK
# entirely -- no error, no events sent -- safe in local dev/CI.
sentry_sdk.init(
    dsn=settings.SENTRY_DSN,
    environment=settings.SENTRY_ENVIRONMENT,
    traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_jobs()
    scheduler.start()
    yield
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

if not settings.ENVIRONMENT.is_deployed:
    app.include_router(conversation_test_router)
