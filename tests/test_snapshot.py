import pytest

from gw2mcp.client import GW2APIError, GW2Client
from gw2mcp.snapshot import (
    UNLOCK_KINDS,
    SnapshotCollector,
    achievement_points,
    character_items,
    iter_slots,
    unlock_value,
)


def test_unlock_value():
    assert unlock_value(5) == ("5", 1)
    assert unlock_value("sleep") == ("sleep", 1)
    assert unlock_value({"id": 3}) == ("3", 1)
    assert unlock_value({"id": 3, "count": 2}) == ("3", 2)
    assert unlock_value({"id": 3, "permanent": False, "quantity": 2}) == ("3", {"permanent": False, "quantity": 2})
    assert unlock_value({"x": 1}) is None
    assert unlock_value(None) is None


def test_iter_slots():
    assert list(iter_slots([None, {"id": 1}, {"id": None}, "x"])) == [{"id": 1}]
    assert list(iter_slots(None)) == []


def test_character_items():
    char = {
        "bags": [
            {"id": 8, "inventory": [{"id": 1, "count": 2}, None, {"id": 1, "count": 3}, {"id": 2}]},
            None,
            {"id": None, "inventory": [{"id": 4, "count": 1}]},
        ],
        "equipment": [{"id": 9}, {"id": 10, "count": 2}, {"slot": "x"}],
    }
    assert character_items(char) == {8: 1, 1: 5, 2: 1, 4: 1, 9: 1, 10: 2}
    assert character_items({}) == {}


@pytest.mark.parametrize(
    ("defn", "progress", "expected"),
    [
        ({"tiers": [{"count": 10, "points": 1}, {"count": 100, "points": 5}]}, {"current": 50}, 1),
        ({"tiers": [{"count": 10, "points": 1}, {"count": 100, "points": 5}]}, {"current": 5}, 0),
        ({"tiers": [{"count": 10, "points": 1}, {"count": 100, "points": 5}]}, {"done": True}, 6),
        ({"tiers": [{"count": 5, "points": 2}]}, {"done": True, "repeated": 3}, 8),
        ({"tiers": [{"count": 5, "points": 2}], "point_cap": 5}, {"done": True, "repeated": 3}, 5),
        ({"tiers": [{"count": 5, "points": 2}], "point_cap": -1}, {"done": True, "repeated": 1}, 4),
        ({}, {"done": True}, 0),
    ],
)
def test_achievement_points(defn, progress, expected):
    assert achievement_points(defn, progress) == expected


async def test_take_full_snapshot(collector, storage):
    snap = await collector.take()
    assert snap.account_name == "Tester.1234"
    assert snap.account_id == "ACC-1"
    # unlock endpoints the fake doesn't know about are missing → reported, not fatal
    assert set(snap.errors) == set()
    assert storage.state("wallet") == {"1": 1234567, "2": 1000, "4": 0}
    items = storage.state("item")
    # bank 255 + char bag 10 + tp delivery 3
    assert items["19721"] == 268
    assert items["30704"] == 2  # bank + equipped
    assert items["8932"] == 1  # the bag itself
    assert items["46731"] == 2
    assert "24277" not in items  # zero count materials are dropped
    loc = storage.state("item_location")
    assert loc["19721|bank"] == 255
    assert loc["19721|character:Hero"] == 10
    assert loc["19721|tp_delivery"] == 3
    assert loc["19684|materials"] == 40
    assert loc["46731|shared_inventory"] == 2

    ach = storage.state("achievement")
    assert ach["1"] == {"current": 50, "max": 100, "done": False}
    assert ach["3"] == {"current": 5, "max": 5, "done": True, "repeated": 10, "bits": 2}

    chars = storage.state("character")
    assert chars["Hero"]["crafting"] == {"Weaponsmith": 500}
    assert chars["Alt"]["level"] == 20

    stats = storage.state("stat")
    assert stats["gold"] == 123.4567
    assert stats["fractal_level"] == 100
    assert stats["wvw_rank"] == 250
    assert stats["commander"] is True
    assert stats["account_age_hours"] == 2.0
    assert stats["achievements_done"] == 2
    # AP: ach1 → 1, ach2 → 10, ach3 → min(2 + 10*2, cap 10) = 10 ; + daily 5000 + monthly 500
    assert stats["achievement_points"] == 1 + 10 + 10 + 5500
    assert "_ap" not in stats
    assert stats["mastery_points_earned"] == 26
    assert stats["mastery_points_spent:tyria"] == 10
    assert stats["luck"] == 12345
    assert stats["progression:fractal_agony_impedance"] == 15
    assert stats["pvp_rank"] == 40 and stats["pvp_wins"] == 10
    assert stats["unlocked:skins"] == 2
    assert stats["unlocked:dyes"] == 3
    assert stats["characters"] == 2
    assert stats["playtime_hours"] == 1.5
    assert stats["deaths"] == 8
    assert stats["level_80_characters"] == 1
    assert stats["tp_delivery_coins"] == 999
    assert stats["unique_items"] == len(items)
    # value: 268 ecto × 2000/2500 + 40 mithril × 50/60
    assert stats["item_value_buy_copper"] == 268 * 2000 + 40 * 50
    assert stats["item_value_sell_copper"] == 268 * 2500 + 40 * 60

    assert storage.state("unlock:finishers") == {"1": True, "2": {"permanent": False, "quantity": 3}}
    assert storage.state("unlock:legendary_armory") == {"30704": 1}
    assert storage.state("unlock:emotes") == {"dance": 1, "sleep": 1}
    assert storage.state("unlock:masteries") == {"1": 4}
    assert set(snap.sections) >= {"wallet", "item", "item_location", "achievement", "character", "stat"}
    assert {f"unlock:{k}" for k in UNLOCK_KINDS} <= set(snap.sections)


async def test_second_snapshot_stores_only_changes(collector, storage, fake):
    await collector.take()
    before = storage._query("SELECT COUNT(*) AS n FROM observations")[0]["n"]
    fake.account["v2/account/wallet"][0]["value"] = 2000000
    fake.account["v2/account/skins"].append(12)
    s2 = await collector.take()
    rows = storage._query("SELECT kind, key, value FROM observations WHERE snapshot_id = ?", (s2.id,))
    changed = {(r["kind"], r["key"]) for r in rows}
    assert ("wallet", "1") in changed
    assert ("unlock:skins", "12") in changed
    assert ("stat", "gold") in changed and ("stat", "unlocked:skins") in changed
    assert ("item", "19721") not in changed
    assert len(rows) < before / 4


async def test_permission_errors_are_recorded(collector, storage, fake):
    fake.fail["v2/account/achievements"] = 403
    fake.fail["v2/pvp/stats"] = 403
    fake.fail["v2/characters"] = 403
    snap = await collector.take()
    assert "achievements" in snap.errors and "permission" in snap.errors["achievements"]
    assert "pvp" in snap.errors
    assert "items:characters" in snap.errors
    # permission errors don't prevent item tracking (the source is just absent)
    assert storage.state("item")["19721"] == 258
    assert "achievement" not in snap.sections
    assert "character" not in snap.sections


async def test_transient_item_error_skips_items(collector, storage, fake):
    await collector.take()
    fake.fail["v2/account/bank"] = 500
    collector.client.max_retries = 0
    snap = await collector.take()
    assert "items:bank" in snap.errors
    assert "item" not in snap.sections
    assert storage.state("item")["19721"] == 268  # carried forward, not "lost"
    assert storage.state("stat")["item_value_sell_copper"] > 0  # stats merged from before


async def test_generic_section_error(collector, fake):
    fake.fail["v2/account/luck"] = 400
    snap = await collector.take()
    assert "luck" in snap.errors and "forced failure" in snap.errors["luck"]


async def test_achievement_definitions_failure(collector, storage, fake):
    fake.fail["v2/achievements"] = 400
    snap = await collector.take()
    assert "achievement_points" in snap.errors
    assert "achievement_points" not in storage.state("stat")
    assert storage.state("achievement")


async def test_value_disabled(client, storage, catalog):
    c = SnapshotCollector(client, storage, catalog, include_value=False)
    await c.take()
    assert "item_value_sell_copper" not in storage.state("stat")


async def test_no_api_key(storage, catalog, fake):
    async with GW2Client(None, transport=fake.transport()) as client:
        c = SnapshotCollector(client, storage, catalog)
        data = await c.collect()
        assert data.errors == {"api_key": "no API key configured"}
        with pytest.raises(GW2APIError, match=r"nothing collected \(api_key: no API key configured\)"):
            await c.take()
    assert storage.count_snapshots() == 0


async def test_legacy_wvw_rank_field(collector, storage, fake):
    acc = fake.account["v2/account"]
    del acc["wvw"]
    acc["wvw_rank"] = 99
    await collector.take()
    assert storage.state("stat")["wvw_rank"] == 99
