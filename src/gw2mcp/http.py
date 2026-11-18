"""Streamable-HTTP ASGI application with optional bearer-token authentication."""

from __future__ import annotations

import hmac
import json

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from gw2mcp.config import Settings

PUBLIC_PATHS = frozenset({"/healthz"})


class BearerAuthMiddleware:
    """Reject HTTP requests without ``Authorization: Bearer <token>`` (except health checks)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        header = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header = value
                break
        scheme, _, supplied = header.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(supplied.strip(), self.token):
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="gw2mcp"'),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def transport_security(settings: Settings) -> TransportSecuritySettings | None:
    """DNS-rebinding protection: always allow loopback plus the configured public hosts."""
    if not settings.allowed_hosts and not settings.allowed_origins:
        return None  # SDK default (auto-protect when bound to localhost)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", *settings.allowed_hosts]
    origins = ["http://127.0.0.1:*", "http://localhost:*", *settings.allowed_origins]
    for host in settings.allowed_hosts:
        if "*" not in host:
            origins += [f"https://{host}", f"http://{host}"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def build_http_app(mcp: MCPServer, settings: Settings) -> ASGIApp:
    settings.check_http_exposure()

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app: ASGIApp = mcp.streamable_http_app(
        streamable_http_path=settings.http_path,
        transport_security=transport_security(settings),
        host=settings.host,
    )
    if settings.auth_token:
        app = BearerAuthMiddleware(app, settings.auth_token)
    return app
