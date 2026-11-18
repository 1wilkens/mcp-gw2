"""Runtime configuration, read from environment variables (and optional secret files)."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

Transport = Literal["stdio", "http"]

SUPPORTED_LANGS = ("en", "de", "es", "fr", "zh")


class ConfigError(ValueError):
    """Raised when the configuration is invalid."""


# systemd credential names (LoadCredential=) checked when no env variable is set
CREDENTIAL_NAMES = {"GW2_API_KEY": "gw2-api-key", "GW2MCP_AUTH_TOKEN": "auth-token"}


def _read_secret(env: Mapping[str, str], name: str) -> str | None:
    """Read ``NAME`` directly, from the file in ``NAME_FILE``, or from a systemd credential."""
    value = env.get(name)
    if value:
        return value.strip() or None
    path = env.get(f"{name}_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            raise ConfigError(f"cannot read {name}_FILE={path!r}: {exc}") from exc
    cred_dir = env.get("CREDENTIALS_DIRECTORY")
    if cred_dir and name in CREDENTIAL_NAMES:
        cred = Path(cred_dir) / CREDENTIAL_NAMES[name]
        if cred.is_file():
            return cred.read_text(encoding="utf-8").strip() or None
    return None


def _read_auth_token(env: Mapping[str, str]) -> str | None:
    """Read the bearer token, failing closed when one is configured but blank."""
    name = "GW2MCP_AUTH_TOKEN"
    token = _read_secret(env, name)
    if token is not None:
        return token
    cred_dir = env.get("CREDENTIALS_DIRECTORY")
    has_credential = bool(cred_dir) and (Path(cred_dir) / CREDENTIAL_NAMES[name]).is_file()
    if name in env or env.get(f"{name}_FILE") or has_credential:
        raise ConfigError(f"{name} is configured but empty; set a token or remove the setting")
    return None


def is_loopback_host(host: str) -> bool:
    """Whether a bind address is only reachable from this machine (unknown names count as remote)."""
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"invalid boolean value: {value!r}")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def default_db_path(env: Mapping[str, str]) -> Path:
    if env.get("STATE_DIRECTORY"):  # systemd StateDirectory=
        return Path(env["STATE_DIRECTORY"].split(":")[0]) / "gw2mcp.sqlite3"
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "gw2mcp" / "gw2mcp.sqlite3"


@dataclass(frozen=True)
class Settings:
    api_key: str | None = None
    db_path: Path = field(default_factory=lambda: default_db_path(os.environ))
    lang: str = "en"
    api_base_url: str = "https://api.guildwars2.com"
    schema_version: str = "latest"
    snapshot_interval: int = 3600
    snapshot_value: bool = True
    catalog_ttl_days: int = 7
    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    http_path: str = "/mcp"
    auth_token: str | None = None
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        lang = env.get("GW2MCP_LANG", "en").strip().lower() or "en"
        if lang not in SUPPORTED_LANGS:
            raise ConfigError(f"GW2MCP_LANG must be one of {SUPPORTED_LANGS}, got {lang!r}")
        transport = env.get("GW2MCP_TRANSPORT", "stdio").strip().lower() or "stdio"
        if transport not in ("stdio", "http"):
            raise ConfigError(f"GW2MCP_TRANSPORT must be 'stdio' or 'http', got {transport!r}")
        http_path = env.get("GW2MCP_HTTP_PATH", "/mcp").strip() or "/mcp"
        if not http_path.startswith("/"):
            http_path = "/" + http_path
        log_level = env.get("GW2MCP_LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"invalid GW2MCP_LOG_LEVEL {log_level!r}")
        return cls(
            api_key=_read_secret(env, "GW2_API_KEY"),
            db_path=Path(env["GW2MCP_DB"]) if env.get("GW2MCP_DB") else default_db_path(env),
            lang=lang,
            api_base_url=env.get("GW2MCP_API_BASE_URL", "https://api.guildwars2.com").rstrip("/"),
            schema_version=env.get("GW2MCP_SCHEMA_VERSION", "latest"),
            snapshot_interval=_int(env, "GW2MCP_SNAPSHOT_INTERVAL", 3600),
            snapshot_value=_bool(env.get("GW2MCP_SNAPSHOT_VALUE"), True),
            catalog_ttl_days=_int(env, "GW2MCP_CATALOG_TTL_DAYS", 7, minimum=1),
            transport=transport,  # type: ignore[arg-type]
            host=env.get("GW2MCP_HOST", "127.0.0.1"),
            port=_int(env, "GW2MCP_PORT", 8000, minimum=1),
            http_path=http_path,
            auth_token=_read_auth_token(env),
            allowed_hosts=_list(env.get("GW2MCP_ALLOWED_HOSTS")),
            allowed_origins=_list(env.get("GW2MCP_ALLOWED_ORIGINS")),
            log_level=log_level,
        )

    def check_http_exposure(self) -> None:
        """Refuse to serve account data over HTTP on a network interface without a bearer token."""
        if self.transport == "http" and not self.auth_token and not is_loopback_host(self.host):
            raise ConfigError(
                f"refusing to listen on {self.host!r} without authentication; "
                "set GW2MCP_AUTH_TOKEN or GW2MCP_AUTH_TOKEN_FILE, or bind to a loopback address"
            )

    def with_overrides(self, **overrides: object) -> Settings:
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)  # type: ignore[arg-type]
