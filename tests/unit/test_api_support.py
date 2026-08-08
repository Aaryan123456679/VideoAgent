"""Fakes and builders shared by the API tests, plus the checks that they are honest fakes.

Every substitute here is narrower than the thing it replaces, which is the point — a test that
needs a live Postgres to assert that `SET LOCAL` is emitted first is a test that never runs.
The risk of a fake is that it drifts into asserting itself, so two properties are enforced:

- **The fakes satisfy the real protocols.** `test_fakes_satisfy_the_resource_protocols` binds
  each one to `DatabaseResource` / `ProbedResource`, so `mypy --strict` fails if the shape it
  stands in for changes underneath it.
- **The production code path is never faked out.** `RecordingDatabase` delegates `tenant_scope`
  to a genuine `Database`, which since `T0.6` delegates in turn to
  `video_agent.persistence.session.tenant_session`. The substitution is the *engine*, one level
  lower than it used to be: the fake hands back a connection that records statements, and the
  transaction, the `set_config` and the session lifetime are all the real ones. Deleting the
  `set_config` statement from `persistence.session` therefore fails the tests that assert it,
  which is what makes them tests rather than decoration.

Named `test_api_support` so the file falls inside this task's ownership; pytest collects it and
the handful of checks in it are real.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any, Final, Never, cast
from uuid import UUID

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from video_agent.api.app import create_app
from video_agent.api.database import Database, tenant_session
from video_agent.api.errors import ApiError
from video_agent.api.idempotency import IdempotencyRecord, IdempotencyState
from video_agent.api.principal import (
    KEY_PREFIX_LENGTH,
    PresentedKey,
    Principal,
    assert_tenant_owns,
    parse_bearer,
    require_tenant,
)
from video_agent.api.resources import (
    ClosableResource,
    DatabaseResource,
    ProbedResource,
    ResourceFactories,
    Resources,
)
from video_agent.config.settings import get_settings
from video_agent.observability.codes import ErrorCode
from video_agent.persistence.session import TenantSession

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Mapping
    from types import TracebackType

    from sqlalchemy.ext.asyncio import AsyncEngine

    from video_agent.api.principal import ApiKeyVerifier
    from video_agent.providers.models import ProviderRegistry

TENANT_A: Final = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B: Final = UUID("22222222-2222-2222-2222-222222222222")
KEY_ID_A: Final = UUID("aaaaaaaa-1111-1111-1111-111111111111")
JOB_ID: Final = UUID("33333333-3333-3333-3333-333333333333")

VALID_KEY: Final = "vak-prefix01-plaintext-secret-value"
"""A credential in the shape `parse_bearer` accepts. Long enough to survive the length floor,
lowercase-and-dashes so it does not itself trip the redaction tripwire in a log assertion."""

BEGIN: Final = "BEGIN"
"""The marker `RecordingSession` writes when a transaction opens, so 'first statement *of the
transaction*' is an assertion about ordering rather than about the list being non-empty."""

OK: Final = 200
"""The one status the `errors` module has no constant for, because nothing in it succeeds."""

PLANTED_DETAIL: Final = "planted-detail-that-must-not-escape"
"""Deliberately low-entropy and hyphenated: it must be findable in a log line, and a value that
tripped the redaction tripwire would fail the test for a different reason than the one under
examination."""


class RecordingConnection:
    """Enough of `AsyncConnection` to see what a transaction emitted, and in what order.

    This is the `DatabaseConnection` protocol `persistence.session` hands to repositories, so
    the fake is exactly as wide as the real interface and no wider.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.parameters: list[Mapping[str, Any]] = []

    async def execute(
        self,
        statement: object,
        parameters: Mapping[str, Any] | None = None,
    ) -> None:
        """Record the SQL text and its bound parameters. Executes nothing."""
        self.events.append(str(statement))
        self.parameters.append(dict(parameters or {}))

    @property
    def tenant_parameter(self) -> str | None:
        """The `tenant_id` bound by the first statement that bound one."""
        for bound in self.parameters:
            if "tenant_id" in bound:
                return str(bound["tenant_id"])
        return None


class _Begin:
    """What `RecordingEngine.begin()` returns: a transaction that records that it opened."""

    def __init__(self, engine: RecordingEngine) -> None:
        self._engine = engine
        self._connection = RecordingConnection()

    async def __aenter__(self) -> RecordingConnection:
        self._connection.events.append(BEGIN)
        self._engine.connections.append(self._connection)
        return self._connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._connection.events.append("COMMIT" if exc is None else "ROLLBACK")


class RecordingEngine:
    """An `AsyncEngine` whose `begin()` yields a connection that records statements.

    The substitution sits here, below `persistence.session.tenant_session`, so the transaction
    boundary, the `set_config` call and the session's close-on-exit are all production code.
    Anything the engine is asked for other than `begin` raises, so a change that reached past
    the transaction would say so rather than being recorded silently.
    """

    def __init__(self) -> None:
        self.connections: list[RecordingConnection] = []

    def begin(self) -> _Begin:
        """Open a recorded transaction."""
        return _Begin(self)

    def __getattr__(self, name: str) -> Never:
        """Refuse every other attribute. `Never` because there is no value this can return."""
        message = (
            f"the engine may only be used to begin a transaction; something asked for {name!r}"
        )
        raise AssertionError(message)


class RecordingDatabase:
    """A `DatabaseResource` whose probe a test controls and whose scopes are the real ones."""

    def __init__(self, *, ping_error: Exception | None = None) -> None:
        self.closed = False
        self.pings = 0
        self._ping_error = ping_error
        self.engine = RecordingEngine()
        self._database = Database(cast("AsyncEngine", self.engine))

    @property
    def sessions(self) -> list[RecordingConnection]:
        """Every connection a scope has opened, in order."""
        return self.engine.connections

    def tenant_scope(self, tenant_id: UUID) -> AbstractAsyncContextManager[TenantSession]:
        """Delegate to the real implementation. Nothing about the SQL is faked."""
        return self._database.tenant_scope(tenant_id)

    async def ping(self) -> None:
        """Count the probe, then raise the configured error or succeed."""
        self.pings += 1
        if self._ping_error is not None:
            raise self._ping_error

    async def aclose(self) -> None:
        """Record that the pool was closed."""
        self.closed = True

    @property
    def last_session(self) -> RecordingConnection:
        """The most recently opened connection."""
        return self.sessions[-1]


class RecordingProbe:
    """A `ProbedResource` that answers, or fails, exactly as the test asked."""

    def __init__(
        self,
        *,
        ping_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.closed = False
        self.pings = 0
        self._ping_error = ping_error
        self._close_error = close_error

    async def ping(self) -> None:
        """Count the probe, then raise the configured error or succeed."""
        self.pings += 1
        if self._ping_error is not None:
            raise self._ping_error

    async def aclose(self) -> None:
        """Record the close, then raise if the test asked for a failing close."""
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class FailingFactory:
    """An opener that refuses. Used to reproduce a partially-failed startup."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def __call__(self) -> Never:
        """Count the attempt, then fail it. `Never` makes it usable as any resource factory."""
        self.calls += 1
        raise self.error


class StaticVerifier:
    """Resolves one known key to one principal. The credential store `T0.5` owns, in miniature."""

    def __init__(self, key: str = VALID_KEY, principal: Principal | None = None) -> None:
        self.key = key
        self.principal = principal or Principal(tenant_id=TENANT_A, key_id=KEY_ID_A)
        self.seen: list[str] = []

    async def verify(self, presented: PresentedKey, /) -> Principal | None:
        """Resolve the configured key and nothing else."""
        self.seen.append(presented.prefix)
        if presented.prefix + presented.secret == self.key:
            return self.principal
        return None


class InMemoryIdempotencyStore:
    """An `IdempotencyStore` with the atomicity the protocol requires and no network.

    `claim` is a single-threaded compare-and-set, which is exactly the guarantee `SET NX` gives
    in Redis, so the algorithm under test sees the same semantics it will see in production.
    """

    def __init__(self) -> None:
        self.records: dict[str, IdempotencyRecord] = {}

    async def claim(self, storage_key: str, record: IdempotencyRecord) -> IdempotencyRecord | None:
        """Store `record` if the key is free; otherwise return the incumbent."""
        existing = self.records.get(storage_key)
        if existing is not None:
            return existing
        self.records[storage_key] = record
        return None

    async def complete(self, storage_key: str, record: IdempotencyRecord) -> None:
        """Overwrite the key with the finished record."""
        self.records[storage_key] = record


class TenantBody(BaseModel):
    """A body carrying a tenant id, so a test can prove the API ignores one."""

    tenant_id: UUID | None = None


probe_router = APIRouter(prefix="/probe")
"""Routes that exist only to exercise the shell. Never mounted on the real application."""


@probe_router.get("/error/{code}")
async def raise_api_error(code: str) -> None:
    """Raise the coded failure named in the path."""
    raise ApiError(ErrorCode.from_value(code), log_detail=f"probe for {code}")


@probe_router.get("/http/{status_code}")
async def raise_http_exception(status_code: int) -> None:
    """Raise a bare framework exception, with no code of its own."""
    raise HTTPException(status_code=status_code)


@probe_router.get("/boom")
async def raise_unclassified() -> None:
    """Raise something nobody classified, carrying a secret in its message."""
    message = f"internal detail: {PLANTED_DETAIL}"
    raise ValueError(message)


@probe_router.get("/boom/{marker}")
async def raise_unclassified_for(marker: str) -> None:
    """Raise from a *templated* route, so a test can tell the template from the concrete path."""
    message = f"internal detail: {PLANTED_DETAIL} while handling {marker}"
    raise ValueError(message)


@probe_router.post("/validate")
async def validate_body(body: TenantBody) -> dict[str, str]:
    """Accept a body so FastAPI's own validation can reject a malformed one."""
    return {"tenant_id": str(body.tenant_id)}


@probe_router.get("/whoami")
async def whoami(principal: Annotated[Principal, Depends(require_tenant)]) -> dict[str, str]:
    """Report the resolved principal, and nothing derived from the request."""
    return {"tenant_id": str(principal.tenant_id), "key_id": str(principal.key_id)}


@probe_router.get("/scope")
async def read_scope(
    request: Request,
    session: Annotated[TenantSession, Depends(tenant_session)],
) -> dict[str, str | None]:
    """Report the tenant the session was actually scoped to, plus what the request claimed."""
    recorded = cast("RecordingConnection", session.connection)
    return {
        "session_tenant_id": recorded.tenant_parameter,
        "header_tenant_id": request.headers.get("X-Tenant-Id"),
    }


@probe_router.post("/scope")
async def write_scope(
    body: TenantBody,
    session: Annotated[TenantSession, Depends(tenant_session)],
) -> dict[str, str | None]:
    """Same, for a tenant id supplied in a JSON body."""
    recorded = cast("RecordingConnection", session.connection)
    return {
        "session_tenant_id": recorded.tenant_parameter,
        "body_tenant_id": str(body.tenant_id) if body.tenant_id else None,
    }


@probe_router.get("/cross-tenant")
async def cross_tenant(principal: Annotated[Principal, Depends(require_tenant)]) -> None:
    """Read a job that belongs to the other tenant."""
    assert_tenant_owns(principal, TENANT_B, job_id=JOB_ID)


def build_resources(
    *,
    database: DatabaseResource | None = None,
    cache: ProbedResource | None = None,
    object_store: ClosableResource | None = None,
) -> Resources:
    """A `Resources` whose factories hand back the objects the test supplied."""
    resolved_database = database if database is not None else RecordingDatabase()
    resolved_cache = cache if cache is not None else RecordingProbe()
    resolved_store = object_store if object_store is not None else RecordingProbe()

    async def open_database() -> DatabaseResource:
        return resolved_database

    async def open_cache() -> ProbedResource:
        return resolved_cache

    async def open_store() -> ClosableResource:
        return resolved_store

    return Resources(
        ResourceFactories(database=open_database, cache=open_cache, object_store=open_store)
    )


def build_app(
    *,
    resources: Resources | None = None,
    verifier: ApiKeyVerifier | None = None,
    provider_registry: ProviderRegistry | None = None,
    probes: bool = True,
) -> FastAPI:
    """The real `create_app`, with fakes injected and the probe routes optionally mounted."""
    app = create_app(
        get_settings(),
        resources=resources if resources is not None else build_resources(),
        verifier=verifier,
        provider_registry=provider_registry,
    )
    if probes:
        app.include_router(probe_router)
    return app


@asynccontextmanager
async def api_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client bound to `app`, with the lifespan actually run.

    `httpx.ASGITransport` does not run a lifespan on its own, so without this the readiness
    probe would find no open resources and every test would pass or fail for the wrong reason.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


def authorised() -> dict[str, str]:
    """Headers carrying the credential `StaticVerifier` accepts."""
    return {"Authorization": f"Bearer {VALID_KEY}"}


def test_fakes_satisfy_the_resource_protocols() -> None:
    """The substitutes are checked against the protocols they stand in for, not assumed to fit.

    A structural assignment rather than `isinstance`: `mypy --strict` resolves it at check time,
    so a change to `DatabaseResource` breaks the build here instead of producing tests that
    pass against a shape production no longer has.
    """
    database: DatabaseResource = RecordingDatabase()
    cache: ProbedResource = RecordingProbe()

    assert database.tenant_scope(TENANT_A) is not None
    assert cache is not None


@pytest.mark.asyncio
async def test_recording_database_delegates_to_the_real_scope() -> None:
    """The fake supplies a connection; the transaction and the binding come from `persistence`."""
    database = RecordingDatabase()

    async with database.tenant_scope(TENANT_A) as session:
        assert session.tenant_id == TENANT_A

    assert database.last_session.events[0] == BEGIN
    assert database.last_session.tenant_parameter == str(TENANT_A)


@pytest.mark.asyncio
async def test_the_recorded_session_is_the_persistence_one() -> None:
    """Not a look-alike: the scope yields `persistence.session.TenantSession` itself.

    Which is what makes `test_api_session`'s assertions assertions about production code.
    """
    database = RecordingDatabase()

    async with database.tenant_scope(TENANT_A) as session:
        assert isinstance(session, TenantSession)


@pytest.mark.asyncio
async def test_in_memory_store_claims_once() -> None:
    """The store used by the idempotency tests really is compare-and-set."""
    store = InMemoryIdempotencyStore()
    record = IdempotencyRecord(state=IdempotencyState.IN_FLIGHT, fingerprint="f")

    first = await store.claim("k", record)
    second = await store.claim("k", record)

    assert first is None
    assert second == record


@pytest.mark.asyncio
async def test_verifier_resolves_only_the_configured_key() -> None:
    """`StaticVerifier` is not a rubber stamp: an unknown key resolves to nothing."""
    verifier = StaticVerifier()
    known = parse_bearer(f"Bearer {VALID_KEY}")
    unknown = parse_bearer(f"Bearer {VALID_KEY[:KEY_PREFIX_LENGTH]}a-different-remainder")
    assert known is not None
    assert unknown is not None

    assert await verifier.verify(known) == verifier.principal
    assert await verifier.verify(unknown) is None
