# gw2mcp

An [MCP](https://modelcontextprotocol.io) server for the [Guild Wars 2 API](https://wiki.guildwars2.com/wiki/API:Main).
It answers questions about your account in real time, and it records snapshots of the
account on a schedule. With those snapshots it can also answer historical questions such as
*"How much gold did I make this week?"*, *"When did I unlock Twilight's skin?"* or
*"How did my ectoplasm stock change over the last three months?"*.

## Features

- **Live account data:** account, wallet, bank, material storage, shared inventory, characters
  (core, inventory, equipment, builds, crafting, …), achievements, all unlock collections, Wizard's
  Vault, trading post orders and history, PvP, daily and weekly completions, and an
  item-location finder.
- **Game data:** name search over items, achievements, skins, currencies and other endpoints,
  using a local index. You can also look up any bulk endpoint by id, read trading post prices
  and order books, check the gem exchange rate and search recipes. A raw `api_get` tool covers
  everything else.
- **History:** periodic snapshots go into SQLite. They cover stats (AP, gold, playtime, item
  value, fractal/WvW/PvP rank, mastery points, luck, unlock counts …), wallet, item counts
  (in total and per location), achievement progress, characters, and every unlock collection.
  Only changes are stored, so hourly snapshots stay small.
- **Transports:** stdio for local clients, and streamable HTTP for remote use, with optional
  bearer-token auth and DNS-rebinding protection.
- **NixOS:** a flake with a package, a NixOS module (hardened systemd service with an optional
  nginx reverse proxy and ACME) and a VM test.

## Tools

| Area | Tools |
|---|---|
| Account (live) | `get_account`, `get_wallet`, `get_bank`, `get_materials`, `get_shared_inventory`, `list_characters`, `get_character`, `find_items`, `get_account_achievements`, `get_unlocks`, `get_wizards_vault`, `get_trading_post`, `get_pvp`, `get_daily_rotations`, `estimate_account_value` |
| Game data | `search`, `get_game_data`, `get_prices`, `get_listings`, `gem_exchange`, `find_recipes`, `api_get` |
| History | `take_snapshot`, `list_snapshots`, `get_snapshot`, `list_tracked_stats`, `stat_history`, `item_history`, `currency_history`, `achievement_history`, `character_history`, `unlock_history`, `compare_snapshots`, `progress_report` |
| Misc | `server_status`; prompts `weekly_review` and `item_tracking` |

History tools take time references as ISO dates (`2025-01-31`, `2025-01-31T18:00:00Z`), as
relative offsets (`24h`, `7d`, `2w`, `3mo`, `1y`), or as `latest`, `first` or a snapshot id.

## Installation & usage

Create an API key at <https://account.arena.net/applications>. For full functionality, grant
these permissions: `account`, `inventories`, `characters`, `wallet`, `unlocks`, `progression`,
`tradingpost`, `pvp`, `builds`, `wvw`. When a permission is missing, the sections that need it
are skipped and reported in the snapshot's `errors`.

```sh
# with uv
uv run gw2mcp serve                      # stdio (default)
GW2MCP_TRANSPORT=http uv run gw2mcp serve --port 8765

# with nix
nix run github:<you>/gw2mcp -- serve
```

Other commands:

```sh
gw2mcp snapshot              # take one snapshot now and print it
gw2mcp snapshots --limit 10  # list recorded snapshots
gw2mcp index items skins     # pre-build name-search indices (otherwise built on first search)
```

### Claude Code / Claude Desktop (stdio)

```sh
claude mcp add gw2 --env GW2_API_KEY=XXXX -- uv --directory /path/to/gw2mcp run gw2mcp serve
```

```json
{
  "mcpServers": {
    "gw2": {
      "command": "uv",
      "args": ["--directory", "/path/to/gw2mcp", "run", "gw2mcp", "serve"],
      "env": { "GW2_API_KEY": "XXXX" }
    }
  }
}
```

### Remote (HTTP)

```sh
claude mcp add --transport http gw2 https://gw2.example.com/mcp --header "Authorization: Bearer <token>"
```

With stdio, snapshots are only taken while the client keeps the server running. For continuous
history, run the HTTP server as a service (see NixOS below), or call `gw2mcp snapshot` from cron
or a systemd timer.

## Configuration

All settings are environment variables. Secrets can also be read from `<NAME>_FILE`, or from
systemd credentials (`$CREDENTIALS_DIRECTORY/gw2-api-key` and `$CREDENTIALS_DIRECTORY/auth-token`).

| Variable | Default | Description |
|---|---|---|
| `GW2_API_KEY` / `GW2_API_KEY_FILE` | – | GW2 API key. Without it, only public data is available and no snapshots are taken. |
| `GW2MCP_DB` | `$STATE_DIRECTORY` or `$XDG_DATA_HOME/gw2mcp/gw2mcp.sqlite3` | SQLite database |
| `GW2MCP_SNAPSHOT_INTERVAL` | `3600` | Seconds between snapshots (`0` disables them) |
| `GW2MCP_SNAPSHOT_VALUE` | `true` | Record the trading post value of all items with each snapshot |
| `GW2MCP_LANG` | `en` | `en`, `de`, `es`, `fr`, `zh` |
| `GW2MCP_CATALOG_TTL_DAYS` | `7` | Cache lifetime for public game data |
| `GW2MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `GW2MCP_HOST` / `GW2MCP_PORT` | `127.0.0.1` / `8000` | HTTP bind address |
| `GW2MCP_HTTP_PATH` | `/mcp` | MCP endpoint path |
| `GW2MCP_AUTH_TOKEN` / `_FILE` | – | Require `Authorization: Bearer <token>` (HTTP). Mandatory when binding to a non-loopback address. A configured but empty token is a startup error. |
| `GW2MCP_ALLOWED_HOSTS` | – | Extra accepted `Host` headers, comma separated (needed behind a reverse proxy) |
| `GW2MCP_ALLOWED_ORIGINS` | – | Extra accepted `Origin` headers |
| `GW2MCP_SCHEMA_VERSION` | `latest` | `X-Schema-Version` sent to the API |
| `GW2MCP_LOG_LEVEL` | `INFO` | Log level |

`GET /healthz` is always reachable without a token.

The raw `api_get` tool only reaches known API routes. Credential endpoints (`createsubtoken`, `tokeninfo`)
are blocked, and `get_account` omits the key's `id` from its token details.

## NixOS

```nix
{
  inputs.gw2mcp.url = "github:<you>/gw2mcp";

  outputs = { nixpkgs, gw2mcp, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        gw2mcp.nixosModules.default
        {
          services.gw2mcp = {
            enable = true;
            apiKeyFile = "/run/secrets/gw2-api-key";     # e.g. from agenix / sops-nix
            authTokenFile = "/run/secrets/gw2mcp-token";
            snapshotInterval = 3600;
            nginx = {
              enable = true;
              virtualHost = "gw2.example.com";
              enableACME = true;                         # forceSSL follows enableACME
            };
          };
          security.acme = { acceptTerms = true; defaults.email = "you@example.com"; };
          networking.firewall.allowedTCPPorts = [ 80 443 ];
        }
      ];
    };
  };
}
```

The service runs as a `DynamicUser` with systemd sandboxing. Data is kept in `/var/lib/gw2mcp`,
and secrets are passed in with `LoadCredential`, so the secret files can stay root-only.
nginx proxies only the MCP path and `/healthz`, and turns off buffering for streaming responses.
The virtual host name is added to the server's allowed `Host` headers automatically. Other options:
`host`, `port` (8765), `path`, `language`, `trackAccountValue`, `catalogTtlDays`, `allowedHosts`,
`logLevel`, `openFirewall`, `environment` and `nginx.extraLocationConfig` (for example for IP allow lists).

The flake also exports `overlays.default`, which adds `python3Packages.gw2mcp` and `pkgs.gw2mcp`.
If your nixpkgs still ships mcp < 2.3, the overlay packages mcp 2.3 from pinned wheels.

```sh
nix build .#gw2mcp      # builds the package and runs the unit tests
nix flake check         # also runs the NixOS VM test (nginx + auth + MCP session)
```

## How history is stored

Each snapshot adds a row to `snapshots`. Tracked values are written to
`observations(kind, key, snapshot_id, value)` **only when they change**, and `null` marks a value
that disappeared. The value at any snapshot is the latest observation at or before it. If a
section fails (for example because of a missing permission or an API outage), its kinds are left
out of that snapshot, so the previous values carry forward instead of looking like losses. Item
totals are only recorded when every inventory source was read successfully.

Kinds: `stat`, `wallet`, `item`, `item_location` (`<item_id>|<location>`), `achievement`,
`character`, `unlock:<collection>`.

The achievement point total is computed from tier points, repeats and point caps, plus daily
and monthly AP. Treat it as an approximation of the in-game value. The item value counts every
copy of a tradeable item at trading post prices, including account-bound copies.

## Development

```sh
uv sync
uv run pytest --cov      # tests use an in-memory fake of the GW2 API; no network needed
uv run ruff check src tests && uv run ruff format --check src tests
```
