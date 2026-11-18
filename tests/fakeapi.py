"""An in-memory fake of the GW2 API served through ``httpx.MockTransport``."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import httpx

API_KEY = "TEST-KEY"

ITEMS = {
    19721: {"id": 19721, "name": "Glob of Ectoplasm", "type": "CraftingMaterial", "rarity": "Exotic"},
    19684: {"id": 19684, "name": "Mithril Ingot", "type": "CraftingMaterial", "rarity": "Basic"},
    24277: {"id": 24277, "name": "Pile of Crystalline Dust", "type": "CraftingMaterial", "rarity": "Rare"},
    30704: {"id": 30704, "name": "Twilight", "type": "Weapon", "rarity": "Legendary"},
    8932: {"id": 8932, "name": "Bag of Holding", "type": "Bag", "rarity": "Fine"},
    46731: {"id": 46731, "name": "Ectoplasm Bucket", "type": "Trophy", "rarity": "Basic"},
}

CURRENCIES = {
    1: {"id": 1, "name": "Coin", "order": 101},
    2: {"id": 2, "name": "Karma", "order": 102},
    4: {"id": 4, "name": "Gem", "order": 103},
    63: {"id": 63, "name": "Astral Acclaim", "order": 104},
}

ACHIEVEMENTS = {
    1: {
        "id": 1,
        "name": "Centaur Slayer",
        "requirement": "Kill centaurs",
        "tiers": [{"count": 10, "points": 1}, {"count": 100, "points": 5}],
    },
    2: {"id": 2, "name": "Dungeon Master", "requirement": "Do dungeons", "tiers": [{"count": 1, "points": 10}]},
    3: {"id": 3, "name": "Daily Grind", "requirement": "Repeat", "tiers": [{"count": 5, "points": 2}], "point_cap": 10},
}

SKINS = {
    10: {"id": 10, "name": "Zojja's Blade", "type": "Weapon"},
    11: {"id": 11, "name": "Zojja's Staff", "type": "Weapon"},
    12: {"id": 12, "name": "Dreamer", "type": "Weapon"},
}

ACHIEVEMENT_CATEGORIES = {50: {"id": 50, "name": "Slaying", "achievements": [{"id": 1}]}}
MATERIAL_CATEGORIES = {5: {"id": 5, "name": "Common Crafting Materials", "items": [19684]}}
RECIPES = {
    7000: {
        "id": 7000,
        "type": "Refinement",
        "output_item_id": 19684,
        "output_item_count": 1,
        "ingredients": [{"id": 19721, "count": 2}],
    },
}
PRICES = {
    19721: {"id": 19721, "buys": {"quantity": 100, "unit_price": 2000}, "sells": {"quantity": 50, "unit_price": 2500}},
    19684: {"id": 19684, "buys": {"quantity": 100, "unit_price": 50}, "sells": {"quantity": 50, "unit_price": 60}},
}


def default_account_data() -> dict[str, Any]:
    return {
        "v2/account": {
            "id": "ACC-1",
            "name": "Tester.1234",
            "age": 7200,
            "world": 2006,
            "guilds": ["g1"],
            "created": "2015-01-01T00:00:00Z",
            "access": ["GuildWars2"],
            "commander": True,
            "fractal_level": 100,
            "daily_ap": 5000,
            "monthly_ap": 500,
            "wvw": {"team_id": 1, "rank": 250},
        },
        "v2/tokeninfo": {
            "id": "tok",
            "name": "test",
            "permissions": ["account", "wallet", "inventories", "characters", "progression", "unlocks", "builds"],
        },
        "v2/account/wallet": [{"id": 1, "value": 1234567}, {"id": 2, "value": 1000}, {"id": 4, "value": 0}],
        "v2/account/bank": [{"id": 19721, "count": 250}, None, {"id": 30704, "count": 1}, {"id": 19721, "count": 5}],
        "v2/account/materials": [{"id": 19684, "category": 5, "count": 40}, {"id": 24277, "category": 5, "count": 0}],
        "v2/account/inventory": [None, {"id": 46731, "count": 2}],
        "v2/commerce/delivery": {"coins": 999, "items": [{"id": 19721, "count": 3}]},
        "v2/characters": [
            {
                "name": "Hero",
                "level": 80,
                "profession": "Guardian",
                "race": "Human",
                "gender": "Female",
                "age": 3600,
                "deaths": 7,
                "created": "2015-01-02T00:00:00Z",
                "crafting": [{"discipline": "Weaponsmith", "rating": 500, "active": True}],
                "bags": [{"id": 8932, "size": 20, "inventory": [{"id": 19721, "count": 10}, None]}, None],
                "equipment": [{"id": 30704, "slot": "WeaponA1"}],
            },
            {
                "name": "Alt",
                "level": 20,
                "profession": "Thief",
                "race": "Asura",
                "gender": "Male",
                "age": 1800,
                "deaths": 1,
                "created": "2016-01-02T00:00:00Z",
                "bags": [],
                "equipment": [],
            },
        ],
        "v2/account/achievements": [
            {"id": 1, "current": 50, "max": 100, "done": False},
            {"id": 2, "current": 1, "max": 1, "done": True},
            {"id": 3, "current": 5, "max": 5, "done": True, "repeated": 10, "bits": [1, 2]},
        ],
        "v2/account/mastery/points": {
            "totals": [{"region": "Tyria", "spent": 10, "earned": 20}, {"region": "Maguuma", "spent": 5, "earned": 6}],
            "unlocked": [1],
        },
        "v2/account/luck": [{"id": "luck", "value": 12345}],
        "v2/account/progression": [{"id": "fractal_agony_impedance", "value": 15}],
        "v2/pvp/stats": {
            "pvp_rank": 40,
            "pvp_rank_points": 100,
            "pvp_rank_rollovers": 0,
            "aggregate": {"wins": 10, "losses": 5, "desertions": 0, "byes": 1, "forfeits": 0},
        },
        "v2/account/skins": [10, 11],
        "v2/account/dyes": [1, 2, 3],
        "v2/account/finishers": [{"id": 1, "permanent": True}, {"id": 2, "permanent": False, "quantity": 3}],
        "v2/account/legendaryarmory": [{"id": 30704, "count": 1}],
        "v2/account/emotes": ["sleep", "dance"],
        "v2/account/masteries": [{"id": 1, "level": 4}],
        "v2/account/worldbosses": ["shadow_behemoth"],
        "v2/account/wizardsvault/daily": {"meta_progress_current": 2, "objectives": [{"id": 1, "title": "Kill"}]},
        "v2/commerce/transactions/current/sells": [{"id": 1, "item_id": 19721, "price": 2600, "quantity": 5}],
        "v2/pvp/games": [{"id": "G1", "result": "Victory"}],
        "v2/characters/Hero/inventory": {"bags": [{"id": 8932, "size": 20, "inventory": [{"id": 19721, "count": 10}]}]},
        "v2/characters/Hero/equipment": {"equipment": [{"id": 30704, "slot": "WeaponA1"}]},
        "v2/characters/Hero/core": {"name": "Hero", "level": 80, "profession": "Guardian", "race": "Human"},
        "v2/characters/Hero/buildtabs": [{"tab": 1, "is_active": True}],
    }


# endpoints (other than these) not listed above respond with 404 so the collector marks them failed
EMPTY_LIST_ENDPOINTS = {
    "v2/account/minis",
    "v2/account/outfits",
    "v2/account/titles",
    "v2/account/recipes",
    "v2/account/gliders",
    "v2/account/mailcarriers",
    "v2/account/novelties",
    "v2/account/jadebots",
    "v2/account/skiffs",
    "v2/account/mounts/skins",
    "v2/account/mounts/types",
    "v2/account/home/cats",
    "v2/account/home/nodes",
    "v2/account/homestead/decorations",
    "v2/account/homestead/glyphs",
    "v2/account/pvp/heroes",
    "v2/account/dungeons",
    "v2/account/raids",
    "v2/account/mapchests",
    "v2/account/dailycrafting",
}

BULK = {
    "v2/items": ITEMS,
    "v2/currencies": CURRENCIES,
    "v2/achievements": ACHIEVEMENTS,
    "v2/achievements/categories": ACHIEVEMENT_CATEGORIES,
    "v2/skins": SKINS,
    "v2/materials": MATERIAL_CATEGORIES,
    "v2/recipes": RECIPES,
    "v2/commerce/prices": PRICES,
    "v2/colors": {1: {"id": 1, "name": "Red"}, 2: {"id": 2, "name": "Blue"}, 3: {"id": 3, "name": "Green"}},
    "v2/finishers": {1: {"id": 1, "name": "Ghost"}, 2: {"id": 2, "name": "Bunny"}},
    "v2/masteries": {1: {"id": 1, "name": "Gliding"}},
}

PUBLIC = {
    "v2/wizardsvault": {"title": "Test Season", "start": "2026-01-01T00:00:00Z", "end": "2027-01-01T00:00:00Z"},
    "v2/wizardsvault/objectives": [{"id": 1, "title": "Complete 3 Events", "track": "PvE", "acclaim": 10}],
    "v2/wizardsvault/listings": [{"id": 1, "item_id": 19721, "item_count": 1, "type": "Normal", "cost": 30}],
    "v2/worldbosses": ["shadow_behemoth", "svanir_shaman"],
    "v2/commerce/listings/19721": {
        "id": 19721,
        "buys": [{"listings": 1, "unit_price": 2000, "quantity": 5}],
        "sells": [{"listings": 1, "unit_price": 2500, "quantity": 7}],
    },
    "v2/commerce/exchange/coins": {"coins_per_gem": 3000, "quantity": 33},
    "v2/commerce/exchange/gems": {"coins_per_gem": 2500, "quantity": 250000},
    "v2/recipes/search": [7000],
    "v2/build": {"id": 170000},
}


class FakeGW2:
    def __init__(self) -> None:
        self.account = default_account_data()
        for ep in EMPTY_LIST_ENDPOINTS:
            self.account.setdefault(ep, [])
        self.bulk = copy.deepcopy(BULK)
        self.public = copy.deepcopy(PUBLIC)
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, int] = {}  # path -> status to return
        self.hooks: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.api_key = API_KEY

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        return [r.url.path.lstrip("/") for r in self.requests]

    def count(self, path: str) -> int:
        return sum(1 for p in self.paths() if p == path)

    @staticmethod
    def _json(status: int, data: Any, headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(
            status, content=json.dumps(data).encode(), headers={"content-type": "application/json", **(headers or {})}
        )

    def _exchange(self, path: str, quantity: int) -> httpx.Response:
        rate = self.public[path]["coins_per_gem"]
        if path.endswith("/coins"):  # `quantity` coins in -> gems out
            return self._json(200, {"coins_per_gem": rate, "quantity": quantity // rate})
        return self._json(200, {"coins_per_gem": rate, "quantity": quantity * rate})  # gems in -> coins out

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.lstrip("/")
        if path in self.hooks:
            return self.hooks[path](request)
        if path in self.fail:
            return self._json(self.fail[path], {"text": "forced failure"})
        params = request.url.params
        if path in ("v2/commerce/exchange/coins", "v2/commerce/exchange/gems") and "quantity" in params:
            return self._exchange(path, int(params["quantity"]))
        if path in self.account:
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {self.api_key}":
                return self._json(401, {"text": "Invalid access token"})
            return self._json(200, self.account[path])
        if path in self.public:
            return self._json(200, self.public[path])
        if path in self.bulk:
            table = self.bulk[path]
            if "ids" in params:
                ids = [i for i in params["ids"].split(",") if i]
                found = [table[k] for k in table if str(k) in ids]
                if not found:
                    return self._json(404, {"text": "all ids provided are invalid"})
                return self._json(200 if len(found) == len(ids) else 206, found)
            if "page" in params:
                size = int(params.get("page_size", 50))
                page = int(params["page"])
                values = list(table.values())
                total = max(1, -(-len(values) // size))
                chunk = values[page * size : (page + 1) * size]
                return self._json(200, chunk, {"X-Page-Total": str(total), "X-Result-Total": str(len(values))})
            return self._json(200, list(table))
        for prefix, table in self.bulk.items():  # /v2/items/123 style
            if path.startswith(prefix + "/"):
                key = path[len(prefix) + 1 :]
                for k, v in table.items():
                    if str(k) == key:
                        return self._json(200, v)
        return self._json(404, {"text": "no such endpoint"})
