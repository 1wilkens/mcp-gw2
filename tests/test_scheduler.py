import asyncio
from datetime import UTC, datetime, timedelta

from gw2mcp.scheduler import SnapshotScheduler, seconds_until_due

NOW = datetime(2025, 1, 1, tzinfo=UTC)


def test_seconds_until_due():
    assert seconds_until_due(None, 3600, NOW) == 0
    assert seconds_until_due(NOW - timedelta(minutes=10), 3600, NOW) == 3000
    assert seconds_until_due(NOW - timedelta(hours=2), 3600, NOW) == 0


def test_next_delay_uses_latest_snapshot(storage):
    s = SnapshotScheduler(lambda: None, storage, 3600, clock=lambda: NOW)
    assert s.next_delay() == 0
    storage.save_snapshot({"stat": {"a": 1}}, taken_at=NOW - timedelta(minutes=30))
    assert s.next_delay() == 1800


async def test_disabled_scheduler(storage):
    s = SnapshotScheduler(lambda: None, storage, 0)
    assert not s.enabled
    assert s.start() is None
    await s.stop()


async def test_runs_and_retries(storage):
    calls = []
    done = asyncio.Event()

    async def take():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")
        storage.save_snapshot({"stat": {"n": len(calls)}})
        done.set()

    s = SnapshotScheduler(take, storage, 3600, retry_delay=0)
    task = s.start()
    assert s.start() is task  # idempotent
    await asyncio.wait_for(done.wait(), 2)
    await asyncio.sleep(0)
    assert s.failures == 1 and s.runs == 1
    # the next one is due in an hour, so the task is sleeping now
    assert not task.done()
    await s.stop()
    assert task.cancelled()


def test_retry_delay_capped_by_interval(storage):
    assert SnapshotScheduler(lambda: None, storage, 60, retry_delay=300).retry_delay == 60
