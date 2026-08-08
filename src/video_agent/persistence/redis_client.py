"""The one async Redis client, and the write path that will not let a key escape without a TTL.

`S0.6.1`. Two properties are load-bearing and neither is about Redis being fast.

**A connection failure is not a miss.** `persistence.md` §9 says Redis unavailable *degrades*
cache and progress but **rejects** idempotency `[D-17]`, and a caller can only make that
distinction if the two outcomes have different shapes. So a miss returns `None` and an
unreachable server raises `RedisUnavailableError` carrying `VA-STORE-003`. Collapsing them —
`except Exception: return None` — is the single change that turns "idempotency is
non-negotiable" into "idempotency is best-effort", silently, with every test still green,
because a store that is down and a key that was never written look identical from the outside.

**Every write carries the registry's TTL.** `RedisStore` takes a `RedisKey`, never a `str`, and
a key whose registry entry does not declare it TTL-less is refused if it arrives without one.
Redis is a cache, a lock table and a progress buffer; it is never authoritative
`[persistence.md §5]`. A key with no expiry in a store that is not the system of record is
memory that is never reclaimed and state that outlives the thing it described.

**The URL is not in any message this module produces.** `REDIS_URL` carries a password in its
userinfo. `observability.redaction` catches that shape on the logging path, but an exception
message travels further than a log line — into an HTTP error body, a traceback, a debugger
frame. Failures here are described by operation and key *name*, never by connection target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol

from redis.asyncio import Redis

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError
from video_agent.persistence.keys import KeyName, RedisKey, TtlPolicy, spec_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable

    from video_agent.config.settings import Settings

DECODE_RESPONSES: Final = True
"""Replies come back as `str`. Every value this codebase stores in Redis is JSON or an
identifier; decoding once at the client is one place rather than one per call site."""


class RedisUnavailableError(VideoAgentError):
    """Redis could not be reached. `VA-STORE-003`, retryable `[persistence.md §9]`.

    A distinct type rather than a bare `RedisError` so the caller's `except` clause states
    which policy it is applying: `api.idempotency` lets this propagate to a `503` `[D-17]`,
    while a cache read may catch it and continue without one.
    """

    code = ErrorCode.VA_STORE_003


class MissingKeyTtlError(VideoAgentError):
    """A write was attempted for a key whose registry entry requires a TTL it did not carry.

    A programming error, and `VA-INT-001` by inheritance rather than a store code: nothing is
    wrong with the store. Raised before the command is issued, so the key is never created.
    """


# --- The commands this module uses -----------------------------------------------------------


class RedisCommands(Protocol):
    """The slice of `redis.asyncio.Redis` the key/value path needs.

    Narrow on purpose, and for the same reason `persistence.session.DatabaseConnection` is
    narrow: a wrapper that could reach the whole client could reach `flushdb`, `keys` and
    `set` without `ex`. Declared with `Awaitable` returns rather than `async def` because
    redis-py's commands are ordinary functions returning awaitables.

    The hash commands are absent even though `persistence.md` §5 types three of the registered
    keys as hashes. Nothing in the tree writes one yet — `RedisIdempotencyStore` stores its
    record as a JSON string under `SET NX EX`, which is what makes the claim atomic — and a
    helper with no caller is a helper no test exercises against a real server. The registry
    still declares the types, so the divergence is visible rather than assumed away.
    """

    def get(self, name: str) -> Awaitable[Any]:
        """Read one string value."""
        ...  # pragma: no cover - protocol declaration

    def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> Awaitable[Any]:
        """Write one string value, optionally only if absent, with an expiry."""
        ...  # pragma: no cover - protocol declaration

    def delete(self, *names: str) -> Awaitable[Any]:
        """Remove keys, returning how many existed."""
        ...  # pragma: no cover - protocol declaration

    def expire(self, name: str, time: int) -> Awaitable[Any]:
        """Set a key's expiry in seconds."""
        ...  # pragma: no cover - protocol declaration

    def ttl(self, name: str) -> Awaitable[Any]:
        """Seconds remaining, `-1` for no expiry, `-2` for no key."""
        ...  # pragma: no cover - protocol declaration

    def ping(self) -> Awaitable[Any]:
        """Round-trip the server."""
        ...  # pragma: no cover - protocol declaration

    def aclose(self) -> Awaitable[None]:
        """Release the connection pool."""
        ...  # pragma: no cover - protocol declaration


NO_EXPIRY_TTL: Final = -1
"""What Redis returns from `TTL` for a key that exists and will never expire."""

NO_SUCH_KEY_TTL: Final = -2
"""What Redis returns from `TTL` for a key that does not exist."""


async def guard[T](awaitable: Awaitable[T], operation: str, key: RedisKey | None = None) -> T:
    """Run one Redis command, converting an unreachable server into `VA-STORE-003`.

    The message names the operation and the key's *registry name*. It does not name the key's
    value — which interpolates a tenant id and sometimes an idempotency key — and it does not
    name the connection target, because `REDIS_URL` carries a password and an exception message
    is not an emission path anybody redacts.

    `VideoAgentError` passes through untouched: a `MissingKeyTtlError` raised inside is a bug in
    this process, and relabelling it as a store outage would send the reader to the wrong system.
    """
    try:
        return await awaitable
    except VideoAgentError:
        raise
    except Exception as exc:
        subject = f" for {key.name}" if key is not None else ""
        message = f"redis {operation}{subject} failed: {type(exc).__name__}"
        raise RedisUnavailableError(message) from exc


def require_ttl(key: RedisKey) -> int | None:
    """The TTL to write `key` with, or a refusal.

    `TtlPolicy.NONE` returns `None` and is the only way a key reaches Redis without an expiry.
    Everything else must carry one — `KEY_REGISTRY` supplies it for `FIXED` keys and the
    constructor demands it for `CALLER` keys, so arriving here without one means the `RedisKey`
    was built by hand rather than by a constructor.
    """
    spec = spec_for(key.name)
    if spec.ttl_policy is TtlPolicy.NONE:
        return None
    if key.ttl_seconds is None or key.ttl_seconds <= 0:
        message = (
            f"{key.name} must be written with a positive TTL "
            f"({spec.ttl_policy} policy); got {key.ttl_seconds!r}"
        )
        raise MissingKeyTtlError(message)
    return key.ttl_seconds


class RedisStore:
    """Cache, locks, rate limits and progress, over a client that is never authoritative.

    Every method takes a `RedisKey`. There is no overload that accepts a string, which is what
    makes `keys.KEY_REGISTRY` the schema rather than a suggestion.
    """

    def __init__(self, client: RedisCommands) -> None:
        """Wrap an already-constructed client so a test can supply its own."""
        self._client = client

    async def get(self, key: RedisKey) -> str | None:
        """The value, or `None` for a miss. An unreachable server raises instead."""
        raw = await guard(self._client.get(key.value), "GET", key)
        return None if raw is None else str(raw)

    async def set(self, key: RedisKey, value: str) -> None:
        """Write `value` with the registry's TTL, refusing a key that has none."""
        ttl = require_ttl(key)
        await guard(self._client.set(key.value, value, ex=ttl), "SET", key)

    async def set_if_absent(self, key: RedisKey, value: str) -> bool:
        """`SET NX EX` — the atomic claim. `True` when this caller created the key.

        One command, never a read followed by a write: two concurrent identical requests would
        both see "absent" and both proceed, which is precisely what the lock and the
        idempotency claim exist to prevent.
        """
        ttl = require_ttl(key)
        created = await guard(self._client.set(key.value, value, ex=ttl, nx=True), "SET NX", key)
        return bool(created)

    async def delete(self, key: RedisKey) -> bool:
        """Remove the key. `True` when it existed."""
        removed = await guard(self._client.delete(key.value), "DEL", key)
        return bool(removed)

    async def ttl_of(self, key: RedisKey) -> int:
        """Seconds remaining. `-1` means no expiry, `-2` means no such key."""
        return int(await guard(self._client.ttl(key.value), "TTL", key))

    async def ping(self) -> None:
        """Raise `VA-STORE-003` if Redis cannot be reached. Used by `/readyz`."""
        await guard(self._client.ping(), "PING")

    async def aclose(self) -> None:
        """Close the connection pool. Called by the lifespan on the way down."""
        await self._client.aclose()


def create_redis_client(settings: Settings) -> Redis:
    """Build the async client from configuration.

    Constructing a client is not connecting: `Redis.from_url` returns immediately and connects
    lazily, which is why a process with no Redis still starts and answers `/healthz` — and why
    `/readyz` has to issue a real `PING` rather than check that an object exists.

    The concrete return type is what lets one client serve both `RedisStore` and
    `persistence.queue.JobQueue`: each declares the narrow protocol it needs, and `Redis`
    satisfying both is checked here rather than asserted in a comment.

    `REDIS_URL` is a `SecretStr` and is unwrapped here and nowhere else in this module. The
    plaintext lives for the width of one argument and goes straight to the driver; the module
    docstring's claim that no message names the connection target holds because the string is
    never bound to a name that a message could reach.
    """
    client: Redis = Redis.from_url(
        settings.REDIS_URL.get_secret_value(), decode_responses=DECODE_RESPONSES
    )
    return client


def create_redis_store(settings: Settings) -> RedisStore:
    """The store the application holds, bound to `REDIS_URL`."""
    return RedisStore(create_redis_client(settings))


__all__ = [
    "DECODE_RESPONSES",
    "NO_EXPIRY_TTL",
    "NO_SUCH_KEY_TTL",
    "KeyName",
    "MissingKeyTtlError",
    "RedisCommands",
    "RedisStore",
    "RedisUnavailableError",
    "create_redis_client",
    "create_redis_store",
    "guard",
    "require_ttl",
]
