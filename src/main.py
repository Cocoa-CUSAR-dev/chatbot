from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.config import settings
from src.exceptions import ServiceException
from src.identity.router import router as identity_router
from src.line.router import router as line_router
from src.reminders.scheduler import configure_jobs, scheduler


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
app.include_router(identity_router)
