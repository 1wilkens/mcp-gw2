"""MCP server exposing the Guild Wars 2 API and the recorded account history as tools."""

from __future__ import annotations

import functools
import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, ParamSpec, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from gw2mcp import __version__
from gw2mcp.catalog import CACHEABLE_ENDPOINTS, SEARCHABLE_ENDPOINTS, Catalog, normalize_endpoint
from gw2mcp.client import GW2APIError, GW2Client, normalize_raw_path
from gw2mcp.config import Settings
from gw2mcp.history import (
    DEFAULT_COMPARE_KINDS,
    HistoryError,
    compare,
    newly_completed_achievements,
    parse_time,
    resolve_snapshot,
    series_points,
    value_before,
)
from gw2mcp.scheduler import SnapshotScheduler
from gw2mcp.snapshot import UNLOCK_KINDS, SnapshotCollector, character_items, iter_slots
from gw2mcp.storage import Storage, utcnow

log = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

INSTRUCTIONS = """\
Guild Wars 2 assistant backed by the official GW2 API (https://api.guildwars2.com/v2).

* Live account data (wallet, bank, characters, achievements, unlocks, trading post …) is read
  with the API key configured on the server.
* Public game data (items, achievements, skins, recipes, prices …) is cached locally;
  use `search` to find ids by name before calling id-based tools.
* The server periodically records account snapshots. Use `list_snapshots`, `progress_report`,
  `compare_snapshots`, `item_history`, `currency_history`, `achievement_history`,
  `stat_history`, `character_history` and `unlock_history` for questions about progress over time.
  Time arguments accept ISO dates (2025-01-31, 2025-01-31T18:00:00Z) or relative offsets
  (24h, 7d, 2w, 3mo, 1y).
* Coin values are in copper; 1 gold = 100 silver = 10000 copper. Responses include a
  human readable `*_formatted` field where useful.
"""

PUBLIC_ENDPOINTS = sorted(CACHEABLE_ENDPOINTS | {"commerce/prices", "commerce/listings", "guild"})

# Fields of v2/tokeninfo that are safe to show; `id` identifies the API key itself and is left out.
TOKEN_INFO_FIELDS = ("name", "permissions", "type", "expires_at", "issued_at", "urls")


def format_coins(copper: int | float | None) -> str:
    if copper is None:
        return "0c"
    c = int(round(copper))
    sign = "-" if c < 0 else ""
    c = abs(c)
    g, rest = divmod(c, 10000)
    s, cc = divmod(rest, 100)
    parts = []
    if g:
        parts.append(f"{g:,}g")
    if s or g:
        parts.append(f"{s}s")
    parts.append(f"{cc}c")
    return sign + " ".join(parts)


@dataclass
class AppContext:
    settings: Settings
    client: GW2Client
    storage: Storage
    catalog: Catalog
    collector: SnapshotCollector
    scheduler: SnapshotScheduler

    @classmethod
    def create(cls, settings: Settings, *, client: GW2Client | None = None, storage: Storage | None = None):
        client = client or GW2Client(
            settings.api_key,
            lang=settings.lang,
            base_url=settings.api_base_url,
            schema_version=settings.schema_version,
        )
        storage = storage or Storage(settings.db_path)
        catalog = Catalog(client, storage, ttl_days=settings.catalog_ttl_days)
        collector = SnapshotCollector(client, storage, catalog, include_value=settings.snapshot_value)
        interval = settings.snapshot_interval if settings.api_key else 0
        scheduler = SnapshotScheduler(collector.take, storage, interval)
        return cls(settings, client, storage, catalog, collector, scheduler)

    async def aclose(self) -> None:
        await self.scheduler.stop()
        await self.client.aclose()
        self.storage.close()


def _handle_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Turn expected exceptions into MCP tool errors with a helpful message."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except GW2APIError as exc:
            raise ToolError(str(exc)) from exc
        except (HistoryError, ValueError, KeyError) as exc:
            raise ToolError(str(exc)) from exc

    wrapper.__signature__ = inspect.signature(fn, eval_str=True)  # type: ignore[attr-defined]
    return wrapper


def create_server(app: AppContext) -> MCPServer:
    mcp = MCPServer(
        "gw2mcp",
        title="Guild Wars 2",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://wiki.guildwars2.com/wiki/API:Main",
    )
    client, catalog, storage = app.client, app.catalog, app.storage

    def tool(annotations: ToolAnnotations = READ_ONLY):
        def deco(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
            wrapped = _handle_errors(fn)
            mcp.add_tool(wrapped, name=fn.__name__, annotations=annotations)
            return wrapped

        return deco

    # ------------------------------------------------------------ helpers
    async def item_rows(counts: dict[str, int] | dict[int, int]) -> list[dict[str, Any]]:
        items = await catalog.get_many("items", counts.keys())
        rows = []
        for item_id, count in counts.items():
            info = items.get(str(item_id), {})
            rows.append(
                {
                    "id": int(item_id),
                    "name": info.get("name"),
                    "rarity": info.get("rarity"),
                    "type": info.get("type"),
                    "count": count,
                }
            )
        rows.sort(key=lambda r: (-(r["count"] or 0), r["name"] or ""))
        return rows

    async def currency_names(ids) -> dict[str, str]:
        return await catalog.names("currencies", ids)

    async def resolve_currency(currency: str | int) -> tuple[str, str]:
        """Accept a currency id or (partial) name; return (id, name)."""
        if isinstance(currency, int) or str(currency).strip().isdigit():
            cid = str(int(currency))
            names = await currency_names([cid])
            return cid, names.get(cid, cid)
        matches = await catalog.search("currencies", str(currency), limit=1)
        if not matches:
            raise ToolError(f"unknown currency {currency!r}")
        return str(matches[0]["id"]), matches[0]["name"]

    async def resolve_item(item: str | int) -> tuple[str, str | None]:
        if isinstance(item, int) or str(item).strip().isdigit():
            iid = str(int(item))
            info = await catalog.get_one("items", iid)
            return iid, (info or {}).get("name")
        matches = await catalog.search("items", str(item), limit=1)
        if not matches:
            raise ToolError(f"no item named {item!r}; use search(endpoint='items', ...) to look it up")
        return str(matches[0]["id"]), matches[0]["name"]

    def window(since: str | None, until: str | None):
        return (parse_time(since) if since else None, parse_time(until) if until else None)

    # ================================================== live account data
    @tool()
    async def get_account() -> dict[str, Any]:
        """Account overview: name, age, world, access, guilds, fractal level, WvW rank, AP, and API key permissions."""
        acc = await client.get("v2/account", auth=True)
        try:
            token = await client.get("v2/tokeninfo", auth=True)
            acc["token"] = {k: token[k] for k in TOKEN_INFO_FIELDS if k in token}
        except GW2APIError:
            pass
        acc["age_hours"] = round(int(acc.get("age", 0)) / 3600, 1)
        return acc

    @tool()
    async def get_wallet(only_nonzero: bool = True) -> dict[str, Any]:
        """All currencies in the account wallet (coins, karma, laurels, gems, …) with names."""
        wallet = await client.get("v2/account/wallet", auth=True)
        names = await currency_names([c["id"] for c in wallet])
        rows = [
            {"id": c["id"], "name": names.get(str(c["id"])), "value": c["value"]}
            for c in wallet
            if c["value"] or not only_nonzero
        ]
        coins = next((c["value"] for c in wallet if c["id"] == 1), 0)
        return {"currencies": rows, "coins_formatted": format_coins(coins)}

    @tool()
    async def get_bank() -> dict[str, Any]:
        """Contents of the account bank vault (item stacks aggregated by item id, with names)."""
        bank = await client.get("v2/account/bank", auth=True)
        counts: dict[int, int] = defaultdict(int)
        for slot in iter_slots(bank):
            counts[slot["id"]] += slot.get("count", 1)
        used = sum(1 for s in bank if s)
        return {"slots_used": used, "slots_total": len(bank), "items": await item_rows(counts)}

    @tool()
    async def get_materials(only_nonzero: bool = True) -> dict[str, Any]:
        """Material storage contents with item names and categories."""
        mats = await client.get("v2/account/materials", auth=True)
        counts = {m["id"]: m.get("count", 0) for m in mats if m.get("count") or not only_nonzero}
        rows = await item_rows(counts)
        cats = await catalog.names("materials", {m["category"] for m in mats if "category" in m})
        cat_of = {m["id"]: cats.get(str(m.get("category"))) for m in mats}
        for r in rows:
            r["category"] = cat_of.get(r["id"])
        return {"items": rows}

    @tool()
    async def get_shared_inventory() -> dict[str, Any]:
        """Items in the account's shared inventory slots."""
        inv = await client.get("v2/account/inventory", auth=True)
        counts: dict[int, int] = defaultdict(int)
        for slot in iter_slots(inv):
            counts[slot["id"]] += slot.get("count", 1)
        return {"slots_total": len(inv), "items": await item_rows(counts)}

    @tool()
    async def list_characters() -> dict[str, Any]:
        """All characters with level, profession, race, playtime, deaths and creation date."""
        chars = await client.get("v2/characters", {"ids": "all"}, auth=True)
        rows = [
            {
                "name": c.get("name"),
                "level": c.get("level"),
                "profession": c.get("profession"),
                "race": c.get("race"),
                "gender": c.get("gender"),
                "playtime_hours": round(int(c.get("age", 0)) / 3600, 1),
                "deaths": c.get("deaths"),
                "created": c.get("created"),
                "crafting": c.get("crafting"),
            }
            for c in chars
        ]
        return {"characters": rows}

    @tool()
    async def get_character(
        name: str,
        section: Literal[
            "all", "core", "inventory", "equipment", "build", "crafting", "training", "sab", "recipes", "heropoints"
        ] = "core",
    ) -> dict[str, Any]:
        """Details for one character. `section` picks a sub-endpoint; 'inventory' and 'equipment' include item names."""
        base = f"v2/characters/{name}"
        if section == "all":
            data = await client.get(base, auth=True)
        elif section == "build":
            data = await client.get(f"{base}/buildtabs", {"tabs": "all"}, auth=True)
        else:
            data = await client.get(f"{base}/{section}", auth=True)
        if section == "inventory":
            counts = character_items({"bags": data.get("bags")})
            return {"name": name, "items": await item_rows(counts)}
        if section == "equipment":
            eq = data.get("equipment", [])
            names = await catalog.names("items", [e["id"] for e in eq if "id" in e])
            for e in eq:
                e["name"] = names.get(str(e.get("id")))
            return {"name": name, "equipment": eq}
        return data if isinstance(data, dict) else {"name": name, section: data}

    @tool()
    async def find_items(items: list[str]) -> dict[str, Any]:
        """Locate items (ids or names) across bank, material storage, shared inventory, all characters and TP delivery box."""
        resolved = [await resolve_item(i) for i in items]
        wanted = {iid for iid, _ in resolved}
        found: dict[str, list[dict[str, Any]]] = {iid: [] for iid in wanted}

        def add(loc: str, slots) -> None:
            for slot in iter_slots(slots):
                if str(slot["id"]) in wanted:
                    found[str(slot["id"])].append({"location": loc, "count": slot.get("count", 1)})

        errors = {}
        for loc, path in (
            ("bank", "v2/account/bank"),
            ("materials", "v2/account/materials"),
            ("shared_inventory", "v2/account/inventory"),
        ):
            try:
                add(loc, await client.get(path, auth=True))
            except GW2APIError as exc:
                errors[loc] = exc.message
        try:
            for c in await client.get("v2/characters", {"ids": "all"}, auth=True):
                for item_id, count in character_items(c).items():
                    if str(item_id) in wanted:
                        found[str(item_id)].append({"location": f"character:{c['name']}", "count": count})
        except GW2APIError as exc:
            errors["characters"] = exc.message
        try:
            add("tp_delivery", (await client.get("v2/commerce/delivery", auth=True)).get("items"))
        except GW2APIError as exc:
            errors["tp_delivery"] = exc.message
        result = []
        for iid, name in resolved:
            locs = found[iid]
            result.append({"id": int(iid), "name": name, "total": sum(x["count"] for x in locs), "locations": locs})
        return {"items": result, "errors": errors}

    @tool()
    async def get_account_achievements(
        ids: list[int] | None = None,
        status: Literal["all", "done", "in_progress"] = "all",
        category_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Account achievement progress, optionally filtered by ids, completion status or achievement category, with names."""
        progress = await client.get("v2/account/achievements", auth=True)
        wanted: set[int] | None = set(ids) if ids else None
        if category_id is not None:
            cat = await catalog.get_one("achievements/categories", category_id)
            if not cat:
                raise ToolError(f"unknown achievement category {category_id}")
            cat_ids = {a["id"] if isinstance(a, dict) else a for a in cat.get("achievements", [])}
            wanted = cat_ids if wanted is None else wanted & cat_ids
        rows = [
            p
            for p in progress
            if (wanted is None or p["id"] in wanted) and (status == "all" or (status == "done") == bool(p.get("done")))
        ]
        total = len(rows)
        rows = rows[:limit]
        defs = await catalog.get_many("achievements", [p["id"] for p in rows])
        out = []
        for p in rows:
            d = defs.get(str(p["id"]), {})
            out.append(
                {
                    "id": p["id"],
                    "name": d.get("name"),
                    "requirement": d.get("requirement"),
                    "current": p.get("current"),
                    "max": p.get("max"),
                    "done": p.get("done"),
                    "repeated": p.get("repeated"),
                    "bits_done": len(p.get("bits") or []),
                }
            )
        return {"total_matching": total, "returned": len(out), "achievements": out}

    @tool()
    async def get_unlocks(kind: str, resolve_names: bool = True, limit: int = 500) -> dict[str, Any]:
        """Unlocked account collections. `kind` is one of: skins, dyes, minis, outfits, titles, recipes, finishers,
        gliders, mailcarriers, novelties, emotes, jadebots, skiffs, mount_skins, mount_types, legendary_armory,
        masteries, home_cats, home_nodes, homestead_decorations, homestead_glyphs, pvp_heroes, and the daily/weekly
        completion lists dungeon_paths_today, raid_encounters_this_week, world_bosses_today, map_chests_today,
        daily_crafting_today."""
        if kind not in UNLOCK_KINDS:
            raise ToolError(f"unknown kind {kind!r}; choose one of {sorted(UNLOCK_KINDS)}")
        account_ep, public_ep = UNLOCK_KINDS[kind]
        data = await client.get("v2/" + account_ep, auth=True)
        entries = [e if isinstance(e, dict) else {"id": e} for e in data or []]
        if resolve_names and public_ep and kind != "recipes":
            names = await catalog.names(public_ep, [e["id"] for e in entries[:limit]])
            for e in entries[:limit]:
                e["name"] = names.get(str(e["id"]))
        return {"kind": kind, "count": len(entries), "entries": entries[:limit]}

    @tool()
    async def get_wizards_vault(period: Literal["daily", "weekly", "special", "listings"] = "daily") -> dict[str, Any]:
        """Wizard's Vault objectives and progress (daily/weekly/special) or purchased reward listings.
        Without an API key, the public season catalog (all objectives or all listings) is returned instead."""
        if not client.has_key:
            season = await client.get("v2/wizardsvault")
            sub = "listings" if period == "listings" else "objectives"
            entries = await client.get(f"v2/wizardsvault/{sub}", {"ids": "all"})
            if sub == "listings":
                names = await catalog.names("items", {e["item_id"] for e in entries})
                for e in entries:
                    e["item_name"] = names.get(str(e["item_id"]))
            return {
                "note": "no API key configured: showing the public season catalog without account progress",
                "season": {k: season.get(k) for k in ("title", "start", "end")},
                sub: entries,
            }
        data = await client.get(f"v2/account/wizardsvault/{period}", auth=True)
        return data if isinstance(data, dict) else {"entries": data}

    @tool()
    async def get_trading_post(
        kind: Literal["current", "history", "delivery"] = "current",
        side: Literal["buys", "sells"] = "sells",
        page: int = 0,
    ) -> dict[str, Any]:
        """Your trading post orders: open (`current`) or completed in the last 90 days (`history`), or items/coins
        waiting for pickup (`delivery`)."""
        if kind == "delivery":
            d = await client.get("v2/commerce/delivery", auth=True)
            return {
                "coins": d.get("coins", 0),
                "coins_formatted": format_coins(d.get("coins", 0)),
                "items": await item_rows({i["id"]: i["count"] for i in d.get("items", [])}),
            }
        rows = await client.get(f"v2/commerce/transactions/{kind}/{side}", {"page": page, "page_size": 200}, auth=True)
        names = await catalog.names("items", [r["item_id"] for r in rows])
        for r in rows:
            r["item_name"] = names.get(str(r["item_id"]))
            r["price_formatted"] = format_coins(r.get("price"))
        return {"kind": kind, "side": side, "page": page, "transactions": rows}

    @tool()
    async def get_pvp(section: Literal["stats", "games", "standings"] = "stats") -> dict[str, Any]:
        """PvP rank and win/loss stats, the most recent games, or ranked season standings."""
        if section == "games":
            data = await client.get("v2/pvp/games", {"ids": "all"}, auth=True)
        else:
            data = await client.get(f"v2/pvp/{section}", auth=True)
        return data if isinstance(data, dict) else {section: data}

    @tool()
    async def estimate_account_value() -> dict[str, Any]:
        """Estimate the trading post value of all items on the account (bank, materials, shared inventory,
        characters) at current buy/sell prices, plus liquid gold. Account-bound items have no price."""
        collected = await app.collector.collect()
        items = collected.data.get("item")
        if items is None:
            raise ToolError(f"could not read all inventories: {collected.errors}")
        prices = {str(p["id"]): p for p in await client.get_many("v2/commerce/prices", items.keys())}
        rows = []
        buy = sell = 0
        for iid, count in items.items():
            p = prices.get(iid)
            if not p:
                continue
            b = count * p["buys"]["unit_price"]
            s = count * p["sells"]["unit_price"]
            buy += b
            sell += s
            rows.append({"id": int(iid), "count": count, "value_buy": b, "value_sell": s})
        rows.sort(key=lambda r: -r["value_sell"])
        top = rows[:25]
        names = await catalog.names("items", [r["id"] for r in top])
        for r in top:
            r["name"] = names.get(str(r["id"]))
            r["value_sell_formatted"] = format_coins(r["value_sell"])
        coins = (collected.data.get("wallet") or {}).get("1", 0)
        return {
            "items_value_buy": buy,
            "items_value_sell": sell,
            "items_value_buy_formatted": format_coins(buy),
            "items_value_sell_formatted": format_coins(sell),
            "items_value_sell_after_fees_formatted": format_coins(sell * 0.85),
            "liquid_coins": coins,
            "liquid_coins_formatted": format_coins(coins),
            "most_valuable": top,
        }

    # ===================================================== game data
    @tool()
    async def search(endpoint: str, query: str, limit: int = 20) -> dict[str, Any]:
        """Find game objects by (partial) name. `endpoint` examples: items, achievements, skins, currencies, minis,
        titles, outfits, colors, masteries, maps, skills, traits, specializations, mounts/skins, gliders, novelties.
        The first search on a large endpoint (items, skins) downloads its index and can take a while."""
        try:
            results = await catalog.search(endpoint, query, limit=max(1, min(limit, 100)))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {"endpoint": normalize_endpoint(endpoint), "query": query, "results": [_summary(r) for r in results]}

    @tool()
    async def get_game_data(endpoint: str, ids: list[str]) -> dict[str, Any]:
        """Fetch public game objects by id from a GW2 API endpoint, e.g. endpoint='items', ids=['19721'].
        Supports items, achievements, achievements/categories, skins, recipes, currencies, colors, minis, maps,
        skills, traits, specializations, professions, masteries, titles, worldbosses, raids, dungeons, …"""
        ep = normalize_endpoint(endpoint)
        if ep not in CACHEABLE_ENDPOINTS:
            raise ToolError(f"unsupported endpoint {ep!r}; supported: {sorted(CACHEABLE_ENDPOINTS)}")
        objs = await catalog.get_many(ep, ids)
        return {
            "endpoint": ep,
            "found": len(objs),
            "missing": [i for i in ids if str(i) not in objs],
            "objects": list(objs.values()),
        }

    @tool()
    async def get_prices(items: list[str]) -> dict[str, Any]:
        """Current trading post buy/sell prices (highest buy order, lowest sell listing) for item ids or names."""
        resolved = [await resolve_item(i) for i in items]
        prices = {str(p["id"]): p for p in await client.get_many("v2/commerce/prices", [i for i, _ in resolved])}
        out = []
        for iid, name in resolved:
            p = prices.get(iid)
            if p is None:
                out.append({"id": int(iid), "name": name, "tradeable": False})
                continue
            buy, sell = p["buys"]["unit_price"], p["sells"]["unit_price"]
            out.append(
                {
                    "id": int(iid),
                    "name": name,
                    "tradeable": True,
                    "buy_price": buy,
                    "buy_quantity": p["buys"]["quantity"],
                    "sell_price": sell,
                    "sell_quantity": p["sells"]["quantity"],
                    "buy_formatted": format_coins(buy),
                    "sell_formatted": format_coins(sell),
                    "flip_profit_after_fees": int(sell * 0.85) - buy,
                }
            )
        return {"prices": out}

    @tool()
    async def get_listings(item: str, depth: int = 10) -> dict[str, Any]:
        """Trading post order book (buy orders and sell listings by price) for one item id or name."""
        iid, name = await resolve_item(item)
        data = await client.get(f"v2/commerce/listings/{iid}")
        return {
            "id": int(iid),
            "name": name,
            "buys": data.get("buys", [])[:depth],
            "sells": data.get("sells", [])[:depth],
        }

    @tool()
    async def gem_exchange(direction: Literal["coins_to_gems", "gems_to_coins"], quantity: int) -> dict[str, Any]:
        """Current currency exchange rate: how many gems you get for `quantity` copper, or coins for `quantity` gems."""
        path = "v2/commerce/exchange/coins" if direction == "coins_to_gems" else "v2/commerce/exchange/gems"
        data = await client.get(path, {"quantity": quantity})
        if direction == "gems_to_coins":
            data["quantity_formatted"] = format_coins(data.get("quantity"))
        else:
            data["input_formatted"] = format_coins(quantity)
        return data

    @tool()
    async def find_recipes(input_item: str | None = None, output_item: str | None = None) -> dict[str, Any]:
        """Recipes that use an item as ingredient (`input_item`) or craft it (`output_item`); ids or names."""
        if bool(input_item) == bool(output_item):
            raise ToolError("pass exactly one of input_item or output_item")
        iid, name = await resolve_item(input_item or output_item)  # type: ignore[arg-type]
        param = "input" if input_item else "output"
        ids = await client.get("v2/recipes/search", {param: iid})
        recipes = list((await catalog.get_many("recipes", ids[:50])).values())
        item_ids = {r["output_item_id"] for r in recipes}
        for r in recipes:
            item_ids |= {i["id"] for i in r.get("ingredients", [])}
        names = await catalog.names("items", item_ids)
        for r in recipes:
            r["output_item_name"] = names.get(str(r["output_item_id"]))
            for ing in r.get("ingredients", []):
                ing["name"] = names.get(str(ing["id"]))
        return {"item": {"id": int(iid), "name": name}, "mode": param, "total": len(ids), "recipes": recipes}

    @tool()
    async def get_daily_rotations() -> dict[str, Any]:
        """Currently active world bosses list, map chests, dungeons and raids (static rotation data) plus the
        account's completions for today/this week."""
        out: dict[str, Any] = {}
        for kind in (
            "world_bosses_today",
            "map_chests_today",
            "dungeon_paths_today",
            "raid_encounters_this_week",
            "daily_crafting_today",
        ):
            try:
                out[kind] = await client.get("v2/" + UNLOCK_KINDS[kind][0], auth=True)
            except GW2APIError as exc:
                out[kind] = {"error": exc.message}
        out["all_world_bosses"] = await client.get("v2/worldbosses")
        return out

    @tool()
    async def api_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """Raw GET request to a GW2 API v2 endpoint (escape hatch), e.g. path='v2/wvw/matches/overview',
        params={'world': '2006'}. The configured API key is sent automatically. Credential endpoints
        (createsubtoken, tokeninfo) are blocked; pass paths unencoded."""
        data = await client.get(normalize_raw_path(path), params or None)
        return data if isinstance(data, dict) else {"data": data}

    # ======================================================== history
    @tool(ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    async def take_snapshot() -> dict[str, Any]:
        """Record a snapshot of the account right now (it is also done automatically on a schedule)."""
        snap = await app.collector.take()
        return snap.as_dict()

    @tool(LOCAL_READ)
    async def list_snapshots(since: str | None = None, until: str | None = None, limit: int = 50) -> dict[str, Any]:
        """List recorded snapshots (newest first) with timestamps and any section errors."""
        s, u = window(since, until)
        snaps = storage.list_snapshots(since=s, until=u, limit=limit)
        first = storage.first_snapshot()
        return {
            "total_snapshots": storage.count_snapshots(),
            "first_snapshot_at": first.as_dict()["taken_at"] if first else None,
            "snapshot_interval_seconds": app.scheduler.interval,
            "snapshots": [x.as_dict() for x in snaps],
        }

    @tool(LOCAL_READ)
    async def get_snapshot(snapshot: str = "latest") -> dict[str, Any]:
        """Account stats recorded at a snapshot (`latest`, `first`, an id, ISO time or offset like `30d`):
        AP, gold, playtime, item value, unlock counts, mastery points, fractal level, ranks …"""
        snap = resolve_snapshot(storage, snapshot)
        stats = storage.state("stat", snap.id)
        for key in ("item_value_buy_copper", "item_value_sell_copper"):
            if key in stats:
                stats[key.replace("_copper", "_formatted")] = format_coins(stats[key])
        wallet = storage.state("wallet", snap.id)
        names = await currency_names(wallet.keys())
        return {
            "snapshot": snap.as_dict(),
            "stats": stats,
            "wallet": {names.get(k, k): v for k, v in wallet.items() if v},
        }

    @tool(LOCAL_READ)
    async def list_tracked_stats() -> dict[str, Any]:
        """Names of all tracked account stats usable with `stat_history`, and the recorded data kinds."""
        return {"stats": storage.keys("stat"), "kinds": storage.kinds()}

    @tool(LOCAL_READ)
    async def stat_history(stat: str, since: str | None = None, until: str | None = None) -> dict[str, Any]:
        """How an account stat changed over time (only change points are stored), e.g. achievement_points, gold,
        playtime_hours, fractal_level, wvw_rank, item_value_sell_copper, unlocked:skins, mastery_points_earned."""
        s, u = window(since, until)
        points = series_points(storage.series("stat", stat, since=s, until=u))
        if not points and stat not in storage.keys("stat"):
            raise ToolError(f"unknown stat {stat!r}; see list_tracked_stats")
        return {
            "stat": stat,
            "value_before_window": value_before(storage, "stat", stat, s),
            "current": storage.value_at("stat", stat),
            "changes": points,
        }

    @tool(LOCAL_READ)
    async def item_history(
        item: str, since: str | None = None, until: str | None = None, by_location: bool = False
    ) -> dict[str, Any]:
        """How many of an item (id or name) the account owned over time, optionally split by location
        (bank, materials, shared_inventory, character:<name>, tp_delivery)."""
        iid, name = await resolve_item(item)
        s, u = window(since, until)
        out: dict[str, Any] = {
            "id": int(iid),
            "name": name,
            "current_total": storage.value_at("item", iid) or 0,
            "total_before_window": value_before(storage, "item", iid, s) if s else None,
            "total_changes": series_points(storage.series("item", iid, since=s, until=u), numeric_default=0),
        }
        if by_location:
            by_loc: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for o in storage.series("item_location", key_prefix=f"{iid}|", since=s, until=u):
                loc = o.key.split("|", 1)[1]
                by_loc[loc].append(
                    {"snapshot_id": o.snapshot_id, "taken_at": o.taken_at.isoformat(), "count": o.value or 0}
                )
            out["locations"] = dict(by_loc)
        return out

    @tool(LOCAL_READ)
    async def currency_history(currency: str, since: str | None = None, until: str | None = None) -> dict[str, Any]:
        """Wallet currency (id or name, e.g. 'Coin', 'Karma', 'Astral Acclaim') over time."""
        cid, name = await resolve_currency(currency)
        s, u = window(since, until)
        points = series_points(storage.series("wallet", cid, since=s, until=u), numeric_default=0)
        if cid == "1":
            for p in points:
                p["formatted"] = format_coins(p["value"])
        return {
            "id": int(cid),
            "name": name,
            "current": storage.value_at("wallet", cid) or 0,
            "value_before_window": value_before(storage, "wallet", cid, s),
            "changes": points,
        }

    @tool(LOCAL_READ)
    async def achievement_history(achievement_id: int, since: str | None = None) -> dict[str, Any]:
        """Progress of one achievement over time (current/max, done, repeats) and when it was completed."""
        aid = str(achievement_id)
        s, _ = window(since, None)
        obs = storage.series("achievement", aid, since=s)
        defn = await catalog.get_one("achievements", aid)
        completed_at = next((o.taken_at.isoformat() for o in obs if (o.value or {}).get("done")), None)
        return {
            "id": achievement_id,
            "name": (defn or {}).get("name"),
            "current": storage.value_at("achievement", aid),
            "first_seen_done_at": completed_at,
            "changes": series_points(obs),
        }

    @tool(LOCAL_READ)
    async def character_history(name: str, since: str | None = None) -> dict[str, Any]:
        """A character's recorded level, playtime, deaths and crafting levels over time."""
        s, _ = window(since, None)
        obs = storage.series("character", name, since=s)
        if not obs and name not in storage.keys("character"):
            raise ToolError(f"no history for character {name!r}; known: {storage.keys('character')}")
        return {"name": name, "current": storage.value_at("character", name), "changes": series_points(obs)}

    @tool(LOCAL_READ)
    async def unlock_history(kind: str, since: str = "30d", until: str | None = None) -> dict[str, Any]:
        """What was unlocked (or completed) in a time window, e.g. kind='skins', since='7d'. Kinds are the same
        as for `get_unlocks`."""
        if kind not in UNLOCK_KINDS:
            raise ToolError(f"unknown kind {kind!r}; choose one of {sorted(UNLOCK_KINDS)}")
        s, u = window(since, until)
        obs = storage.series(f"unlock:{kind}", since=s, until=u)
        first_ever = storage.first_snapshot()
        gained = [o for o in obs if o.value is not None and o.snapshot_id != (first_ever.id if first_ever else -1)]
        lost = [o for o in obs if o.value is None]
        public_ep = UNLOCK_KINDS[kind][1]
        names = (
            await catalog.names(public_ep, [o.key for o in gained + lost]) if public_ep and kind != "recipes" else {}
        )
        fmt = lambda o: {"id": o.key, "name": names.get(o.key), "at": o.taken_at.isoformat(), "value": o.value}  # noqa: E731
        return {
            "kind": kind,
            "note": "entries present in the very first snapshot are baseline and not listed as new",
            "unlocked": [fmt(o) for o in gained],
            "removed": [fmt(o) for o in lost],
        }

    @tool(LOCAL_READ)
    async def compare_snapshots(
        start: str = "7d",
        end: str = "latest",
        kinds: list[str] | None = None,
        limit_per_kind: int = 100,
    ) -> dict[str, Any]:
        """Detailed diff between two snapshots (ids, `latest`, `first`, ISO times or offsets like `7d`).
        `kinds` defaults to stat, wallet, item, achievement, character; also possible: item_location and
        unlock:<kind> (e.g. unlock:skins)."""
        a = resolve_snapshot(storage, start, default="first")
        b = resolve_snapshot(storage, end)
        diff = compare(storage, a, b, kinds or DEFAULT_COMPARE_KINDS)
        out: dict[str, Any] = {"from": a.as_dict(), "to": b.as_dict(), "changes": {}}
        for kind, changes in diff.items():
            entries = list(changes.items())
            if kind in ("item", "wallet"):
                entries.sort(key=lambda kv: -abs(kv[1].get("delta", 0)))
            total = len(entries)
            entries = entries[:limit_per_kind]
            names: dict[str, str] = {}
            if kind == "item":
                names = await catalog.names("items", [k for k, _ in entries])
            elif kind == "wallet":
                names = await currency_names([k for k, _ in entries])
            elif kind == "achievement":
                names = await catalog.names("achievements", [k for k, _ in entries])
            rows = [{"key": k, **({"name": names[k]} if k in names else {}), **v} for k, v in entries]
            out["changes"][kind] = {"total_changes": total, "entries": rows}
        return out

    @tool(LOCAL_READ)
    async def progress_report(since: str = "7d", until: str = "latest") -> dict[str, Any]:
        """Summary of account progress between two points in time: stat deltas (AP, gold, playtime, item value…),
        currency changes, newly completed achievements, new unlocks, biggest item gains/losses, character levels."""
        a = resolve_snapshot(storage, since, default="first")
        b = resolve_snapshot(storage, until)
        kinds = [*DEFAULT_COMPARE_KINDS, *(f"unlock:{k}" for k in UNLOCK_KINDS if not k.endswith(("today", "week")))]
        diff = compare(storage, a, b, kinds)
        stats = {k: v for k, v in diff["stat"].items()}
        wallet_names = await currency_names(diff["wallet"].keys())
        wallet = [
            {
                "currency": wallet_names.get(k, k),
                "before": v["before"] or 0,
                "after": v["after"] or 0,
                "delta": v.get("delta"),
            }
            for k, v in diff["wallet"].items()
        ]
        wallet.sort(key=lambda r: -abs(r["delta"] or 0))
        done_ids = newly_completed_achievements(diff["achievement"])
        ach_names = await catalog.names("achievements", done_ids[:100])
        items = sorted(diff["item"].items(), key=lambda kv: kv[1].get("delta", 0))
        losses = [kv for kv in items[:15] if kv[1].get("delta", 0) < 0]
        gains = [kv for kv in reversed(items[-15:]) if kv[1].get("delta", 0) > 0]
        item_names = await catalog.names("items", [k for k, _ in losses + gains])
        fmt_item = lambda kv: {"id": int(kv[0]), "name": item_names.get(kv[0]), "delta": kv[1].get("delta")}  # noqa: E731
        unlocks = {
            kind.split(":", 1)[1]: sum(1 for c in changes.values() if c["before"] is None)
            for kind, changes in diff.items()
            if kind.startswith("unlock:") and changes
        }
        return {
            "from": a.as_dict()["taken_at"],
            "to": b.as_dict()["taken_at"],
            "days": round((b.taken_at - a.taken_at).total_seconds() / 86400, 2),
            "stats": stats,
            "gold_delta_formatted": format_coins(stats["gold"]["delta"] * 10000)
            if "gold" in stats and "delta" in stats["gold"]
            else None,
            "wallet": wallet[:30],
            "achievements_completed": [{"id": int(i), "name": ach_names.get(i)} for i in done_ids],
            "achievements_progressed": len(diff["achievement"]) - len(done_ids),
            "new_unlocks": unlocks,
            "top_item_gains": [fmt_item(kv) for kv in gains],
            "top_item_losses": [fmt_item(kv) for kv in losses],
            "characters": diff["character"],
        }

    @tool(LOCAL_READ)
    async def server_status() -> dict[str, Any]:
        """Server configuration and health: API key presence, snapshot schedule, database stats."""
        last = storage.latest_snapshot()
        return {
            "version": __version__,
            "api_key_configured": client.has_key,
            "language": app.settings.lang,
            "snapshot_interval_seconds": app.scheduler.interval,
            "scheduler_runs": app.scheduler.runs,
            "scheduler_failures": app.scheduler.failures,
            "snapshots": storage.count_snapshots(),
            "last_snapshot": last.as_dict() if last else None,
            "searchable_endpoints": sorted(SEARCHABLE_ENDPOINTS),
            "now": utcnow().isoformat(timespec="seconds"),
        }

    # ========================================================= prompts
    @mcp.prompt()
    def weekly_review() -> str:
        """Review the account's progress over the last week."""
        return (
            "Use the gw2mcp tools to review my Guild Wars 2 progress over the last 7 days. Call progress_report "
            "with since='7d', then highlight achievements, gold/currency changes, notable item gains/losses and "
            "new unlocks. Finish with suggestions for what to work on next."
        )

    @mcp.prompt()
    def item_tracking(item: str) -> str:
        """Analyse how the amount of an item changed over time."""
        return (
            f"Use item_history with item={item!r} and by_location=true, then get_prices for it, and explain how "
            "my holdings of this item and their value evolved."
        )

    return mcp


def _summary(obj: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "title", "type", "rarity", "level", "description", "requirement", "icon")
    return {k: obj[k] for k in keys if k in obj and obj[k] not in (None, "")}
