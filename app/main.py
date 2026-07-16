from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import campaigns, ws
from app.config import settings
from app.db import build_engine
from app.models import Base
from app.ratelimit import SlidingWindowLimiter


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine, sessionmaker = build_engine(settings.database_url)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.create_limiter = SlidingWindowLimiter(settings.create_limit_per_hour, 3600)
    # Real deploys own their schema via `alembic upgrade head`; auto_create is a
    # tests/dev convenience.
    if settings.auto_create:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="Prophecy Companion Server", version="0.1.0", lifespan=lifespan)
app.include_router(campaigns.router)
app.include_router(ws.router)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
