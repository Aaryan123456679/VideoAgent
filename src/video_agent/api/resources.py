"""The three long-lived clients the process holds, and the guarantee that they get closed.

`S0.4.1` acceptance 5 is the whole reason this is a class and not three module globals:
*shutdown closes all three pools even when startup partially failed*. The failure it describes
is ordinary — Redis refuses a connection while the database pool is already up — and the
ordinary handling of it leaks the pool that did open, because the `finally` that would have
closed it is attached to a `yield` the lifespan never reached.

So opening is recorded as it happens. `close()` walks what was actually opened, in reverse, and
`open()` is not responsible for cleaning up after itself — the caller closes unconditionally.
One place decides, and it decides the same way whether startup finished or not.

`close()` never raises. A shutdown path that propagates an error hides the reason the process
was shutting down, and there is nothing useful to do about a pool that failed to close. Each
failure is logged; the walk continues.

The resources are held behind `Protocol`s rather than concrete types so an application can be
built with fakes, and so the object store — which belongs to `persistence` once that module
owns presigned URLs `[api.md` §7`]` — can be replaced without touching the lifespan.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from video_agent.observability.codes import ErrorCode
from video_agent.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractAsyncContextManager
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

_LOGGER: Final = get_logger(__name__)


class ClosableResource(Protocol):
    """Anything the lifespan opens. `aclose` is the redis-py spelling; the others follow it."""

    async def aclose(self) -> None:
        """Release the underlying connections."""
        ...  # pragma: no cover - protocol declaration


class ProbedResource(ClosableResource, Protocol):
    """A resource `/readyz` can ask about. `ping` raises when the dependency is unreachable."""

    async def ping(self) -> None:
        """Raise if the dependency cannot be reached."""
        ...  # pragma: no cover - protocol declaration


class DatabaseResource(ProbedResource, Protocol):
    """The database, as the API is allowed to see it: tenant-scoped sessions and a probe."""

    def tenant_scope(self, tenant_id: UUID) -> AbstractAsyncContextManager[AsyncSession]:
        """A transaction bound to `tenant_id` for row-level security."""
        ...  # pragma: no cover - protocol declaration


@dataclass(frozen=True, slots=True)
class ResourceFactories:
    """How to open each resource. Async because opening one may do I/O."""

    database: Callable[[], Awaitable[DatabaseResource]]
    cache: Callable[[], Awaitable[ProbedResource]]
    object_store: Callable[[], Awaitable[ClosableResource]]


class ResourceNotOpenError(RuntimeError):
    """Raised when a resource is read before `open()` or after `close()`.

    A distinct type rather than `AttributeError` on a `None`, because the two situations it
    covers — a route running outside the lifespan, and one running during shutdown — are both
    bugs worth a sentence rather than a stack frame.
    """


class Resources:
    """The opened clients, and the record of which ones actually opened."""

    def __init__(self, factories: ResourceFactories) -> None:
        """Hold `factories`. Nothing is opened until `open()` is awaited."""
        self._factories = factories
        self._open: list[tuple[str, ClosableResource]] = []
        self._database: DatabaseResource | None = None
        self._cache: ProbedResource | None = None
        self._object_store: ClosableResource | None = None
        self.closed_names: tuple[str, ...] = ()

    @property
    def open_names(self) -> tuple[str, ...]:
        """The resources currently open, in the order they were opened."""
        return tuple(name for name, _ in self._open)

    @property
    def database(self) -> DatabaseResource:
        """The open database, or a clear error saying it is not open."""
        if self._database is None:
            message = "the database resource is not open"
            raise ResourceNotOpenError(message)
        return self._database

    @property
    def cache(self) -> ProbedResource:
        """The open cache, or a clear error saying it is not open."""
        if self._cache is None:
            message = "the cache resource is not open"
            raise ResourceNotOpenError(message)
        return self._cache

    @property
    def object_store(self) -> ClosableResource:
        """The open object store, or a clear error saying it is not open."""
        if self._object_store is None:
            message = "the object store resource is not open"
            raise ResourceNotOpenError(message)
        return self._object_store

    async def open(self) -> None:
        """Open all three, in dependency order, recording each as it succeeds.

        An exception propagates: startup should fail loudly. What has already opened stays
        recorded, which is what lets the caller's unconditional `close()` clean it up.
        """
        self._database = await self._factories.database()
        self._open.append(("database", self._database))
        self._cache = await self._factories.cache()
        self._open.append(("cache", self._cache))
        self._object_store = await self._factories.object_store()
        self._open.append(("object_store", self._object_store))

    async def close(self) -> None:
        """Close everything opened, newest first. Idempotent, and never raises."""
        closed: list[str] = list(self.closed_names)
        while self._open:
            name, resource = self._open.pop()
            try:
                await resource.aclose()
            except Exception as exc:
                _LOGGER.error(
                    "failed to close resource %s",
                    name,
                    exc_info=exc,
                    extra={
                        "event": "resource_close_failed",
                        "code": ErrorCode.VA_INT_001.value,
                        "reason": str(exc),
                    },
                )
            closed.append(name)
        self._database = None
        self._cache = None
        self._object_store = None
        self.closed_names = tuple(closed)


@asynccontextmanager
async def open_resources(resources: Resources) -> AsyncIterator[None]:
    """Open for the duration of the block, and close whatever opened, always.

    The `except BaseException` before the `yield` is the partial-startup case `S0.4.1`
    acceptance 5 names: an exception on the way up never reaches the `finally`, so the cleanup
    has to be written twice or not at all. Twice.
    """
    try:
        await resources.open()
    except BaseException:
        await resources.close()
        raise
    try:
        yield
    finally:
        await resources.close()
