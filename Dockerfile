FROM python:3.14-slim

# uv for dependency install.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install deps first (cached) from the lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# SQLite lives on a mounted volume so data survives container replacement.
ENV PCS_DATABASE_URL=sqlite+aiosqlite:////data/prophecy.db
RUN useradd -m appuser && mkdir -p /data && chown appuser /data
USER appuser
VOLUME /data
EXPOSE 8000

# Slim image has no curl; probe /healthz with stdlib urllib instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["uv", "run", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

# Apply migrations, then serve.
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn app.main:app --host 0.0.0.0 --port 8000"]
