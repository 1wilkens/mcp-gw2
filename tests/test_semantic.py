"""Backend-agnostic tests: they run against the fake API and, when a key is configured, the real GW2 API.

Nothing here may depend on concrete account data. We only check invariants (types, ranges, internal consistency,
agreement between tools and the raw API) that must hold for *any* account.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from gw2mcp.server import format_coins

PROFESSIONS = {
    "Guardian", "Warrior", "Engineer", "Ranger", "Thief", "Elementalist", "Mesmer", "Necromancer", "Revenant",
}  # fmt: skip
RACES = {"Asura", "Charr", "Human", "Norn", "Sylvari"}
KNOWN_PERMISSIONS = {
    "account", "builds", "characters", "guilds", "inventories", "progression", "pvp", "tradingpost", "unlocks",
    "wallet", "wvw",
}  # fmt: skip


def is_iso(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return True


def is_unique(values) -> bool:
    values = list(values)
    return len(values) == len(set(values))


# ------------------------------------------------------------------ key / account
async def test_tokeninfo_permissions_are_known(api):
    token = await api.get("v2/tokeninfo", auth=True)
    assert token["name"]
    assert token["permissions"], "a usable key has at least one permission"
    assert set(token["permissions"]) <= KNOWN_PERMISSIONS


async def test_get_account(api_call, require):
    require("account")
    acc = await api_call("get_account")
    assert acc["name"].rpartition(".")[2].isdigit() and "." in acc["name"]  # Name.1234
    assert acc["age"] >= 0
    assert acc["age_hours"] == pytest.approx(acc["age"] / 3600, abs=0.05)
    assert isinstance(acc["world"], int)
    assert is_iso(acc["created"])
    assert acc["access"], "every account has at least one access entry"
    assert set(acc["token"]["permissions"]) <= KNOWN_PERMISSIONS
    assert "id" not in acc["token"], "the token id is a credential and must not be exposed"


async def test_account_name_matches_raw_api(api, api_call, require):
    require("account")
    assert (await api_call("get_account"))["name"] == (await api.get("v2/account", auth=True))["name"]


# ------------------------------------------------------------------ wallet / inventory
async def test_wallet_is_consistent(api, api_call, require):
    require("wallet")
    wallet = await api_call("get_wallet", only_nonzero=False)
    rows = wallet["currencies"]
    assert rows and is_unique(r["id"] for r in rows)
    assert all(isinstance(r["id"], int) and r["value"] >= 0 for r in rows)
    assert all(r["name"] for r in rows), "every currency id must resolve to a name"
    raw_coins = next((c["value"] for c in await api.get("v2/account/wallet", auth=True) if c["id"] == 1), 0)
    assert wallet["coins_formatted"] == format_coins(raw_coins)
    nonzero = await api_call("get_wallet", only_nonzero=True)
    assert {r["id"] for r in nonzero["currencies"]} == {r["id"] for r in rows if r["value"]}


async def test_bank_is_consistent(api, api_call, require):
    require("inventories")
    bank = await api_call("get_bank")
    raw = await api.get("v2/account/bank", auth=True)
    assert bank["slots_total"] == len(raw)
    assert 0 <= bank["slots_used"] <= bank["slots_total"]
    assert is_unique(i["id"] for i in bank["items"])
    assert sum(i["count"] for i in bank["items"]) == sum(s.get("count", 1) for s in raw if s)
    assert all(i["count"] > 0 and i["name"] for i in bank["items"])


async def test_materials_are_consistent(api, api_call, require):
    require("inventories")
    mats = await api_call("get_materials", only_nonzero=True)
    assert is_unique(i["id"] for i in mats["items"])
    assert all(i["count"] > 0 and i["name"] for i in mats["items"])
    raw = await api.get("v2/account/materials", auth=True)
    assert {i["id"] for i in mats["items"]} == {m["id"] for m in raw if m.get("count")}


async def test_find_items_agrees_with_bank(api_call, require):
    require("inventories", "characters")
    bank = await api_call("get_bank")
    if not bank["items"]:
        pytest.skip("account bank is empty")
    item = bank["items"][0]
    found = (await api_call("find_items", items=[str(item["id"])]))["items"][0]
    assert found["id"] == item["id"]
    assert found["total"] >= item["count"], "total across all locations includes the bank stack"
    assert found["total"] == sum(loc["count"] for loc in found["locations"])


# ------------------------------------------------------------------ characters
async def test_characters_are_consistent(api, api_call, require):
    require("characters")
    chars = (await api_call("list_characters"))["characters"]
    names = await api.get("v2/characters", auth=True)
    names = [n["name"] if isinstance(n, dict) else n for n in names]  # the fake returns full objects
    assert sorted(c["name"] for c in chars) == sorted(names)
    for c in chars:
        assert 1 <= c["level"] <= 80
        assert c["profession"] in PROFESSIONS
        assert c["race"] in RACES
        assert c["gender"] in {"Male", "Female"}
        assert c["playtime_hours"] >= 0 and c["deaths"] >= 0
        assert is_iso(c["created"])
    if chars:
        core = await api_call("get_character", name=chars[0]["name"], section="core")
        assert core["name"] == chars[0]["name"]
        assert core["level"] == chars[0]["level"]
        assert core["profession"] == chars[0]["profession"]


# ------------------------------------------------------------------ achievements
async def test_achievement_filters_partition(api_call, require):
    require("progression")
    done = (await api_call("get_account_achievements", status="done", limit=100000))["achievements"]
    todo = (await api_call("get_account_achievements", status="in_progress", limit=100000))["achievements"]
    assert not {a["id"] for a in done} & {a["id"] for a in todo}
    assert all(a["done"] for a in done)
    assert not any(a["done"] for a in todo)


# ------------------------------------------------------------------ public game data
async def test_get_game_data_items(api_call):
    data = await api_call("get_game_data", endpoint="items", ids=["19721", "19684", "999999999"])
    assert data["found"] == len(data["objects"]) == 2
    assert data["missing"] == ["999999999"]
    assert {o["id"] for o in data["objects"]} == {19721, 19684}
    assert all(o["name"] and o["type"] and o["rarity"] for o in data["objects"])


async def test_search_results_match_query(api_call):
    res = (await api_call("search", endpoint="currencies", query="karma"))["results"]
    assert res, "karma is a currency in every GW2 version"
    assert all("karma" in r["name"].lower() for r in res)


async def test_resolve_item_by_name_roundtrip(api_call):
    # name -> id -> name must be stable for a tradeable item
    by_id = (await api_call("get_prices", items=["19721"]))["prices"][0]
    by_name = (await api_call("get_prices", items=[by_id["name"]]))["prices"][0]
    assert by_name["id"] == 19721 == by_id["id"]


# ------------------------------------------------------------------ trading post
async def test_prices_are_sane(api_call):
    p = (await api_call("get_prices", items=["19721"]))["prices"][0]  # Glob of Ectoplasm: always listed
    assert p["tradeable"]
    assert p["buy_price"] > 0 and p["sell_price"] > 0
    assert p["buy_price"] <= p["sell_price"], "a crossing book would have been matched"
    assert p["buy_quantity"] >= 0 and p["sell_quantity"] >= 0
    assert p["flip_profit_after_fees"] == int(p["sell_price"] * 0.85) - p["buy_price"]
    assert p["buy_formatted"] == format_coins(p["buy_price"])


async def test_untradeable_item_has_no_price(api_call):
    # Item 1 does not exist on the TP; the tool must say so instead of erroring
    p = (await api_call("get_prices", items=["1"]))["prices"][0]
    assert p["tradeable"] is False


async def test_listings_are_ordered(api_call):
    book = await api_call("get_listings", item="19721", depth=5)
    assert 0 < len(book["buys"]) <= 5 and 0 < len(book["sells"]) <= 5
    buys = [b["unit_price"] for b in book["buys"]]
    sells = [s["unit_price"] for s in book["sells"]]
    assert buys == sorted(buys, reverse=True) or buys == sorted(buys), "buys must be monotonic"
    assert sells == sorted(sells) or sells == sorted(sells, reverse=True), "sells must be monotonic"
    assert max(buys) <= min(sells), "best buy order is below the best sell listing"
    assert all(e["quantity"] > 0 and e["listings"] > 0 for e in book["buys"] + book["sells"])


async def test_gem_exchange_is_self_consistent(api_call):
    gems = await api_call("gem_exchange", direction="gems_to_coins", quantity=1000)
    assert gems["coins_per_gem"] > 0
    assert gems["quantity"] == pytest.approx(gems["coins_per_gem"] * 1000, rel=0.02)
    coins = await api_call("gem_exchange", direction="coins_to_gems", quantity=10_000_000)
    assert coins["coins_per_gem"] > 0
    assert coins["quantity"] == pytest.approx(10_000_000 / coins["coins_per_gem"], rel=0.02)
    assert coins["input_formatted"] == format_coins(10_000_000)


# ------------------------------------------------------------------ history / snapshots
async def test_snapshot_roundtrip(api, api_call, require):
    require("account", "wallet")
    snap = await api_call("take_snapshot")
    assert snap["account_name"] and is_iso(snap["taken_at"])
    assert snap["sections"], "a snapshot with a valid key collects at least one section"

    listing = await api_call("list_snapshots")
    assert listing["total_snapshots"] == 1
    assert listing["snapshots"][0]["id"] == snap["id"]

    latest = await api_call("get_snapshot", snapshot="latest")
    assert latest["snapshot"]["id"] == snap["id"]
    stats = latest["stats"]
    raw_coins = next((c["value"] for c in await api.get("v2/account/wallet", auth=True) if c["id"] == 1), None)
    if "gold" in stats and raw_coins is not None:
        # wallet may legitimately change between the two reads (e.g. while playing): allow 1% drift
        assert stats["gold"] == pytest.approx(raw_coins / 10000, rel=0.01, abs=1)
    tracked = (await api_call("list_tracked_stats"))["stats"]
    assert {k for k in stats if not k.endswith("_formatted")} <= set(tracked)

    status = await api_call("server_status")
    assert status["api_key_configured"] is True
    assert status["snapshots"] == 1


async def test_two_snapshots_diff_is_plausible(api_call, require):
    require("account", "wallet", "inventories")
    first = await api_call("take_snapshot")
    second = await api_call("take_snapshot")
    assert second["id"] > first["id"]
    cmp = await api_call("compare_snapshots", start=str(first["id"]), end=str(second["id"]))
    assert cmp["from"]["id"] == first["id"] and cmp["to"]["id"] == second["id"]
    wallet = cmp["changes"]["wallet"]
    assert wallet["total_changes"] == len(wallet["entries"]) or wallet["total_changes"] > len(wallet["entries"])
    # two snapshots seconds apart: the account must not have changed wildly
    assert all(abs(e.get("delta", 0)) < 10_000_000 for e in wallet["entries"] if e.get("name") == "Coin")


async def test_estimate_account_value_is_consistent(api_call, require):
    require("account", "wallet", "inventories", "characters")
    v = await api_call("estimate_account_value")
    assert v["items_value_buy"] >= 0 and v["items_value_sell"] >= 0
    assert v["items_value_buy"] <= v["items_value_sell"], "sell prices are never below buy prices"
    assert v["items_value_sell_after_fees_formatted"] == format_coins(v["items_value_sell"] * 0.85)
    top = [r["value_sell"] for r in v["most_valuable"]]
    assert top == sorted(top, reverse=True)
    assert sum(top) <= v["items_value_sell"]
    assert v["liquid_coins"] >= 0


# ------------------------------------------------------------------ errors
async def test_unknown_character_is_an_error(api, api_call, require):
    require("characters")
    with pytest.raises(Exception, match="(?i)404|no such|not found|error"):
        await api_call("get_character", name="Definitely Not A Real Character 123", section="core")
