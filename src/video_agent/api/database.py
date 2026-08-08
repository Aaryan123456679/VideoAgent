"""The request-scoped database session — which this module obtains rather than implements.

`persistence.md` §3 rule 3 requires `SET LOCAL app.tenant_id` at the start of *every*
transaction, and `S0.5.8` makes that enforceable by putting the statement in exactly one place:
`video_agent.persistence.session`. This module is the HTTP half of that arrangement and nothing
more — it resolves the tenant from the `Principal` and hands the persistence layer an engine.

**It used to be the other half of a duplicate.** `T0.4` and `T0.5` were written concurrently
and both grew a tenant-scoped transaction: two `set_config` calls, two copies of the
`app.tenant_id` constant, two engines. Two implementations of an isolation boundary is one more
than can be reviewed, and the failure mode is not that one of them is wrong today but that a fix
applied to one of them looks complete. `T0.5` shipped the boundary gate with a temporary
exemption naming this file; consolidating here is what expires it.

**The tenant id comes from the `Principal`.** `tenant_session` takes a `UUID` and the only
source is the resolved principal. There is no overload that accepts a string from a header or a
body `[api.md §6]`, `[D-68]`.

**The engine is not reachable from here.** It is a private attribute of `Database`, absent from
`__all__` and from the module namespace, so a route cannot `from ... import engine` and open a
connection that skips the binding. That is `S0.4.2` acceptance 4: the rule is not "do not do
this", it is "there is nothing to do it with".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

from fastapi import Depends, Request
from sqlalchemy import text

from video_agent.api.principal import Principal, require_tenant
from video_agent.persistence.rls import TENANT_SETTING
from video_agent.persistence.session import TenantSession
from video_agent.persistence.session import tenant_session as open_tenant_session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from video_agent.api.resources import DatabaseResource

__all__ = [
    "PING_SQL",
    "TENANT_SETTING",
    "Database",
    "TenantSession",
    "get_database",
    "tenant_session",
]
"""Deliberately without an engine. See the module docstring: the absence is the control.

`TENANT_SETTING` is re-exported, not redefined. It is `persistence.rls`'s constant, and the
policies that read it and the statement that sets it now agree by construction rather than by
two files spelling the same string.
"""

PING_SQL: Final = "SELECT 1"
"""`/readyz`'s query. Trivial on purpose, and deliberately not the tenant binding: a readiness
probe that could set `app.tenant_id` would be a second way into a scoped connection."""


class Database:
    """Owns the engine, hands out tenant-scoped sessions, and answers the readiness probe.

    `tenant_scope` is one line because it must be: every property the scope has —
    `SET LOCAL` first, parameterised, transaction-local, closed on the way out — belongs to
    `persistence.session.tenant_session`, and re-deriving any of them here would recreate the
    duplication this module was consolidated to remove.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Wrap `engine`. Nothing else in the process holds a reference to it."""
        self._engine = engine

    def tenant_scope(self, tenant_id: UUID) -> AbstractAsyncContextManager[TenantSession]:
        """A transaction whose very first statement binds `tenant_id` for row-level security."""
        return open_tenant_session(self._engine, tenant_id)

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
) -> AsyncIterator[TenantSession]:
    """The only way a route obtains a session, and it is always scoped to the caller's tenant."""
    async with get_database(request).tenant_scope(principal.tenant_id) as session:
        yield session
