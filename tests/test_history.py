from datetime import UTC, datetime, timedelta

import pytest

from gw2mcp.history import (
    HistoryError,
    compare,
    compare_states,
    diff_values,
    newly_completed_achievements,
    parse_time,
    resolve_snapshot,
    series_points,
    value_before,
)

NOW = datetime(2025, 6, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7d", NOW - timedelta(days=7)),
        ("24h", NOW - timedelta(hours=24)),
        ("2w", NOW - timedelta(weeks=2)),
        ("30m", NOW - timedelta(minutes=30)),
        ("3mo", NOW - timedelta(days=90)),
        ("1y ago", NOW - timedelta(days=365)),
        ("1.5d", NOW - timedelta(hours=36)),
        ("now", NOW),
        ("yesterday", NOW - timedelta(days=1)),
        ("2025-01-31", datetime(2025, 1, 31, tzinfo=UTC)),
        ("2025-01-31T10:00:00Z", datetime(2025, 1, 31, 10, tzinfo=UTC)),
    ],
)
def test_parse_time(text, expected):
    assert parse_time(text, now=NOW) == expected


def test_parse_time_invalid():
    with pytest.raises(HistoryError):
        parse_time("last tuesday")


def test_diff_values():
    assert diff_values(1, 1) is None
    assert diff_values(1, 5) == {"before": 1, "after": 5, "delta": 4}
    assert diff_values(None, 3) == {"before": None, "after": 3, "delta": 3}
    assert diff_values(2.5, 1.0)["delta"] == -1.5
    assert diff_values(True, False)["delta"] == -1
    assert diff_values("a", "b") == {"before": "a", "after": "b"}
    d = diff_values({"current": 1, "done": False}, {"current": 3, "done": True, "x": "y"})
    assert d["changes"] == {
        "current": {"before": 1, "after": 3, "delta": 2},
        "done": {"before": False, "after": True, "delta": 1},
        "x": {"before": None, "after": "y"},
    }
    assert diff_values(None, {"a": 1})["changes"] == {"a": {"before": None, "after": 1, "delta": 1}}


def test_compare_states_sorted():
    out = compare_states({"10": 1, "2": 1, "b": 1}, {"10": 2, "2": 3, "a": 1})
    assert list(out) == ["2", "10", "a", "b"]


def test_newly_completed():
    changes = {
        "1": {"before": {"done": False}, "after": {"done": True}},
        "2": {"before": None, "after": {"done": True}},
        "3": {"before": {"done": True}, "after": {"done": True, "repeated": 2}},
        "4": {"before": {"done": False}, "after": {"done": False}},
    }
    assert newly_completed_achievements(changes) == ["1", "2"]


def make_history(storage):
    a = storage.save_snapshot({"wallet": {"1": 10}, "stat": {"ap": 5}}, taken_at=NOW - timedelta(days=10))
    b = storage.save_snapshot({"wallet": {"1": 30}, "stat": {"ap": 5}}, taken_at=NOW - timedelta(days=5))
    c = storage.save_snapshot({"wallet": {"1": 25, "2": 1}, "stat": {"ap": 9}}, taken_at=NOW - timedelta(days=1))
    return a, b, c


def test_resolve_snapshot(storage):
    with pytest.raises(HistoryError, match="no snapshots"):
        resolve_snapshot(storage, "latest")
    a, b, c = make_history(storage)
    assert resolve_snapshot(storage, None).id == c.id
    assert resolve_snapshot(storage, "", default="first").id == a.id
    assert resolve_snapshot(storage, "first").id == a.id
    assert resolve_snapshot(storage, b.id).id == b.id
    assert resolve_snapshot(storage, str(b.id)).id == b.id
    assert resolve_snapshot(storage, (NOW - timedelta(days=6)).isoformat()).id == a.id
    with pytest.raises(HistoryError, match="does not exist"):
        resolve_snapshot(storage, 999)


def test_compare(storage):
    a, b, c = make_history(storage)
    diff = compare(storage, a, c, ["wallet", "stat"])
    assert diff["wallet"] == {
        "1": {"before": 10, "after": 25, "delta": 15},
        "2": {"before": None, "after": 1, "delta": 1},
    }
    assert diff["stat"] == {"ap": {"before": 5, "after": 9, "delta": 4}}
    assert compare(storage, a, b, ["stat"]) == {"stat": {}}


def test_series_points_and_value_before(storage):
    a, b, c = make_history(storage)
    storage.save_snapshot({"wallet": {}}, taken_at=NOW)
    pts = series_points(storage.series("wallet", "2"), numeric_default=0)
    assert [p["value"] for p in pts] == [1, 0]
    assert value_before(storage, "wallet", "1", NOW - timedelta(days=3)) == 30
    assert value_before(storage, "wallet", "1", None) is None
    assert value_before(storage, "wallet", "1", NOW - timedelta(days=30)) is None
