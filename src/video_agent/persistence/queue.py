"""The job queue: a Redis stream with a consumer group, and at-least-once delivery. `[D-67]`

`persistence.md` §5.1. Redis 7 is already mandated for locks, idempotency and progress, so the
queue adds no dependency — but it does add a semantic, and the semantic is the whole point:

> Delivery is **at-least-once**: a job step can be delivered twice (worker crash before `XACK`,
> or an `XAUTOCLAIM` of a stalled pending entry).

At-most-once was rejected because it drops work on a worker crash, and a dropped shot on a
paid, partially-billed job is worse than a duplicate. The duplicate is survivable **only
because `[D-24]` already makes it survivable** — `shot_attempt.request_fingerprint` is unique
and `provider_project_id` reconciliation re-reads an existing render rather than submitting a
new paid one. This module does not add a second, weaker copy of that guarantee, and it must
not: a queue that deduplicated would be a queue whose deduplication is the thing that is
trusted, and it would be trusted right up to the first Redis flush.

**Nothing here is authoritative.** `persistence.md` §5.1: queue entries are recoverable from
job status in Postgres. A message carries `tenant_id` and `job_id` so a worker knows which
tenant scope to open, and that is all it is trusted for — the worker's first act is to load the
job under that scope, and row-level security returns zero rows for a pair that does not exist.
A forged or stale message therefore finds no job rather than reaching another tenant's.

**Three ways an entry comes back, and each is exercised.**

- `read_new` takes only entries nobody has been given (`>`), so it never redelivers.
- `read_own_pending` takes the entries *this* consumer was given and did not `XACK` — the
  worker-restarted-after-a-crash path.
- `claim_stalled` takes entries another consumer was given and has held past `min_idle_ms`
  (`XAUTOCLAIM`) — the worker-died path. `persistence.md` §9 names it by that mechanism.

Both redelivery paths set `Delivery.redelivered`, which exists so a consumer can *record* the
redelivery. It must never be used to skip work: a consumer that trusts the flag has replaced
the fingerprint check with a Redis field, and Redis is not authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import UUID

from video_agent.observability.errors import VideoAgentError
from video_agent.persistence.keys import RedisKey, jobs_stream_key
from video_agent.persistence.redis_client import (
    RedisUnavailableError,
    create_redis_client,
    guard,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Mapping, Sequence

    from video_agent.config.settings import Settings

CONSUMER_GROUP: Final = "workers"
"""The single consumer group. One group means one delivery of each entry to one worker; a
second group would deliver every entry twice by design, which is a fan-out, not a queue."""

NEW_MESSAGES: Final = ">"
"""The `XREADGROUP` id that means *entries never delivered to anyone*."""

OWN_PENDING: Final = "0-0"
"""The `XREADGROUP` id that means *the entries this consumer already holds*, oldest first."""

GROUP_FROM_BEGINNING: Final = "0"
"""`XGROUP CREATE ... 0` — the group sees entries already in the stream.

`$` would have the group start at the *end*, silently abandoning anything enqueued between the
stream being created and the group being created. That window is exactly a cold start.
"""

FIELD_TENANT_ID: Final = "tenant_id"
FIELD_JOB_ID: Final = "job_id"
FIELD_NODE: Final = "node"

BUSYGROUP: Final = "BUSYGROUP"
"""What Redis says when the consumer group already exists. Not an error: `ensure_group` is
called by every worker on start, and exactly one of them wins the race."""

ENTRY_PAIR_LENGTH: Final = 2
"""A stream entry is `(entry_id, field_map)`. Anything shorter is not one, and is skipped
rather than unpacked — a malformed reply must not raise inside a normalisation helper."""

AUTOCLAIM_MESSAGES_INDEX: Final = 1
"""`XAUTOCLAIM` replies `[next_start_id, entries, deleted_ids]`. Only the entries are used —
`next_start_id` matters when paging a very long pending list, which a caller can do by looping
until `claim_stalled` returns nothing."""


class MalformedQueueMessageError(VideoAgentError):
    """A stream entry whose fields are not a job message.

    Carries the entry id so the consumer can `XACK` it out of the pending list rather than
    letting one unparseable entry be reclaimed forever. `VA-INT-001` by inheritance: the store
    is fine, the thing written to it was not.
    """

    def __init__(self, message: str, entry_id: str) -> None:
        """Record which entry could not be parsed."""
        super().__init__(message)
        self.entry_id = entry_id


class QueueUnavailableError(RedisUnavailableError):
    """The stream could not be reached. `VA-STORE-003`, retryable.

    A subclass rather than a sibling, so a worker's `except RedisUnavailableError` covers the
    queue as well as the key/value store. Every command in this module ultimately raises one or
    the other, and a caller backing off from Redis should not have to enumerate which.
    """


@dataclass(frozen=True, slots=True)
class JobMessage:
    """One unit of dispatched work: which job, in which tenant, at which node.

    Deliberately three identifiers and no state. Anything else would be a second copy of
    something Postgres owns, and the copy would be the one that is stale after a resume.
    """

    tenant_id: UUID
    job_id: UUID
    node: str

    def to_fields(self) -> dict[str, str]:
        """The stream entry's field map. Identifiers only — never a prompt, never bytes."""
        return {
            FIELD_TENANT_ID: str(self.tenant_id),
            FIELD_JOB_ID: str(self.job_id),
            FIELD_NODE: self.node,
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str, str], entry_id: str) -> JobMessage:
        """Parse a stream entry, refusing anything that is not a job message."""
        try:
            return cls(
                tenant_id=UUID(fields[FIELD_TENANT_ID]),
                job_id=UUID(fields[FIELD_JOB_ID]),
                node=fields[FIELD_NODE],
            )
        except (KeyError, ValueError, AttributeError, TypeError) as exc:
            message = f"stream entry {entry_id} is not a job message: {type(exc).__name__}"
            raise MalformedQueueMessageError(message, entry_id) from exc


@dataclass(frozen=True, slots=True)
class Delivery:
    """One entry handed to a consumer, and whether it had been handed out before.

    `redelivered` is for the record, not for the decision. See the module docstring.
    """

    entry_id: str
    message: JobMessage
    redelivered: bool


class RedisStreamCommands(Protocol):
    """The stream commands the queue issues, and no others.

    Every parameter is positional-only so the protocol constrains the *shape* of each call
    rather than redis-py's parameter names, and so the queue's own call sites read the same as
    the Redis documentation they implement.
    """

    def xadd(self, name: str, fields: dict[Any, Any], /) -> Awaitable[Any]:
        """Append an entry, returning its id."""
        ...  # pragma: no cover - protocol declaration

    def xgroup_create(self, name: str, group: str, start: str, mkstream: bool, /) -> Awaitable[Any]:
        """Create a consumer group, optionally creating the stream with it."""
        ...  # pragma: no cover - protocol declaration

    def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: dict[Any, Any],
        count: int | None,
        block: int | None,
        /,
    ) -> Awaitable[Any]:
        """Read entries for one consumer of one group."""
        ...  # pragma: no cover - protocol declaration

    def xack(self, name: str, group: str, /, *ids: str) -> Awaitable[Any]:
        """Remove entries from the group's pending list."""
        ...  # pragma: no cover - protocol declaration

    def xautoclaim(
        self,
        name: str,
        group: str,
        consumer: str,
        min_idle_time: int,
        /,
        *,
        count: int | None,
    ) -> Awaitable[Any]:
        """Reassign entries idle longer than `min_idle_time` to `consumer`.

        `start_id` is not in this signature and is therefore redis-py's default, `0-0` — the
        beginning of the pending-entries list. Paging from a cursor is not needed: claiming an
        entry resets its idle time, so a second call with the same `min_idle_time` skips
        everything the first call took and a caller loops until it gets nothing back.
        """
        ...  # pragma: no cover - protocol declaration

    def xpending(self, name: str, group: str, /) -> Awaitable[Any]:
        """Summarise the group's pending-entries list."""
        ...  # pragma: no cover - protocol declaration

    def xlen(self, name: str, /) -> Awaitable[Any]:
        """How many entries the stream holds."""
        ...  # pragma: no cover - protocol declaration


def _entries(reply: object) -> list[tuple[str, dict[str, str]]]:
    """Normalise an `XREADGROUP` reply into `(entry_id, fields)` pairs.

    `XREADGROUP` answers `[[stream_name, [(id, fields), ...]], ...]` and, when nothing is
    available, `None` or an empty list depending on the server and the client. All three mean
    the same thing to this module.
    """
    if not isinstance(reply, list):
        return []
    pairs: list[tuple[str, dict[str, str]]] = []
    for stream in reply:
        if not isinstance(stream, list | tuple) or len(stream) < ENTRY_PAIR_LENGTH:
            continue
        pairs.extend(_pairs(stream[1]))
    return pairs


def _pairs(raw: object) -> list[tuple[str, dict[str, str]]]:
    """Normalise a list of `(entry_id, field_map)` tuples."""
    if not isinstance(raw, list | tuple):
        return []
    pairs: list[tuple[str, dict[str, str]]] = []
    for entry in raw:
        if not isinstance(entry, list | tuple) or len(entry) < ENTRY_PAIR_LENGTH:
            continue
        entry_id, fields = entry[0], entry[1]
        if not isinstance(fields, dict):
            continue
        decoded = {str(name): str(value) for name, value in fields.items()}
        pairs.append((str(entry_id), decoded))
    return pairs


class JobQueue:
    """`jobs:stream` with one consumer group, and the three ways an entry is delivered."""

    def __init__(
        self,
        client: RedisStreamCommands,
        *,
        group: str = CONSUMER_GROUP,
        stream: RedisKey | None = None,
    ) -> None:
        """Bind to the stream key from the registry. `stream` is an injection point for tests."""
        self._client = client
        self._group = group
        self._stream = stream if stream is not None else jobs_stream_key()

    @property
    def stream_key(self) -> RedisKey:
        """The registry key this queue writes to."""
        return self._stream

    async def ensure_group(self) -> None:
        """Create the consumer group if it does not exist, and the stream with it.

        `mkstream` is set because a cold deployment has no stream yet and `XGROUP CREATE`
        against a missing key is an error — a worker that started before the first job would
        otherwise crash-loop until someone enqueued something.

        `BUSYGROUP` is swallowed and nothing else is: every worker calls this on start, so the
        group already existing is the normal case, while a connection failure here must still
        surface as `VA-STORE-003`.
        """
        try:
            await self._client.xgroup_create(
                self._stream.value, self._group, GROUP_FROM_BEGINNING, True
            )
        except Exception as exc:
            if BUSYGROUP in str(exc):
                return
            message = f"redis XGROUP CREATE failed: {type(exc).__name__}"
            raise QueueUnavailableError(message) from exc

    async def publish(self, message: JobMessage) -> str:
        """Append one step to the queue, returning the entry id."""
        entry_id = await guard(
            self._client.xadd(self._stream.value, message.to_fields()),
            "XADD",
            self._stream,
        )
        return str(entry_id)

    async def read_new(
        self, consumer: str, *, count: int = 1, block_ms: int | None = None
    ) -> list[Delivery]:
        """Entries nobody has been given yet. Never redelivered, by definition of `>`."""
        reply = await guard(
            self._client.xreadgroup(
                self._group, consumer, {self._stream.value: NEW_MESSAGES}, count, block_ms
            ),
            "XREADGROUP",
            self._stream,
        )
        return self._deliveries(reply, redelivered=False)

    async def read_own_pending(self, consumer: str, *, count: int = 10) -> list[Delivery]:
        """Entries this consumer was given and never acknowledged.

        The restart path: a worker that died between `XREADGROUP` and `XACK` still owns those
        entries, and reading with an explicit id rather than `>` is how it gets them back
        without waiting for another worker's `XAUTOCLAIM` idle timer.
        """
        reply = await guard(
            self._client.xreadgroup(
                self._group, consumer, {self._stream.value: OWN_PENDING}, count, None
            ),
            "XREADGROUP PENDING",
            self._stream,
        )
        return self._deliveries(reply, redelivered=True)

    async def claim_stalled(
        self, consumer: str, *, min_idle_ms: int, count: int = 10
    ) -> list[Delivery]:
        """Take over entries another consumer has held past `min_idle_ms`.

        `persistence.md` §9: *worker dies holding a pending entry → `XPENDING` idle time
        exceeded → `XAUTOCLAIM` reassigns it; the job resumes from its last checkpoint.*
        """
        reply = await guard(
            self._client.xautoclaim(
                self._stream.value, self._group, consumer, min_idle_ms, count=count
            ),
            "XAUTOCLAIM",
            self._stream,
        )
        raw = (
            reply[AUTOCLAIM_MESSAGES_INDEX]
            if isinstance(reply, list | tuple) and len(reply) > AUTOCLAIM_MESSAGES_INDEX
            else []
        )
        return self._to_deliveries(_pairs(raw), redelivered=True)

    async def ack(self, entry_id: str) -> bool:
        """Acknowledge one entry. `True` when it was still pending.

        Acknowledging is the *last* thing a consumer does. Acking on receipt would convert this
        queue to at-most-once and drop the entry on a crash, which `[D-67]` rejected.
        """
        acked = await guard(
            self._client.xack(self._stream.value, self._group, entry_id), "XACK", self._stream
        )
        return bool(acked)

    async def pending_count(self) -> int:
        """How many entries the group has handed out and not had acknowledged."""
        summary = await guard(
            self._client.xpending(self._stream.value, self._group), "XPENDING", self._stream
        )
        if isinstance(summary, dict):
            return int(summary.get("pending", 0))
        if isinstance(summary, list | tuple) and summary:
            return int(summary[0])
        return 0

    async def depth(self) -> int:
        """How many entries the stream holds, acknowledged or not."""
        return int(await guard(self._client.xlen(self._stream.value), "XLEN", self._stream))

    def _deliveries(self, reply: object, *, redelivered: bool) -> list[Delivery]:
        return self._to_deliveries(_entries(reply), redelivered=redelivered)

    @staticmethod
    def _to_deliveries(
        pairs: Sequence[tuple[str, dict[str, str]]], *, redelivered: bool
    ) -> list[Delivery]:
        return [
            Delivery(
                entry_id=entry_id,
                message=JobMessage.from_fields(fields, entry_id),
                redelivered=redelivered,
            )
            for entry_id, fields in pairs
        ]


def queue_from_settings(settings: Settings) -> JobQueue:
    """The queue bound to `REDIS_URL`, for a worker process that holds no other client.

    One client serves both this and `RedisStore`: `create_redis_client` returns the concrete
    `Redis`, and its satisfying `RedisStreamCommands` is checked by this call rather than
    asserted anywhere.
    """
    return JobQueue(create_redis_client(settings))
