"""The async engine and the tenant-scoped transaction every repository call runs inside.

`persistence.md` §3 rule 3: *`SET LOCAL app.tenant_id` runs at the start of every transaction,
sourced from `Principal.tenant_id` only.* This module is the only place that statement is
issued, which is what makes "only" enforceable — there is one function to review rather than
one per call site.

Three properties are load-bearing.

**The tenant id is never interpolated into SQL.** `set_config` is called with a bound
parameter, so a tenant id is data all the way to the server. `SET LOCAL app.tenant_id = '...'`
cannot take a parameter and would have to be formatted into the statement, and a formatted
tenant id in the statement that *establishes the isolation boundary* is the one injection site
worth being paranoid about.

**`SET LOCAL`, not `SET`.** The third argument to `set_config` is `is_local`. Without it the
setting outlives the transaction and survives back into the connection pool, so the next
borrower of that connection starts inside the previous caller's tenant. That is cross-tenant
leakage with no bug in any query.

**Using a session after its transaction ended raises.** `[persistence.md §9]` says an absent
`app.tenant_id` yields zero rows plus an alarm rather than an error, and that is the right
behaviour *for the policy* — a hard error there would turn a leak-safe default into an
outage. It is the wrong behaviour for the *application*, where zero rows is indistinguishable
from an empty table and the bug is invisible. So the policy fails safe and quiet, this layer
fails loud, and the alarm counter records that it happened.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Executable, Result, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from video_agent.config.settings import Settings
from video_agent.observability.alarms import AlarmCounter
from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError
from video_agent.persistence.rls import TENANT_SETTING

UNSET_TENANT_CONTEXT_ALARM = AlarmCounter("persistence.unset_tenant_context")
"""Counts every attempt to reach the database without a tenant context.

`persistence.md` §9 requires the alarm as well as the safe default. A policy that quietly
returns zero rows is correct and completely invisible; the counter is what makes the condition
observable instead of merely survivable.
"""

SET_TENANT_STATEMENT = text(f"SELECT set_config('{TENANT_SETTING}', :tenant_id, true)")
"""Transaction-local, parameterised. The setting name is a constant; only the value is data."""


class TenantContextMissingError(VideoAgentError):
    """A database call was attempted outside a tenant-scoped transaction.

    `VA-STORE-003` rather than a new code: from the caller's point of view the store was not
    usable for this request. The distinguishing detail is in the message, and the alarm
    counter is what separates it from a genuine connectivity failure in aggregate.
    """

    code = ErrorCode.VA_STORE_003


class DatabaseConnection(Protocol):
    """The slice of `AsyncConnection` a repository is allowed to use.

    Narrow on purpose. A repository that could reach `begin()`, `commit()` or the raw
    driver connection could open its own transaction, and `[persistence.md §8]` requires the
    checkpoint write and the node's domain writes to share one `[D-23]`. Handing repositories
    an interface with no transaction control makes that structural rather than remembered.
    """

    async def execute(
        self,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Result[Any]:
        """Run one statement on the caller's open transaction."""
        ...


@dataclass(eq=False)
class TenantSession:
    """An open transaction with `app.tenant_id` already set, and the tenant it is set to.

    Repositories take one of these and never an engine, so "no module outside `persistence`
    opens a session" is a rule about one import rather than a rule about discipline.
    """

    connection: DatabaseConnection
    tenant_id: UUID
    _open: bool = field(default=True, init=False, repr=False)

    async def execute(
        self,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Result[Any]:
        """Run one statement, refusing if the tenant-scoped transaction has already ended."""
        self.require_open()
        return await self.connection.execute(statement, parameters)

    def require_open(self) -> None:
        """Raise and alarm if the transaction this session scoped has already closed."""
        if self._open:
            return
        UNSET_TENANT_CONTEXT_ALARM.increment()
        message = (
            "the tenant-scoped transaction has ended; app.tenant_id is no longer set, so "
            "this query would be filtered to zero rows rather than fail"
        )
        raise TenantContextMissingError(message)

    def close(self) -> None:
        """Mark the session unusable. Called by `tenant_session` on the way out."""
        self._open = False


def create_database_engine(settings: Settings, *, echo: bool = False) -> AsyncEngine:
    """Build the async engine from configuration.

    `echo` stays off by default and is a developer's local switch, never a deployment one:
    SQLAlchemy's echo writes bound parameters, which for this schema means the user's prompt
    and every tenant id, straight to stdout. `[CPS §Observability]`

    `pool_pre_ping` because the failure it prevents is the one that matters here — a
    connection the pool believes is alive after a database restart produces a spurious
    `VA-STORE-003` on the first query of an otherwise healthy job.
    """
    return create_async_engine(settings.DATABASE_URL, echo=echo, pool_pre_ping=True)


@asynccontextmanager
async def tenant_session(engine: AsyncEngine, tenant_id: UUID) -> AsyncIterator[TenantSession]:
    """Open one transaction, scope it to `tenant_id`, and close it on the way out.

    `tenant_id` is typed as `UUID`, not `str`. `[D-68]` sources it from `Principal.tenant_id`
    and from nothing else; requiring a parsed `UUID` at this boundary means a value that
    arrived as a string from a request body cannot be passed here without someone writing the
    parse, which is the point at which the mistake becomes visible in a diff.
    """
    async with engine.begin() as connection:
        await connection.execute(SET_TENANT_STATEMENT, {"tenant_id": str(tenant_id)})
        session = TenantSession(connection=connection, tenant_id=tenant_id)
        try:
            yield session
        finally:
            session.close()
