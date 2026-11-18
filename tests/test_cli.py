import asyncio
import json
from pathlib import Path

import pytest

from gw2mcp import cli
from gw2mcp.config import Settings
from gw2mcp.storage import Storage


def test_parser_defaults():
    args = cli.build_parser().parse_args([])
    assert args.command is None
    args = cli.build_parser().parse_args(["--db", "/tmp/x", "serve", "--transport", "http", "--port", "9"])
    assert (args.db, args.command, args.transport, args.port) == ("/tmp/x", "serve", "http", 9)
    args = cli.build_parser().parse_args(["index", "items", "--force"])
    assert args.endpoints == ["items"] and args.force


def test_settings_from_args(monkeypatch):
    monkeypatch.setenv("GW2MCP_PORT", "1234")
    monkeypatch.setenv("GW2_API_KEY", "k")
    args = cli.build_parser().parse_args(
        ["--db", "/x/y.db", "--log-level", "debug", "serve", "--host", "0.0.0.0", "--snapshot-interval", "0"]
    )
    s = cli.settings_from_args(args)
    assert s.db_path == Path("/x/y.db")
    assert s.log_level == "DEBUG"
    assert (s.host, s.port, s.snapshot_interval, s.api_key) == ("0.0.0.0", 1234, 0, "k")


def test_invalid_env_exits(monkeypatch):
    monkeypatch.setenv("GW2MCP_LANG", "xx")
    with pytest.raises(SystemExit) as exc:
        cli.main(["snapshots"])
    assert exc.value.code == 2


def test_snapshots_command(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db.sqlite3"
    s = Storage(db)
    s.save_snapshot({"stat": {"a": 1}}, account_name="Acc.1", errors={"luck": "x"})
    s.close()
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(db), "snapshots"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "#1" in out and "Acc.1" in out and "errors: luck" in out


@pytest.fixture
def patched_app(monkeypatch, fake):
    from gw2mcp.client import GW2Client
    from gw2mcp.server import AppContext

    original = AppContext.create.__func__

    def create(cls, settings, **kw):
        kw.setdefault("client", GW2Client("TEST-KEY", transport=fake.transport(), backoff=0))
        return original(cls, settings, **kw)

    monkeypatch.setattr(AppContext, "create", classmethod(create))


def test_snapshot_command(tmp_path, patched_app, capsys):
    rc = asyncio.run(cli.snapshot_once(Settings(api_key="TEST-KEY", db_path=tmp_path / "db")))
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["account_name"] == "Tester.1234"
    storage = Storage(tmp_path / "db")
    assert storage.count_snapshots() == 1
    storage.close()


def test_index_command(tmp_path, patched_app, capsys):
    rc = asyncio.run(cli.build_indices(Settings(db_path=tmp_path / "db"), ["items", "recipes"], False))
    assert rc == 1
    captured = capsys.readouterr()
    assert "items: 6 objects" in captured.out
    assert "skipping recipes" in captured.err


def test_serve_stdio_starts_and_closes(tmp_path, patched_app, monkeypatch):
    from mcp.server.mcpserver import MCPServer

    started = {}

    async def fake_stdio(self):
        started["tools"] = len(await self.list_tools())

    monkeypatch.setattr(MCPServer, "run_stdio_async", fake_stdio)
    asyncio.run(cli.serve(Settings(api_key="TEST-KEY", db_path=tmp_path / "db", snapshot_interval=0)))
    assert started["tools"] > 30


def test_serve_http_uses_uvicorn(tmp_path, patched_app, monkeypatch):
    import uvicorn

    seen = {}

    async def fake_serve(self):
        seen["host"], seen["port"] = self.config.host, self.config.port

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    settings = Settings(
        api_key="TEST-KEY",
        db_path=tmp_path / "db",
        transport="http",
        host="0.0.0.0",
        port=9999,
        snapshot_interval=0,
        auth_token="token",
    )
    asyncio.run(cli.serve(settings))
    assert seen == {"host": "0.0.0.0", "port": 9999}


def test_main_dispatch(monkeypatch, tmp_path):
    calls = []

    async def fake_serve(settings):
        calls.append(("serve", settings.transport))

    monkeypatch.setattr(cli, "serve", fake_serve)
    monkeypatch.delenv("GW2_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(tmp_path / "d"), "serve", "--transport", "http"])
    assert exc.value.code == 0
    assert calls == [("serve", "http")]


def test_main_refuses_unauthenticated_remote_bind(monkeypatch, tmp_path, capsys):
    async def fail_serve(settings):
        raise AssertionError("server must not start")

    monkeypatch.setattr(cli, "serve", fail_serve)
    monkeypatch.delenv("GW2MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("GW2MCP_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(tmp_path / "d"), "serve", "--transport", "http", "--host", "0.0.0.0"])
    assert exc.value.code == 2
    assert "without authentication" in capsys.readouterr().err
