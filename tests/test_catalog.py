import time

import pytest

from gw2mcp.catalog import CACHEABLE_ENDPOINTS, SEARCHABLE_ENDPOINTS, normalize_endpoint


def test_normalize_endpoint():
    assert normalize_endpoint("/v2/items") == "items"
    assert normalize_endpoint("achievements/categories") == "achievements/categories"


def test_searchable_subset():
    assert SEARCHABLE_ENDPOINTS <= CACHEABLE_ENDPOINTS
    assert "items" in SEARCHABLE_ENDPOINTS and "recipes" not in SEARCHABLE_ENDPOINTS


async def test_get_many_uses_cache(catalog, fake):
    first = await catalog.get_many("items", [19721, 19684, 999])
    assert set(first) == {"19721", "19684"}
    n = fake.count("v2/items")
    again = await catalog.get_many("/v2/items", ["19721", 19684])
    assert again == {k: first[k] for k in ("19721", "19684")}
    assert fake.count("v2/items") == n  # served from cache


async def test_get_many_refetches_stale(catalog, fake, storage):
    await catalog.get_many("items", [19721])
    storage._conn.execute("UPDATE catalog SET fetched_at = ?", (time.time() - 30 * 86400,))
    await catalog.get_many("items", [19721])
    assert fake.count("v2/items") == 2


async def test_non_cacheable_endpoint_not_stored(catalog, storage, fake):
    out = await catalog.get_many("commerce/prices", [19721])
    assert out["19721"]["sells"]["unit_price"] == 2500
    assert storage.catalog_get("commerce/prices", ["19721"]) == {}
    await catalog.get_many("commerce/prices", [19721])
    assert fake.count("v2/commerce/prices") == 2


async def test_names_and_get_one(catalog):
    assert await catalog.names("currencies", [1, 2]) == {"1": "Coin", "2": "Karma"}
    assert (await catalog.get_one("items", 30704))["name"] == "Twilight"
    assert await catalog.get_one("items", 1) is None
    assert await catalog.get_many("items", []) == {}


async def test_search_builds_index_once(catalog, fake):
    res = await catalog.search("items", "ecto")
    assert [r["name"] for r in res] == ["Ectoplasm Bucket", "Glob of Ectoplasm"]
    pages = fake.count("v2/items")
    await catalog.search("items", "twilight")
    assert fake.count("v2/items") == pages
    assert catalog.index_age("items") is not None
    assert catalog.index_age("skins") is None


async def test_build_index_force(catalog, fake):
    assert await catalog.build_index("currencies") == 4
    assert await catalog.build_index("currencies") == 4
    assert fake.count("v2/currencies") == 1
    await catalog.build_index("currencies", force=True)
    assert fake.count("v2/currencies") == 2


async def test_search_rejects_unsearchable(catalog):
    with pytest.raises(ValueError, match="not searchable"):
        await catalog.search("recipes", "x")
    with pytest.raises(ValueError, match="cannot be indexed"):
        await catalog.build_index("commerce/prices")
