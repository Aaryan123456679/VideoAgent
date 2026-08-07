"""`S0.4.2` — the RLS binding, and the two ways a caller would try to move it.

The one property everything else rests on: **the first statement of every transaction binds the
tenant, and the tenant comes from the `Principal`.** If the binding runs second, the first query
is unscoped. If the value can come from a header or a body, row-level security is protecting
nothing — the client chooses its own tenant.

Both are asserted against the real `Database.tenant_scope`; only the session it opens is
substituted. Deleting the `set_config` call, moving it after the first query, or reading the
tenant from anywhere but the principal each fails at least one test here.
"""

from __future__ import annotations

from typing import Final

import pytest
from sqlalchemy import text

from tests.unit.test_api_support import (
    BEGIN,
    OK,
    TENANT_A,
    TENANT_B,
    RecordingDatabase,
    StaticVerifier,
    api_client,
    authorised,
    build_app,
    build_resources,
)
from video_agent.api import database as database_module
from video_agent.api.database import (
    PING_SQL,
    SET_LOCAL_TENANT_SQL,
    TENANT_SETTING,
    Database,
)
from video_agent.api.errors import HTTP_UNAUTHORIZED

PROBE_QUERY: Final = "SELECT * FROM job"
EXPECTED_SESSIONS: Final = 2


@pytest.mark.asyncio
async def test_set_local_tenant_runs_first() -> None:
    """The transaction opens, and the very next statement is the RLS binding.

    Asserted on the position, not on membership: a `set_config` issued after the first query
    would still be present in the list, and the query it followed would still have run
    unscoped.
    """
    database = RecordingDatabase()

    async with database.tenant_scope(TENANT_A) as session:
        await session.execute(text(PROBE_QUERY))

    events = database.last_session.events
    assert events[0] == BEGIN
    assert events[1] == SET_LOCAL_TENANT_SQL
    assert events[2] == PROBE_QUERY


@pytest.mark.asyncio
async def test_the_tenant_travels_as_a_bound_parameter_not_as_sql() -> None:
    """`set_config` takes the value as a parameter, so a tenant id is never SQL text.

    `SET LOCAL` cannot be parameterised, which is why the statement is spelled this way. The
    assertion is that the identifier does not appear in the statement at all: if it did, some
    future value would be interpolated instead of bound.
    """
    database = RecordingDatabase()

    async with database.tenant_scope(TENANT_A):
        pass

    assert str(TENANT_A) not in SET_LOCAL_TENANT_SQL
    assert TENANT_SETTING in SET_LOCAL_TENANT_SQL
    # The placeholder, not merely the parameter dict: the dict is passed either way, so a
    # statement that stopped referencing `:tenant_id` would still satisfy an assertion about
    # what was bound while binding it to nothing.
    assert ":tenant_id" in SET_LOCAL_TENANT_SQL
    assert database.last_session.parameters[0] == {"tenant_id": str(TENANT_A)}


@pytest.mark.asyncio
async def test_the_binding_is_local_to_the_transaction() -> None:
    """`is_local => true` is what keeps one request's tenant out of the next request's session.

    A pooled connection is reused; a `SET` without `LOCAL` would survive the checkin and scope
    the next tenant's queries to the previous tenant.
    """
    assert SET_LOCAL_TENANT_SQL.rstrip(")").endswith("true")


@pytest.mark.asyncio
async def test_a_new_transaction_rebinds_for_each_request() -> None:
    """Two scopes, two sessions, two bindings — never one binding reused across tenants."""
    database = RecordingDatabase()

    async with database.tenant_scope(TENANT_A):
        pass
    async with database.tenant_scope(TENANT_B):
        pass

    assert len(database.sessions) == EXPECTED_SESSIONS
    assert database.sessions[0].tenant_parameter == str(TENANT_A)
    assert database.sessions[1].tenant_parameter == str(TENANT_B)


@pytest.mark.asyncio
async def test_header_tenant_override_ignored() -> None:
    """An `X-Tenant-Id` naming another tenant changes nothing about the session's scope."""
    database = RecordingDatabase()
    app = build_app(resources=build_resources(database=database), verifier=StaticVerifier())

    async with api_client(app) as client:
        response = await client.get(
            "/probe/scope",
            headers={**authorised(), "X-Tenant-Id": str(TENANT_B)},
        )

    assert response.status_code == OK
    assert response.json() == {
        "session_tenant_id": str(TENANT_A),
        "header_tenant_id": str(TENANT_B),
    }


@pytest.mark.asyncio
async def test_body_tenant_override_ignored() -> None:
    """The same, for a `tenant_id` in the JSON body.

    The header and the body are asserted separately because they arrive through different
    FastAPI machinery, and a fix that covered one would leave the other open.
    """
    database = RecordingDatabase()
    app = build_app(resources=build_resources(database=database), verifier=StaticVerifier())

    async with api_client(app) as client:
        response = await client.post(
            "/probe/scope",
            headers=authorised(),
            json={"tenant_id": str(TENANT_B)},
        )

    assert response.status_code == OK
    assert response.json() == {
        "session_tenant_id": str(TENANT_A),
        "body_tenant_id": str(TENANT_B),
    }


@pytest.mark.asyncio
async def test_a_session_is_unobtainable_without_a_principal() -> None:
    """No credential means no session — the dependency cannot run half-way."""
    database = RecordingDatabase()
    app = build_app(resources=build_resources(database=database), verifier=StaticVerifier())

    async with api_client(app) as client:
        response = await client.get("/probe/scope")

    assert response.status_code == HTTP_UNAUTHORIZED
    assert database.sessions == []


def test_engine_not_exported() -> None:
    """There is no engine to import, so there is no way to open an unscoped connection.

    `S0.4.2` acceptance 4 is not "do not do this"; it is "there is nothing to do it with".
    Three spellings of the same reach are closed: the module's export list, a direct attribute
    read, and a public attribute on `Database` itself.
    """
    engine_attribute = "engine"

    assert engine_attribute not in database_module.__all__
    with pytest.raises(AttributeError):
        getattr(database_module, engine_attribute)
    assert engine_attribute not in {name for name in dir(Database) if not name.startswith("_")}


def test_the_readiness_query_is_not_the_tenant_binding() -> None:
    """`/readyz` issues its own trivial query and must not be able to bind a tenant."""
    assert PING_SQL != SET_LOCAL_TENANT_SQL
    assert "set_config" not in PING_SQL
