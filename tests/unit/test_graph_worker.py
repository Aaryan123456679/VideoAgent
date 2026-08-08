"""`JobWorker.handle_one`'s lock/heartbeat/ack orchestration. `graph.md` §6, S1.4.1.

Deliberately does not exercise `_run_job`'s database/graph internals — those are covered by
the graph module's own tests. What's under test here is the control flow around them: the lock
is skipped when unavailable, released whether the run succeeds or raises, and the delivery is
only acked on success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.unit.test_persistence_redis_support import FakeRedis
from video_agent.graph.lock import JobLock
from video_agent.graph.worker import JobWorker
from video_agent.persistence.queue import Delivery, JobMessage, JobQueue
from video_agent.persistence.redis_client import RedisStore


@dataclass
class RecordingQueue:
    """Records `ack` calls; nothing else in `JobWorker.handle_one` touches the queue."""

    acked: list[str] = field(default_factory=list)

    async def ack(self, entry_id: str) -> bool:
        self.acked.append(entry_id)
        return True


def a_delivery(*, entry_id: str = "1-0") -> Delivery:
    return Delivery(
        entry_id=entry_id,
        message=JobMessage(tenant_id=uuid4(), job_id=uuid4(), node="plan_story"),
        redelivered=False,
    )


def a_worker(queue: RecordingQueue, lock: JobLock) -> JobWorker:
    return JobWorker(
        consumer="worker-1",
        queue=cast(JobQueue, queue),
        lock=lock,
        engine=cast(Any, None),
        gateway=cast(Any, None),
    )


@pytest.mark.asyncio
async def test_handle_one_skips_and_does_not_ack_when_lock_unavailable() -> None:
    store = RedisStore(FakeRedis())
    lock = JobLock(store)
    delivery = a_delivery()
    await lock.acquire(delivery.message.job_id)  # another worker already holds it

    queue = RecordingQueue()
    worker = a_worker(queue, lock)
    worker._run_job = AsyncMock()  # type: ignore[method-assign]

    await worker.handle_one(delivery)

    worker._run_job.assert_not_called()
    assert queue.acked == []


@pytest.mark.asyncio
async def test_handle_one_runs_then_acks_and_releases_the_lock() -> None:
    store = RedisStore(FakeRedis())
    lock = JobLock(store)
    delivery = a_delivery(entry_id="2-0")

    queue = RecordingQueue()
    worker = a_worker(queue, lock)
    worker._run_job = AsyncMock()  # type: ignore[method-assign]

    await worker.handle_one(delivery)

    worker._run_job.assert_awaited_once_with(delivery.message.tenant_id, delivery.message.job_id)
    assert queue.acked == ["2-0"]
    # released: a fresh acquire on the same job now succeeds
    reacquired = await lock.acquire(delivery.message.job_id)
    assert reacquired is not None


@pytest.mark.asyncio
async def test_handle_one_releases_the_lock_but_does_not_ack_when_run_job_raises() -> None:
    store = RedisStore(FakeRedis())
    lock = JobLock(store)
    delivery = a_delivery(entry_id="3-0")

    queue = RecordingQueue()
    worker = a_worker(queue, lock)
    worker._run_job = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="boom"):
        await worker.handle_one(delivery)

    assert queue.acked == []  # a failed run is not acked; it stays pending for reclaim
    reacquired = await lock.acquire(delivery.message.job_id)
    assert reacquired is not None
