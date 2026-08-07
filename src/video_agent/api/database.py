"""The request-scoped database session, and the RLS binding it opens every transaction with.

`persistence.md` §3 rule 3 and `[CPS §Canonical stack]`: the tenant is pushed into the session
as a Postgres setting before any query runs, so cross-tenant access fails at the database
rather than at a `WHERE` clause somebody has to remember to write. Three things make that hold
mechanically rather than by discipline:

**The setting is issued first, inside the transaction.** `SET LOCAL` is scoped to the
transaction, so a session handed back to the pool cannot carry one tenant's id into the next
request. Issuing it after the first query would leave that query unscoped — and it would be the
query nobody thought to check.

**The tenant id comes from the `Principal`.** `tenant_scope` takes a `UUID` and the only caller
that supplies one is `tenant_session`, which reads it from the resolved principal. There is no
overload that accepts a string from a header or a body `[api.md` §6`]`.

**The engine is not reachable from here.** It is a private attribute of `Database` and is
absent from `__all__` and from the module namespace, so a route cannot `from ... import engine`
and open a connection that skips the setting. That is the point of `S0.4.2` acceptance 4: the
rule is not "do not do this", it is "there is nothing to do it with".

The statement is spelled `set_config(..., true)` rather than the literal `SET LOCAL app.tenant_id
= ...` because `SET` takes no bind parameters — the value would have to be interpolated into
SQL text. `set_config(name, value, is_local => true)` is exactly `SET LOCAL` and accepts a bound
parameter, so the tenant id travels as a parameter and never as SQL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from video_agent.api.principal import Principal, require_tenant

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Callable
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from video_agent.api.resources import DatabaseResource

__all__ = [
    "PING_SQL",
    "SET_LOCAL_TENANT_SQL",
    "TENANT_SETTING",
    "Database",
    "get_database",
    "tenant_session",
]
"""Deliberately without an engine. See the module docstring: the absence is the control."""

TENANT_SETTING: Final = "app.tenant_id"
"""The Postgres setting every RLS policy reads. One spelling, referenced by both sides."""

SET_LOCAL_TENANT_SQL: Final = f"SELECT set_config('{TENANT_SETTING}', :tenant_id, true)"
"""`SET LOCAL app.tenant_id`, in the spelling that accepts a bound parameter."""

PING_SQL: Final = "SELECT 1"


class Database:
    """Owns the engine, hands out tenant-scoped sessions, and answers the readiness probe."""

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        """Wrap `engine`. `session_factory` is an injection point for tests, not for routes."""
        self._engine = engine
        self._session_factory: Callable[[], AsyncSession] = session_factory or async_sessionmaker(
            engine, expire_on_commit=False
        )

    @asynccontextmanager
    async def tenant_scope(self, tenant_id: UUID) -> AsyncIterator[AsyncSession]:
        """A transaction whose very first statement binds `tenant_id` for row-level security."""
        session = self._session_factory()
        async with session, session.begin():
            await session.execute(text(SET_LOCAL_TENANT_SQL), {"tenant_id": str(tenant_id)})
            yield session

    async def ping(self) -> None:
        """Raise if Postgres cannot be reached. Used only by `/readyz`."""
        async with self._engine.connect() as connection:
            await connection.execute(text(PING_SQL))

    async def aclose(self) -> None:
        """Dispose of the pool. Safe to call after a failed startup."""
        await self._engine.dispose()


def get_database(request: Request) -> DatabaseResource:
    """The open database resource for this application."""
    resources = request.app.state.resources
    database: DatabaseResource = resources.database
    return database


async def tenant_session(
    request: Request,
    principal: Annotated[Principal, Depends(require_tenant)],
) -> AsyncIterator[AsyncSession]:
    """The only way a route obtains a session, and it is always scoped to the caller's tenant."""
    async with get_database(request).tenant_scope(principal.tenant_id) as session:
        yield session
