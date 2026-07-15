# Prophecy Companion Server

Live campaign relay for the [Prophecy companion app](https://github.com/aehru/ProphecyCompanionApp).
A Game Master creates a campaign, players join with a code and share a **minimized,
read-only projection** of their character; the GM sees a live roster. The full
character sheet never leaves the player's device — see the app repo's
`docs/campaign-protocol.md` for the wire contract this implements.

Self-hostable: it's a single service with an embedded SQLite database, so a group
can run it on their own box (`docker compose up`) and keep their data in-house.

## Status — Phase 1

Implemented:

- `POST /campaigns` → `{ campaignId, code }` (stores only a hash of the GM token)
- `DELETE /campaigns/{code}` (GM token required; cascades projections)
- `GET /healthz`
- `WS /ws` — `hello` → `welcome`, GM receives the persisted `roster`, players
  announce `presence`, `ping`/`pong`.

Not yet (Phase 2): `share` / `unshare` / `update` / `remove` — the projection
write path. `share`/`unshare` currently return an `error` frame.

## Stack

Python 3.14 · [uv](https://docs.astral.sh/uv/) · FastAPI · uvicorn ·
SQLAlchemy 2.0 (async) + aiosqlite · Alembic · pydantic v2 · pytest · ruff.

## Develop

```bash
uv sync                         # install (incl. dev tools)
uv run alembic upgrade head     # create/upgrade the SQLite schema
uv run uvicorn app.main:app --reload
uv run pytest                   # tests (use their own throwaway DB)
uv run ruff check .             # lint (incl. annotation rules)
uv run ruff format .            # format
uv run mypy                     # type gate
```

### Typing policy

Type annotations are **enforced on every function** — arguments and returns.
Two tools, deliberately, because they check different things:

- **ruff `ANN`** — that annotations *exist* (`def f(x):` is rejected).
- **mypy** (`disallow_untyped_defs`) — that annotations are *true*
  (`def f(x: int) -> str: return x` is rejected) and that inferred local types
  line up.

Tests don't need Alembic: they build the schema from the models via the
`PCS_AUTO_CREATE` flag against a temp database.

### VS Code

Run `uv sync` once, then reopen the folder — the interpreter is auto-discovered
from `.venv/`. Committed config lives in `.vscode/`:

- **F5 → "Server (uvicorn --reload)"** — runs `alembic upgrade head` first (same
  order as the Docker CMD), then serves on `:8000` with the debugger attached.
- **F5 → "Pytest: all tests" / "Pytest: current file"** — debug tests with
  breakpoints.
- **Test Explorer** is wired to pytest; **Ctrl+Shift+B**-style tasks cover
  `pytest`, `ruff: check`, `ruff: format`, `alembic: upgrade head`, `uv: sync`.
- Ruff is the formatter, on save, with import organisation.

## Configuration

All settings are env vars with a `PCS_` prefix (or a `.env` file):

| Var | Default | Purpose |
|-----|---------|---------|
| `PCS_DATABASE_URL` | `sqlite+aiosqlite:///./data/prophecy.db` | DB URL. Swap for `postgresql+asyncpg://…` on a large hosted instance. |
| `PCS_AUTO_CREATE` | `false` | Create tables from models on startup (dev/test only; prod uses Alembic). |
| `PCS_CODE_LENGTH` | `8` | Join-code length (Crockford base32). |
| `PCS_MAX_MESSAGE_BYTES` | `65536` | Max accepted WS frame size. |
| `PCS_HOST` / `PCS_PORT` | `0.0.0.0` / `8000` | Bind address. |

## Deploy (self-host)

```bash
docker compose up -d
```

The container runs `alembic upgrade head` then serves on `:8000`. SQLite lives on
the `pcs-data` volume. **TLS:** terminate `wss://`/`https://` at a reverse proxy
(Caddy/nginx) in front — iOS blocks cleartext WebSocket connections.
