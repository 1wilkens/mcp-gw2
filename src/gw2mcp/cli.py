"""Command line interface: ``gw2mcp serve|snapshot|snapshots|index``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence

from gw2mcp import __version__
from gw2mcp.catalog import SEARCHABLE_ENDPOINTS
from gw2mcp.config import ConfigError, Settings
from gw2mcp.server import AppContext, create_server

log = logging.getLogger("gw2mcp")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gw2mcp", description="MCP server for the Guild Wars 2 API")
    p.add_argument("--version", action="version", version=f"gw2mcp {__version__}")
    p.add_argument("--db", help="SQLite database path (env GW2MCP_DB)")
    p.add_argument("--log-level", help="log level (env GW2MCP_LOG_LEVEL)")
    sub = p.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=["stdio", "http"], help="env GW2MCP_TRANSPORT")
    serve.add_argument("--host", help="HTTP bind address (env GW2MCP_HOST)")
    serve.add_argument("--port", type=int, help="HTTP port (env GW2MCP_PORT)")
    serve.add_argument("--snapshot-interval", type=int, help="seconds between snapshots, 0 disables")

    sub.add_parser("snapshot", help="take one account snapshot and exit")

    ls = sub.add_parser("snapshots", help="list recorded snapshots")
    ls.add_argument("--limit", type=int, default=20)

    idx = sub.add_parser("index", help="download game data indices used by name search")
    idx.add_argument("endpoints", nargs="*", default=["items", "achievements", "currencies", "skins"])
    idx.add_argument("--force", action="store_true")
    return p


def settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    overrides: dict[str, object] = {
        "db_path": args.db,
        "log_level": args.log_level.upper() if args.log_level else None,
        "transport": getattr(args, "transport", None),
        "host": getattr(args, "host", None),
        "port": getattr(args, "port", None),
        "snapshot_interval": getattr(args, "snapshot_interval", None),
    }
    if args.db:
        from pathlib import Path

        overrides["db_path"] = Path(args.db)
    return settings.with_overrides(**overrides)


async def serve(settings: Settings) -> None:
    app = AppContext.create(settings)
    mcp = create_server(app)
    try:
        app.scheduler.start()
        if settings.transport == "stdio":
            await mcp.run_stdio_async()
        else:
            import uvicorn

            from gw2mcp.http import build_http_app

            config = uvicorn.Config(
                build_http_app(mcp, settings),
                host=settings.host,
                port=settings.port,
                log_level=settings.log_level.lower(),
                proxy_headers=True,
                forwarded_allow_ips="127.0.0.1,::1",
            )
            log.info("serving MCP over HTTP on http://%s:%s%s", settings.host, settings.port, settings.http_path)
            await uvicorn.Server(config).serve()
    finally:
        await app.aclose()


async def snapshot_once(settings: Settings) -> int:
    app = AppContext.create(settings)
    try:
        snap = await app.collector.take()
        print(json.dumps(snap.as_dict(), indent=2))
        return 0
    finally:
        await app.aclose()


async def build_indices(settings: Settings, endpoints: Sequence[str], force: bool) -> int:
    app = AppContext.create(settings)
    rc = 0
    try:
        for ep in endpoints:
            if ep not in SEARCHABLE_ENDPOINTS:
                print(f"skipping {ep}: not searchable", file=sys.stderr)
                rc = 1
                continue
            n = await app.catalog.build_index(ep, force=force)
            print(f"{ep}: {n} objects")
    finally:
        await app.aclose()
    return rc


def list_snapshots(settings: Settings, limit: int) -> int:
    from gw2mcp.storage import Storage

    storage = Storage(settings.db_path)
    try:
        for s in storage.list_snapshots(limit=limit):
            d = s.as_dict()
            errors = f"  errors: {', '.join(d['errors'])}" if d["errors"] else ""
            print(f"#{d['id']:<6} {d['taken_at']}  {d['account_name'] or '?'}{errors}")
    finally:
        storage.close()
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"
    try:
        settings = settings_from_args(args)
        if command == "serve":
            settings.check_http_exposure()
    except ConfigError as exc:
        parser.error(str(exc))
    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if command == "serve":
        if not settings.api_key:
            log.warning("no GW2 API key configured: account tools and snapshots are disabled")
        asyncio.run(serve(settings))
        rc = 0
    elif command == "snapshot":
        rc = asyncio.run(snapshot_once(settings))
    elif command == "snapshots":
        rc = list_snapshots(settings, args.limit)
    else:
        rc = asyncio.run(build_indices(settings, args.endpoints, args.force))
    sys.exit(rc)
