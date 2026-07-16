from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration. Every field is overridable via a `PCS_`-prefixed env
    var (e.g. `PCS_DATABASE_URL`) or a local `.env` file."""

    model_config = SettingsConfigDict(env_prefix="PCS_", env_file=".env", extra="ignore")

    # SQLite for self-hosting; swap to a postgresql+asyncpg URL for a big instance.
    database_url: str = "sqlite+aiosqlite:///./data/prophecy.db"
    # Create tables from the models on startup instead of via Alembic. Meant for
    # tests/dev only — real deploys run `alembic upgrade head`.
    auto_create: bool = False

    host: str = "0.0.0.0"
    port: int = 8000

    # Join-code length in Crockford base32 chars (8 ≈ 40 bits).
    code_length: int = 8
    # Reject oversized WS frames before parsing.
    max_message_bytes: int = 64 * 1024
    # Refuse NEW roster slots beyond this many per campaign (updating an existing
    # slot always passes). Guards against a code-holder looping random charIds to
    # fill the disk; generous vs a real table of 4-6 players.
    max_projections_per_campaign: int = 16


settings = Settings()
