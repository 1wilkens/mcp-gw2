"""Collect a snapshot of the account state and persist it to the history store."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from gw2mcp.catalog import Catalog
from gw2mcp.client import GW2APIError, GW2AuthError, GW2Client
from gw2mcp.storage import Snapshot, Storage


class SnapshotError(GW2APIError):
    """A snapshot collected nothing at all (no key, invalid key or API outage)."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        details = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "unknown error"
        super().__init__(0, details, "snapshot")
        self.args = (f"snapshot failed, nothing collected ({details})",)


log = logging.getLogger(__name__)

# kind name -> (account endpoint, public endpoint used to resolve names)
UNLOCK_KINDS: dict[str, tuple[str, str | None]] = {
    "skins": ("account/skins", "skins"),
    "dyes": ("account/dyes", "colors"),
    "minis": ("account/minis", "minis"),
    "outfits": ("account/outfits", "outfits"),
    "titles": ("account/titles", "titles"),
    "recipes": ("account/recipes", "recipes"),
    "finishers": ("account/finishers", "finishers"),
    "gliders": ("account/gliders", "gliders"),
    "mailcarriers": ("account/mailcarriers", "mailcarriers"),
    "novelties": ("account/novelties", "novelties"),
    "emotes": ("account/emotes", None),
    "jadebots": ("account/jadebots", "jadebots"),
    "skiffs": ("account/skiffs", "skiffs"),
    "mount_skins": ("account/mounts/skins", "mounts/skins"),
    "mount_types": ("account/mounts/types", "mounts/types"),
    "legendary_armory": ("account/legendaryarmory", "items"),
    "masteries": ("account/masteries", "masteries"),
    "home_cats": ("account/home/cats", None),
    "home_nodes": ("account/home/nodes", None),
    "homestead_decorations": ("account/homestead/decorations", "homestead/decorations"),
    "homestead_glyphs": ("account/homestead/glyphs", "homestead/glyphs"),
    "pvp_heroes": ("account/pvp/heroes", "pvp/heroes"),
    "dungeon_paths_today": ("account/dungeons", None),
    "raid_encounters_this_week": ("account/raids", None),
    "world_bosses_today": ("account/worldbosses", None),
    "map_chests_today": ("account/mapchests", None),
    "daily_crafting_today": ("account/dailycrafting", None),
}

ITEM_KINDS = ("item", "item_location")


def unlock_value(entry: Any) -> tuple[str, Any] | None:
    """Turn an entry of an account unlock list into ``(key, value)``."""
    if isinstance(entry, (int, str)):
        return str(entry), 1
    if isinstance(entry, dict) and "id" in entry:
        rest = {k: v for k, v in entry.items() if k != "id"}
        if not rest:
            return str(entry["id"]), 1
        if len(rest) == 1:
            return str(entry["id"]), next(iter(rest.values()))
        return str(entry["id"]), rest
    return None


def iter_slots(slots: Iterable[Any] | None) -> Iterable[dict[str, Any]]:
    for slot in slots or ():
        if isinstance(slot, dict) and slot.get("id") is not None:
            yield slot


def character_items(char: Mapping[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for bag in char.get("bags") or ():
        if not isinstance(bag, dict):
            continue
        if bag.get("id") is not None:
            counts[int(bag["id"])] += 1
        for slot in iter_slots(bag.get("inventory")):
            counts[int(slot["id"])] += int(slot.get("count", 1) or 0)
    for eq in iter_slots(char.get("equipment")):
        counts[int(eq["id"])] += int(eq.get("count", 1) or 1)
    return dict(counts)


def achievement_points(defn: Mapping[str, Any], progress: Mapping[str, Any]) -> int:
    """Approximate AP earned for one achievement, following the official tier rules."""
    tiers = defn.get("tiers") or []
    total_tier_points = sum(int(t.get("points", 0)) for t in tiers)
    current = int(progress.get("current") or 0)
    if progress.get("done"):
        earned = total_tier_points
    else:
        earned = sum(int(t.get("points", 0)) for t in tiers if current >= int(t.get("count", 0)))
    repeated = int(progress.get("repeated") or 0)
    if repeated:
        earned += repeated * total_tier_points
    cap = defn.get("point_cap")
    if isinstance(cap, int) and cap >= 0:
        earned = min(earned, cap)
    return earned


@dataclass
class CollectedData:
    data: dict[str, dict[str, Any]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    account: dict[str, Any] = field(default_factory=dict)
    characters: list[dict[str, Any]] | None = None
    delivery_coins: int | None = None


class SnapshotCollector:
    def __init__(self, client: GW2Client, storage: Storage, catalog: Catalog, *, include_value: bool = True) -> None:
        self.client = client
        self.storage = storage
        self.catalog = catalog
        self.include_value = include_value
        self._lock = asyncio.Lock()

    async def take(self) -> Snapshot:
        """Collect and persist a snapshot. Concurrent calls are serialized."""
        async with self._lock:
            start = time.monotonic()
            collected = await self.collect()
            if not collected.data and not collected.stats:
                raise SnapshotError(collected.errors)
            data = dict(collected.data)
            # stats are merged with the previous state so a failed section never "deletes" a stat
            data["stat"] = {**self.storage.state("stat"), **collected.stats}
            snap = self.storage.save_snapshot(
                data,
                account_id=collected.account.get("id"),
                account_name=collected.account.get("name"),
                errors=collected.errors,
                duration=time.monotonic() - start,
            )
            log.info(
                "snapshot %s saved (%d kinds, %d errors) in %.1fs",
                snap.id,
                len(data),
                len(collected.errors),
                snap.duration or 0,
            )
            return snap

    async def collect(self) -> CollectedData:
        out = CollectedData()
        if not self.client.has_key:
            out.errors["api_key"] = "no API key configured"
            return out

        async def run(name: str, fn: Callable[[CollectedData], Awaitable[None]]) -> None:
            try:
                await fn(out)
            except GW2AuthError as exc:
                out.errors[name] = f"unavailable (permission?): {exc.message}"
            except Exception as exc:  # noqa: BLE001 - one failing section must not abort the snapshot
                log.warning("snapshot section %s failed: %s", name, exc)
                out.errors[name] = str(exc)

        await asyncio.gather(
            run("account", self._account),
            run("wallet", self._wallet),
            run("items", self._items),
            run("achievements", self._achievements),
            run("mastery_points", self._mastery_points),
            run("luck", self._luck),
            run("progression", self._progression),
            run("pvp", self._pvp),
            *(run(f"unlock:{kind}", self._unlock_fn(kind)) for kind in UNLOCK_KINDS),
        )
        if self.include_value and "item" in out.data:
            await run("value", self._value)
        self._finalize(out)
        return out

    # ------------------------------------------------------------- sections
    async def _account(self, out: CollectedData) -> None:
        acc = await self.client.get("v2/account", auth=True)
        out.account = acc
        stats = out.stats
        stats["account_age_hours"] = round(int(acc.get("age", 0)) / 3600, 1)
        for key in ("fractal_level", "daily_ap", "monthly_ap"):
            if key in acc:
                stats[key] = acc[key]
        wvw = acc.get("wvw")
        if isinstance(wvw, dict) and "rank" in wvw:
            stats["wvw_rank"] = wvw["rank"]
        elif "wvw_rank" in acc:
            stats["wvw_rank"] = acc["wvw_rank"]
        if "commander" in acc:
            stats["commander"] = bool(acc["commander"])
        stats["guild_count"] = len(acc.get("guilds") or [])

    async def _wallet(self, out: CollectedData) -> None:
        wallet = await self.client.get("v2/account/wallet", auth=True)
        out.data["wallet"] = {str(c["id"]): int(c["value"]) for c in wallet}
        out.stats["gold"] = round(out.data["wallet"].get("1", 0) / 10000, 4)

    async def _items(self, out: CollectedData) -> None:
        sources: dict[str, Callable[[CollectedData], Awaitable[dict[str, dict[int, int]]]]] = {
            "bank": self._bank,
            "materials": self._materials,
            "shared_inventory": self._shared,
            "characters": self._characters_items,
            "tp_delivery": self._delivery,
        }
        results = await asyncio.gather(*(fn(out) for fn in sources.values()), return_exceptions=True)
        locations: dict[str, dict[int, int]] = {}
        transient_failure = False
        for name, result in zip(sources, results, strict=True):
            if isinstance(result, GW2AuthError):
                out.errors[f"items:{name}"] = f"unavailable (permission?): {result.message}"
            elif isinstance(result, BaseException):
                out.errors[f"items:{name}"] = str(result)
                transient_failure = True
            else:
                locations.update(result)
        if transient_failure:
            # incomplete totals would look like items vanished; skip item tracking this round
            return
        totals: dict[str, int] = defaultdict(int)
        per_location: dict[str, int] = {}
        for loc, counts in locations.items():
            for item_id, count in counts.items():
                if count <= 0:
                    continue
                totals[str(item_id)] += count
                per_location[f"{item_id}|{loc}"] = count
        out.data["item"] = dict(totals)
        out.data["item_location"] = per_location
        out.stats["unique_items"] = len(totals)
        out.stats["total_item_count"] = sum(totals.values())

    async def _bank(self, out: CollectedData) -> dict[str, dict[int, int]]:
        bank = await self.client.get("v2/account/bank", auth=True)
        counts: dict[int, int] = defaultdict(int)
        for slot in iter_slots(bank):
            counts[int(slot["id"])] += int(slot.get("count", 1))
        return {"bank": dict(counts)}

    async def _materials(self, out: CollectedData) -> dict[str, dict[int, int]]:
        mats = await self.client.get("v2/account/materials", auth=True)
        counts: dict[int, int] = defaultdict(int)
        for slot in iter_slots(mats):
            counts[int(slot["id"])] += int(slot.get("count", 0))
        return {"materials": dict(counts)}

    async def _shared(self, out: CollectedData) -> dict[str, dict[int, int]]:
        inv = await self.client.get("v2/account/inventory", auth=True)
        counts: dict[int, int] = defaultdict(int)
        for slot in iter_slots(inv):
            counts[int(slot["id"])] += int(slot.get("count", 1))
        return {"shared_inventory": dict(counts)}

    async def _delivery(self, out: CollectedData) -> dict[str, dict[int, int]]:
        delivery = await self.client.get("v2/commerce/delivery", auth=True)
        out.delivery_coins = int(delivery.get("coins", 0))
        counts: dict[int, int] = defaultdict(int)
        for slot in iter_slots(delivery.get("items")):
            counts[int(slot["id"])] += int(slot.get("count", 1))
        return {"tp_delivery": dict(counts)}

    async def _characters_items(self, out: CollectedData) -> dict[str, dict[int, int]]:
        chars = await self.client.get("v2/characters", {"ids": "all"}, auth=True)
        out.characters = [c for c in chars if isinstance(c, dict)]
        return {f"character:{c['name']}": character_items(c) for c in chars if "name" in c}

    async def _achievements(self, out: CollectedData) -> None:
        progress = await self.client.get("v2/account/achievements", auth=True)
        values: dict[str, Any] = {}
        for p in progress:
            v: dict[str, Any] = {
                "current": p.get("current", 0),
                "max": p.get("max", 0),
                "done": bool(p.get("done", False)),
            }
            if p.get("repeated"):
                v["repeated"] = p["repeated"]
            if p.get("bits"):
                v["bits"] = len(p["bits"])
            values[str(p["id"])] = v
        out.data["achievement"] = values
        out.stats["achievements_done"] = sum(1 for v in values.values() if v["done"])
        try:
            defs = await self.catalog.get_many("achievements", values.keys())
        except GW2APIError as exc:
            out.errors["achievement_points"] = str(exc)
            return
        points = sum(achievement_points(defs[k], v) for k, v in values.items() if k in defs)
        out.stats["_ap"] = points  # daily/monthly AP are added in _finalize

    async def _mastery_points(self, out: CollectedData) -> None:
        data = await self.client.get("v2/account/mastery/points", auth=True)
        earned = spent = 0
        for total in data.get("totals", []):
            region = str(total.get("region", "unknown")).lower().replace(" ", "_")
            out.stats[f"mastery_points_earned:{region}"] = total.get("earned", 0)
            out.stats[f"mastery_points_spent:{region}"] = total.get("spent", 0)
            earned += int(total.get("earned", 0))
            spent += int(total.get("spent", 0))
        out.stats["mastery_points_earned"] = earned
        out.stats["mastery_points_spent"] = spent

    async def _luck(self, out: CollectedData) -> None:
        data = await self.client.get("v2/account/luck", auth=True)
        out.stats["luck"] = sum(int(x.get("value", 0)) for x in data if isinstance(x, dict))

    async def _progression(self, out: CollectedData) -> None:
        data = await self.client.get("v2/account/progression", auth=True)
        for entry in data:
            if isinstance(entry, dict) and "id" in entry:
                out.stats[f"progression:{entry['id']}"] = entry.get("value")

    async def _pvp(self, out: CollectedData) -> None:
        data = await self.client.get("v2/pvp/stats", auth=True)
        for key in ("pvp_rank", "pvp_rank_points", "pvp_rank_rollovers"):
            if key in data:
                out.stats[key] = data[key]
        agg = data.get("aggregate") or {}
        for key in ("wins", "losses", "desertions", "byes", "forfeits"):
            if key in agg:
                out.stats[f"pvp_{key}"] = agg[key]

    def _unlock_fn(self, kind: str) -> Callable[[CollectedData], Awaitable[None]]:
        endpoint = UNLOCK_KINDS[kind][0]

        async def fn(out: CollectedData) -> None:
            data = await self.client.get("v2/" + endpoint, auth=True)
            values: dict[str, Any] = {}
            for entry in data or ():
                kv = unlock_value(entry)
                if kv:
                    values[kv[0]] = kv[1]
            out.data[f"unlock:{kind}"] = values
            out.stats[f"unlocked:{kind}"] = len(values)

        return fn

    async def _value(self, out: CollectedData) -> None:
        items = out.data["item"]
        prices = await self.client.get_many("v2/commerce/prices", items.keys())
        buy = sell = 0
        for p in prices:
            count = items.get(str(p["id"]), 0)
            buy += count * int((p.get("buys") or {}).get("unit_price", 0))
            sell += count * int((p.get("sells") or {}).get("unit_price", 0))
        out.stats["item_value_buy_copper"] = buy
        out.stats["item_value_sell_copper"] = sell

    # ----------------------------------------------------------- post steps
    @staticmethod
    def _finalize(out: CollectedData) -> None:
        """Derive stats that combine several sections."""
        ap = out.stats.pop("_ap", None)
        if ap is not None and out.account:
            out.stats["achievement_points"] = (
                ap + int(out.account.get("daily_ap", 0)) + int(out.account.get("monthly_ap", 0))
            )
        if out.characters is not None:
            chars: dict[str, Any] = {}
            for c in out.characters:
                if "name" not in c:
                    continue
                chars[c["name"]] = {
                    k: c[k]
                    for k in ("level", "profession", "race", "gender", "age", "deaths", "created", "title")
                    if c.get(k) is not None
                }
                crafting = {
                    d["discipline"]: d.get("rating", 0)
                    for d in c.get("crafting") or ()
                    if isinstance(d, dict) and "discipline" in d
                }
                if crafting:
                    chars[c["name"]]["crafting"] = crafting
            out.data["character"] = chars
            out.stats["characters"] = len(chars)
            out.stats["playtime_hours"] = round(sum(int(c.get("age", 0)) for c in chars.values()) / 3600, 1)
            out.stats["deaths"] = sum(int(c.get("deaths", 0)) for c in chars.values())
            out.stats["level_80_characters"] = sum(1 for c in chars.values() if c.get("level") == 80)
        if out.delivery_coins is not None:
            out.stats["tp_delivery_coins"] = out.delivery_coins
