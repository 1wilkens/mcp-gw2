"""Cached access to public (static-ish) game data, with a local name search index."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any

from gw2mcp.client import GW2Client, normalize_path
from gw2mcp.storage import Storage

# Public bulk endpoints that can be looked up by id and are worth caching.
CACHEABLE_ENDPOINTS: frozenset[str] = frozenset(
    {
        "achievements",
        "achievements/categories",
        "achievements/groups",
        "backstory/answers",
        "backstory/questions",
        "colors",
        "currencies",
        "dungeons",
        "emotes",
        "files",
        "finishers",
        "gliders",
        "guild/permissions",
        "guild/upgrades",
        "home/cats",
        "home/nodes",
        "homestead/decorations",
        "homestead/decorations/categories",
        "homestead/glyphs",
        "items",
        "itemstats",
        "jadebots",
        "legendaryarmory",
        "legends",
        "mailcarriers",
        "mapchests",
        "maps",
        "masteries",
        "materials",
        "minis",
        "mounts/skins",
        "mounts/types",
        "novelties",
        "outfits",
        "pets",
        "professions",
        "pvp/amulets",
        "pvp/heroes",
        "pvp/ranks",
        "pvp/seasons",
        "quaggans",
        "quests",
        "races",
        "raids",
        "recipes",
        "skiffs",
        "skills",
        "skins",
        "specializations",
        "stories",
        "stories/seasons",
        "titles",
        "traits",
        "worldbosses",
        "wvw/abilities",
        "wvw/objectives",
        "wvw/ranks",
        "wvw/upgrades",
    }
)

# Endpoints whose objects carry a name and therefore support ``search``.
SEARCHABLE_ENDPOINTS: frozenset[str] = CACHEABLE_ENDPOINTS - {
    "backstory/answers",
    "backstory/questions",
    "files",
    "guild/permissions",
    "legendaryarmory",
    "mapchests",
    "recipes",
    "worldbosses",
    "dungeons",
    "raids",
    "home/cats",
    "home/nodes",
}


def normalize_endpoint(endpoint: str) -> str:
    return normalize_path(endpoint).removeprefix("v2").strip("/")


class Catalog:
    def __init__(self, client: GW2Client, storage: Storage, ttl_days: int = 7) -> None:
        self.client = client
        self.storage = storage
        self.ttl = ttl_days * 86400
        self._index_locks: dict[str, asyncio.Lock] = {}

    async def get_many(self, endpoint: str, ids: Iterable[int | str]) -> dict[str, dict[str, Any]]:
        """Return ``{str(id): object}`` for the requested ids, using and refreshing the cache."""
        endpoint = normalize_endpoint(endpoint)
        wanted = list(dict.fromkeys(str(i) for i in ids))
        if not wanted:
            return {}
        cacheable = endpoint in CACHEABLE_ENDPOINTS
        found = self.storage.catalog_get(endpoint, wanted, max_age=self.ttl) if cacheable else {}
        missing = [i for i in wanted if i not in found]
        if missing:
            fetched = await self.client.get_many("v2/" + endpoint, missing)
            if cacheable and fetched:
                self.storage.catalog_put(endpoint, fetched)
            for obj in fetched:
                found[str(obj["id"])] = obj
        return {i: found[i] for i in wanted if i in found}

    async def get_one(self, endpoint: str, id_: int | str) -> dict[str, Any] | None:
        return (await self.get_many(endpoint, [id_])).get(str(id_))

    async def names(self, endpoint: str, ids: Iterable[int | str]) -> dict[str, str]:
        objs = await self.get_many(endpoint, ids)
        return {k: _display_name(v) for k, v in objs.items()}

    def index_age(self, endpoint: str) -> float | None:
        info = self.storage.catalog_index_info(normalize_endpoint(endpoint))
        return None if info is None else time.time() - info[0]

    async def build_index(self, endpoint: str, *, force: bool = False) -> int:
        """Download every object of ``endpoint`` into the local cache (for search)."""
        endpoint = normalize_endpoint(endpoint)
        if endpoint not in CACHEABLE_ENDPOINTS:
            raise ValueError(f"endpoint {endpoint!r} cannot be indexed")
        lock = self._index_locks.setdefault(endpoint, asyncio.Lock())
        async with lock:
            info = self.storage.catalog_index_info(endpoint)
            if not force and info is not None and time.time() - info[0] < self.ttl:
                return info[1]
            objects = await self.client.get_all_pages("v2/" + endpoint)
            objects = [o for o in objects if isinstance(o, dict)]
            self.storage.catalog_put(endpoint, objects)
            self.storage.catalog_mark_indexed(endpoint, len(objects))
            return len(objects)

    async def search(self, endpoint: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        endpoint = normalize_endpoint(endpoint)
        if endpoint not in SEARCHABLE_ENDPOINTS:
            raise ValueError(f"endpoint {endpoint!r} is not searchable; choose one of {sorted(SEARCHABLE_ENDPOINTS)}")
        await self.build_index(endpoint)
        return self.storage.catalog_search(endpoint, query, limit)


def _display_name(obj: dict[str, Any]) -> str:
    for field in ("name", "title"):
        if obj.get(field):
            return str(obj[field])
    return str(obj.get("id"))
