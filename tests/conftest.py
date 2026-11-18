from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from gw2mcp.catalog import Catalog
from gw2mcp.client import GW2Client
from gw2mcp.config import Settings
from gw2mcp.server import AppContext, create_server
from gw2mcp.snapshot import SnapshotCollector
from gw2mcp.storage import Storage
from tests.fakeapi import API_KEY, FakeGW2


@pytest.fixture
def fake() -> FakeGW2:
    return FakeGW2()


@pytest_asyncio.fixture
async def client(fake: FakeGW2) -> AsyncIterator[GW2Client]:
    c = GW2Client(API_KEY, transport=fake.transport(), backoff=0)
    yield c
    await c.aclose()


@pytest.fixture
def storage() -> Storage:
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture
def catalog(client: GW2Client, storage: Storage) -> Catalog:
    return Catalog(client, storage)


@pytest.fixture
def collector(client: GW2Client, storage: Storage, catalog: Catalog) -> SnapshotCollector:
    return SnapshotCollector(client, storage, catalog)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(api_key=API_KEY, db_path=tmp_path / "db.sqlite3", snapshot_interval=3600)


@pytest.fixture
def app(settings: Settings, client: GW2Client, storage: Storage) -> AppContext:
    return AppContext.create(settings, client=client, storage=storage)


@pytest.fixture
def mcp(app: AppContext):
    return create_server(app)


@pytest.fixture
def call(mcp):
    """Call a tool and return its structured result; raises ToolError on failure."""

    async def _call(tool_name: str, /, **arguments: Any) -> dict[str, Any]:
        result = await mcp.call_tool(tool_name, arguments)
        assert not result.is_error, result.content
        return result.structured_content

    return _call


# --------------------------------------------------------------------------------------------
# Backend-agnostic fixtures: tests using ``api``/``api_call`` run against the in-memory fake API
# and, when a GW2 API key is available, a second time against the real API.
#
#   GW2_TEST_BACKEND=auto (default)  fake always, live too if GW2_API_KEY(_FILE) is set
#   GW2_TEST_BACKEND=fake            never touch the network
#   GW2_TEST_BACKEND=live            live only (fails instead of skipping when no key)
#
# Tests of this kind must not assert concrete account data, only invariants that hold for any account.
# --------------------------------------------------------------------------------------------
import os  # noqa: E402


def _live_key() -> str | None:
    try:
        return Settings.from_env().api_key
    except Exception:
        return None


def _backends() -> list[Any]:
    mode = os.environ.get("GW2_TEST_BACKEND", "auto").lower()
    fake = pytest.param("fake", id="fake")
    live = pytest.param("live", id="live", marks=pytest.mark.live)
    return {"fake": [fake], "live": [live]}.get(mode, [fake, live])


@pytest.fixture(params=_backends())
def backend(request) -> str:
    if request.param == "live" and not _live_key():
        if os.environ.get("GW2_TEST_BACKEND", "").lower() == "live":
            pytest.fail("GW2_TEST_BACKEND=live but no GW2_API_KEY configured")
        pytest.skip("no GW2_API_KEY configured; live API tests skipped")
    return request.param


@pytest_asyncio.fixture
async def api(backend: str) -> AsyncIterator[GW2Client]:
    c = GW2Client(API_KEY, transport=FakeGW2().transport(), backoff=0) if backend == "fake" else GW2Client(_live_key())
    yield c
    await c.aclose()


@pytest_asyncio.fixture
async def permissions(api: GW2Client) -> set[str]:
    return set((await api.get("v2/tokeninfo", auth=True)).get("permissions", []))


@pytest.fixture
def require(permissions: set[str]):
    """``require("inventories", ...)`` skips the test when the key lacks one of the permissions."""

    def _require(*needed: str) -> None:
        missing = set(needed) - permissions
        if missing:
            pytest.skip(f"API key lacks permission(s): {', '.join(sorted(missing))}")

    return _require


@pytest.fixture
def api_call(api: GW2Client, tmp_path):
    """Call an MCP tool through a server wired to the selected backend (fresh DB per test)."""
    storage = Storage(":memory:")
    app = AppContext.create(
        Settings(api_key=api.api_key, db_path=tmp_path / "db.sqlite3", snapshot_interval=0),
        client=api,
        storage=storage,
    )
    server = create_server(app)

    async def _call(tool_name: str, /, **arguments: Any) -> dict[str, Any]:
        result = await server.call_tool(tool_name, arguments)
        assert not result.is_error, result.content
        return result.structured_content

    yield _call
    storage.close()
