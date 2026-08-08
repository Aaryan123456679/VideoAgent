"""`S0.6.1` and `[D-67]` against a live Redis 7: TTLs, the atomic claim, and the real PEL.

The unit suite models Redis's semantics and puts the model under test. What it cannot do is
show that redis-py's `xautoclaim` signature, the `BUSYGROUP` error string, the `TTL` encoding
and the `SET NX EX` return value are what this code assumes — those are facts about a server
and a driver, and every one of them is a place a model can be wrong in the same direction as
the code it is checking.

**Skipping, not erroring, not hanging.** The guard is a bounded `PING` against the configured
`REDIS_URL`, not a check that `redis` imports. A wedged container leaves the client installed
and the socket unanswered; a short probe turns that into a skip with a reason, once per module.

**A key prefix per run.** Every key is written under `{registry key}` in a database this module
does not own, so the stream and the group are named with a run-scoped suffix and deleted
afterwards. A test that flushed the database would destroy a developer's local state.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from video_agent.persistence.keys import (
    KeyName,
    RedisKey,
    job_lock_key,
    llm_cache_key,
    progress_key,
)
from video_agent.persistence.queue import JobMessage, JobQueue
from video_agent.persistence.redis_client import (
    NO_EXPIRY_TTL,
    RedisStore,
    RedisUnavailableError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

PROBE_TIMEOUT_SECONDS: Final = 3.0
DEFAULT_URL: Final = "redis://localhost:6379/0"

UNREACHABLE_URL: Final = "redis://127.0.0.1:1/0"
"""Port 1 is reserved and refuses immediately, so the outage path is exercised without taking
the real server down."""

TENANT: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")

IDLE_MS: Final = 200
WORKER_A: Final = "worker-a"
WORKER_B: Final = "worker-b"
TTL_TOLERANCE_SECONDS: Final = 5
"""A round trip can consume a second or two, so an exact TTL equality would be flaky. The
assertion that matters is that the TTL is *the registry's*, not that no time has passed."""


# --- Reachability ---------------------------------------------------------------------------------


def _configured_url() -> str:
    return os.environ.get("REDIS_URL", DEFAULT_URL)


async def _probe(url: str) -> str | None:
    client: Redis = Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    finally:
        await client.aclose()
    return None


def _unreachable_reason(url: str) -> str | None:
    """None when the server answers within the probe timeout, otherwise why it did not."""

    async def bounded() -> str | None:
        try:
            return await asyncio.wait_for(_probe(url), timeout=PROBE_TIMEOUT_SECONDS)
        except TimeoutError:
            return f"no answer within {PROBE_TIMEOUT_SECONDS}s"

    return asyncio.run(bounded())


@pytest.fixture(scope="module")
def redis_url() -> str:
    """The configured URL, or a skip naming why the server did not answer."""
    url = _configured_url()
    reason = _unreachable_reason(url)
    if reason is not None:
        pytest.skip(f"redis unavailable: {reason}")
    return url


@pytest_asyncio.fixture
async def client(redis_url: str) -> AsyncIterator[Redis]:
    """A client for one test, closed afterwards."""
    connection: Redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        yield connection
    finally:
        await connection.aclose()


@pytest.fixture
def store(client: Redis) -> RedisStore:
    """The production store over a live client.

    Each test writes keys under fresh UUIDs and deletes them, rather than the fixture flushing:
    this module runs against whatever `REDIS_URL` names, which on a developer's machine is
    their own working database.
    """
    return RedisStore(client)


@pytest_asyncio.fixture
async def queue(client: Redis) -> AsyncIterator[JobQueue]:
    """A queue on a run-scoped stream, with its group created and both removed afterwards."""
    suffix = uuid.uuid4().hex[:12]
    stream = RedisKey(
        name=KeyName.JOBS_STREAM, value=f"jobs:stream:test:{suffix}", ttl_seconds=None
    )
    built = JobQueue(client, group=f"workers-{suffix}", stream=stream)
    await built.ensure_group()
    try:
        yield built
    finally:
        await client.delete(stream.value)


# --- Keys and TTLs --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_registry_ttl_is_what_the_server_records(store: RedisStore) -> None:
    """`persistence.md` §5's number, observed through `TTL` on a real server."""
    key = progress_key(uuid.uuid4())

    await store.set(key, "event")
    remaining = await store.ttl_of(key)
    await store.delete(key)

    assert key.ttl_seconds is not None
    assert key.ttl_seconds - TTL_TOLERANCE_SECONDS <= remaining <= key.ttl_seconds


@pytest.mark.asyncio
async def test_no_key_this_module_writes_is_left_without_an_expiry(store: RedisStore) -> None:
    """The unit suite asserts the TTL is *passed*; this asserts the server *applied* it.

    `-1` is Redis's answer for a key that will never expire, and a key that never expires in a
    store that is not the system of record is a leak nobody notices until the memory alarm.
    """
    key = llm_cache_key(uuid.uuid4().hex)

    await store.set(key, "cached")
    remaining = await store.ttl_of(key)
    await store.delete(key)

    assert remaining != NO_EXPIRY_TTL


@pytest.mark.asyncio
async def test_set_nx_is_atomic_on_the_server(store: RedisStore) -> None:
    """`SET NX EX` really is one round trip, and the loser is told it lost `[D-10]`."""
    key = job_lock_key(uuid.uuid4())

    first = await store.set_if_absent(key, WORKER_A)
    second = await store.set_if_absent(key, WORKER_B)
    holder = await store.get(key)
    await store.delete(key)

    assert (first, second) == (True, False)
    assert holder == WORKER_A


@pytest.mark.asyncio
async def test_a_miss_is_none_against_a_live_server(store: RedisStore) -> None:
    """The other half of the `[D-17]` distinction, confirmed where it actually matters."""
    assert await store.get(llm_cache_key(uuid.uuid4().hex)) is None


@pytest.mark.asyncio
async def test_an_unreachable_server_raises_store_003() -> None:
    """A refused connection is `VA-STORE-003`, not a miss — asserted against a real socket."""
    unreachable: Redis = Redis.from_url(UNREACHABLE_URL, decode_responses=True)
    try:
        with pytest.raises(RedisUnavailableError):
            await RedisStore(unreachable).get(llm_cache_key("anything"))
    finally:
        await unreachable.aclose()


# --- The queue, against a real consumer group -----------------------------------------------------


@pytest.mark.asyncio
async def test_group_creation_is_idempotent_against_a_real_server(queue: JobQueue) -> None:
    """`BUSYGROUP` is the string this code matches on, and only a real server produces it."""
    await queue.ensure_group()

    assert await queue.pending_count() == 0


@pytest.mark.asyncio
async def test_publish_read_ack_round_trips(queue: JobQueue) -> None:
    """The driver's reply shapes for `XADD`, `XREADGROUP` and `XACK` are what the parser assumes."""
    message = JobMessage(tenant_id=TENANT, job_id=uuid.uuid4(), node="generate_shot")
    entry_id = await queue.publish(message)

    delivered = await queue.read_new(WORKER_A)
    acked = await queue.ack(delivered[0].entry_id)

    assert delivered[0].entry_id == entry_id
    assert delivered[0].message == message
    assert acked is True
    assert await queue.pending_count() == 0


@pytest.mark.asyncio
async def test_an_unacknowledged_entry_is_redelivered_to_its_own_consumer(
    queue: JobQueue,
) -> None:
    """The crash-before-`XACK` path, on the server's own pending-entries list."""
    message = JobMessage(tenant_id=TENANT, job_id=uuid.uuid4(), node="generate_shot")
    entry_id = await queue.publish(message)
    await queue.read_new(WORKER_A)

    replayed = await queue.read_own_pending(WORKER_A)

    assert [delivery.entry_id for delivery in replayed] == [entry_id]
    assert replayed[0].redelivered is True
    await queue.ack(entry_id)


@pytest.mark.asyncio
async def test_a_stalled_entry_is_claimed_by_another_consumer(queue: JobQueue) -> None:
    """`XAUTOCLAIM` against a real idle timer — the one thing a fake clock cannot establish.

    The sleep is the point: `min_idle_time` is measured by the server, and the reply shape of
    `XAUTOCLAIM` changed between Redis 6 and 7.
    """
    message = JobMessage(tenant_id=TENANT, job_id=uuid.uuid4(), node="generate_shot")
    entry_id = await queue.publish(message)
    await queue.read_new(WORKER_A)

    too_soon = await queue.claim_stalled(WORKER_B, min_idle_ms=IDLE_MS * 10)
    await asyncio.sleep(IDLE_MS / 1000 * 2)
    claimed = await queue.claim_stalled(WORKER_B, min_idle_ms=IDLE_MS)

    assert too_soon == []
    assert [delivery.entry_id for delivery in claimed] == [entry_id]
    assert claimed[0].redelivered is True
    await queue.ack(entry_id)


@pytest.mark.asyncio
async def test_an_acknowledged_entry_is_not_reclaimed(queue: JobQueue) -> None:
    """At-least-once, not at-least-twice. `XACK` ends delivery on the server too."""
    message = JobMessage(tenant_id=TENANT, job_id=uuid.uuid4(), node="assemble")
    entry_id = await queue.publish(message)
    delivered = await queue.read_new(WORKER_A)
    await queue.ack(delivered[0].entry_id)

    await asyncio.sleep(IDLE_MS / 1000 * 2)
    claimed = await queue.claim_stalled(WORKER_B, min_idle_ms=IDLE_MS)

    assert delivered[0].entry_id == entry_id
    assert claimed == []


@pytest.mark.asyncio
async def test_the_stream_key_carries_no_expiry(queue: JobQueue, client: Redis) -> None:
    """`jobs:stream` is the one TTL-less key, and a queue that expired would drop work."""
    await queue.publish(JobMessage(tenant_id=TENANT, job_id=uuid.uuid4(), node="plan"))

    assert await client.ttl(queue.stream_key.value) == NO_EXPIRY_TTL
