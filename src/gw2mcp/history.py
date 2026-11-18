"""Queries over the snapshot history (time parsing, diffs, series)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from gw2mcp.storage import Observation, Snapshot, Storage, from_iso, utcnow

_REL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|min|h|d|w|mo|y)\s*(?:ago)?\s*$", re.IGNORECASE)
_UNITS = {
    "m": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
    "mo": timedelta(days=30),
    "y": timedelta(days=365),
}

# Kinds compared by `compare_snapshots` by default (item_location is very verbose).
DEFAULT_COMPARE_KINDS = ("stat", "wallet", "item", "achievement", "character")


class HistoryError(ValueError):
    pass


def parse_time(value: str, *, now: datetime | None = None) -> datetime:
    """Parse an ISO date/datetime or a relative offset such as ``7d``, ``12h``, ``2w``, ``3mo``."""
    now = now or utcnow()
    v = value.strip()
    low = v.lower()
    if low in ("now", "today"):
        return now
    if low == "yesterday":
        return now - timedelta(days=1)
    m = _REL_RE.match(v)
    if m:
        return now - float(m.group(1)) * _UNITS[m.group(2).lower()]
    try:
        return from_iso(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoryError(
            f"cannot parse time {value!r}; use ISO 8601 (2025-01-31 or 2025-01-31T12:00:00Z) "
            "or a relative offset like 24h, 7d, 2w, 3mo, 1y"
        ) from exc


def resolve_snapshot(storage: Storage, ref: str | int | None, *, default: str = "latest") -> Snapshot:
    """Resolve a snapshot reference: id, ``latest``, ``first``, ISO time or relative offset."""
    if ref is None or (isinstance(ref, str) and not ref.strip()):
        ref = default
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        snap = storage.get_snapshot(int(ref))
        if snap is None:
            raise HistoryError(f"snapshot {ref} does not exist")
        return snap
    assert isinstance(ref, str)
    key = ref.strip().lower()
    if key == "latest":
        snap = storage.latest_snapshot()
    elif key in ("first", "oldest"):
        snap = storage.first_snapshot()
    else:
        snap = storage.snapshot_at(parse_time(ref))
    if snap is None:
        raise HistoryError("no snapshots recorded yet; run take_snapshot first")
    return snap


def _num(v: Any) -> float | int | None:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    return None


def diff_values(before: Any, after: Any) -> dict[str, Any] | None:
    """Describe the change between two values, or ``None`` when unchanged."""
    if before == after:
        return None
    entry: dict[str, Any] = {"before": before, "after": after}
    b, a = _num(before if before is not None else 0), _num(after if after is not None else 0)
    if b is not None and a is not None and not isinstance(before, dict) and not isinstance(after, dict):
        entry["delta"] = round(a - b, 4) if isinstance(a - b, float) else a - b
    elif isinstance(before, dict) or isinstance(after, dict):
        bd, ad = before or {}, after or {}
        changes = {}
        for k in sorted(set(bd) | set(ad)):
            if bd.get(k) != ad.get(k):
                bn, an = _num(bd.get(k, 0)), _num(ad.get(k, 0))
                changes[k] = (
                    {"before": bd.get(k), "after": ad.get(k), "delta": an - bn}
                    if bn is not None and an is not None
                    else {"before": bd.get(k), "after": ad.get(k)}
                )
        entry = {"before": before, "after": after, "changes": changes}
    return entry


def compare_states(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(set(before) | set(after), key=_sort_key):
        d = diff_values(before.get(key), after.get(key))
        if d is not None:
            out[key] = d
    return out


def _sort_key(k: str) -> tuple[int, Any]:
    return (0, int(k)) if k.isdigit() else (1, k)


def compare(
    storage: Storage, a: Snapshot, b: Snapshot, kinds: Iterable[str] = DEFAULT_COMPARE_KINDS
) -> dict[str, dict[str, dict[str, Any]]]:
    """Diff the full state of each kind between snapshots ``a`` and ``b``."""
    return {kind: compare_states(storage.state(kind, a.id), storage.state(kind, b.id)) for kind in kinds}


def newly_completed_achievements(changes: dict[str, dict[str, Any]]) -> list[str]:
    done = []
    for key, change in changes.items():
        before = change.get("before") or {}
        after = change.get("after") or {}
        if after.get("done") and not before.get("done"):
            done.append(key)
    return done


def series_points(observations: list[Observation], *, numeric_default: Any = None) -> list[dict[str, Any]]:
    """Convert change points to ``[{taken_at, snapshot_id, value}]`` (null → default)."""
    return [
        {
            "snapshot_id": o.snapshot_id,
            "taken_at": o.taken_at.isoformat(timespec="seconds"),
            "value": numeric_default if o.value is None else o.value,
        }
        for o in observations
    ]


def value_before(storage: Storage, kind: str, key: str, since: datetime | None) -> Any:
    """Value of a series just before ``since`` (to anchor a windowed history)."""
    if since is None:
        return None
    snap = storage.list_snapshots(until=since - timedelta(microseconds=1), limit=1)
    if not snap:
        return None
    return storage.value_at(kind, key, snap[0].id)
