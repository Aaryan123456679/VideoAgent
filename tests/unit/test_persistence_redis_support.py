"""An in-memory Redis with a real pending-entries list, and the checks that it is honest.

`T0.6`'s two hardest properties — a connection failure is not a miss, and delivery is
at-least-once — cannot be demonstrated against a live server in this environment, and would be
demonstrated badly against a live server anyway: killing a worker mid-`XACK` is not something a
test suite arranges reliably. So the semantics are modelled here, and the model is itself put
under test.

**What the fake models, and why each piece is present.**

- `SET ... NX EX` as one operation, because `claim` being atomic is the entire idempotency
  guarantee and a read-then-write fake would pass a broken implementation.
- A per-group **pending-entries list**: which consumer holds each delivered entry, when it was
  delivered, and how many times. That is the structure at-least-once delivery is made of.
  `XACK` removes from it; a crash does not.
- `XAUTOCLAIM` with a real idle-time comparison against a clock the test advances, so
  "reassigned after `min_idle_ms`" is asserted rather than assumed.

**What it deliberately does not model.** Reading history with an explicit id (`XREADGROUP` at
`0-0`) returns the consumer's pending entries **without** resetting their idle time or bumping
the delivery counter, which is Redis's own behaviour and is the reason a restarted worker can
still have its entries claimed out from under it. Modelling that as a reset would make a
`claim_stalled` test pass for the wrong reason.

The fakes are asserted against the production protocols at the bottom of this file, so a change
to `RedisCommands` or `RedisStreamCommands` breaks the build here rather than leaving tests
passing against a shape production no longer has.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

import pytest

from video_agent.persistence.keys import job_lock_key
from video_agent.persistence.queue import RedisStreamCommands
from video_agent.persistence.redis_client import (
    NO_EXPIRY_TTL,
    NO_SUCH_KEY_TTL,
    RedisCommands,
    RedisStore,
)

JOB: Final = UUID("22222222-2222-2222-2222-222222222222")

BUSYGROUP_MESSAGE: Final = "BUSYGROUP Consumer Group name already exists"
NEW_MESSAGES: Final = ">"
MILLISECONDS: Final = 1000


class FakeConnectionError(Exception):
    """What an unreachable server raises. Deliberately not a `redis` type.

    `persistence.redis_client.guard` catches `Exception` and reports `VA-STORE-003`. If the
    fake raised `redis.ConnectionError`, a `guard` narrowed to redis's own exception hierarchy
    would still pass this suite while missing a socket error, a DNS failure or a TLS error —
    all of which are the same outage to a caller.
    """


@dataclass
class _Stored:
    """One string value and its remaining TTL, in seconds. `None` means no expiry."""

    value: str
    ttl_seconds: int | None


@dataclass
class _PendingEntry:
    """One delivered, unacknowledged entry: who holds it, since when, and how often."""

    consumer: str
    delivered_at_ms: int
    delivery_count: int


@dataclass
class _Group:
    """A consumer group: how far it has read, and what it has handed out and not got back."""

    last_delivered: int = -1
    pending: dict[str, _PendingEntry] = field(default_factory=dict)


class FakeRedis:
    """An in-memory Redis covering the commands this repository issues.

    `now_ms` is public and writable: a test advances it to make an entry stale rather than
    sleeping, which is what keeps the `XAUTOCLAIM` test deterministic and instant.
    """

    def __init__(self, *, fail: Exception | None = None) -> None:
        """`fail` makes every command raise, which is how "Redis is down" is expressed."""
        self.values: dict[str, _Stored] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], _Group] = {}
        self.blocks: list[int | None] = []
        self.now_ms = 0
        self.closed = False
        self.commands: list[str] = []
        self._sequence = 0
        self._fail = fail

    # --- plumbing ----------------------------------------------------------------------------

    def _record(self, command: str) -> None:
        if self._fail is not None:
            raise self._fail
        self.commands.append(command)

    # --- key/value ---------------------------------------------------------------------------

    async def get(self, name: str) -> str | None:
        """The stored value, or `None` for a miss."""
        self._record("GET")
        stored = self.values.get(name)
        return None if stored is None else stored.value

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        """`SET`, optionally `NX`, optionally `EX`. One operation, as Redis makes it."""
        self._record("SET")
        if nx and name in self.values:
            return None
        self.values[name] = _Stored(value=value, ttl_seconds=ex)
        return True

    async def delete(self, *names: str) -> int:
        """Remove keys, returning how many existed."""
        self._record("DEL")
        return sum(self.values.pop(name, None) is not None for name in names)

    async def expire(self, name: str, time: int) -> bool:
        """Apply a TTL to an existing key."""
        self._record("EXPIRE")
        stored = self.values.get(name)
        if stored is None:
            return False
        stored.ttl_seconds = time
        return True

    async def ttl(self, name: str) -> int:
        """Seconds remaining, `-1` for no expiry, `-2` for no key — Redis's own encoding."""
        self._record("TTL")
        stored = self.values.get(name)
        if stored is None:
            return NO_SUCH_KEY_TTL
        return NO_EXPIRY_TTL if stored.ttl_seconds is None else stored.ttl_seconds

    async def ping(self) -> bool:
        """Round-trip."""
        self._record("PING")
        return True

    async def aclose(self) -> None:
        """Record the close. Never fails, even when the connection does."""
        self.closed = True

    # --- streams -----------------------------------------------------------------------------

    def entries_of(self, name: str) -> list[tuple[str, dict[str, str]]]:
        """The stream's entries. Keyed by name, so writing to the wrong stream is visible."""
        return self.streams.setdefault(name, [])

    async def xadd(self, name: str, fields: dict[Any, Any]) -> str:
        """Append an entry with a monotonic id."""
        self._record("XADD")
        self._sequence += 1
        entry_id = f"{self.now_ms}-{self._sequence}"
        self.entries_of(name).append((entry_id, {str(k): str(v) for k, v in fields.items()}))
        return entry_id

    async def xgroup_create(self, name: str, group: str, start: str, mkstream: bool) -> bool:
        """Create the group, raising `BUSYGROUP` when it already exists."""
        self._record("XGROUP CREATE")
        if (name, group) in self.groups:
            raise RuntimeError(BUSYGROUP_MESSAGE)
        if not mkstream and name not in self.streams:
            message = "ERR The XGROUP subcommand requires the key to exist"
            raise RuntimeError(message)
        last = len(self.entries_of(name)) - 1 if start == "$" else -1
        self.groups[name, group] = _Group(last_delivered=last)
        return True

    async def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: dict[Any, Any],
        count: int | None,
        block: int | None,
    ) -> list[Any] | None:
        """Deliver new entries (`>`) or replay this consumer's own pending ones."""
        self._record("XREADGROUP")
        self.blocks.append(block)
        name, cursor = next(iter(streams.items()))
        state = self.groups[str(name), group]
        pairs = (
            self._deliver_new(str(name), state, consumer, count)
            if cursor == NEW_MESSAGES
            else self._replay_own(str(name), state, consumer, count)
        )
        return [[str(name), pairs]] if pairs else None

    def _deliver_new(
        self, name: str, state: _Group, consumer: str, count: int | None
    ) -> list[tuple[str, dict[str, str]]]:
        entries = self.entries_of(name)
        limit = len(entries) if count is None else state.last_delivered + 1 + count
        selected = entries[state.last_delivered + 1 : limit]
        for entry_id, _ in selected:
            state.pending[entry_id] = _PendingEntry(
                consumer=consumer, delivered_at_ms=self.now_ms, delivery_count=1
            )
        state.last_delivered += len(selected)
        return list(selected)

    def _replay_own(
        self, name: str, state: _Group, consumer: str, count: int | None
    ) -> list[tuple[str, dict[str, str]]]:
        """History read: no idle-time reset, no delivery-count bump. See the module docstring."""
        owned = [entry_id for entry_id, held in state.pending.items() if held.consumer == consumer]
        if count is not None:
            owned = owned[:count]
        fields = dict(self.entries_of(name))
        return [(entry_id, fields[entry_id]) for entry_id in owned]

    async def xack(self, name: str, group: str, *ids: str) -> int:
        """Remove entries from the pending list. This is the only thing that removes them."""
        self._record("XACK")
        state = self.groups[name, group]
        return sum(state.pending.pop(entry_id, None) is not None for entry_id in ids)

    async def xautoclaim(
        self,
        name: str,
        group: str,
        consumer: str,
        min_idle_time: int,
        *,
        count: int | None,
    ) -> list[Any]:
        """Reassign entries idle at least `min_idle_time`, resetting their idle clock."""
        self._record("XAUTOCLAIM")
        state = self.groups[name, group]
        fields = dict(self.entries_of(name))
        claimed: list[tuple[str, dict[str, str]]] = []
        for entry_id, held in state.pending.items():
            if count is not None and len(claimed) >= count:
                break
            if self.now_ms - held.delivered_at_ms < min_idle_time:
                continue
            held.consumer = consumer
            held.delivered_at_ms = self.now_ms
            held.delivery_count += 1
            claimed.append((entry_id, fields[entry_id]))
        return ["0-0", claimed, []]

    async def xpending(self, name: str, group: str) -> dict[str, Any]:
        """The pending-entries summary."""
        self._record("XPENDING")
        return {"pending": len(self.groups[name, group].pending)}

    async def xlen(self, name: str) -> int:
        """How many entries the stream holds."""
        self._record("XLEN")
        return len(self.entries_of(name))

    # --- inspection used by tests --------------------------------------------------------------

    def delivery_count(self, name: str, group: str, entry_id: str) -> int:
        """How many times the group has handed `entry_id` out."""
        return self.groups[name, group].pending[entry_id].delivery_count

    def holder(self, name: str, group: str, entry_id: str) -> str:
        """Which consumer currently holds `entry_id`."""
        return self.groups[name, group].pending[entry_id].consumer


# --- The fake is a fake of something --------------------------------------------------------------


def test_the_fake_satisfies_both_production_protocols() -> None:
    """Structural assignment, resolved by `mypy --strict` rather than at runtime.

    Without this, a change to `RedisCommands` would leave every test below passing against a
    shape the real client no longer has. The runtime assertions are near-trivial because the
    check that matters happens at type-check time; they are here so the binding is not dead
    code that a formatter could remove.
    """
    fake = FakeRedis()
    commands: RedisCommands = fake
    streams: RedisStreamCommands = fake

    assert commands is fake
    assert streams is fake


@pytest.mark.asyncio
async def test_set_nx_is_compare_and_set_not_read_then_write() -> None:
    """The second `SET NX` on a live key returns `None`, exactly as Redis does."""
    fake = FakeRedis()

    first = await fake.set("k", "a", nx=True, ex=10)
    second = await fake.set("k", "b", nx=True, ex=10)

    assert first is True
    assert second is None
    assert await fake.get("k") == "a"


@pytest.mark.asyncio
async def test_ttl_encodes_the_three_redis_answers() -> None:
    """`-2` no key, `-1` no expiry, otherwise the seconds. A store that returned `0` for all
    three would make every TTL assertion in this suite vacuous."""
    fake = FakeRedis()

    assert await fake.ttl("absent") == NO_SUCH_KEY_TTL
    await fake.set("forever", "v")
    assert await fake.ttl("forever") == NO_EXPIRY_TTL
    await fake.set("expiring", "v", ex=30)
    expected_ttl = 30
    assert await fake.ttl("expiring") == expected_ttl


@pytest.mark.asyncio
async def test_a_failing_fake_fails_every_command() -> None:
    """The outage switch really does cut the connection, including on a plain read."""
    fake = FakeRedis(fail=FakeConnectionError("connection refused"))

    with pytest.raises(FakeConnectionError):
        await fake.get("k")
    with pytest.raises(FakeConnectionError):
        await fake.ping()


@pytest.mark.asyncio
async def test_the_pending_list_survives_a_missing_ack() -> None:
    """The property the whole queue rests on: delivered and unacknowledged stays pending."""
    fake = FakeRedis()
    await fake.xgroup_create("s", "g", "0", True)
    await fake.xadd("s", {"a": "1"})

    await fake.xreadgroup("g", "worker-1", {"s": NEW_MESSAGES}, 10, None)

    assert (await fake.xpending("s", "g"))["pending"] == 1
    assert fake.blocks == [None]


@pytest.mark.asyncio
async def test_an_acked_entry_leaves_the_pending_list_and_is_never_redelivered() -> None:
    """`XACK` is the only thing that removes an entry, and it removes it for good."""
    fake = FakeRedis()
    await fake.xgroup_create("s", "g", "0", True)
    entry_id = await fake.xadd("s", {"a": "1"})
    await fake.xreadgroup("g", "worker-1", {"s": NEW_MESSAGES}, 10, None)

    acked = await fake.xack("s", "g", entry_id)
    fake.now_ms += MILLISECONDS
    reclaimed = await fake.xautoclaim("s", "g", "worker-2", 1, count=10)

    assert acked == 1
    assert (await fake.xpending("s", "g"))["pending"] == 0
    assert reclaimed[1] == []


@pytest.mark.asyncio
async def test_autoclaim_refuses_an_entry_that_is_not_yet_stale() -> None:
    """A claim that ignored `min_idle_time` would hand every in-flight entry to a second worker."""
    fake = FakeRedis()
    await fake.xgroup_create("s", "g", "0", True)
    await fake.xadd("s", {"a": "1"})
    await fake.xreadgroup("g", "worker-1", {"s": NEW_MESSAGES}, 10, None)

    fresh = await fake.xautoclaim("s", "g", "worker-2", 30 * MILLISECONDS, count=10)

    assert fresh[1] == []


@pytest.mark.asyncio
async def test_two_streams_do_not_share_entries() -> None:
    """Entries are keyed by stream name, so a queue writing to the wrong key is visible.

    Without this the fake would be one global list and every assertion about `jobs:stream`
    would hold for any key at all.
    """
    fake = FakeRedis()
    await fake.xgroup_create("stream-a", "g", "0", True)
    await fake.xgroup_create("stream-b", "g", "0", True)
    await fake.xadd("stream-a", {"a": "1"})

    assert await fake.xlen("stream-a") == 1
    assert await fake.xlen("stream-b") == 0
    assert await fake.xreadgroup("g", "w", {"stream-b": NEW_MESSAGES}, 10, None) is None


@pytest.mark.asyncio
async def test_group_creation_raises_busygroup_the_second_time() -> None:
    """Every worker calls `XGROUP CREATE` on start; exactly one of them wins."""
    fake = FakeRedis()
    await fake.xgroup_create("s", "g", "0", True)

    with pytest.raises(RuntimeError, match="BUSYGROUP"):
        await fake.xgroup_create("s", "g", "0", True)


@pytest.mark.asyncio
async def test_a_group_created_at_the_beginning_sees_earlier_entries() -> None:
    """`XGROUP CREATE ... 0` versus `$` is the cold-start window, and the fake models both."""
    early = FakeRedis()
    await early.xadd("s", {"a": "1"})
    await early.xgroup_create("s", "from-zero", "0", True)
    await early.xgroup_create("s", "from-end", "$", True)

    from_zero = await early.xreadgroup("from-zero", "w", {"s": NEW_MESSAGES}, 10, None)
    from_end = await early.xreadgroup("from-end", "w", {"s": NEW_MESSAGES}, 10, None)

    assert from_zero is not None
    assert from_end is None


@pytest.mark.asyncio
async def test_the_store_over_the_fake_round_trips() -> None:
    """One end-to-end check that the fake is wired to the real `RedisStore`, not to itself."""
    fake = FakeRedis()
    store = RedisStore(fake)
    key = job_lock_key(JOB)

    created = await store.set_if_absent(key, "token")

    assert created is True
    assert await store.get(key) == "token"
    assert await store.ttl_of(key) == key.ttl_seconds
