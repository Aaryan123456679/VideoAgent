"""`graph.md` §6.2's one-writer-per-job Redis lock — claim, heartbeat, release, and reclaim."""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.unit.test_persistence_redis_support import FakeRedis
from video_agent.graph.lock import JobLock, JobLockLostError
from video_agent.persistence.redis_client import RedisStore


@pytest.mark.asyncio
async def test_acquire_then_second_acquire_fails() -> None:
    store = RedisStore(FakeRedis())
    lock = JobLock(store)
    job_id = uuid4()

    first = await lock.acquire(job_id)
    second = await lock.acquire(job_id)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_release_lets_a_new_worker_acquire() -> None:
    store = RedisStore(FakeRedis())
    lock = JobLock(store)
    job_id = uuid4()

    token = await lock.acquire(job_id)
    assert token is not None
    await lock.release(token)

    second = await lock.acquire(job_id)
    assert second is not None


@pytest.mark.asyncio
async def test_heartbeat_succeeds_while_this_worker_still_owns_the_lock() -> None:
    store = RedisStore(FakeRedis())
    lock = JobLock(store)
    token = await lock.acquire(uuid4())
    assert token is not None

    await lock.heartbeat(token)  # must not raise


@pytest.mark.asyncio
async def test_heartbeat_raises_once_another_worker_holds_the_lock() -> None:
    store = RedisStore(FakeRedis())
    lock = JobLock(store)
    job_id = uuid4()

    stale = await lock.acquire(job_id)
    assert stale is not None
    await lock.release(stale)
    fresh = await lock.acquire(job_id)
    assert fresh is not None

    with pytest.raises(JobLockLostError):
        await lock.heartbeat(stale)


@pytest.mark.asyncio
async def test_release_is_a_no_op_when_the_lock_was_already_reclaimed() -> None:
    store = RedisStore(FakeRedis())
    lock = JobLock(store)
    job_id = uuid4()

    stale = await lock.acquire(job_id)
    assert stale is not None
    await lock.release(stale)
    fresh = await lock.acquire(job_id)
    assert fresh is not None

    await lock.release(stale)  # must not tear down `fresh`'s lock

    await lock.heartbeat(fresh)  # still holds it — must not raise
