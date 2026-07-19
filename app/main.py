import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import campaigns, ws
from app.config import settings
from app.db import build_engine
from app.logging_setup import configure_logging, event
from app.models import Base
from app.ratelimit import SlidingWindowLimiter

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # First thing in the process lifecycle: everything below may log.
    configure_logging(settings.log_level)
    engine, sessionmaker = build_engine(settings.database_url)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.create_limiter = SlidingWindowLimiter(settings.create_limit_per_hour, 3600)
    # Real deploys own their schema via `alembic upgrade head`; auto_create is a
    # tests/dev convenience.
    if settings.auto_create:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    log.info(event("server_started", level=settings.log_level, auto_create=settings.auto_create))
    try:
        yield
    finally:
        log.info(event("server_stopping"))
        await engine.dispose()


app = FastAPI(title="Prophecy Companion Server", version="0.1.0", lifespan=lifespan)
app.include_router(campaigns.router)
app.include_router(ws.router)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: an unhandled error must leave a traceback in the log with
    the request that caused it, not just a bare 500 on the wire."""
    log.exception(event("unhandled_error", method=request.method, path=request.url.path))
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
