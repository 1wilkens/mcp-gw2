import json

import pytest
from starlette.testclient import TestClient

from gw2mcp.config import ConfigError, Settings
from gw2mcp.http import BearerAuthMiddleware, build_http_app, transport_security
from gw2mcp.server import create_server

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def parse(resp):
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
    return resp.json()


@pytest.fixture
def http_client(app):
    def make(**overrides):
        settings = app.settings.with_overrides(**overrides)
        return TestClient(build_http_app(create_server(app), settings), base_url="http://127.0.0.1:8000")

    return make


def test_healthz_and_auth(http_client):
    with http_client(auth_token="s3cret") as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        r = c.post("/mcp", json=INIT, headers=HEADERS)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer wrong"}).status_code == 401
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "authorization": "Basic s3cret"}).status_code == 401
        r = c.post("/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer s3cret"})
        assert r.status_code == 200, r.text
        assert parse(r)["result"]["serverInfo"]["name"] == "gw2mcp"


def test_no_auth_configured(http_client):
    with http_client() as c:
        r = c.post("/mcp", json=INIT, headers=HEADERS)
        assert r.status_code == 200, r.text


def test_full_session_tool_call(http_client):
    with http_client(auth_token="t") as c:
        h = {**HEADERS, "authorization": "Bearer t"}
        r = c.post("/mcp", json=INIT, headers=h)
        sid = r.headers.get("mcp-session-id")
        if sid:
            h["mcp-session-id"] = sid
        h["mcp-protocol-version"] = "2025-06-18"
        c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_wallet", "arguments": {}}},
            headers=h,
        )
        assert r.status_code == 200, r.text
        result = parse(r)["result"]
        assert result["structuredContent"]["coins_formatted"] == "123g 45s 67c"


def test_custom_path_and_host_check(http_client):
    with http_client(http_path="/gw2", allowed_hosts=["gw2.example.com"]) as c:
        ok = c.post("/gw2", json=INIT, headers={**HEADERS, "host": "gw2.example.com"})
        assert ok.status_code == 200, ok.text
        bad = c.post("/gw2", json=INIT, headers={**HEADERS, "host": "evil.example.com"})
        assert bad.status_code in (400, 403, 421)
        origin_ok = c.post(
            "/gw2", json=INIT, headers={**HEADERS, "host": "gw2.example.com", "origin": "https://gw2.example.com"}
        )
        assert origin_ok.status_code == 200
        origin_bad = c.post(
            "/gw2", json=INIT, headers={**HEADERS, "host": "gw2.example.com", "origin": "https://evil.example"}
        )
        assert origin_bad.status_code in (400, 403)


def test_transport_security_settings():
    assert transport_security(Settings()) is None
    ts = transport_security(Settings(allowed_hosts=["gw2.example.com", "*.x:*"], allowed_origins=["https://o"]))
    assert ts.enable_dns_rebinding_protection
    assert "gw2.example.com" in ts.allowed_hosts and "127.0.0.1:*" in ts.allowed_hosts
    assert "https://gw2.example.com" in ts.allowed_origins and "https://o" in ts.allowed_origins
    assert not any("*.x" in o for o in ts.allowed_origins)


async def test_middleware_passes_non_http_scopes():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = BearerAuthMiddleware(inner, "tok")
    await mw({"type": "lifespan"}, None, None)
    await mw({"type": "http", "path": "/healthz", "headers": []}, None, None)
    assert seen == ["lifespan", "http"]


def test_build_http_app_refuses_unauthenticated_remote_bind(app):
    settings = app.settings.with_overrides(transport="http", host="0.0.0.0")
    with pytest.raises(ConfigError, match="without authentication"):
        build_http_app(create_server(app), settings)
