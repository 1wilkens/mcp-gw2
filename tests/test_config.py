from pathlib import Path

import pytest

from gw2mcp.config import ConfigError, Settings, default_db_path


def test_defaults():
    s = Settings.from_env({"HOME": "/h", "XDG_DATA_HOME": "/data"})
    assert s.api_key is None
    assert s.db_path == Path("/data/gw2mcp/gw2mcp.sqlite3")
    assert s.lang == "en"
    assert s.transport == "stdio"
    assert s.snapshot_interval == 3600
    assert s.snapshot_value is True
    assert s.http_path == "/mcp"


def test_full_env(tmp_path):
    keyfile = tmp_path / "key"
    keyfile.write_text("  SECRET-KEY \n")
    tokfile = tmp_path / "tok"
    tokfile.write_text("tok\n")
    s = Settings.from_env(
        {
            "GW2_API_KEY_FILE": str(keyfile),
            "GW2MCP_AUTH_TOKEN_FILE": str(tokfile),
            "GW2MCP_DB": "/x/db.sqlite3",
            "GW2MCP_LANG": "DE",
            "GW2MCP_TRANSPORT": "http",
            "GW2MCP_HOST": "0.0.0.0",
            "GW2MCP_PORT": "9000",
            "GW2MCP_HTTP_PATH": "gw2",
            "GW2MCP_SNAPSHOT_INTERVAL": "0",
            "GW2MCP_SNAPSHOT_VALUE": "no",
            "GW2MCP_ALLOWED_HOSTS": "gw2.example.com, other.example.com",
            "GW2MCP_ALLOWED_ORIGINS": "https://a.example",
            "GW2MCP_LOG_LEVEL": "debug",
            "GW2MCP_API_BASE_URL": "http://localhost:1/",
        }
    )
    assert s.api_key == "SECRET-KEY"
    assert s.auth_token == "tok"
    assert s.db_path == Path("/x/db.sqlite3")
    assert s.lang == "de"
    assert s.transport == "http"
    assert (s.host, s.port, s.http_path) == ("0.0.0.0", 9000, "/gw2")
    assert s.snapshot_interval == 0
    assert s.snapshot_value is False
    assert s.allowed_hosts == ["gw2.example.com", "other.example.com"]
    assert s.allowed_origins == ["https://a.example"]
    assert s.log_level == "DEBUG"
    assert s.api_base_url == "http://localhost:1"


def test_direct_key_wins_over_file(tmp_path):
    s = Settings.from_env({"GW2_API_KEY": "direct", "GW2_API_KEY_FILE": str(tmp_path / "missing")})
    assert s.api_key == "direct"


def test_state_directory_default():
    assert default_db_path({"STATE_DIRECTORY": "/var/lib/gw2mcp:/other"}) == Path("/var/lib/gw2mcp/gw2mcp.sqlite3")


@pytest.mark.parametrize(
    "env",
    [
        {"GW2MCP_LANG": "xx"},
        {"GW2MCP_TRANSPORT": "sse"},
        {"GW2MCP_PORT": "abc"},
        {"GW2MCP_PORT": "0"},
        {"GW2MCP_SNAPSHOT_INTERVAL": "-1"},
        {"GW2MCP_SNAPSHOT_VALUE": "maybe"},
        {"GW2MCP_LOG_LEVEL": "loud"},
        {"GW2_API_KEY_FILE": "/nonexistent/key"},
    ],
)
def test_invalid(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_with_overrides_ignores_none():
    s = Settings(port=1).with_overrides(port=None, host="h")
    assert (s.port, s.host) == (1, "h")


def test_systemd_credentials(tmp_path):
    (tmp_path / "gw2-api-key").write_text("CRED-KEY\n")
    (tmp_path / "auth-token").write_text("CRED-TOKEN")
    s = Settings.from_env({"CREDENTIALS_DIRECTORY": str(tmp_path)})
    assert (s.api_key, s.auth_token) == ("CRED-KEY", "CRED-TOKEN")
    s = Settings.from_env({"CREDENTIALS_DIRECTORY": str(tmp_path / "missing")})
    assert s.api_key is None and s.auth_token is None


@pytest.mark.parametrize("value", ["", "  \n"])
def test_blank_auth_token_env_fails_closed(value):
    with pytest.raises(ConfigError, match="configured but empty"):
        Settings.from_env({"GW2MCP_AUTH_TOKEN": value})


def test_blank_auth_token_file_fails_closed(tmp_path):
    token = tmp_path / "token"
    token.write_text("\n")
    with pytest.raises(ConfigError, match="configured but empty"):
        Settings.from_env({"GW2MCP_AUTH_TOKEN_FILE": str(token)})


def test_blank_auth_token_credential_fails_closed(tmp_path):
    (tmp_path / "auth-token").write_text(" \n")
    with pytest.raises(ConfigError, match="configured but empty"):
        Settings.from_env({"CREDENTIALS_DIRECTORY": str(tmp_path)})


def test_blank_api_key_still_means_no_key(tmp_path):
    key = tmp_path / "key"
    key.write_text("\n")
    assert Settings.from_env({"GW2_API_KEY_FILE": str(key)}).api_key is None
    assert Settings.from_env({"GW2_API_KEY": "  "}).api_key is None


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "gw2.example.com", ""])
def test_remote_http_requires_token(host):
    with pytest.raises(ConfigError, match="without authentication"):
        Settings(transport="http", host=host).check_http_exposure()
    Settings(transport="http", host=host, auth_token="token").check_http_exposure()
    Settings(transport="stdio", host=host).check_http_exposure()


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "LOCALHOST"])
def test_loopback_http_allows_no_token(host):
    Settings(transport="http", host=host).check_http_exposure()
