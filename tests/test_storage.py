import time
from datetime import UTC, datetime, timedelta

from gw2mcp.storage import Storage, from_iso, to_iso

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def save(storage, data, hours, **kw):
    return storage.save_snapshot(data, taken_at=T0 + timedelta(hours=hours), **kw)


def obs_rows(storage):
    return storage._query("SELECT kind, key, snapshot_id, value FROM observations ORDER BY snapshot_id, kind, key")


def test_iso_roundtrip():
    assert to_iso(datetime(2025, 1, 1, 12, 0, 0)) == "2025-01-01T12:00:00+00:00"
    assert from_iso("2025-01-01T12:00:00") == datetime(2025, 1, 1, 12, tzinfo=UTC)


def test_file_database_persists(tmp_path):
    path = tmp_path / "sub" / "db.sqlite3"
    s = Storage(path)
    s.save_snapshot({"wallet": {"1": 5}}, taken_at=T0, account_name="A")
    s.set_meta("k", "v")
    s.close()
    s2 = Storage(path)
    assert s2.latest_snapshot().account_name == "A"
    assert s2.state("wallet") == {"1": 5}
    assert s2.get_meta("k") == "v"
    assert s2.get_meta("schema_version") == "1"
    assert s2.get_meta("missing") is None
    s2.close()


def test_delta_encoding(storage):
    s1 = save(storage, {"wallet": {"1": 100, "2": 5}}, 0, account_id="id", account_name="Acc")
    s2 = save(storage, {"wallet": {"1": 100, "2": 6}}, 1)
    s3 = save(storage, {"wallet": {"1": 150}}, 2)
    rows = [tuple(r) for r in obs_rows(storage)]
    assert rows == [
        ("wallet", "1", s1.id, "100"),
        ("wallet", "2", s1.id, "5"),
        ("wallet", "2", s2.id, "6"),
        ("wallet", "1", s3.id, "150"),
        ("wallet", "2", s3.id, None),
    ]
    assert storage.state("wallet", s1.id) == {"1": 100, "2": 5}
    assert storage.state("wallet", s2.id) == {"1": 100, "2": 6}
    assert storage.state("wallet") == {"1": 150}
    assert storage.value_at("wallet", "2", s2.id) == 6
    assert storage.value_at("wallet", "2") is None
    assert storage.value_at("wallet", "9") is None


def test_missing_kind_carries_forward(storage):
    save(storage, {"wallet": {"1": 1}, "item": {"5": 2}}, 0)
    s2 = save(storage, {"wallet": {"1": 2}}, 1, errors={"items": "boom"})
    assert storage.state("item", s2.id) == {"5": 2}
    assert s2.complete is False
    assert s2.errors == {"items": "boom"}
    assert s2.sections == ["wallet"]


def test_complex_values_and_none_filtered(storage):
    save(storage, {"achievement": {"1": {"current": 1, "done": False}, "2": None}}, 0)
    s2 = save(storage, {"achievement": {"1": {"done": False, "current": 1}}}, 1)
    # key order of dicts must not cause spurious changes
    assert [r["snapshot_id"] for r in obs_rows(storage)] == [1]
    assert storage.state("achievement", s2.id) == {"1": {"current": 1, "done": False}}


def test_series_and_prefix(storage):
    save(storage, {"item_location": {"5|bank": 1, "5|materials": 2, "50|bank": 9}}, 0)
    save(storage, {"item_location": {"5|bank": 3, "50|bank": 9}}, 1)
    series = storage.series("item_location", key_prefix="5|")
    assert [(o.key, o.value) for o in series] == [
        ("5|bank", 1),
        ("5|materials", 2),
        ("5|bank", 3),
        ("5|materials", None),
    ]
    assert [o.value for o in storage.series("item_location", "50|bank")] == [9]
    windowed = storage.series("item_location", key_prefix="5|", since=T0 + timedelta(minutes=30))
    assert len(windowed) == 2
    assert storage.series("item_location", key_prefix="5|", until=T0)[0].as_dict()["value"] == 1
    assert storage.kinds() == ["item_location"]
    assert storage.keys("item_location") == ["50|bank", "5|bank", "5|materials"]


def test_snapshot_lookup(storage):
    assert storage.latest_snapshot() is None
    assert storage.snapshot_at(T0) is None
    a = save(storage, {"stat": {"x": 1}}, 0)
    b = save(storage, {"stat": {"x": 2}}, 24)
    assert storage.first_snapshot().id == a.id
    assert storage.latest_snapshot().id == b.id
    assert storage.snapshot_at(T0 + timedelta(hours=12)).id == a.id
    assert storage.snapshot_at(T0 + timedelta(hours=48)).id == b.id
    assert storage.snapshot_at(T0 - timedelta(days=1)).id == a.id  # falls back to the first after
    assert [s.id for s in storage.list_snapshots()] == [b.id, a.id]
    assert [s.id for s in storage.list_snapshots(since=T0 + timedelta(hours=1))] == [b.id]
    assert [s.id for s in storage.list_snapshots(until=T0 + timedelta(hours=1))] == [a.id]
    assert len(storage.list_snapshots(limit=1)) == 1
    assert storage.count_snapshots() == 2
    assert storage.get_snapshot(999) is None
    d = a.as_dict()
    assert d["taken_at"] == "2025-01-01T00:00:00+00:00" and d["complete"] is True


def test_catalog_put_get_ttl(storage):
    n = storage.catalog_put("items", [{"id": 1, "name": "A"}, {"id": 2, "title": "B"}, {"noid": True}])
    assert n == 2
    assert storage.catalog_get("items", ["1", "2", "3"]) == {"1": {"id": 1, "name": "A"}, "2": {"id": 2, "title": "B"}}
    assert storage.catalog_get("items", []) == {}
    storage._conn.execute("UPDATE catalog SET fetched_at = ? WHERE id = '1'", (time.time() - 1000,))
    assert set(storage.catalog_get("items", ["1", "2"], max_age=500)) == {"2"}


def test_catalog_get_many_ids(storage):
    storage.catalog_put("items", [{"id": i, "name": f"I{i}"} for i in range(1200)])
    assert len(storage.catalog_get("items", [str(i) for i in range(1200)])) == 1200


def test_catalog_search_ranking(storage):
    storage.catalog_put(
        "items",
        [
            {"id": 1, "name": "Mighty Ectoplasm Bucket"},
            {"id": 2, "name": "Ectoplasm"},
            {"id": 3, "name": "Ectoplasm Bucket"},
            {"id": 4, "name": "Glob of Ectoplasm"},
            {"id": 5, "name": "Unrelated"},
            {"id": 6},
        ],
    )
    assert [r["id"] for r in storage.catalog_search("items", "ectoplasm")] == [2, 3, 4, 1]
    assert [r["id"] for r in storage.catalog_search("items", "bucket ecto")] == [3, 1]
    assert [r["id"] for r in storage.catalog_search("items", "ECTO", limit=1)] == [2]
    assert storage.catalog_search("items", "   ") == []


def test_catalog_index_info(storage):
    assert storage.catalog_index_info("items") is None
    storage.catalog_mark_indexed("items", 42)
    built, count = storage.catalog_index_info("items")
    assert count == 42 and built <= time.time()
