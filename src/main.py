from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import settings
from src.exceptions import ServiceException
from src.line.router import router as line_router
from src.logging_config import configure_logging
from src.notifications.router import router as notifications_router
from src.reminders.scheduler import configure_jobs, scheduler
from src.request_id import request_id_middleware

# X-2e: must run before the app is built, and before uvicorn serves its
# first request, so every log line carries the request ID (see
# src/logging_config.py for why this has to happen after uvicorn's own
# logging setup, which it does -- uvicorn imports this module).
configure_logging()

if not settings.ENVIRONMENT.is_deployed:
    from src.conversation.router import router as conversation_test_router


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
# X-2e: added last so it's the outermost middleware (Starlette executes
# middleware in reverse registration order) -- the request ID needs to be
# set before CORS or anything else in the stack runs.
app.add_middleware(BaseHTTPMiddleware, dispatch=request_id_middleware)


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
