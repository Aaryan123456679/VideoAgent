"""The concrete clients behind the resource protocols, and how they are built from settings.

Kept apart from `resources.py` so the lifespan's contract — open these, close all of them —
does not import a driver. It also means the two things that would otherwise be tangled stay
separable: the *policy* (order, partial-failure handling) is unit-testable with fakes, and the
*wiring* (URLs, credentials) is the part that needs a live dependency and is exercised under
`@pytest.mark.integration`.

**Constructing a client is not connecting.** `create_async_engine` and `Redis.from_url` both
return immediately and connect lazily, which is why a process with no database still starts and
answers `/healthz` — and why `/readyz` has to issue a real query rather than check that an
object exists.

**The object store has a slot but not yet a client.** `api.md` §7 gives presigned-URL minting to
`persistence`, so the S3-dialect client belongs there; and `boto3` ships neither type stubs nor
a `py.typed` marker, which `mypy --strict` with no `overrides` table cannot import at all.
Rather than reach for a suppression, the slot holds `UnconfiguredObjectStore`: it opens, it
closes, and any attempt to *use* it raises `VA-STORE-002` rather than quietly succeeding. That
is the same shape as `UnconfiguredApiKeyVerifier` in `principal.py`, and for the same reason —
a missing dependency should be impossible to mistake for a working one. Nothing in `T0.4`
touches artifacts, so nothing calls it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from video_agent.api.database import Database
from video_agent.api.errors import ApiError
from video_agent.api.resources import ResourceFactories
from video_agent.observability.codes import ErrorCode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.config.settings import Settings

POOL_PRE_PING: Final = True
"""Validate a pooled connection before handing it out. Without it the first request after a
database restart fails with a stale connection, which reads as an application bug."""


def build_database(settings: Settings) -> Database:
    """An engine bound to `DATABASE_URL`, wrapped so routes only ever see tenant scopes."""
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=POOL_PRE_PING)
    return Database(engine)


class Cache:
    """Redis, as `/readyz` and the idempotency store need it."""

    def __init__(self, client: Redis) -> None:
        """Wrap an already-constructed client so a test can supply its own."""
        self.client = client

    async def ping(self) -> None:
        """Raise if Redis cannot be reached."""
        await self.client.ping()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self.client.aclose()


def build_cache(settings: Settings) -> Cache:
    """A Redis client bound to `REDIS_URL`, decoding replies as text."""
    return Cache(Redis.from_url(settings.REDIS_URL, decode_responses=True))


class UnconfiguredObjectStore:
    """Holds the object-store slot open, and refuses to pretend it can store anything.

    `aclose` succeeds, because there is genuinely nothing to release and a shutdown path that
    raises would mask the reason for the shutdown. `presign` raises, because returning a
    plausible URL that resolves to nothing is the failure mode `[CPS §Failure behaviour]` calls
    dishonest: the caller would hand it to a customer.
    """

    async def presign(self, storage_key: str) -> str:
        """Always raises `VA-STORE-002`. See the class docstring."""
        raise ApiError(
            ErrorCode.VA_STORE_002,
            log_detail=f"no object store client is configured; cannot presign {storage_key}",
        )

    async def aclose(self) -> None:
        """Nothing to release."""


def build_object_store(_settings: Settings) -> UnconfiguredObjectStore:
    """The object-store slot. See the module docstring for why it is not an S3 client yet."""
    return UnconfiguredObjectStore()


def default_factories(settings: Settings) -> ResourceFactories:
    """The production wiring: what `create_app` uses when nothing is injected."""

    async def database() -> Database:
        return build_database(settings)

    async def cache() -> Cache:
        return build_cache(settings)

    async def object_store() -> UnconfiguredObjectStore:
        return build_object_store(settings)

    return ResourceFactories(database=database, cache=cache, object_store=object_store)
