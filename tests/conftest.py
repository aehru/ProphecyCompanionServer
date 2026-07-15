import os
import pathlib
import tempfile

# Point the app at a throwaway SQLite file and let the lifespan create the schema
# from the models. MUST run before `app` is imported so Settings() reads it.
_TMPDIR = pathlib.Path(tempfile.mkdtemp(prefix="pcs-test-"))
_DBFILE = _TMPDIR / "test.db"
os.environ["PCS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DBFILE.as_posix()}"
os.environ["PCS_AUTO_CREATE"] = "true"

from collections.abc import Iterator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Entering the context runs the lifespan (schema create_all).
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_file() -> str:
    return str(_DBFILE)


def make_campaign(
    client: TestClient, name: str = "Table", token: str = "gm-secret-token"
) -> dict[str, Any]:
    r = client.post("/campaigns", json={"name": name, "gmToken": token})
    assert r.status_code == 201, r.text
    return r.json()
