import pytest
from mcp.server.mcpserver.exceptions import ToolError

from gw2mcp.server import AppContext, create_server, format_coins

EXPECTED_TOOLS = {
    "get_account",
    "get_wallet",
    "get_bank",
    "get_materials",
    "get_shared_inventory",
    "list_characters",
    "get_character",
    "find_items",
    "get_account_achievements",
    "get_unlocks",
    "get_wizards_vault",
    "get_trading_post",
    "get_pvp",
    "estimate_account_value",
    "search",
    "get_game_data",
    "get_prices",
    "get_listings",
    "gem_exchange",
    "find_recipes",
    "get_daily_rotations",
    "api_get",
    "take_snapshot",
    "list_snapshots",
    "get_snapshot",
    "list_tracked_stats",
    "stat_history",
    "item_history",
    "currency_history",
    "achievement_history",
    "character_history",
    "unlock_history",
    "compare_snapshots",
    "progress_report",
    "server_status",
}


@pytest.mark.parametrize(
    ("copper", "text"),
    [
        (0, "0c"),
        (None, "0c"),
        (5, "5c"),
        (105, "1s 5c"),
        (1234567, "123g 45s 67c"),
        (-10000, "-1g 0s 0c"),
        (12345678901, "1,234,567g 89s 1c"),
        (99.6, "1s 0c"),
    ],
)
def test_format_coins(copper, text):
    assert format_coins(copper) == text


async def test_tool_list(mcp):
    tools = {t.name: t for t in await mcp.list_tools()}
    assert set(tools) == EXPECTED_TOOLS
    for t in tools.values():
        assert t.description, t.name
        assert t.annotations.read_only_hint is (t.name != "take_snapshot")


async def test_prompts(mcp):
    prompts = {p.name for p in await mcp.list_prompts()}
    assert prompts == {"weekly_review", "item_tracking"}
    res = await mcp.get_prompt("item_tracking", {"item": "Glob of Ectoplasm"})
    assert "Glob of Ectoplasm" in res.messages[0].content.text


async def expect_error(mcp, tool_name, match, /, **args):
    with pytest.raises(ToolError, match=match):
        result = await mcp.call_tool(tool_name, args)
        if result.is_error:  # pragma: no cover - depending on SDK behaviour
            raise ToolError(result.content[0].text)


# ---------------------------------------------------------------- live account
async def test_get_account(call, fake):
    acc = await call("get_account")
    assert acc["name"] == "Tester.1234"
    assert acc["age_hours"] == 2.0
    assert acc["token"]["permissions"] == fake.account["v2/tokeninfo"]["permissions"]


async def test_get_account_without_tokeninfo(call, fake):
    fake.fail["v2/tokeninfo"] = 400
    assert "token" not in await call("get_account")


async def test_get_wallet(call):
    w = await call("get_wallet")
    assert w["currencies"] == [{"id": 1, "name": "Coin", "value": 1234567}, {"id": 2, "name": "Karma", "value": 1000}]
    assert w["coins_formatted"] == "123g 45s 67c"
    assert len((await call("get_wallet", only_nonzero=False))["currencies"]) == 3


async def test_get_bank(call):
    bank = await call("get_bank")
    assert bank["slots_used"] == 3 and bank["slots_total"] == 4
    assert bank["items"][0] == {
        "id": 19721,
        "name": "Glob of Ectoplasm",
        "rarity": "Exotic",
        "type": "CraftingMaterial",
        "count": 255,
    }


async def test_get_materials(call):
    mats = await call("get_materials")
    assert [(m["name"], m["count"], m["category"]) for m in mats["items"]] == [
        ("Mithril Ingot", 40, "Common Crafting Materials")
    ]
    assert len((await call("get_materials", only_nonzero=False))["items"]) == 2


async def test_get_shared_inventory(call):
    inv = await call("get_shared_inventory")
    assert inv["items"][0]["name"] == "Ectoplasm Bucket"


async def test_list_characters(call):
    chars = (await call("list_characters"))["characters"]
    assert [c["name"] for c in chars] == ["Hero", "Alt"]
    assert chars[0]["playtime_hours"] == 1.0


async def test_get_character_sections(call, fake):
    assert (await call("get_character", name="Hero"))["level"] == 80
    inv = await call("get_character", name="Hero", section="inventory")
    assert {i["name"] for i in inv["items"]} == {"Glob of Ectoplasm", "Bag of Holding"}
    eq = await call("get_character", name="Hero", section="equipment")
    assert eq["equipment"][0]["name"] == "Twilight"
    build = await call("get_character", name="Hero", section="build")
    assert build == {"name": "Hero", "build": [{"tab": 1, "is_active": True}]}
    assert fake.requests[-1].url.params["tabs"] == "all"
    fake.account["v2/characters/Hero"] = {"name": "Hero", "everything": True}
    assert (await call("get_character", name="Hero", section="all"))["everything"]


async def test_get_character_unknown(mcp):
    await expect_error(mcp, "get_character", "404", name="Nobody")


async def test_find_items(call):
    res = await call("find_items", items=["19721", "Twilight"])
    ecto, twilight = res["items"]
    assert ecto["total"] == 268
    assert {loc["location"] for loc in ecto["locations"]} == {"bank", "character:Hero", "tp_delivery"}
    assert twilight["name"] == "Twilight" and twilight["total"] == 2
    assert res["errors"] == {}


async def test_find_items_partial_errors(call, fake):
    for p in (
        "v2/account/bank",
        "v2/account/materials",
        "v2/account/inventory",
        "v2/characters",
        "v2/commerce/delivery",
    ):
        fake.fail[p] = 403
    res = await call("find_items", items=["19721"])
    assert res["items"][0]["total"] == 0
    assert set(res["errors"]) == {"bank", "materials", "shared_inventory", "characters", "tp_delivery"}


async def test_find_items_unknown_name(mcp):
    await expect_error(mcp, "find_items", "no item named", items=["Nonexistent Thing"])


async def test_get_account_achievements(call, mcp):
    res = await call("get_account_achievements")
    assert res["total_matching"] == 3
    assert res["achievements"][0]["name"] == "Centaur Slayer"
    done = await call("get_account_achievements", status="done")
    assert {a["id"] for a in done["achievements"]} == {2, 3}
    prog = await call("get_account_achievements", status="in_progress")
    assert [a["id"] for a in prog["achievements"]] == [1]
    by_ids = await call("get_account_achievements", ids=[3], limit=1)
    assert by_ids["achievements"][0]["bits_done"] == 2
    by_cat = await call("get_account_achievements", category_id=50)
    assert [a["id"] for a in by_cat["achievements"]] == [1]
    assert (await call("get_account_achievements", limit=1))["returned"] == 1
    await expect_error(mcp, "get_account_achievements", "unknown achievement category", category_id=999)


async def test_get_unlocks(call, mcp):
    skins = await call("get_unlocks", kind="skins")
    assert skins["count"] == 2
    assert [e["name"] for e in skins["entries"]] == ["Zojja's Blade", "Zojja's Staff"]
    fin = await call("get_unlocks", kind="finishers")
    assert fin["entries"][1] == {"id": 2, "permanent": False, "quantity": 3, "name": "Bunny"}
    emotes = await call("get_unlocks", kind="emotes")
    assert emotes["entries"] == [{"id": "sleep"}, {"id": "dance"}]
    raw = await call("get_unlocks", kind="skins", resolve_names=False, limit=1)
    assert raw["entries"] == [{"id": 10}] and raw["count"] == 2
    await expect_error(mcp, "get_unlocks", "unknown kind", kind="nope")


async def test_get_wizards_vault(call):
    assert (await call("get_wizards_vault"))["meta_progress_current"] == 2


async def test_get_trading_post(call, fake):
    cur = await call("get_trading_post")
    assert cur["transactions"][0]["item_name"] == "Glob of Ectoplasm"
    assert cur["transactions"][0]["price_formatted"] == "26s 0c"
    delivery = await call("get_trading_post", kind="delivery")
    assert delivery["coins_formatted"] == "9s 99c"
    assert delivery["items"][0]["count"] == 3


async def test_get_pvp(call):
    assert (await call("get_pvp"))["pvp_rank"] == 40
    assert (await call("get_pvp", section="games"))["games"][0]["result"] == "Victory"


async def test_estimate_account_value(call, mcp, fake):
    v = await call("estimate_account_value")
    assert v["items_value_sell"] == 268 * 2500 + 40 * 60
    assert v["items_value_buy"] == 268 * 2000 + 40 * 50
    assert v["most_valuable"][0]["name"] == "Glob of Ectoplasm"
    assert v["liquid_coins_formatted"] == "123g 45s 67c"
    fake.fail["v2/account/bank"] = 400
    await expect_error(mcp, "estimate_account_value", "could not read all inventories")


async def test_account_tools_without_key(fake, storage, settings):
    from gw2mcp.client import GW2Client

    async with GW2Client(None, transport=fake.transport()) as client:
        mcp = create_server(AppContext.create(settings.with_overrides(api_key=""), client=client, storage=storage))
        await expect_error(mcp, "get_wallet", "no API key configured")
        vault = (await mcp.call_tool("get_wizards_vault", {})).structured_content
        assert vault["season"]["title"] == "Test Season" and "no API key" in vault["note"]
        assert vault["objectives"][0]["track"] == "PvE"
        vault = (await mcp.call_tool("get_wizards_vault", {"period": "listings"})).structured_content
        assert vault["listings"][0]["item_name"] == "Glob of Ectoplasm"


# ------------------------------------------------------------------ game data
async def test_search(call, mcp):
    res = await call("search", endpoint="items", query="ecto")
    assert [r["name"] for r in res["results"]] == ["Ectoplasm Bucket", "Glob of Ectoplasm"]
    assert res["results"][0] == {"id": 46731, "name": "Ectoplasm Bucket", "type": "Trophy", "rarity": "Basic"}
    assert (await call("search", endpoint="/v2/skins", query="zojja", limit=1))["results"][0]["id"] == 10
    await expect_error(mcp, "search", "not searchable", endpoint="recipes", query="x")


async def test_get_game_data(call, mcp):
    res = await call("get_game_data", endpoint="items", ids=["19721", "1"])
    assert res["found"] == 1 and res["missing"] == ["1"]
    await expect_error(mcp, "get_game_data", "unsupported endpoint", endpoint="account/bank", ids=["1"])


async def test_get_prices(call):
    res = (await call("get_prices", items=["19721", "Twilight"]))["prices"]
    assert res[0]["sell_formatted"] == "25s 0c"
    assert res[0]["flip_profit_after_fees"] == int(2500 * 0.85) - 2000
    assert res[1] == {"id": 30704, "name": "Twilight", "tradeable": False}


async def test_get_listings(call):
    res = await call("get_listings", item="Glob of Ectoplasm", depth=1)
    assert res["id"] == 19721 and res["sells"][0]["unit_price"] == 2500


async def test_gem_exchange(call, fake):
    res = await call("gem_exchange", direction="coins_to_gems", quantity=100000)
    assert res["quantity"] == 33 and res["input_formatted"] == "10g 0s 0c"
    assert fake.requests[-1].url.params["quantity"] == "100000"
    res = await call("gem_exchange", direction="gems_to_coins", quantity=100)
    assert res["quantity_formatted"] == "25g 0s 0c"


async def test_find_recipes(call, mcp, fake):
    res = await call("find_recipes", output_item="Mithril Ingot")
    assert res["mode"] == "output" and res["total"] == 1
    assert res["recipes"][0]["output_item_name"] == "Mithril Ingot"
    assert res["recipes"][0]["ingredients"][0]["name"] == "Glob of Ectoplasm"
    res = await call("find_recipes", input_item="19721")
    assert res["mode"] == "input"
    await expect_error(mcp, "find_recipes", "exactly one")
    await expect_error(mcp, "find_recipes", "exactly one", input_item="1", output_item="2")


async def test_get_daily_rotations(call, fake):
    fake.fail["v2/account/raids"] = 403
    res = await call("get_daily_rotations")
    assert res["world_bosses_today"] == ["shadow_behemoth"]
    assert res["raid_encounters_this_week"] == {"error": "forced failure"}
    assert "svanir_shaman" in res["all_world_bosses"]


async def test_api_get(call, mcp):
    assert (await call("api_get", path="/v2/build"))["id"] == 170000
    assert (await call("api_get", path="worldbosses"))["data"][0] == "shadow_behemoth"
    assert (await call("api_get", path="v2/account/wallet"))["data"][0]["id"] == 1
    await expect_error(mcp, "api_get", "invalid API path", path="https://evil.example/v2")


@pytest.mark.parametrize(
    "path",
    [
        "v2/createsubtoken",
        "/v2/createsubtoken/",
        "createsubtoken",
        "v2/CreateSubToken",
        "v2/tokeninfo",
        "v2/createsubtoke%6E",
        "v2/items/%2e%2e/createsubtoken",
        "v2/items\\..\\createsubtoken",
        "v2/unknown-route",
    ],
)
async def test_api_get_blocks_credential_and_unknown_routes(call, mcp, fake, path):
    fake.account["v2/createsubtoken"] = {"subtoken": "MINTED"}
    await expect_error(mcp, "api_get", "API path", path=path, params={"permissions": "account"})
    assert fake.requests == []


async def test_get_character_rejects_encoded_traversal(mcp, fake):
    await expect_error(mcp, "get_character", "invalid API path", name="%2e%2e/%2e%2e/createsubtoken", section="all")
    assert fake.requests == []


async def test_get_account_hides_token_id(call, fake):
    fake.account["v2/tokeninfo"]["id"] = "SECRET-KEY-ID"
    acc = await call("get_account")
    assert acc["token"] == {"name": "test", "permissions": fake.account["v2/tokeninfo"]["permissions"]}
    assert "SECRET-KEY-ID" not in str(acc)


# -------------------------------------------------------------------- history
@pytest.fixture
async def history(app, fake):
    s1 = await app.collector.take()
    fake.account["v2/account/wallet"][0]["value"] += 50000
    fake.account["v2/account/skins"].append(12)
    fake.account["v2/account/achievements"][0].update(current=100, done=True)
    fake.account["v2/account/bank"][0]["count"] = 200
    fake.account["v2/characters"][1]["level"] = 30
    s2 = await app.collector.take()
    return s1, s2


async def test_take_snapshot_tool(call, app):
    snap = await call("take_snapshot")
    assert snap["id"] == 1 and snap["account_name"] == "Tester.1234"
    assert app.storage.count_snapshots() == 1


async def test_history_tools_need_snapshots(mcp):
    await expect_error(mcp, "get_snapshot", "no snapshots recorded")
    await expect_error(mcp, "progress_report", "no snapshots recorded")


async def test_list_snapshots(call, history):
    res = await call("list_snapshots")
    assert res["total_snapshots"] == 2
    assert [s["id"] for s in res["snapshots"]] == [2, 1]
    assert res["snapshot_interval_seconds"] == 3600
    assert (await call("list_snapshots", since="2000-01-01", until="2000-01-02"))["snapshots"] == []


async def test_list_snapshots_bad_time(mcp):
    await expect_error(mcp, "list_snapshots", "cannot parse time", since="whenever")


async def test_get_snapshot(call, history):
    res = await call("get_snapshot")
    assert res["snapshot"]["id"] == 2
    assert res["stats"]["gold"] == 128.4567
    assert res["stats"]["item_value_sell_formatted"].endswith("c")
    assert res["wallet"] == {"Coin": 1284567, "Karma": 1000}
    first = await call("get_snapshot", snapshot="first")
    assert first["stats"]["gold"] == 123.4567


async def test_list_tracked_stats(call, history):
    res = await call("list_tracked_stats")
    assert "achievement_points" in res["stats"]
    assert {"stat", "wallet", "item", "unlock:skins"} <= set(res["kinds"])


async def test_stat_history(call, mcp, history):
    res = await call("stat_history", stat="gold")
    assert [p["value"] for p in res["changes"]] == [123.4567, 128.4567]
    assert res["current"] == 128.4567
    assert res["value_before_window"] is None
    same = await call("stat_history", stat="fractal_level")
    assert len(same["changes"]) == 1
    await expect_error(mcp, "stat_history", "unknown stat", stat="nope")


async def test_item_history(call, history):
    res = await call("item_history", item="Glob of Ectoplasm", by_location=True)
    assert res["id"] == 19721
    assert [p["value"] for p in res["total_changes"]] == [268, 218]
    assert res["current_total"] == 218
    assert [p["count"] for p in res["locations"]["bank"]] == [255, 205]
    assert len(res["locations"]["tp_delivery"]) == 1
    windowed = await call("item_history", item="19721", since="1h")
    assert windowed["total_before_window"] is None
    unknown = await call("item_history", item="19684")
    assert unknown["current_total"] == 40


async def test_currency_history(call, mcp, history):
    res = await call("currency_history", currency="coin")
    assert res["id"] == 1 and res["name"] == "Coin"
    assert [p["formatted"] for p in res["changes"]] == ["123g 45s 67c", "128g 45s 67c"]
    karma = await call("currency_history", currency="2")
    assert karma["name"] == "Karma" and "formatted" not in karma["changes"][0]
    await expect_error(mcp, "currency_history", "unknown currency", currency="dragonite")


async def test_achievement_history(call, history):
    res = await call("achievement_history", achievement_id=1)
    assert res["name"] == "Centaur Slayer"
    assert res["first_seen_done_at"] is not None
    assert [p["value"]["current"] for p in res["changes"]] == [50, 100]
    never = await call("achievement_history", achievement_id=999)
    assert never["changes"] == [] and never["current"] is None


async def test_character_history(call, mcp, history):
    res = await call("character_history", name="Alt")
    assert [p["value"]["level"] for p in res["changes"]] == [20, 30]
    await expect_error(mcp, "character_history", "no history for character", name="Ghost")


async def test_unlock_history(call, mcp, history):
    res = await call("unlock_history", kind="skins")
    assert res["unlocked"] == [{"id": "12", "name": "Dreamer", "at": res["unlocked"][0]["at"], "value": 1}]
    assert res["removed"] == []
    emotes = await call("unlock_history", kind="emotes")
    assert emotes["unlocked"] == []
    await expect_error(mcp, "unlock_history", "unknown kind", kind="hats")


async def test_compare_snapshots(call, history):
    res = await call("compare_snapshots", start="first", end="latest")
    ch = res["changes"]
    assert ch["wallet"]["entries"] == [
        {"key": "1", "name": "Coin", "before": 1234567, "after": 1284567, "delta": 50000}
    ]
    assert ch["item"]["entries"][0]["name"] == "Glob of Ectoplasm"
    assert ch["item"]["entries"][0]["delta"] == -50
    assert ch["achievement"]["entries"][0]["name"] == "Centaur Slayer"
    assert ch["character"]["entries"][0]["key"] == "Alt"
    assert "gold" in {e["key"] for e in ch["stat"]["entries"]}
    custom = await call("compare_snapshots", start="1", end="2", kinds=["unlock:skins"], limit_per_kind=5)
    assert custom["changes"]["unlock:skins"]["entries"] == [{"key": "12", "before": None, "after": 1, "delta": 1}]


async def test_progress_report(call, history):
    r = await call("progress_report")
    assert r["stats"]["gold"]["delta"] == 5.0
    assert r["gold_delta_formatted"] == "5g 0s 0c"
    assert r["wallet"][0] == {"currency": "Coin", "before": 1234567, "after": 1284567, "delta": 50000}
    assert r["achievements_completed"] == [{"id": 1, "name": "Centaur Slayer"}]
    assert r["new_unlocks"] == {"skins": 1}
    assert r["top_item_losses"] == [{"id": 19721, "name": "Glob of Ectoplasm", "delta": -50}]
    assert r["top_item_gains"] == []
    assert r["characters"]["Alt"]["changes"]["level"]["delta"] == 10


async def test_server_status(call, history):
    s = await call("server_status")
    assert s["api_key_configured"] is True
    assert s["snapshots"] == 2
    assert s["last_snapshot"]["id"] == 2
    assert "items" in s["searchable_endpoints"]


async def test_app_context_scheduler_disabled_without_key(settings, tmp_path):
    app = AppContext.create(settings.with_overrides(api_key="", db_path=tmp_path / "x.db"))
    try:
        assert app.scheduler.interval == 0
    finally:
        await app.aclose()
