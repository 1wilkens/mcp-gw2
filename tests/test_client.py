import httpx
import pytest

from gw2mcp.client import (
    GW2APIError,
    GW2AuthError,
    GW2Client,
    MissingAPIKeyError,
    chunked,
    normalize_path,
)
from tests.fakeapi import API_KEY


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/v2/items", "v2/items"),
        ("v2/account/bank/", "v2/account/bank"),
        ("items", "v2/items"),
        ("account/achievements", "v2/account/achievements"),
        ("v2", "v2"),
        ("v2/characters/Zoë Name/core", "v2/characters/Zoë Name/core"),
    ],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("raw", ["http://evil/v2", "v2/../x", "v2/items?ids=1", "v2//items", "v2/a#b"])
def test_normalize_path_rejects(raw):
    with pytest.raises(ValueError):
        normalize_path(raw)


def test_chunked():
    assert [list(c) for c in chunked([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]


async def test_get_sends_headers(client, fake):
    data = await client.get("v2/account", auth=True)
    assert data["name"] == "Tester.1234"
    req = fake.requests[-1]
    assert req.headers["authorization"] == f"Bearer {API_KEY}"
    assert req.headers["x-schema-version"] == "latest"
    assert req.headers["user-agent"].startswith("gw2mcp/")
    assert req.url.params["lang"] == "en"


async def test_missing_key_raises_before_request(fake):
    async with GW2Client(None, transport=fake.transport()) as c:
        assert not c.has_key
        with pytest.raises(MissingAPIKeyError):
            await c.get("v2/account", auth=True)
        # public endpoints still work
        assert await c.get("v2/worldbosses") == ["shadow_behemoth", "svanir_shaman"]
    assert len(fake.requests) == 1


async def test_auth_error(fake):
    async with GW2Client("WRONG", transport=fake.transport(), backoff=0) as c:
        with pytest.raises(GW2AuthError) as exc:
            await c.get("v2/account", auth=True)
    assert exc.value.status == 401
    assert "Invalid access token" in str(exc.value)


async def test_404_raises(client):
    with pytest.raises(GW2APIError) as exc:
        await client.get("v2/nothing")
    assert exc.value.status == 404
    assert exc.value.message == "no such endpoint"


async def test_error_body_with_error_key(client, fake):
    fake.hooks["v2/gone"] = lambda request: httpx.Response(404, json={"error": "not found"})
    with pytest.raises(GW2APIError) as exc:
        await client.get("v2/gone")
    assert exc.value.message == "not found"


async def test_retry_on_429_then_success(client, fake):
    calls = {"n": 0}

    def hook(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"text": "too many requests"}, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    fake.hooks["v2/build"] = hook
    assert await client.get("v2/build") == {"ok": True}
    assert calls["n"] == 3


async def test_retry_exhausted(fake):
    fake.fail["v2/build"] = 503
    async with GW2Client(transport=fake.transport(), backoff=0, max_retries=2) as c:
        with pytest.raises(GW2APIError) as exc:
            await c.get("v2/build")
    assert exc.value.status == 503
    assert fake.count("v2/build") == 3


async def test_network_error_retries_then_fails():
    def handler(request):
        raise httpx.ConnectError("boom")

    async with GW2Client(transport=httpx.MockTransport(handler), backoff=0, max_retries=1) as c:
        with pytest.raises(GW2APIError) as exc:
            await c.get("v2/build")
    assert exc.value.status == 0


async def test_non_json_error_body():
    async with GW2Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="plain")), backoff=0) as c:
        with pytest.raises(GW2APIError) as exc:
            await c.get("v2/build")
    assert exc.value.message == "plain"


async def test_get_many_chunks_and_dedupes(client, fake):
    ids = [19721, 19684] + list(range(100000, 100400)) + [19721]
    objs = await client.get_many("v2/items", ids)
    assert {o["id"] for o in objs} == {19721, 19684}
    item_requests = [r for r in fake.requests if r.url.path == "/v2/items"]
    assert len(item_requests) == 3  # 402 unique ids → 200 + 200 + 2
    assert all(len(r.url.params["ids"].split(",")) <= 200 for r in item_requests)


async def test_get_many_all_invalid_returns_empty(client):
    assert await client.get_many("v2/items", [1, 2]) == []
    assert await client.get_many("v2/items", []) == []


async def test_get_many_propagates_other_errors(client, fake):
    fake.fail["v2/items"] = 400
    with pytest.raises(GW2APIError):
        await client.get_many("v2/items", [1])


async def test_get_all_pages(fake):
    fake.bulk["v2/items"] = {i: {"id": i, "name": f"Item {i}"} for i in range(450)}
    async with GW2Client(transport=fake.transport()) as c:
        objs = await c.get_all_pages("v2/items")
    assert len(objs) == 450
    assert sorted(o["id"] for o in objs) == list(range(450))
    assert fake.count("v2/items") == 3
