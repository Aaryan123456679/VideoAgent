"""`S0.6.1` / `[D-67]` — the job queue delivers at least once, and the tests prove the *least*.

A queue test suite that publishes, reads and acknowledges demonstrates nothing about the
property this transport was chosen for. At-most-once would pass that suite. The three cases
below are the ones that separate them, and each is a failure that actually happens:

1. **A worker crashes before `XACK`.** The entry stays in the pending-entries list. It is not
   lost, and it is not silently completed.
2. **The restarted worker reads its own history.** The same entry comes back, to the same
   consumer, with `redelivered` set.
3. **The worker never comes back.** Another consumer `XAUTOCLAIM`s the entry once it has been
   idle past the threshold — and, crucially, *not before*.

`persistence.md` §5.1 is explicit that this is safe *only because* `[D-24]` already makes it
safe: `shot_attempt.request_fingerprint` is unique and `provider_project_id` reconciliation
re-reads an existing render. The queue therefore must not deduplicate, and
`test_the_queue_does_not_deduplicate` asserts the absence — because a queue that quietly
collapsed duplicates would be trusted to, right up until the first Redis flush.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import pytest

from tests.unit.test_persistence_redis_support import FakeConnectionError, FakeRedis
from video_agent.observability.codes import ErrorCode
from video_agent.persistence.keys import jobs_stream_key
from video_agent.persistence.queue import (
    CONSUMER_GROUP,
    Delivery,
    JobMessage,
    JobQueue,
    MalformedQueueMessageError,
    QueueUnavailableError,
)
from video_agent.persistence.redis_client import RedisUnavailableError

TENANT: Final = UUID("11111111-1111-1111-1111-111111111111")
JOB: Final = UUID("22222222-2222-2222-2222-222222222222")
OTHER_JOB: Final = UUID("33333333-3333-3333-3333-333333333333")

WORKER_A: Final = "worker-a"
WORKER_B: Final = "worker-b"

SECOND_MS: Final = 1000
IDLE_THRESHOLD_MS: Final = 30 * SECOND_MS
DELIVERED_TWICE: Final = 2

MESSAGE: Final = JobMessage(tenant_id=TENANT, job_id=JOB, node="generate_shot")


async def queue_with_group(fake: FakeRedis | None = None) -> tuple[JobQueue, FakeRedis]:
    """A queue over a fake with its consumer group already created."""
    client = fake if fake is not None else FakeRedis()
    queue = JobQueue(client)
    await queue.ensure_group()
    return queue, client


# --- The shape of the queue ------------------------------------------------------------------


def test_the_queue_writes_to_the_registered_stream_key() -> None:
    """`jobs:stream` from the registry, not a literal. One place decides the key."""
    assert JobQueue(FakeRedis()).stream_key == jobs_stream_key()


def test_a_message_carries_identifiers_and_nothing_else() -> None:
    """No prompt, no bytes, no state. `[CPS §Observability]` and `persistence.md` §5.1.

    Redis is not authoritative, so a message that carried state would be a second copy of
    something Postgres owns — and the copy is the one that is stale after a resume.
    """
    fields = MESSAGE.to_fields()

    assert set(fields) == {"tenant_id", "job_id", "node"}
    assert fields == {"tenant_id": str(TENANT), "job_id": str(JOB), "node": "generate_shot"}


@pytest.mark.asyncio
async def test_group_creation_is_idempotent_across_workers() -> None:
    """Every worker calls `ensure_group` on start; `BUSYGROUP` is the normal case."""
    queue, client = await queue_with_group()

    await queue.ensure_group()

    assert list(client.groups) == [(jobs_stream_key().value, CONSUMER_GROUP)]


@pytest.mark.asyncio
async def test_group_creation_still_reports_a_real_outage() -> None:
    """Only `BUSYGROUP` is swallowed. A connection failure must not be read as "already there"."""
    queue = JobQueue(FakeRedis(fail=FakeConnectionError("connection refused")))

    with pytest.raises(QueueUnavailableError) as raised:
        await queue.ensure_group()

    assert raised.value.code is ErrorCode.VA_STORE_003


@pytest.mark.asyncio
async def test_the_group_starts_at_the_beginning_of_the_stream() -> None:
    """An entry enqueued before the first worker started is still delivered.

    `XGROUP CREATE ... $` would abandon it, and that window is exactly a cold start.
    """
    client = FakeRedis()
    await client.xadd(jobs_stream_key().value, MESSAGE.to_fields())
    queue, _ = await queue_with_group(client)

    delivered = await queue.read_new(WORKER_A)

    assert [d.message for d in delivered] == [MESSAGE]


# --- The happy path, which proves the least ----------------------------------------------------


@pytest.mark.asyncio
async def test_publish_read_ack_removes_the_entry_from_the_pending_list() -> None:
    """The ordinary case. Necessary, and on its own not evidence of anything."""
    queue, client = await queue_with_group()
    entry_id = await queue.publish(MESSAGE)

    delivered = await queue.read_new(WORKER_A)
    acked = await queue.ack(delivered[0].entry_id)

    assert delivered[0].entry_id == entry_id
    assert delivered[0].message == MESSAGE
    assert delivered[0].redelivered is False
    assert acked is True
    assert await queue.pending_count() == 0
    assert client.commands.count("XACK") == 1


@pytest.mark.asyncio
async def test_a_new_read_never_returns_an_entry_someone_already_holds() -> None:
    """`>` means *never delivered*. Two workers polling do not both get the same entry."""
    queue, _ = await queue_with_group()
    await queue.publish(MESSAGE)

    first = await queue.read_new(WORKER_A)
    second = await queue.read_new(WORKER_B)

    assert len(first) == 1
    assert second == []


# --- At-least-once: the three cases that matter -------------------------------------------------


@pytest.mark.asyncio
async def test_a_consumer_crash_before_ack_leaves_the_entry_pending() -> None:
    """Case 1. The work is not lost, and it is not marked done.

    "Crash" is modelled as the thing a crash actually is from Redis's point of view: the entry
    was delivered and `XACK` never arrived. Nothing else about the process matters.
    """
    queue, _ = await queue_with_group()
    await queue.publish(MESSAGE)

    await queue.read_new(WORKER_A)

    assert await queue.pending_count() == 1
    assert await queue.depth() == 1


@pytest.mark.asyncio
async def test_the_restarted_worker_gets_its_own_entry_back_marked_redelivered() -> None:
    """Case 2. `XREADGROUP` at an explicit id replays what this consumer still holds."""
    queue, _ = await queue_with_group()
    entry_id = await queue.publish(MESSAGE)
    await queue.read_new(WORKER_A)

    replayed = await queue.read_own_pending(WORKER_A)

    assert [d.entry_id for d in replayed] == [entry_id]
    assert replayed[0].message == MESSAGE
    assert replayed[0].redelivered is True


@pytest.mark.asyncio
async def test_a_replay_is_scoped_to_the_consumer_that_holds_the_entry() -> None:
    """A second worker's history is empty; it must not adopt work by reading its own past."""
    queue, _ = await queue_with_group()
    await queue.publish(MESSAGE)
    await queue.read_new(WORKER_A)

    assert await queue.read_own_pending(WORKER_B) == []


@pytest.mark.asyncio
async def test_a_stalled_entry_is_claimed_by_another_consumer() -> None:
    """Case 3. `persistence.md` §9: `XPENDING` idle time exceeded, `XAUTOCLAIM` reassigns."""
    queue, client = await queue_with_group()
    entry_id = await queue.publish(MESSAGE)
    await queue.read_new(WORKER_A)

    client.now_ms += IDLE_THRESHOLD_MS
    claimed = await queue.claim_stalled(WORKER_B, min_idle_ms=IDLE_THRESHOLD_MS)

    stream = jobs_stream_key().value

    assert [d.entry_id for d in claimed] == [entry_id]
    assert claimed[0].redelivered is True
    assert client.holder(stream, CONSUMER_GROUP, entry_id) == WORKER_B
    assert client.delivery_count(stream, CONSUMER_GROUP, entry_id) == DELIVERED_TWICE


@pytest.mark.asyncio
async def test_an_entry_still_being_worked_is_not_claimed() -> None:
    """The idle threshold is enforced, not decorative.

    Without this the previous test would pass against an `XAUTOCLAIM` that ignored
    `min_idle_time` — which would hand every in-flight job to a second worker and turn
    at-least-once into always-twice.
    """
    queue, client = await queue_with_group()
    await queue.publish(MESSAGE)
    await queue.read_new(WORKER_A)

    client.now_ms += SECOND_MS
    claimed = await queue.claim_stalled(WORKER_B, min_idle_ms=IDLE_THRESHOLD_MS)

    assert claimed == []
    stream = jobs_stream_key().value
    assert client.holder(stream, CONSUMER_GROUP, jobs_first_entry(client)) == WORKER_A


def jobs_first_entry(client: FakeRedis) -> str:
    """The id of the first entry on the job stream."""
    return client.entries_of(jobs_stream_key().value)[0][0]


@pytest.mark.asyncio
async def test_an_acknowledged_entry_is_never_redelivered() -> None:
    """At-least-once is not at-least-twice: `XACK` ends it, on both redelivery paths."""
    queue, client = await queue_with_group()
    await queue.publish(MESSAGE)
    delivered = await queue.read_new(WORKER_A)
    await queue.ack(delivered[0].entry_id)

    client.now_ms += IDLE_THRESHOLD_MS

    assert await queue.read_own_pending(WORKER_A) == []
    assert await queue.claim_stalled(WORKER_B, min_idle_ms=IDLE_THRESHOLD_MS) == []
    assert await queue.read_new(WORKER_A) == []


@pytest.mark.asyncio
async def test_acknowledging_after_the_work_is_the_only_ordering_that_survives_a_crash() -> None:
    """Ack-on-receipt would be at-most-once, and `[D-67]` rejected it.

    Modelled directly: acknowledge first, then "crash". The entry is gone from the pending list
    and no redelivery path can find it, so the shot is dropped on a job that has already been
    partially billed.
    """
    queue, client = await queue_with_group()
    delivered_entry = await queue.publish(MESSAGE)
    delivered = await queue.read_new(WORKER_A)

    await queue.ack(delivered[0].entry_id)  # the mistake, made explicitly
    client.now_ms += IDLE_THRESHOLD_MS

    assert delivered[0].entry_id == delivered_entry
    assert await queue.pending_count() == 0
    assert await queue.claim_stalled(WORKER_B, min_idle_ms=IDLE_THRESHOLD_MS) == []


@pytest.mark.asyncio
async def test_the_queue_does_not_deduplicate() -> None:
    """Two publishes of the same step produce two entries. The collapse happens at `[D-24]`.

    A queue that deduplicated would be the thing relied on, and it would be relied on across a
    Redis flush — which is precisely when it stops working and the fingerprint constraint is
    all that is left.
    """
    queue, _ = await queue_with_group()

    first = await queue.publish(MESSAGE)
    second = await queue.publish(MESSAGE)
    delivered = await queue.read_new(WORKER_A, count=10)

    assert first != second
    assert [d.message for d in delivered] == [MESSAGE, MESSAGE]


@pytest.mark.asyncio
async def test_redelivered_is_a_record_not_a_gate() -> None:
    """The flag distinguishes the paths; it never suppresses one.

    Asserted as a property of the API: a redelivered `Delivery` carries the same message as the
    first delivery, so a consumer that skipped on the flag would skip real work.
    """
    queue, _ = await queue_with_group()
    await queue.publish(MESSAGE)
    first = await queue.read_new(WORKER_A)
    again = await queue.read_own_pending(WORKER_A)

    assert first[0].message == again[0].message
    assert (first[0].redelivered, again[0].redelivered) == (False, True)


# --- Ordering and parsing -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entries_are_delivered_in_publication_order() -> None:
    """A stream is ordered, and shot order is the order the graph enqueued."""
    queue, _ = await queue_with_group()
    other = JobMessage(tenant_id=TENANT, job_id=OTHER_JOB, node="assemble")
    await queue.publish(MESSAGE)
    await queue.publish(other)

    delivered = await queue.read_new(WORKER_A, count=10)

    assert [d.message for d in delivered] == [MESSAGE, other]


@pytest.mark.asyncio
async def test_a_malformed_entry_raises_carrying_its_id() -> None:
    """One unparseable entry must be `XACK`-able out, not reclaimed forever.

    Redis is not authoritative and can be written to by anything with the credentials; an entry
    that is not a job message is a fact to handle, not an invariant to assume.
    """
    queue, client = await queue_with_group()
    entry_id = await client.xadd(jobs_stream_key().value, {"job_id": "not-a-uuid"})

    with pytest.raises(MalformedQueueMessageError) as raised:
        await queue.read_new(WORKER_A)

    assert raised.value.entry_id == entry_id


@pytest.mark.asyncio
async def test_an_empty_read_is_an_empty_list_not_an_error() -> None:
    """An idle queue is the normal state of a queue."""
    queue, _ = await queue_with_group()

    assert await queue.read_new(WORKER_A) == []
    assert await queue.claim_stalled(WORKER_B, min_idle_ms=1) == []
    assert await queue.pending_count() == 0


@pytest.mark.asyncio
async def test_every_operation_reports_an_outage() -> None:
    """A dead Redis is `VA-STORE-003` on the queue too, never an empty batch.

    An empty batch from a down server reads as "no work", so a fleet of workers would idle
    quietly through an outage with nothing in the logs.
    """
    queue = JobQueue(FakeRedis(fail=FakeConnectionError("connection refused")))

    for operation in (
        queue.publish(MESSAGE),
        queue.read_new(WORKER_A),
        queue.read_own_pending(WORKER_A),
        queue.claim_stalled(WORKER_B, min_idle_ms=1),
        queue.ack("1-1"),
        queue.pending_count(),
        queue.depth(),
    ):
        with pytest.raises(RedisUnavailableError):
            await operation


def test_delivery_is_immutable() -> None:
    """A consumer cannot flip `redelivered` off and hand the object on.

    `setattr` rather than an assignment because an assignment to a frozen dataclass field is a
    type error, and `S0.1.2` allows no inline type-checker suppression to silence one. The
    runtime behaviour is
    what is under test either way.
    """
    delivery = Delivery(entry_id="1-1", message=MESSAGE, redelivered=True)
    field = "redelivered"

    with pytest.raises(AttributeError):
        setattr(delivery, field, False)
