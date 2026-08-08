"""The concrete clients behind the resource protocols, and how they are built from settings.

Kept apart from `resources.py` so the lifespan's contract — open these, close all of them — does
not import a driver. It also means the two things that would otherwise be tangled stay
separable: the *policy* (order, partial-failure handling) is unit-testable with fakes, and the
*wiring* (URLs, credentials) is the part that needs a live dependency and is exercised under
`@pytest.mark.integration`.

**Constructing a client is not connecting.** The engine, `Redis.from_url` and `boto3.client` all
return immediately and connect lazily, which is why a process with no dependencies still starts
and answers `/healthz` — and why `/readyz` has to issue a real query rather than check that an
object exists.

**Nothing here builds an engine or a session.** `T0.4` did, and `T0.5` shipped a boundary gate
with a temporary exemption naming this file for it. `build_database` now calls
`video_agent.persistence.create_database_engine`, so pool configuration, `echo` policy and the
tenant binding have one home; the exemption is gone.

**The object store is a real client now.** `T0.4` held the slot with `UnconfiguredObjectStore`
because `boto3` ships no `py.typed` and `mypy --strict` with no `overrides` table cannot import
it. The fix was the type stubs, not a suppression: `boto3-stubs[s3]` is in the dev group and
`persistence.objects` owns the dialect. The placeholder is gone with the reason for it.

**No URL and no credential is constructed, logged or interpolated here.** Each value goes from
`Settings` straight into the client that needs it. `DATABASE_URL` and `REDIS_URL` carry
passwords in their userinfo; see the note on `build_cache`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

from video_agent.api.database import Database
from video_agent.api.idempotency import RedisIdempotencyStore
from video_agent.api.resources import ResourceFactories
from video_agent.config.aliases import get_alias_table
from video_agent.gateway.breaker import (
    CircuitBreaker,
    RedisCircuitStateStore,
    ResilientCircuitStateStore,
)
from video_agent.gateway.capabilities import ProxyCapabilityRegistry
from video_agent.gateway.clock import SystemClock
from video_agent.gateway.gateway import GatewayDeps, LiteLLMGateway
from video_agent.gateway.prompts import CachingPromptRegistry, FilePromptRegistry
from video_agent.gateway.transport import HttpxLiteLLMTransport
from video_agent.persistence.objects import ArtifactStore, S3ObjectTransport, create_s3_client
from video_agent.persistence.queue import JobQueue, RedisStreamCommands
from video_agent.persistence.redis_client import RedisCommands, RedisStore, create_redis_client
from video_agent.persistence.session import create_database_engine

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.api.idempotency import IdempotencyStore
    from video_agent.config.settings import Settings

_DEFAULT_PROMPTS_ROOT: Path = Path("prompts")
"""Relative to the process's working directory, same convention as `config/aliases.yaml`'s
own discovery. No caller has needed anything else built from `Settings` yet — `gateway.md`'s
own construction of `FilePromptRegistry`/`GatewayDeps` was, until now, exercised only by tests
and by ad hoc scripts, never by a committed entrypoint."""


def build_database(settings: Settings) -> Database:
    """An engine from `persistence`, wrapped so routes only ever see tenant scopes."""
    return Database(create_database_engine(settings))


class RedisClient(RedisCommands, RedisStreamCommands, Protocol):
    """Both halves of what this process asks of Redis, in one name.

    `redis.asyncio.Redis` satisfies each of the two protocols independently, but a parameter
    has to be annotated with something, and naming the concrete driver here would make the
    lifespan untestable without a server — which is exactly how `[D-17]`'s refusal path went
    unexercised until `T0.6`. Python has no intersection type, so the intersection is written
    as a protocol that inherits both.
    """


class Cache:
    """Redis, as `/readyz`, the idempotency store and the job queue all need it.

    One client, three consumers. `redis.asyncio.Redis` satisfies both
    `persistence.redis_client.RedisCommands` and `persistence.queue.RedisStreamCommands`, so
    the queue and the key/value store share a connection pool rather than opening two.
    """

    def __init__(self, client: RedisClient) -> None:
        """Wrap an already-constructed client so a test can supply its own."""
        self.client = client
        self.store = RedisStore(client)
        self.queue = JobQueue(client)

    def idempotency_store(self) -> IdempotencyStore:
        """The production idempotency store `[D-16]`, `[D-17]`.

        Built here rather than per request: the store is stateless over the client, and a route
        that constructed its own would be a route that could construct a different one. `T0.4`
        implemented the mechanism and left this unwired, which meant a dead Redis had no path to
        the `503` that `[D-17]` requires — nothing in the process held a `RedisIdempotencyStore`
        at all.
        """
        return RedisIdempotencyStore(self.client)

    async def ping(self) -> None:
        """Raise if Redis cannot be reached."""
        await self.store.ping()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self.store.aclose()


def build_cache(settings: Settings) -> Cache:
    """A Redis client bound to `REDIS_URL`.

    `REDIS_URL` is a plain `str` in `Settings` and carries a password in its userinfo. It is
    read once, here, and handed straight to the driver — it is never formatted into a message,
    an f-string or a log line anywhere in this package. `observability.redaction` catches the
    shape on the logging path (`is_credentialed_url`), but that net does not cover an exception
    message or a debugger frame, so the discipline is to not construct the string at all. Making
    the field a `SecretStr` would close the remaining gap and belongs in `config/settings.py`;
    see this task's report.
    """
    return Cache(create_redis_client(settings))


class ObjectStore:
    """The artifact store, as the lifespan sees it: something that opens and closes.

    A thin holder rather than an alias for `ArtifactStore` because the lifespan's contract is
    `aclose()` and `ArtifactStore`'s contract is upload/download/verify. Keeping them apart
    means the store can be handed to a worker without also handing over the right to close the
    process-wide client.
    """

    def __init__(self, transport: S3ObjectTransport) -> None:
        """Hold the transport and the policy layer built over it."""
        self.transport = transport
        self.artifacts = ArtifactStore(transport)

    async def aclose(self) -> None:
        """Release the HTTP session the S3 client holds."""
        await self.transport.aclose()


def build_object_store(settings: Settings) -> ObjectStore:
    """An S3-compatible client bound to `ARTIFACT_BUCKET`.

    The credentials stay inside their `SecretStr` until `create_s3_client` hands them to
    `boto3`; nothing in this module ever holds the plaintext.
    """
    return ObjectStore(S3ObjectTransport(create_s3_client(settings), settings.ARTIFACT_BUCKET))


class GatewayResources:
    """The gateway, as a worker process sees it: something that opens and closes.

    Same shape as `Cache`/`ObjectStore` above — a thin holder over the one thing that owns a
    connection (`http_client`), so a caller can shut it down without reaching into the
    `LiteLLMGateway` internals.
    """

    def __init__(self, http_client: httpx.AsyncClient, gateway: LiteLLMGateway) -> None:
        self.http_client = http_client
        self.gateway = gateway

    async def aclose(self) -> None:
        await self.http_client.aclose()


def build_gateway(
    settings: Settings,
    *,
    redis_client: RedisClient,
    prompts_root: Path = _DEFAULT_PROMPTS_ROOT,
) -> GatewayResources:
    """The real `Gateway` a worker calls `plan_story`/`lock_bible` against.

    `redis_client` is accepted rather than built here because a worker already holds one
    (`Cache.client`, shared with the job queue and lock) and circuit state has no reason to
    open a second pool. `settings.require_litellm_master_key` is passed as the transport's
    `key_provider` — a callable, not a captured string — so the key is read fresh per call and
    never sits in a frame or a `repr`, same discipline `HttpxLiteLLMTransport` documents.
    """
    http_client = httpx.AsyncClient(base_url=settings.LITELLM_BASE_URL)
    transport = HttpxLiteLLMTransport(http_client, settings.require_litellm_master_key)
    circuit_store = ResilientCircuitStateStore(primary=RedisCircuitStateStore(redis_client))
    breaker = CircuitBreaker(store=circuit_store, clock=SystemClock())
    prompts = CachingPromptRegistry(FilePromptRegistry(prompts_root))
    deps = GatewayDeps(
        table=get_alias_table(),
        transport=transport,
        capabilities=ProxyCapabilityRegistry(transport),
        prompts=prompts,
        breaker=breaker,
    )
    return GatewayResources(http_client=http_client, gateway=LiteLLMGateway(deps))


def default_factories(settings: Settings) -> ResourceFactories:
    """The production wiring: what `create_app` uses when nothing is injected."""

    async def database() -> Database:
        return build_database(settings)

    async def cache() -> Cache:
        return build_cache(settings)

    async def object_store() -> ObjectStore:
        return build_object_store(settings)

    return ResourceFactories(database=database, cache=cache, object_store=object_store)
