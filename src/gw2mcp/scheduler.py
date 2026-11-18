"""Background task that takes account snapshots at a fixed interval."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from gw2mcp.storage import Storage, utcnow

log = logging.getLogger(__name__)


def seconds_until_due(last: datetime | None, interval: float, now: datetime) -> float:
    """How long to wait before the next snapshot is due (0 if overdue or none taken yet)."""
    if last is None:
        return 0.0
    elapsed = (now - last).total_seconds()
    return max(0.0, interval - elapsed)


class SnapshotScheduler:
    def __init__(
        self,
        take_snapshot: Callable[[], Awaitable[object]],
        storage: Storage,
        interval: float,
        *,
        retry_delay: float = 300.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.take_snapshot = take_snapshot
        self.storage = storage
        self.interval = interval
        self.retry_delay = min(retry_delay, interval) if interval > 0 else retry_delay
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self.runs = 0
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def next_delay(self) -> float:
        last = self.storage.latest_snapshot()
        return seconds_until_due(last.taken_at if last else None, self.interval, self.clock())

    async def run_forever(self) -> None:
        while True:
            delay = self.next_delay()
            if delay > 0:
                log.debug("next snapshot in %.0fs", delay)
                await asyncio.sleep(delay)
            try:
                await self.take_snapshot()
                self.runs += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.failures += 1
                log.exception("scheduled snapshot failed; retrying in %.0fs", self.retry_delay)
                await asyncio.sleep(self.retry_delay)

    def start(self) -> asyncio.Task[None] | None:
        if not self.enabled:
            log.info("periodic snapshots disabled")
            return None
        if self._task is None or self._task.done():
            log.info("periodic snapshots every %ss", self.interval)
            self._task = asyncio.create_task(self.run_forever(), name="gw2mcp-snapshots")
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
